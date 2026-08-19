"""
Storage for Tier 6: pending actions awaiting Sean's yes, plus the audit log.

Same pattern as agent/factory/storage.py — stdlib sqlite3, schema applied
idempotently on construction, one short-lived connection per operation.
JSON-shaped columns (arguments, detail) are stored as TEXT and
json.dumps/json.loads'd at the boundary.

Two tables:
  pending_actions  one row per gated tool call, with its arguments FROZEN at
                   gate time. The model asks Sean about action #47; approving
                   #47 runs exactly the arguments that were shown, not
                   whatever the model would like them to be by then.
  audit_log        append-only record of every gate decision. This is the
                   answer to "what did it try to do while I wasn't looking",
                   so nothing in here is ever updated or deleted.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone

# pending_actions state machine.
PENDING = "pending"
APPROVED = "approved"
DENIED = "denied"
EXPIRED = "expired"
EXECUTED = "executed"

TERMINAL_STATUSES = {DENIED, EXPIRED, EXECUTED}

# Legal transitions, keyed by current status. Anything else is refused loudly
# (InvalidTransition) rather than silently corrupting the record — for this
# table in particular, a wrong write is a false claim that Sean approved
# something.
#
# APPROVED goes only to EXECUTED because approval and execution are one step
# (Gate.confirm approves, then immediately runs). There is deliberately no
# path back out of a terminal status: an expired or denied action is re-asked
# as a new action, never revived.
_VALID_TRANSITIONS: dict[str, set[str]] = {
    PENDING: {APPROVED, DENIED, EXPIRED},
    APPROVED: {EXECUTED},
}

# Audit event vocabulary. Kept here so the Gate and any future reader agree
# without importing each other.
EVENT_GATED = "gated"
EVENT_APPROVED = "approved"
EVENT_DENIED = "denied"
EVENT_EXPIRED = "expired"
EVENT_EXECUTED = "executed"
EVENT_BLOCKED_PAUSED = "blocked_paused"
EVENT_SELF_APPROVAL_REFUSED = "self_approval_refused"
EVENT_DENIAL_OVERRIDE_REFUSED = "denial_override_refused"
# Distinct from DENIAL_OVERRIDE: Sean said something, but it was neither a
# yes nor a recognizable no ("not yet", "why is that necessary?"). Worth its
# own event because a run of these means the affirmation vocabulary is too
# narrow for how he actually talks, which is a tuning signal, not an attack.
EVENT_UNCLEAR_CONSENT_REFUSED = "unclear_consent_refused"
EVENT_PAUSED = "paused"
EVENT_RESUMED = "resumed"


class InvalidTransition(ValueError):
    """Raised when a pending_actions status write isn't a legal transition."""


SCHEMA = """
CREATE TABLE IF NOT EXISTS pending_actions (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    tool_name      TEXT    NOT NULL,
    arguments      TEXT    NOT NULL DEFAULT '{}',
    digest         TEXT    NOT NULL,
    summary        TEXT    NOT NULL DEFAULT '',
    risk           TEXT    NOT NULL DEFAULT 'consequential',
    status         TEXT    NOT NULL DEFAULT 'pending',
    history_index  INTEGER NOT NULL DEFAULT 0,
    reason         TEXT,
    expires_at     TEXT    NOT NULL,
    created_at     TEXT    NOT NULL,
    updated_at     TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pending_actions_status ON pending_actions(status);

CREATE TABLE IF NOT EXISTS audit_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    event      TEXT NOT NULL,
    tool_name  TEXT,
    action_id  INTEGER,
    detail     TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_log_created_at ON audit_log(created_at);
"""


def default_db_path() -> str:
    """Where the safety database lives. Override with $TRILLION_SAFETY_DB."""
    return os.getenv("TRILLION_SAFETY_DB", "safety.db")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_args(arguments: dict | None) -> str:
    """
    Arguments as stable JSON — sorted keys, no incidental whitespace.

    Stable because it's what the digest is computed over: the same action
    described twice has to produce the same digest, or the digest is
    decoration.
    """
    return json.dumps(arguments or {}, sort_keys=True, default=str)


def digest_for(tool_name: str, arguments: dict | None) -> str:
    """
    Short fingerprint of a specific tool call.

    Shown to Sean alongside the request so that "approve #47" is verifiably
    approval of the thing he was shown, not of a same-numbered row whose
    arguments moved underneath it.
    """
    payload = f"{tool_name}\n{canonical_args(arguments)}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:12]


