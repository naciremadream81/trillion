"""
Mining tracker storage — playbooks/btc-mining-tracker.md, "Storage shape".

Four tables, following the playbook's shape adapted to this codebase's
SQLite conventions (see agent/cost/storage.py and agent/safety/storage.py):

  wallets           the payout addresses being tracked
  wallet_snapshots  one row per fast tick — hashrate windows, unpaid, workers
  worker_snapshots  one row per worker per fast tick
  payouts           append-only and idempotent

Two decisions carry the weight here.

**Idempotent payout ingestion.** Ocean has no "payouts since X" cursor, and
the playbook says not to fight that: re-poll a sliding window every slow tick
and dedupe at the storage layer. Overlap is free; a missed payout is not. The
uniqueness key is (wallet_id, paid_at, amount_sats) — the playbook's
suggestion, and deliberately NOT the on-chain txid: one transaction can pay
several of a pool's users, and Ocean's generation-transaction payouts share a
txid by construction, so a txid-keyed unique index would silently discard
real payouts.

Amounts are stored in **satoshis as INTEGER**, not BTC as REAL. A payout is
money; binary floating point cannot represent 0.1 exactly, and summing
thousands of REAL payouts accumulates error into a number Sean might
reasonably compare against his wallet. BTC is a presentation concern, applied
at the edge.

**Retention.** Snapshots are written every 60s per wallet — roughly 43k rows
a month, plus one per worker. That is fine for SQLite and not fine forever on
a Pi whose SD card is the part that fails. prune() drops snapshots older than
the retention window; payouts are never pruned, because they are the
financial record and they are tiny.
"""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

SATS_PER_BTC = 100_000_000

SCHEMA = """
CREATE TABLE IF NOT EXISTS wallets (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    address         TEXT    NOT NULL UNIQUE,
    label           TEXT    NOT NULL DEFAULT '',
    created_at      TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS wallet_snapshots (
    id                        INTEGER PRIMARY KEY AUTOINCREMENT,
    wallet_id                 INTEGER NOT NULL,
    hashrates_json            TEXT    NOT NULL DEFAULT '{}',
    unpaid_sats               INTEGER NOT NULL DEFAULT 0,
    estimated_next_block_sats INTEGER NOT NULL DEFAULT 0,
    active_worker_count       INTEGER NOT NULL DEFAULT 0,
    last_share_at             TEXT,
    btc_usd_at_snapshot       REAL,
    captured_at               TEXT    NOT NULL,
    FOREIGN KEY (wallet_id) REFERENCES wallets(id)
);
CREATE INDEX IF NOT EXISTS idx_wallet_snapshots_captured
    ON wallet_snapshots(wallet_id, captured_at);

CREATE TABLE IF NOT EXISTS worker_snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    wallet_id       INTEGER NOT NULL,
    worker_name     TEXT    NOT NULL,
    hashrate_60s    INTEGER NOT NULL DEFAULT 0,
    hashrate_3600s  INTEGER NOT NULL DEFAULT 0,
    status          TEXT    NOT NULL DEFAULT 'unknown',
    captured_at     TEXT    NOT NULL,
    FOREIGN KEY (wallet_id) REFERENCES wallets(id)
);
CREATE INDEX IF NOT EXISTS idx_worker_snapshots_captured
    ON worker_snapshots(wallet_id, captured_at);

CREATE TABLE IF NOT EXISTS payouts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    wallet_id       INTEGER NOT NULL,
    paid_at         TEXT    NOT NULL,
    amount_sats     INTEGER NOT NULL,
    on_chain_txid   TEXT    NOT NULL DEFAULT '',
    is_generation   INTEGER NOT NULL DEFAULT 0,
    recorded_at     TEXT    NOT NULL,
    FOREIGN KEY (wallet_id) REFERENCES wallets(id),
    UNIQUE (wallet_id, paid_at, amount_sats)
);
CREATE INDEX IF NOT EXISTS idx_payouts_paid_at ON payouts(wallet_id, paid_at);
"""

DEFAULT_RETENTION_DAYS = 30


