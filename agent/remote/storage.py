"""
The durable cross-machine task queue — playbook/cloud-to-local.md Tier 1,
plus Tier 4's presence table.

**The queue is the contract.** Routing work across machines means the
requester and the runner are never guaranteed to be online at the same
moment. The only thing that survives that gap is a persisted row. A push
notification is an optimization on top; the source of truth is this table,
and the worker drains it on startup — which is what makes a request survive
the laptop having been asleep when it was made.

THE ATOMIC CLAIM IS THE LOAD-BEARING DETAIL. Two workers, or one worker woken
twice, must never claim the same row. Postgres does this with `SELECT ... FOR
UPDATE SKIP LOCKED`; SQLite has no such clause, so the equivalent here is a
conditional UPDATE inside a `BEGIN IMMEDIATE` transaction — the write lock is
taken before the SELECT, so the read-decide-write cannot interleave with
another claimer. A plain `SELECT` then `UPDATE` in autocommit races, and both
callers believe they won.

Presence (Tier 4) is ADVISORY ONLY. It changes the wording of an
acknowledgement and nothing else. Work always queues regardless: a stale
presence read should cost a slightly-off sentence, never a lost task.
"""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone

from .. import storage_utils

PENDING = "pending"
CLAIMED = "claimed"
COMPLETED = "completed"
FAILED = "failed"
TERMINAL_STATUSES = {COMPLETED, FAILED}

# Task kinds. `noop` exists so drain-on-startup can be verified end to end
# without running a real agent — the playbook's Tier 1 verification.
KIND_NOOP = "noop"
KIND_REMOTE_DISPATCH = "remote_agent_dispatch"

# How long a heartbeat stays fresh. ~3x the 30s heartbeat interval, so one
# missed beat doesn't flip the laptop to "offline" and reword every ack.
DEFAULT_PRESENCE_MAX_AGE = 90.0

# A claimed task whose worker died would otherwise sit claimed forever. This
# is the reclaim horizon, not a task timeout — a real run can exceed it, so
# it is generous.
STALE_CLAIM_SECONDS = 3600.0

SCHEMA = """
CREATE TABLE IF NOT EXISTS remote_tasks (
    id             TEXT PRIMARY KEY,
    worker_role    TEXT NOT NULL,
    kind           TEXT NOT NULL,
    payload        TEXT NOT NULL DEFAULT '{}',
    status         TEXT NOT NULL DEFAULT 'pending',
    result         TEXT,
    error_message  TEXT,
    claimed_by     TEXT,
    created_at     TEXT NOT NULL,
    claimed_at     TEXT,
    completed_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_remote_tasks_claim
    ON remote_tasks(worker_role, status, created_at);

CREATE TABLE IF NOT EXISTS worker_presence (
    worker_role TEXT PRIMARY KEY,
    claimed_by  TEXT NOT NULL DEFAULT '',
    last_seen   TEXT NOT NULL
);
"""


def default_db_path() -> str:
    return os.getenv("TRILLION_REMOTE_DB", "remote.db")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso() -> str:
    return _now().isoformat()


