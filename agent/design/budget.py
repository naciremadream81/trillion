"""
Dispatch cost ceiling — the cap Sean set in the Tier 0 interview.

$5 per dispatch, $15 per day, and the important half: **it refuses rather
than truncating**. A design dispatch that runs out of budget halfway through
leaves a half-composed screen and a half-spent budget, which is the worst of
both. Refusing before spawning leaves the budget intact and the failure
legible.

Claude Code reports its own spend as `total_cost_usd` on the result event, so
the ledger records what was actually spent rather than an estimate. The check
before a dispatch is necessarily against *past* spend — the cost of the run
about to start is unknowable until it finishes, which is exactly why the
per-dispatch ceiling is passed to the runner as --max-turns and a timeout as
well as being recorded here. Three bounds, because a subprocess that spends
money needs all three.

Separate from agent/cost/ deliberately: that ledger is per-API-call token
usage from the provider seam, and Claude Code reports dollars for a whole
subprocess run. Forcing one shape onto the other would make both less honest.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS design_dispatches (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    project_slug   TEXT    NOT NULL,
    feature_slug   TEXT    NOT NULL DEFAULT '',
    screen_name    TEXT    NOT NULL DEFAULT '',
    cost_usd       REAL    NOT NULL DEFAULT 0,
    succeeded      INTEGER NOT NULL DEFAULT 0,
    dispatched_at  TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_design_dispatches_at ON design_dispatches(dispatched_at);
"""

DEFAULT_PER_DISPATCH_USD = 5.0
DEFAULT_DAILY_USD = 15.0


class BudgetExceeded(RuntimeError):
    pass


def default_db_path() -> str:
    return os.getenv("TRILLION_DESIGN_DB", "design.db")


class DesignBudget:
    def __init__(
        self,
        db_path: str | None = None,
        *,
        per_dispatch_usd: float = DEFAULT_PER_DISPATCH_USD,
        daily_usd: float = DEFAULT_DAILY_USD,
    ) -> None:
        self.db_path = db_path or default_db_path()
        self.per_dispatch_usd = per_dispatch_usd
        self.daily_usd = daily_usd
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def spent_today(self) -> float:
        """
        Today's spend, in UTC days.

        UTC rather than local time so the window can't shift under a
        timezone change and silently hand back budget — the same reasoning
        as the heartbeat's quiet hours.
        """
        start = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(cost_usd), 0) AS total FROM design_dispatches"
                " WHERE dispatched_at >= ?",
                (start,),
            ).fetchone()
        return float(row["total"])

    def check_before_dispatch(self) -> None:
        """
        Raise if there isn't room for another dispatch. Called BEFORE
        spawning, so a refusal costs nothing.

        The headroom test is against a whole per-dispatch allowance rather
        than against any remaining cents: starting a dispatch that can only
        afford a third of itself produces exactly the half-composed screen
        this ceiling exists to prevent.
        """
        spent = self.spent_today()
        if spent + self.per_dispatch_usd > self.daily_usd:
            raise BudgetExceeded(
                f"Design budget: ${spent:.2f} spent today of ${self.daily_usd:.2f}, "
                f"which leaves less than one ${self.per_dispatch_usd:.2f} dispatch. "
                "Refusing rather than starting a run that would stop halfway. "
                "It resets at UTC midnight."
            )

    def record(
        self,
        project_slug: str,
        *,
        feature_slug: str = "",
        screen_name: str = "",
        cost_usd: float = 0.0,
        succeeded: bool = False,
    ) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO design_dispatches
                    (project_slug, feature_slug, screen_name, cost_usd, succeeded, dispatched_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    project_slug,
                    feature_slug,
                    screen_name,
                    float(cost_usd or 0.0),
                    1 if succeeded else 0,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            return int(cur.lastrowid)

    def recent(self, limit: int = 10) -> list:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM design_dispatches ORDER BY dispatched_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]
