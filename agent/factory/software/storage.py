"""
Storage for the Software Factory: one table, build_tasks.

Same pattern as agent/factory/storage.py — stdlib sqlite3, schema applied
idempotently on every repo construction, one short-lived connection per
operation. Deliberately a separate database/table from spawn_tasks: builds
and agent-spawns are different lifecycles (a build has no approval step).

`plan` (tech stack, file list, entry points, test command — produced by the
PLANNING pipeline step) is stored as TEXT and json.dumps/json.loads'd at the
boundary, same as the JSON-shaped columns in agent/factory/storage.py.
"""

from __future__ import annotations

import json
import os
import sqlite3

from ... import storage_utils
from datetime import datetime, timezone


# Terminal/non-terminal states for the build_tasks state machine.
PENDING = "PENDING"
PLANNING = "PLANNING"
ARCHITECTURE = "ARCHITECTURE"
SCAFFOLDING = "SCAFFOLDING"
CODING = "CODING"
TESTING = "TESTING"
INTEGRATION = "INTEGRATION"
DOCS = "DOCS"
BUILT = "BUILT"
FAILED = "FAILED"

TERMINAL_STATUSES = {BUILT, FAILED}

# Legal status transitions, keyed by current status. Any write not listed
# here is refused loudly (InvalidTransition) — see agent/factory/storage.py
# for the rationale. TESTING -> CODING is the one corrective-retry edge: a
# failed whole-project test run gets one bounded pass back through CODING
# (a single whole-project _run_coding() pass, not another per-task loop)
# before the pipeline proceeds to INTEGRATION (or fails) regardless.
_VALID_TRANSITIONS: dict[str, set[str]] = {
    PENDING: {PLANNING, FAILED},
    PLANNING: {ARCHITECTURE, FAILED},
    ARCHITECTURE: {SCAFFOLDING, FAILED},
    SCAFFOLDING: {CODING, FAILED},
    CODING: {TESTING, FAILED},
    TESTING: {CODING, INTEGRATION, FAILED},
    INTEGRATION: {DOCS, FAILED},
    DOCS: {BUILT, FAILED},
}


class InvalidTransition(ValueError):
    """Raised when a build_tasks status write isn't a legal state-machine transition."""


SCHEMA = """
CREATE TABLE IF NOT EXISTS build_tasks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    slug            TEXT,
    description     TEXT    NOT NULL,
    status          TEXT    NOT NULL DEFAULT 'PENDING',
    plan            TEXT,
    retry_count     INTEGER NOT NULL DEFAULT 0,
    failure_reason  TEXT,
    created_by      TEXT    NOT NULL DEFAULT 'sean',
    created_at      TEXT    NOT NULL,
    updated_at      TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_build_tasks_status ON build_tasks(status);
CREATE INDEX IF NOT EXISTS idx_build_tasks_created_at ON build_tasks(created_at);

-- playbook/opportunity-scout.md Tier 4: an operator-set document that
-- shadows the file default shipped next to the code, so retuning the
-- scout's doctrine or its hunting lanes doesn't need a deploy.
--
-- The key is an arbitrary string, deliberately NOT a foreign key to any
-- agent table: an extra document *belongs to* an agent without *being* the
-- agent, so it is keyed "scout_lanes", not "scout". One row, one document.
CREATE TABLE IF NOT EXISTS agent_documents (
    key         TEXT    PRIMARY KEY,
    body        TEXT    NOT NULL,
    updated_at  TEXT    NOT NULL
);

-- playbook/opportunity-scout.md Tier 7: every candidate the scout EXAMINED,
-- not only the one it picked. Four of five candidates are thrown away today;
-- kept, they are the cheapest signal in the system — the delta between two
-- runs that saw the same problem is something you get for free, and it is
-- the only way to know whether a candidate is growing or going stale.
--
-- `fingerprint` is a stable identity for "the same problem", so repeat
-- sightings collapse onto one row rather than accumulating duplicates. It is
-- also what the repetition memory reads back to tell the next run what it
-- has already seen.
CREATE TABLE IF NOT EXISTS scout_sightings (
    fingerprint     TEXT    PRIMARY KEY,
    lane            TEXT,
    problem         TEXT    NOT NULL,
    evidence        TEXT    NOT NULL DEFAULT '',
    source_url      TEXT    NOT NULL DEFAULT '',
    times_seen      INTEGER NOT NULL DEFAULT 1,
    times_selected  INTEGER NOT NULL DEFAULT 0,
    first_seen_at   TEXT    NOT NULL,
    last_seen_at    TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_scout_sightings_last_seen ON scout_sightings(last_seen_at);
CREATE INDEX IF NOT EXISTS idx_scout_sightings_lane ON scout_sightings(lane);
"""


