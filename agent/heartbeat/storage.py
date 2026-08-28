"""
Storage for Tier 5: heartbeat notices, per-check scheduling, and per-check
dedup state.

Same pattern as agent/safety/storage.py — stdlib sqlite3, schema applied
idempotently on construction, one short-lived connection per operation.
JSON-shaped columns (cursor) are stored as TEXT and json.dumps/json.loads'd
at the boundary.

Three tables:
  notices       one row per surfaced notice. deliver_at holds non-critical
                notices until quiet hours end, so a notice is never dropped —
                only its visibility is time-gated. dismissed_at marks it read.
  check_state   next_due_at per check name, so scheduling survives a restart.
  check_cursor  arbitrary per-check dedup state (last-seen SHA, PR id, ...) so
                a check doesn't re-fire the same notice on every tick.
"""

from __future__ import annotations

import json
import os
import sqlite3

from .. import storage_utils
from datetime import datetime, timedelta, timezone


SCHEMA = """
CREATE TABLE IF NOT EXISTS notices (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    check_name   TEXT    NOT NULL,
    severity     TEXT    NOT NULL DEFAULT 'info',
    message      TEXT    NOT NULL,
    created_at   TEXT    NOT NULL,
    deliver_at   TEXT    NOT NULL,
    dismissed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_notices_deliver_at ON notices(deliver_at);
CREATE INDEX IF NOT EXISTS idx_notices_dismissed_at ON notices(dismissed_at);

CREATE TABLE IF NOT EXISTS check_state (
    check_name  TEXT PRIMARY KEY,
    next_due_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS check_cursor (
    check_name TEXT PRIMARY KEY,
    cursor     TEXT NOT NULL DEFAULT '{}'
);
"""

CRITICAL = "critical"


def default_db_path() -> str:
    """Where the heartbeat database lives. Override with $TRILLION_HEARTBEAT_DB."""
    return os.getenv("TRILLION_HEARTBEAT_DB", "heartbeat.db")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def is_quiet_hours(start_hour: int, end_hour: int, now: datetime) -> bool:
    """
    Whether `now` falls inside the quiet-hours window (both hours UTC).

    start_hour == end_hour means quiet hours are disabled (zero-width
    window). start_hour > end_hour wraps past midnight (e.g. 22 -> 8).
    """
    if start_hour == end_hour:
        return False
    hour = now.hour
    if start_hour < end_hour:
        return start_hour <= hour < end_hour
    return hour >= start_hour or hour < end_hour


def _next_quiet_hours_end(end_hour: int, now: datetime) -> datetime:
    """The next UTC timestamp at which quiet hours end, on the hour."""
    candidate = now.replace(hour=end_hour, minute=0, second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    return candidate


class HeartbeatRepo:
    """Reads and writes notices / check_state / check_cursor."""

    def __init__(self, db_path: str | None = None) -> None:
        self.db_path = db_path or default_db_path()
        self._init_schema()

    # ── Connection / schema ──────────────────────────────────────────────────

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

    # ── Notices: writes ──────────────────────────────────────────────────────

    def add_notice(
        self,
        *,
        check_name: str,
        severity: str,
        message: str,
        quiet_hours_start: int = 22,
        quiet_hours_end: int = 8,
    ) -> int:
        """
        Record a notice and return its id.

        Critical notices deliver immediately, bypassing quiet hours — only
        truly critical items interrupt outside them. Everything else holds
        until quiet hours end, so nothing is ever dropped, only delayed
        (this is also what makes "catch up on return" work for free: the
        notice was there the whole time, just not yet deliverable).
        """
        now = _now()
        if severity == CRITICAL or not is_quiet_hours(quiet_hours_start, quiet_hours_end, now):
            deliver_at = now
        else:
            deliver_at = _next_quiet_hours_end(quiet_hours_end, now)
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO notices (check_name, severity, message, created_at, deliver_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (check_name, severity, message, now.isoformat(), deliver_at.isoformat()),
            )
            return int(cur.lastrowid)

    # ── Notices: reads ───────────────────────────────────────────────────────

    def list_active_notices(self, now: datetime | None = None) -> list[dict]:
        """Undismissed notices whose deliver_at has passed, oldest first."""
        now = now or _now()
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM notices
                WHERE dismissed_at IS NULL AND deliver_at <= ?
                ORDER BY id
                """,
                (now.isoformat(),),
            ).fetchall()
        return [dict(row) for row in rows]

    def dismiss(self, notice_id: int) -> bool:
        """Mark a notice dismissed. Returns False if it doesn't exist or is
        already dismissed."""
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE notices SET dismissed_at = ? WHERE id = ? AND dismissed_at IS NULL",
                (_now().isoformat(), notice_id),
            )
            return cur.rowcount > 0

    # ── Check scheduling (restart-safe) ─────────────────────────────────────

    def get_next_due_at(self, check_name: str) -> datetime | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT next_due_at FROM check_state WHERE check_name = ?", (check_name,)
            ).fetchone()
        if row is None:
            return None
        return datetime.fromisoformat(row["next_due_at"])

    def set_next_due_at(self, check_name: str, when: datetime) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO check_state (check_name, next_due_at) VALUES (?, ?)
                ON CONFLICT(check_name) DO UPDATE SET next_due_at = excluded.next_due_at
                """,
                (check_name, when.isoformat()),
            )

    # ── Per-check dedup state ───────────────────────────────────────────────

    def get_cursor(self, check_name: str) -> dict:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT cursor FROM check_cursor WHERE check_name = ?", (check_name,)
            ).fetchone()
        if row is None:
            return {}
        try:
            return json.loads(row["cursor"])
        except json.JSONDecodeError:
            return {}

    def set_cursor(self, check_name: str, cursor: dict) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO check_cursor (check_name, cursor) VALUES (?, ?)
                ON CONFLICT(check_name) DO UPDATE SET cursor = excluded.cursor
                """,
                (check_name, json.dumps(cursor, default=str)),
            )
