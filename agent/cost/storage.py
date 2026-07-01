"""
Storage for usage rows — one row per LLM call.

SQLite via the standard library: no new dependency, and a single local file
is the right weight for a personal assistant. Even a heavy month is a few
thousand rows, so there are no rollup tables and no premature optimization —
every dashboard query is a time-range scan over an indexed timestamp.

Phase 2 provides the schema and a repo that can record and read back rows.
Phase 3 adds the `usage_since()` aggregation and wires the repo into the
LLM client.

Thread-safety note: the agent runs on asyncio and record() may be called
from anywhere. Each operation opens its own short-lived connection rather
than sharing one across threads — a local INSERT is sub-millisecond, so the
overhead is irrelevant and it sidesteps sqlite's same-thread restriction.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone

# One column per token class the provider bills separately, plus the computed
# cost, a free-text source label, and an ISO-8601 UTC timestamp (indexed).
SCHEMA = """
CREATE TABLE IF NOT EXISTS usage (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    model              TEXT    NOT NULL,
    input_tokens       INTEGER NOT NULL DEFAULT 0,
    output_tokens      INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens  INTEGER NOT NULL DEFAULT 0,
    cost_usd           REAL    NOT NULL DEFAULT 0.0,
    source             TEXT    NOT NULL DEFAULT 'conversation',
    created_at         TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_usage_created_at ON usage(created_at);
"""


def default_db_path() -> str:
    """Where the usage database lives. Override with $TRILLION_USAGE_DB."""
    return os.getenv("TRILLION_USAGE_DB", "usage.db")


class UsageRepo:
    """Reads and writes usage rows. One SQLite file, one table."""

    def __init__(self, db_path: str | None = None) -> None:
        self.db_path = db_path or default_db_path()
        self._init_schema()

    # ── Connection / schema ───────────────────────────────────────────────────

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    # ── Writes ────────────────────────────────────────────────────────────────

    def record(
        self,
        *,
        model: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_write_tokens: int = 0,
        cache_read_tokens: int = 0,
        cost_usd: float = 0.0,
        source: str = "conversation",
        created_at: str | None = None,
    ) -> int:
        """
        Insert one usage row. Returns the new row id.

        `created_at` is an ISO-8601 string; when omitted it defaults to now in
        UTC. It's a parameter so tests (and later backfills) can set it.
        """
        ts = created_at or datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO usage (
                    model, input_tokens, output_tokens,
                    cache_write_tokens, cache_read_tokens,
                    cost_usd, source, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    model,
                    input_tokens,
                    output_tokens,
                    cache_write_tokens,
                    cache_read_tokens,
                    cost_usd,
                    source,
                    ts,
                ),
            )
            return int(cur.lastrowid)

    # ── Reads ─────────────────────────────────────────────────────────────────

    def all(self) -> list[dict]:
        """Every row, oldest first. For round-trip tests and debugging."""
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM usage ORDER BY id").fetchall()
        return [dict(r) for r in rows]

    def usage_since(self, start: str, end: str) -> dict:
        """
        Aggregate usage in the half-open time range [start, end).

        Returns summed token counts, summed cost, total call count, and a
        per-model breakdown (sorted by cost, highest first). `start`/`end` are
        ISO-8601 strings; they compare lexicographically because every row is
        stored as UTC isoformat.
        """
        with self._connect() as conn:
            totals = conn.execute(
                """
                SELECT
                    COALESCE(SUM(input_tokens), 0)       AS input_tokens,
                    COALESCE(SUM(output_tokens), 0)      AS output_tokens,
                    COALESCE(SUM(cache_write_tokens), 0) AS cache_write_tokens,
                    COALESCE(SUM(cache_read_tokens), 0)  AS cache_read_tokens,
                    COALESCE(SUM(cost_usd), 0.0)         AS cost_usd,
                    COUNT(*)                             AS calls
                FROM usage
                WHERE created_at >= ? AND created_at < ?
                """,
                (start, end),
            ).fetchone()

            by_model_rows = conn.execute(
                """
                SELECT
                    model,
                    COALESCE(SUM(input_tokens), 0)       AS input_tokens,
                    COALESCE(SUM(output_tokens), 0)      AS output_tokens,
                    COALESCE(SUM(cache_write_tokens), 0) AS cache_write_tokens,
                    COALESCE(SUM(cache_read_tokens), 0)  AS cache_read_tokens,
                    COALESCE(SUM(cost_usd), 0.0)         AS cost_usd,
                    COUNT(*)                             AS calls
                FROM usage
                WHERE created_at >= ? AND created_at < ?
                GROUP BY model
                ORDER BY cost_usd DESC
                """,
                (start, end),
            ).fetchall()

            by_source_rows = conn.execute(
                """
                SELECT
                    source,
                    COALESCE(SUM(cost_usd), 0.0) AS cost_usd,
                    COUNT(*)                     AS calls
                FROM usage
                WHERE created_at >= ? AND created_at < ?
                GROUP BY source
                ORDER BY cost_usd DESC
                """,
                (start, end),
            ).fetchall()

        result = dict(totals)
        result["by_model"] = [dict(r) for r in by_model_rows]
        result["by_source"] = [dict(r) for r in by_source_rows]
        return result

    def usage_by_day(self, start: str, end: str) -> list[dict]:
        """
        Per-calendar-day aggregate over [start, end), newest day first.

        Grouping is free: every row is timestamped, so date(created_at) buckets
        them. Days are UTC (rows are stored in UTC).
        """
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    date(created_at)             AS day,
                    COALESCE(SUM(cost_usd), 0.0) AS cost_usd,
                    COUNT(*)                     AS calls,
                    COALESCE(SUM(input_tokens), 0)       AS input_tokens,
                    COALESCE(SUM(output_tokens), 0)      AS output_tokens,
                    COALESCE(SUM(cache_write_tokens), 0) AS cache_write_tokens,
                    COALESCE(SUM(cache_read_tokens), 0)  AS cache_read_tokens
                FROM usage
                WHERE created_at >= ? AND created_at < ?
                GROUP BY day
                ORDER BY day DESC
                """,
                (start, end),
            ).fetchall()
        return [dict(r) for r in rows]