def default_db_path() -> str:
    """Where the software factory database lives. Override with $TRILLION_SOFTWARE_FACTORY_DB."""
    return os.getenv("TRILLION_SOFTWARE_FACTORY_DB", "software_factory.db")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class BuildRepo:
    """Reads and writes build_tasks."""

    def __init__(self, db_path: str | None = None) -> None:
        self.db_path = db_path or default_db_path()
        self._init_schema()

    # ── Connection / schema ───────────────────────────────────────────────────

    def _connect(self):
        """
        Connection context manager — see agent/storage_utils.py.

        Was a bare `sqlite3.connect(...)` returned raw. Every call site wraps
        it in `with`, and sqlite3's own context manager commits without
        closing, so each request leaked a connection. Same call-site shape,
        with the close that was missing.
        """
        return storage_utils.connect(self.db_path)

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    def _check_transition(self, conn: sqlite3.Connection, task_id: int, new_status: str) -> None:
        """Refuse (InvalidTransition) any status write that isn't a legal
        state-machine move from the task's current status."""
        row = conn.execute("SELECT status FROM build_tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            raise InvalidTransition(f"build task {task_id} not found")
        current = row["status"]
        if new_status not in _VALID_TRANSITIONS.get(current, set()):
            raise InvalidTransition(
                f"build task {task_id}: illegal status transition {current} -> {new_status}"
            )

    # ── Writes ───────────────────────────────────────────────────────────────

    def create_build_task(self, description: str, created_by: str = "sean") -> int:
        ts = _now()
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO build_tasks (description, status, created_by, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (description, PENDING, created_by, ts, ts),
            )
            return int(cur.lastrowid)

    def update_status(self, task_id: int, status: str, failure_reason: str | None = None) -> None:
        with self._connect() as conn:
            self._check_transition(conn, task_id, status)
            conn.execute(
                "UPDATE build_tasks SET status = ?, failure_reason = ?, updated_at = ? WHERE id = ?",
                (status, failure_reason, _now(), task_id),
            )

    def set_plan(self, task_id: int, *, slug: str, plan: dict) -> None:
        """Save the drafted slug/build plan, moving the task to ARCHITECTURE."""
        with self._connect() as conn:
            self._check_transition(conn, task_id, ARCHITECTURE)
            conn.execute(
                """
                UPDATE build_tasks
                SET slug = ?, plan = ?, status = ?, updated_at = ?
                WHERE id = ?
                """,
                (slug, json.dumps(plan), ARCHITECTURE, _now(), task_id),
            )

    def retry_coding(self, task_id: int) -> int:
        """
        Bump retry_count and transition TESTING -> CODING for one corrective
        coding pass after a failed test run. Returns the new retry_count so
        the pipeline can cap it (one retry) without a second DB round-trip.
        """
        with self._connect() as conn:
            self._check_transition(conn, task_id, CODING)
            conn.execute(
                "UPDATE build_tasks SET retry_count = retry_count + 1, status = ?, updated_at = ? WHERE id = ?",
                (CODING, _now(), task_id),
            )
            row = conn.execute("SELECT retry_count FROM build_tasks WHERE id = ?", (task_id,)).fetchone()
        return int(row["retry_count"])

    def set_task_results(self, task_id: int, results: list[dict]) -> None:
        """
        Record the per-task Dev<->QA loop's outcomes into the plan JSON's
        task_results key, once the loop has finished. Doesn't change status
        — CODING is already the status for the loop's whole duration, so
        this is a plain data write, not a state-machine transition.
        """
        with self._connect() as conn:
            row = conn.execute("SELECT plan FROM build_tasks WHERE id = ?", (task_id,)).fetchone()
            if row is None:
                raise InvalidTransition(f"build task {task_id} not found")
            plan = json.loads(row["plan"]) if row["plan"] else {}
            plan["task_results"] = results
            conn.execute(
                "UPDATE build_tasks SET plan = ?, updated_at = ? WHERE id = ?",
                (json.dumps(plan), _now(), task_id),
            )

    def set_error(self, task_id: int, reason: str) -> None:
        """Convenience wrapper: transition straight to FAILED with a reason."""
        self.update_status(task_id, FAILED, failure_reason=reason)

    # ── Reads ────────────────────────────────────────────────────────────────

    def get_build_task(self, task_id: int) -> dict | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM build_tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["plan"] = json.loads(result["plan"]) if result["plan"] else None
        return result

    def list_recent_builds(self, limit: int = 20) -> list[dict]:
        """Most recent builds first, any status — the /builds observability
        view. There's nothing to approve, so unlike list_pending_approval()
        this isn't filtered to a single status."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM build_tasks ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["plan"] = json.loads(d["plan"]) if d["plan"] else None
            result.append(d)
        return result

    def count_builds_today(self) -> int:
        """Build tasks created since UTC midnight — the daily-cap check."""
        start_of_day = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00")
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM build_tasks WHERE created_at >= ?", (start_of_day,)
            ).fetchone()
        return int(row["n"])

    def slug_taken(self, slug: str) -> bool:
        """Whether a build (any status) has already used this slug — used to
        avoid two builds colliding on the same generated-projects/<slug>/
        directory."""
        with self._connect() as conn:
            row = conn.execute("SELECT 1 FROM build_tasks WHERE slug = ?", (slug,)).fetchone()
        return row is not None

    # ── Operator document overrides (opportunity-scout.md Tier 4) ────────────

    def get_document(self, key: str) -> str | None:
        """The operator's override for `key`, or None if they haven't set one.

        None and "" are different answers and both are meaningful: None means
        "no override, use the shipped file", while "" means the operator
        saved an empty document — which agent/factory/software/doctrine.py
        treats as unusable and falls back on, loudly. Returning "" for a
        missing row would collapse those two cases into one."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT body FROM agent_documents WHERE key = ?", (key,)
            ).fetchone()
        return None if row is None else row["body"]

    def set_document(self, key: str, body: str) -> None:
        """Write (or replace) the operator's override for `key`."""
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO agent_documents (key, body, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET body = excluded.body,
                                               updated_at = excluded.updated_at
                """,
                (key, body, _now()),
            )

    def delete_document(self, key: str) -> None:
        """Drop the override so the shipped file default returns. Idempotent."""
        with self._connect() as conn:
            conn.execute("DELETE FROM agent_documents WHERE key = ?", (key,))

    # ── Scout sightings (opportunity-scout.md Tier 7) ────────────────────────

    def record_sighting(
        self,
        *,
        fingerprint: str,
        problem: str,
        evidence: str = "",
        source_url: str = "",
        lane: str | None = None,
        selected: bool = False,
    ) -> None:
        """
        Record one candidate the scout examined.

        Called once per candidate, and the caller wraps each call in its own
        try/except — one malformed candidate must not discard the four good
        ones beside it. Seeing the same fingerprint again bumps the counters
        and refreshes the evidence rather than inserting a duplicate: that is
        what makes "this problem has been sighted four times across three
        weeks" a fact the next run can use.

        The stored problem/evidence text is deliberately the LATEST wording,
        not the first — a second sighting usually has better evidence, and
        the first_seen_at column already preserves when it started.
        """
        ts = _now()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO scout_sightings
                    (fingerprint, lane, problem, evidence, source_url,
                     times_seen, times_selected, first_seen_at, last_seen_at)
                VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?)
                ON CONFLICT(fingerprint) DO UPDATE SET
                    lane           = excluded.lane,
                    problem        = excluded.problem,
                    evidence       = excluded.evidence,
                    source_url     = excluded.source_url,
                    times_seen     = scout_sightings.times_seen + 1,
                    times_selected = scout_sightings.times_selected + excluded.times_selected,
                    last_seen_at   = excluded.last_seen_at
                """,
                (fingerprint, lane, problem, evidence, source_url,
                 1 if selected else 0, ts, ts),
            )

    def recent_sightings(self, limit: int = 40) -> list[dict]:
        """The most recently seen candidates, newest first — the input to the
        next run's "you have already seen these" instruction."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM scout_sightings ORDER BY last_seen_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def repeat_sightings(self, limit: int = 20) -> list[dict]:
        """
        Candidates seen more than once, most-seen first.

        This is the compounding part: a problem that keeps reappearing across
        runs and lanes is evidence of persistence that no single run could
        have produced.
        """
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM scout_sightings
                WHERE times_seen > 1
                ORDER BY times_seen DESC, last_seen_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def all_documents(self) -> dict[str, str]:
        """Every override, for the whole-cache refresh in doctrine.py.

        A full read rather than a per-key fetch on purpose: this table holds
        a handful of rows, and partial invalidation is more bug surface than
        the work it saves."""
        with self._connect() as conn:
            rows = conn.execute("SELECT key, body FROM agent_documents").fetchall()
        return {row["key"]: row["body"] for row in rows}