def default_db_path() -> str:
    """Where mining history lives. Override with $TRILLION_MINING_DB."""
    return os.getenv("TRILLION_MINING_DB", "mining.db")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def btc_to_sats(btc: float) -> int:
    """Convert at the edge. round(), not int(): int() truncates, which loses
    a satoshi on almost every value that isn't exactly representable."""
    return int(round(float(btc) * SATS_PER_BTC))


def sats_to_btc(sats: int) -> float:
    return int(sats) / SATS_PER_BTC


@dataclass
class MiningSummary:
    """What the widget and the query tool both read."""

    address: str = ""
    hashrate_60s: int = 0
    hashrate_86400s: int = 0
    unpaid_btc: float = 0.0
    estimated_next_block_btc: float = 0.0
    active_worker_count: int = 0
    workers_online: int = 0
    workers_degraded: int = 0
    workers_offline: int = 0
    last_share_at: str | None = None
    last_payout_at: str | None = None
    last_payout_btc: float = 0.0
    payouts_30d_btc: float = 0.0
    btc_usd: float | None = None
    captured_at: str | None = None

    def to_dict(self) -> dict:
        data = dict(self.__dict__)
        # Convenience for the widget: it should not be doing this arithmetic.
        data["unpaid_usd"] = (
            round(self.unpaid_btc * self.btc_usd, 2) if self.btc_usd else None
        )
        data["payouts_30d_usd"] = (
            round(self.payouts_30d_btc * self.btc_usd, 2) if self.btc_usd else None
        )
        return data