class RemoteQueue:
    def __init__(self, db_path: str | None = None) -> None:
        self.db_path = db_path or default_db_path()
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    def _connect(self):
        return storage_utils.connect(self.db_path)

    # ── Queue ────────────────────────────────────────────────────────────────

    def enqueue(self, worker_role: str, kind: str, payload: dict | None = None) -> str:
        """Insert a pending task and return its id. The id is minted here so
        the caller can report it before the worker has ever seen the row."""
        task_id = uuid.uuid4().hex
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO remote_tasks (id, worker_role, kind, payload, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (task_id, worker_role, kind, json.dumps(payload or {}), PENDING, _iso()),
            )
        return task_id

    def claim(self, worker_role: str, claimed_by: str) -> dict | None:
        """
        Atomically claim the oldest pending task for `worker_role`, or None.

        BEGIN IMMEDIATE takes the database's write lock BEFORE the SELECT, so
        two claimers serialize instead of both reading the same pending row
        and both updating it. Without it this is a textbook race, and the
        symptom is one task running twice — which for a dispatch means paying
        for the same work twice and, worse, doing it twice.
        """
        with self._connect() as conn:
            conn.isolation_level = None  # take transaction control ourselves
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    """
                    SELECT * FROM remote_tasks
                    WHERE worker_role = ? AND status = ?
                    ORDER BY created_at, id LIMIT 1
                    """,
                    (worker_role, PENDING),
                ).fetchone()
                if row is None:
                    conn.execute("COMMIT")
                    return None
                conn.execute(
                    "UPDATE remote_tasks SET status = ?, claimed_by = ?, claimed_at = ? "
                    "WHERE id = ? AND status = ?",
                    (CLAIMED, claimed_by, _iso(), row["id"], PENDING),
                )
                conn.execute("COMMIT")
            except sqlite3.Error:
                conn.execute("ROLLBACK")
                raise
        return self.get(row["id"])

    def release_claim(self, task_id: str) -> bool:
        """
        Return one claimed task to pending so the next drain retries it.

        Used when THIS worker is shutting down mid-run and will not complete
        the row. Without this, CancelledError bypasses drain()'s
        `except Exception` (it is a BaseException) and the task sits
        `claimed` forever — claim() only picks pending rows, and
        release_stale_claims() runs at startup with a one-hour horizon, so a
        systemd restart during a dispatch strands the job.

        The status predicate keeps this a no-op if the row is already
        terminal (another worker finished it).
        """
        with self._connect() as conn:
            cur = conn.execute(
                """
                UPDATE remote_tasks
                SET status = ?, claimed_by = NULL, claimed_at = NULL
                WHERE id = ? AND status = ?
                """,
                (PENDING, task_id, CLAIMED),
            )
            return (cur.rowcount or 0) > 0

    def release_claims_by(self, claimed_by: str) -> int:
        """
        Return every task this worker still has claimed.

        A process that is starting cannot still be running those tasks —
        they belong to a previous incarnation that died. Releasing them
        here, regardless of age, is what makes `systemctl restart` (or an
        OOM + Restart=always) recover work instead of leaving it claimed
        until a second restart happens after STALE_CLAIM_SECONDS.

        Scoped to `claimed_by` so a second worker on a different host does
        not steal an in-flight run. Same-host two-process is already a
        misconfiguration (one role, two drain loops).
        """
        if not claimed_by:
            return 0
        with self._connect() as conn:
            cur = conn.execute(
                """
                UPDATE remote_tasks
                SET status = ?, claimed_by = NULL, claimed_at = NULL
                WHERE status = ? AND claimed_by = ?
                """,
                (PENDING, CLAIMED, claimed_by),
            )
            return cur.rowcount or 0

    def complete(self, task_id: str, result: dict | None = None) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE remote_tasks SET status = ?, result = ?, completed_at = ? WHERE id = ?",
                (COMPLETED, json.dumps(result or {}), _iso(), task_id),
            )

    def fail(self, task_id: str, error: str, result: dict | None = None) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE remote_tasks
                SET status = ?, error_message = ?, result = ?, completed_at = ?
                WHERE id = ?
                """,
                (FAILED, str(error)[:2000], json.dumps(result or {}), _iso(), task_id),
            )

    def _hydrate(self, row) -> dict:
        task = dict(row)
        task["payload"] = json.loads(task["payload"] or "{}")
        task["result"] = json.loads(task["result"]) if task["result"] else None
        return task

    def get(self, task_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM remote_tasks WHERE id = ?", (task_id,)).fetchone()
        return None if row is None else self._hydrate(row)

    def pending_count(self, worker_role: str) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM remote_tasks WHERE worker_role = ? AND status = ?",
                (worker_role, PENDING),
            ).fetchone()
        return int(row["n"])

    def recent(self, limit: int = 20) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM remote_tasks ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self._hydrate(row) for row in rows]

    def release_stale_claims(self, older_than_seconds: float = STALE_CLAIM_SECONDS) -> int:
        """
        Return long-claimed tasks to pending so a worker that died mid-run
        doesn't strand them forever.

        Generous by design: this is a crash-recovery horizon, not a timeout.
        A real design dispatch can run for minutes, and reclaiming a task
        that is still running would execute it twice.
        """
        cutoff = (_now() - timedelta(seconds=older_than_seconds)).isoformat()
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE remote_tasks SET status = ?, claimed_by = NULL, claimed_at = NULL "
                "WHERE status = ? AND claimed_at IS NOT NULL AND claimed_at < ?",
                (PENDING, CLAIMED, cutoff),
            )
            return cur.rowcount or 0

    # ── Presence (Tier 4) ────────────────────────────────────────────────────

    def heartbeat(self, worker_role: str, claimed_by: str = "") -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO worker_presence (worker_role, claimed_by, last_seen)
                VALUES (?, ?, ?)
                ON CONFLICT(worker_role) DO UPDATE SET
                    claimed_by = excluded.claimed_by, last_seen = excluded.last_seen
                """,
                (worker_role, claimed_by, _iso()),
            )

    def is_online(self, worker_role: str, max_age_seconds: float = DEFAULT_PRESENCE_MAX_AGE) -> bool:
        """
        Whether the worker beat recently enough to be called online.

        NEVER raises and never blocks work. Any failure — missing row,
        unparseable timestamp, unreadable database — answers False, because
        the cost of a wrong "offline" is one slightly pessimistic sentence
        while the cost of an exception here is a dispatch that never queued.
        """
        try:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT last_seen FROM worker_presence WHERE worker_role = ?",
                    (worker_role,),
                ).fetchone()
            if row is None:
                return False
            last_seen = datetime.fromisoformat(row["last_seen"])
        except Exception:  # noqa: BLE001 — advisory only, see the docstring
            return False
        if last_seen.tzinfo is None:
            last_seen = last_seen.replace(tzinfo=timezone.utc)
        return (_now() - last_seen).total_seconds() < max_age_seconds