class SafetyRepo:
    """Reads and writes pending_actions / audit_log."""

    def __init__(self, db_path: str | None = None) -> None:
        self.db_path = db_path or default_db_path()
        self._init_schema()

    # ── Connection / schema ──────────────────────────────────────────────────

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    def _check_transition(
        self, conn: sqlite3.Connection, action_id: int, new_status: str
    ) -> None:
        row = conn.execute(
            "SELECT status FROM pending_actions WHERE id = ?", (action_id,)
        ).fetchone()
        if row is None:
            raise InvalidTransition(f"pending action {action_id} not found")
        current = row["status"]
        if new_status not in _VALID_TRANSITIONS.get(current, set()):
            raise InvalidTransition(
                f"pending action {action_id}: illegal status transition "
                f"{current} -> {new_status}"
            )

    # ── Pending actions: writes ──────────────────────────────────────────────

    def create_pending(
        self,
        *,
        tool_name: str,
        arguments: dict | None,
        summary: str,
        risk: str,
        history_index: int,
        ttl_seconds: int,
    ) -> int:
        """
        Park a tool call and return its action id.

        history_index is len(agent.history) at the moment the call was gated.
        It's the self-approval defense: approving requires a genuine human
        turn appearing in the history *after* this index, which the model
        cannot manufacture for itself. See agent/safety/approval.py.
        """
        now = datetime.now(timezone.utc)
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO pending_actions
                    (tool_name, arguments, digest, summary, risk, status,
                     history_index, expires_at, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    tool_name,
                    canonical_args(arguments),
                    digest_for(tool_name, arguments),
                    summary,
                    risk,
                    PENDING,
                    history_index,
                    (now + timedelta(seconds=ttl_seconds)).isoformat(),
                    now.isoformat(),
                    now.isoformat(),
                ),
            )
            action_id = int(cur.lastrowid)
        self.log(EVENT_GATED, tool_name=tool_name, action_id=action_id,
                 detail={"summary": summary, "risk": risk})
        return action_id

    def approve(self, action_id: int) -> dict:
        """Move a pending action to APPROVED and return it."""
        with self._connect() as conn:
            self._check_transition(conn, action_id, APPROVED)
            conn.execute(
                "UPDATE pending_actions SET status = ?, updated_at = ? WHERE id = ?",
                (APPROVED, _now(), action_id),
            )
        action = self.get(action_id)
        self.log(EVENT_APPROVED, tool_name=action["tool_name"], action_id=action_id)
        return action

    def deny(self, action_id: int, reason: str = "") -> None:
        with self._connect() as conn:
            self._check_transition(conn, action_id, DENIED)
            conn.execute(
                "UPDATE pending_actions SET status = ?, reason = ?, updated_at = ? WHERE id = ?",
                (DENIED, reason, _now(), action_id),
            )
        self.log(EVENT_DENIED, action_id=action_id, detail={"reason": reason})

    def mark_executed(self, action_id: int, result_preview: str = "") -> None:
        from agent.security.log_redact import redact

        with self._connect() as conn:
            self._check_transition(conn, action_id, EXECUTED)
            conn.execute(
                "UPDATE pending_actions SET status = ?, updated_at = ? WHERE id = ?",
                (EXECUTED, _now(), action_id),
            )
        self.log(EVENT_EXECUTED, action_id=action_id,
                 detail={"result_preview": redact(result_preview)})

    def expire_stale(self) -> int:
        """
        Move every pending action past its expiry to EXPIRED. Returns how many.

        Expiry is what makes the gate safe with no human present: an action
        the heartbeat (Tier 5) parks at 3am is not still sitting there
        approvable at noon.
        """
        now = _now()
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id FROM pending_actions WHERE status = ? AND expires_at <= ?",
                (PENDING, now),
            ).fetchall()
            if not rows:
                return 0
            conn.execute(
                "UPDATE pending_actions SET status = ?, updated_at = ? "
                "WHERE status = ? AND expires_at <= ?",
                (EXPIRED, now, PENDING, now),
            )
        for row in rows:
            self.log(EVENT_EXPIRED, action_id=int(row["id"]))
        return len(rows)

    # ── Pending actions: reads ───────────────────────────────────────────────

    def get(self, action_id: int) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM pending_actions WHERE id = ?", (action_id,)
            ).fetchone()
        return self._hydrate(row)

    def list_pending(self) -> list[dict]:
        """Still-approvable actions, oldest first. Does not mutate — call
        expire_stale() first if you want the stale ones filtered out."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM pending_actions WHERE status = ? ORDER BY id",
                (PENDING,),
            ).fetchall()
        return [self._hydrate(r) for r in rows]

    @staticmethod
    def _hydrate(row: sqlite3.Row | None) -> dict | None:
        if row is None:
            return None
        action = dict(row)
        try:
            action["arguments"] = json.loads(action["arguments"] or "{}")
        except json.JSONDecodeError:
            action["arguments"] = {}
        return action

    # ── Audit log ────────────────────────────────────────────────────────────

    def log(
        self,
        event: str,
        *,
        tool_name: str | None = None,
        action_id: int | None = None,
        detail: dict | None = None,
    ) -> None:
        """Append one audit row. Never raises: losing an audit line is bad,
        but taking down the conversation to report it is worse."""
        try:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO audit_log (event, tool_name, action_id, detail, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        event,
                        tool_name,
                        action_id,
                        json.dumps(detail, default=str) if detail else None,
                        _now(),
                    ),
                )
        except sqlite3.Error:
            pass

    def recent_audit(self, limit: int = 20) -> list[dict]:
        """Most recent audit rows, newest first."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        entries = []
        for row in rows:
            entry = dict(row)
            if entry.get("detail"):
                try:
                    entry["detail"] = json.loads(entry["detail"])
                except json.JSONDecodeError:
                    pass
            entries.append(entry)
        return entries