class MiningRepo:
    def __init__(self, db_path: str | None = None) -> None:
        self.db_path = db_path or default_db_path()
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    # ── wallets ──────────────────────────────────────────────────────────
    def ensure_wallet(self, address: str, label: str = "") -> int:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO wallets (address, label, created_at) VALUES (?, ?, ?)",
                (address, label, _now()),
            )
            row = conn.execute(
                "SELECT id FROM wallets WHERE address = ?", (address,)
            ).fetchone()
        return int(row["id"])

    # ── snapshots ────────────────────────────────────────────────────────
    def record_snapshot(
        self,
        wallet_id: int,
        *,
        hashrates: dict,
        unpaid_btc: float,
        estimated_next_block_btc: float,
        active_worker_count: int,
        last_share_at: str | None,
        btc_usd: float | None,
        captured_at: str | None = None,
    ) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO wallet_snapshots
                    (wallet_id, hashrates_json, unpaid_sats, estimated_next_block_sats,
                     active_worker_count, last_share_at, btc_usd_at_snapshot, captured_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    wallet_id,
                    json.dumps({str(k): int(v) for k, v in (hashrates or {}).items()}),
                    btc_to_sats(unpaid_btc),
                    btc_to_sats(estimated_next_block_btc),
                    int(active_worker_count),
                    last_share_at,
                    btc_usd,
                    captured_at or _now(),
                ),
            )
            return int(cur.lastrowid)

    def record_workers(self, wallet_id: int, workers, captured_at: str | None = None) -> int:
        stamp = captured_at or _now()
        rows = [
            (wallet_id, w.worker_name, int(w.hashrate_60s), int(w.hashrate_3600s), w.status, stamp)
            for w in workers or []
        ]
        if not rows:
            return 0
        with self._connect() as conn:
            conn.executemany(
                """
                INSERT INTO worker_snapshots
                    (wallet_id, worker_name, hashrate_60s, hashrate_3600s, status, captured_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
        return len(rows)

    # ── payouts ──────────────────────────────────────────────────────────
    def record_payouts(self, wallet_id: int, payouts) -> int:
        """
        Insert payouts, ignoring ones already stored. Returns how many were
        actually new — which is what the "payout landed" notice keys on, and
        why the count has to come from the database rather than from the
        length of what the pool returned.
        """
        rows = [
            (
                wallet_id,
                p.paid_at,
                btc_to_sats(p.amount_btc),
                p.on_chain_txid,
                1 if p.is_generation_txn else 0,
                _now(),
            )
            for p in payouts or []
        ]
        if not rows:
            return 0
        with self._connect() as conn:
            before = conn.execute(
                "SELECT COUNT(*) AS n FROM payouts WHERE wallet_id = ?", (wallet_id,)
            ).fetchone()["n"]
            conn.executemany(
                """
                INSERT OR IGNORE INTO payouts
                    (wallet_id, paid_at, amount_sats, on_chain_txid, is_generation, recorded_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            after = conn.execute(
                "SELECT COUNT(*) AS n FROM payouts WHERE wallet_id = ?", (wallet_id,)
            ).fetchone()["n"]
        return int(after - before)

    # ── reads ────────────────────────────────────────────────────────────
    def latest_snapshot(self, wallet_id: int) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM wallet_snapshots WHERE wallet_id = ?"
                " ORDER BY captured_at DESC, id DESC LIMIT 1",
                (wallet_id,),
            ).fetchone()
        return dict(row) if row else None

    def latest_workers(self, wallet_id: int) -> list:
        """
        The most recent tick's worker rows.

        Scoped to one captured_at rather than "the newest row per worker":
        a worker that vanished from the pool's response entirely should drop
        out of the view, not linger forever on its last-seen values.
        """
        with self._connect() as conn:
            newest = conn.execute(
                "SELECT MAX(captured_at) AS ts FROM worker_snapshots WHERE wallet_id = ?",
                (wallet_id,),
            ).fetchone()["ts"]
            if not newest:
                return []
            rows = conn.execute(
                "SELECT * FROM worker_snapshots WHERE wallet_id = ? AND captured_at = ?"
                " ORDER BY worker_name",
                (wallet_id, newest),
            ).fetchall()
        return [dict(r) for r in rows]

    def recent_payouts(self, wallet_id: int, limit: int = 10) -> list:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM payouts WHERE wallet_id = ? ORDER BY paid_at DESC LIMIT ?",
                (wallet_id, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def payouts_total_sats(self, wallet_id: int, since_days: int = 30) -> int:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=since_days)).isoformat()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(amount_sats), 0) AS total FROM payouts"
                " WHERE wallet_id = ? AND paid_at >= ?",
                (wallet_id, cutoff),
            ).fetchone()
        return int(row["total"])

    def summary(self, address: str) -> MiningSummary:
        """Everything the widget and the query tool need, in one read."""
        wallet_id = self.ensure_wallet(address)
        snapshot = self.latest_snapshot(wallet_id)
        workers = self.latest_workers(wallet_id)
        payouts = self.recent_payouts(wallet_id, limit=1)

        summary = MiningSummary(address=address)
        if snapshot:
            hashrates = json.loads(snapshot["hashrates_json"] or "{}")
            summary.hashrate_60s = int(hashrates.get("60", 0))
            summary.hashrate_86400s = int(hashrates.get("86400", 0))
            summary.unpaid_btc = sats_to_btc(snapshot["unpaid_sats"])
            summary.estimated_next_block_btc = sats_to_btc(snapshot["estimated_next_block_sats"])
            summary.active_worker_count = int(snapshot["active_worker_count"])
            summary.last_share_at = snapshot["last_share_at"]
            summary.btc_usd = snapshot["btc_usd_at_snapshot"]
            summary.captured_at = snapshot["captured_at"]
        summary.workers_online = sum(1 for w in workers if w["status"] == "online")
        summary.workers_degraded = sum(1 for w in workers if w["status"] == "degraded")
        summary.workers_offline = sum(1 for w in workers if w["status"] == "offline")
        if payouts:
            summary.last_payout_at = payouts[0]["paid_at"]
            summary.last_payout_btc = sats_to_btc(payouts[0]["amount_sats"])
        summary.payouts_30d_btc = sats_to_btc(self.payouts_total_sats(wallet_id, 30))
        return summary

    # ── retention ────────────────────────────────────────────────────────
    def prune(self, retention_days: int = DEFAULT_RETENTION_DAYS) -> int:
        """
        Drop snapshots older than the window. Payouts are never pruned —
        they are the financial record, and there are a handful a day.
        """
        cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat()
        removed = 0
        with self._connect() as conn:
            for table in ("wallet_snapshots", "worker_snapshots"):
                cur = conn.execute(f"DELETE FROM {table} WHERE captured_at < ?", (cutoff,))
                removed += int(cur.rowcount or 0)
        return removed
