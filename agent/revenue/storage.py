"""
Payment records and celebration state — playbook/money-celebration.md
Phases 1 and 2.

Two ideas do all the work here.

**Stable identity, deduplicated at the source.** The Stripe charge id is the
primary key, so the same payment produces exactly one row no matter how many
overlapping polls, retries or restarts see it. Everything downstream leans on
that key.

**Celebrated is a separate fact from detected.** A payment that arrived while
nobody was looking at the screen is detected but not celebrated, and it stays
that way until a screen actually shows it. That single column is the fix for
the gap the playbook calls the heart of the feature: build only the live
push, and every payment that lands with the tab closed is silently never
celebrated, which feels random and is miserable to debug.

Quiet hours deliberately do NOT apply. The playbook warns that deferred
notifications get released through a different code path that forgets to
celebrate — so celebrations never enter the heartbeat notice store at all.
There is no deferred-release path to forget, because there is no deferral: an
uncelebrated payment simply waits in this table until a screen asks.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

from .. import storage_utils

SCHEMA = """
CREATE TABLE IF NOT EXISTS payments (
    charge_id      TEXT PRIMARY KEY,
    amount_minor   INTEGER NOT NULL,
    currency       TEXT    NOT NULL DEFAULT 'usd',
    customer_label TEXT    NOT NULL DEFAULT '',
    paid_at        TEXT    NOT NULL,
    detected_at    TEXT    NOT NULL,
    celebrated_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_payments_paid_at ON payments(paid_at);
CREATE INDEX IF NOT EXISTS idx_payments_uncelebrated
    ON payments(celebrated_at, paid_at);
"""

# How far back a reconnecting screen looks for something it hasn't shown.
# A payment older than this has missed its moment; it lives on in history.
CATCHUP_WINDOW_HOURS = 24

# Most celebrations replayed in one catch-up burst. Never a silent
# truncation — read_catchup() reports how many it left behind so the UI can
# say "and 4 more in history" rather than quietly dropping them.
DEFAULT_REPLAY_CAP = 6


def default_db_path() -> str:
    return os.getenv("TRILLION_REVENUE_DB", "revenue.db")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class RevenueRepo:
    def __init__(self, db_path: str | None = None) -> None:
        self.db_path = db_path or default_db_path()
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    def _connect(self):
        return storage_utils.connect(self.db_path)

    def record_payment(
        self,
        *,
        charge_id: str,
        amount_minor: int,
        currency: str = "usd",
        customer_label: str = "",
        paid_at: str = "",
    ) -> bool:
        """
        Record a detected payment. Returns True only the FIRST time.

        The INSERT ... ON CONFLICT DO NOTHING is the deduplication: two
        overlapping polls, or a restart re-reading the same window, both
        arrive here and only one of them is new. Callers use the return value
        to decide whether anything actually happened.

        Note it does NOT touch celebrated_at on conflict. Re-detecting a
        payment somebody already saw must never make it eligible to be
        celebrated again.
        """
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO payments
                    (charge_id, amount_minor, currency, customer_label, paid_at, detected_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(charge_id) DO NOTHING
                """,
                (charge_id, int(amount_minor), currency, customer_label,
                 paid_at or _now(), _now()),
            )
            return (cur.rowcount or 0) > 0

    def mark_celebrated(self, charge_ids) -> int:
        """
        Mark payments as shown. Idempotent, and never un-marks.

        Both the live path and the catch-up path call this, which is what
        keeps them in agreement: a payment celebrated live is not replayed on
        the next reload, because the live path recorded it here too.
        """
        ids = [str(i) for i in (charge_ids or []) if i]
        if not ids:
            return 0
        placeholders = ",".join("?" * len(ids))
        with self._connect() as conn:
            cur = conn.execute(
                f"UPDATE payments SET celebrated_at = ? "
                f"WHERE charge_id IN ({placeholders}) AND celebrated_at IS NULL",
                (_now(), *ids),
            )
            return cur.rowcount or 0

    def read_catchup(self, *, window_hours: int = CATCHUP_WINDOW_HOURS,
                     cap: int = DEFAULT_REPLAY_CAP) -> dict:
        """
        What a screen should celebrate now, newest first, plus what it is NOT
        being shown.

        `withheld` exists so a burst is never a silent truncation. The UI can
        say "and 4 more in history", which is the difference between a
        feature that feels honest and one that feels like it lost something.
        """
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=window_hours)).isoformat()
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM payments WHERE celebrated_at IS NULL AND paid_at >= ? "
                "ORDER BY paid_at DESC",
                (cutoff,),
            ).fetchall()
        payments = [dict(row) for row in rows]
        return {
            "payments": payments[:max(0, cap)],
            "withheld": max(0, len(payments) - max(0, cap)),
        }

    def recent(self, limit: int = 20) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM payments ORDER BY paid_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]

    def get(self, charge_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM payments WHERE charge_id = ?", (charge_id,)
            ).fetchone()
        return None if row is None else dict(row)
