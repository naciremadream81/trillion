"""
Ocean mining pool client — playbooks/btc-mining-tracker.md, Step 2.

The playbook is emphatic on one point: "Read its docs (or ask me for a sample
response) before you write any parsing code — do **not** invent field names or
endpoint shapes." Ocean publishes no complete API reference, so every field
below was read off a live response from the real API; those responses are
checked into tests/fixtures/ocean/ and the parsers are tested against them.
Nothing here is guessed.

Four endpoints, all keyless and address-addressed:

  /v1/statsnap/{address}           live snapshot: short hashrate windows,
                                   unpaid balance, next-block estimates
  /v1/user_hashrate/{address}      all eight hashrate windows + worker count
  /v1/user_hashrate_full/{address} per-worker hashrate windows
  /v1/earnpay/{address}/{since_ts} earnings and payouts in a window

THREE QUIRKS THE REAL RESPONSES HAVE, each of which silently yields nothing
if you write the name you'd expect instead of the name that is actually there:

  1. `lastest_share_ts` — Ocean's own spelling. Not "latest".
  2. `fees_colected_satoshis` — Ocean's own spelling. Not "collected".
  3. Hashrates are JSON *strings*, not numbers, and they have to be: a
     ~9.3e17 h/s pool figure is well past 2^53, so parsing it as a JSON
     number would quietly lose precision. Kept as int throughout.

And one shape that is easy to get wrong: in user_hashrate_full, `workers`
maps a worker name to a **list containing one dict**, not to a dict.

Parsing is split from I/O — pure functions that take a decoded payload — for
the same reason auth.py splits is_authorized() from its middleware: the part
worth testing is the part that has no network in it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

import aiohttp

OCEAN_API_BASE = "https://api.ocean.xyz"

# Ocean's hashrate windows, in seconds. The API exposes all eight on
# user_hashrate; statsnap carries only the two shortest.
HASHRATE_WINDOWS = (60, 300, 600, 1800, 3600, 10800, 43200, 86400)

SATS_PER_BTC = 100_000_000


class MiningPoolError(RuntimeError):
    pass


def _to_int(value) -> int:
    """
    Parse a hashrate/share count that arrives as a string.

    int() rather than float(): see the module docstring — these exceed 2^53
    and float would round them. Returns 0 rather than raising on anything
    unparseable, because one bad field in a snapshot should cost that field,
    not the whole tick.
    """
    if value is None:
        return 0
    try:
        return int(str(value).strip())
    except (ValueError, TypeError):
        try:
            return int(float(value))
        except (ValueError, TypeError):
            return 0


def _to_btc(value) -> float:
    """Parse a BTC amount, which Ocean sends as an 8dp decimal string."""
    try:
        return float(str(value).strip())
    except (ValueError, TypeError):
        return 0.0


def _unix_to_iso(value) -> str | None:
    """Ocean's unix-second timestamps arrive as strings."""
    seconds = _to_int(value)
    if seconds <= 0:
        return None
    return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat()


def _naive_iso_to_utc(value) -> str | None:
    """
    Ocean's earnings/payouts timestamps are ISO strings with **no timezone**
    ("2026-08-20T23:11:03"). They are UTC; stamping that explicitly here
    stops a naive datetime from being read as local time later, which on a
    machine that isn't on UTC would silently shift every payout.
    """
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).strip())
    except (ValueError, TypeError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


@dataclass
class WalletSnapshot:
    """One fast-tick reading of a wallet's live state."""

    hashrates: dict = field(default_factory=dict)  # window seconds -> h/s
    unpaid_btc: float = 0.0
    estimated_next_block_btc: float = 0.0
    active_worker_count: int = 0
    last_share_at: str | None = None
    captured_at: str = ""

    def __post_init__(self) -> None:
        if not self.captured_at:
            self.captured_at = datetime.now(timezone.utc).isoformat()


@dataclass
class WorkerSnapshot:
    worker_name: str
    hashrate_60s: int = 0
    hashrate_3600s: int = 0
    hashrate_86400s: int = 0
    status: str = "offline"


@dataclass
class Payout:
    paid_at: str
    amount_btc: float
    on_chain_txid: str = ""
    is_generation_txn: bool = False


def parse_statsnap(payload: dict) -> WalletSnapshot:
    """Parse /v1/statsnap — the live-state endpoint."""
    result = (payload or {}).get("result") or {}
    return WalletSnapshot(
        hashrates={
            60: _to_int(result.get("hashrate_60s")),
            300: _to_int(result.get("hashrate_300s")),
        },
        unpaid_btc=_to_btc(result.get("unpaid")),
        estimated_next_block_btc=_to_btc(result.get("estimated_payout_next_block")),
        # statsnap has no worker count; user_hashrate supplies it.
        active_worker_count=0,
        last_share_at=_unix_to_iso(result.get("lastest_share_ts")),  # sic — see module docstring
        captured_at=_unix_to_iso(result.get("snap_ts")) or "",
    )


def parse_user_hashrate(payload: dict) -> WalletSnapshot:
    """Parse /v1/user_hashrate — all eight windows plus the worker count."""
    result = (payload or {}).get("result") or {}
    return WalletSnapshot(
        hashrates={w: _to_int(result.get(f"hashrate_{w}s")) for w in HASHRATE_WINDOWS},
        active_worker_count=_to_int(result.get("active_worker_count")),
        last_share_at=_unix_to_iso(result.get("lastest_share_ts")),  # sic
        captured_at=_unix_to_iso(result.get("snap_ts")) or "",
    )


def classify_worker(hashrate_60s: int, hashrate_3600s: int, degraded_ratio: float = 0.5) -> str:
    """
    Derive a worker's status from its hashrate windows.

    The playbook calls this out: most pools, Ocean included, expose no
    per-worker "last share" timestamp, so status has to be inferred. The
    long window is the worker's own recent normal, which makes this
    self-calibrating — a 2 TH/s miner and a 200 TH/s miner are judged
    against themselves rather than against a shared threshold that would be
    meaningless for one of them.

      offline   nothing in the last minute, having hashed over the hour
      degraded  producing, but well under its own recent baseline (thermal
                throttling, a failing hashboard, an intermittent PSU)
      online    keeping up with itself
    """
    if hashrate_60s <= 0:
        # Never-seen and just-stopped are different things. A worker with no
        # history either way is not "offline" — there is nothing to be
        # offline *from*, and reporting it as such would fire an alert for
        # every worker name that ever existed.
        return "offline" if hashrate_3600s > 0 else "unknown"
    if hashrate_3600s > 0 and hashrate_60s < hashrate_3600s * degraded_ratio:
        return "degraded"
    return "online"


def parse_workers(payload: dict) -> list:
    """
    Parse /v1/user_hashrate_full into per-worker snapshots.

    Note the shape: `workers` maps a name to a LIST containing one dict, not
    to a dict. Indexing it as a dict raises; assuming a bare dict yields
    nothing.
    """
    result = (payload or {}).get("result") or {}
    workers = result.get("workers") or {}
    if not isinstance(workers, dict):
        return []

    snapshots = []
    for name, entry in workers.items():
        if isinstance(entry, list):
            record = entry[0] if entry and isinstance(entry[0], dict) else {}
        elif isinstance(entry, dict):
            record = entry
        else:
            continue
        h60 = _to_int(record.get("hashrate_60s"))
        h3600 = _to_int(record.get("hashrate_3600s"))
        snapshots.append(
            WorkerSnapshot(
                worker_name=str(name),
                hashrate_60s=h60,
                hashrate_3600s=h3600,
                hashrate_86400s=_to_int(record.get("hashrate_86400s")),
                status=classify_worker(h60, h3600),
            )
        )
    return snapshots


def parse_payouts(payload: dict) -> list:
    """
    Parse the `payouts` half of /v1/earnpay.

    Amounts arrive in satoshis as integers; they are converted to BTC once,
    here, so nothing downstream has to remember which unit it is holding.
    """
    result = (payload or {}).get("result") or {}
    payouts = []
    for entry in result.get("payouts") or []:
        if not isinstance(entry, dict):
            continue
        paid_at = _naive_iso_to_utc(entry.get("ts"))
        if not paid_at:
            continue
        payouts.append(
            Payout(
                paid_at=paid_at,
                amount_btc=_to_int(entry.get("total_satoshis_net_paid")) / SATS_PER_BTC,
                on_chain_txid=str(entry.get("on_chain_txid") or ""),
                is_generation_txn=bool(entry.get("is_generation_txn")),
            )
        )
    return payouts


def parse_earnings_total_btc(payload: dict) -> float:
    """Total net earned across the `earnings` half of /v1/earnpay, in BTC."""
    result = (payload or {}).get("result") or {}
    total = 0
    for entry in result.get("earnings") or []:
        if isinstance(entry, dict):
            total += _to_int(entry.get("satoshis_net_earned"))
    return total / SATS_PER_BTC


class OceanClient:
    """
    Thin async client. One method per endpoint, each returning the decoded
    payload for the pure parsers above to handle.

    Behind a seam like agent/providers/: a different pool is a different
    client with the same four methods, not a change to storage, the
    heartbeat checks, or the widget.
    """

    def __init__(self, address: str, *, base_url: str = OCEAN_API_BASE, timeout: float = 15.0) -> None:
        if not address:
            raise MiningPoolError("No mining wallet configured (TRILLION_MINING_WALLET).")
        self.address = address
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    async def _get(self, path: str) -> dict:
        url = f"{self.base_url}{path}"
        timeout = aiohttp.ClientTimeout(total=self.timeout)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        raise MiningPoolError(f"Ocean {resp.status} on {path}: {body[:200]}")
                    return await resp.json(content_type=None)
        except aiohttp.ClientError as e:
            raise MiningPoolError(f"Could not reach Ocean ({e}).") from e

    async def statsnap(self) -> dict:
        return await self._get(f"/v1/statsnap/{self.address}")

    async def user_hashrate(self) -> dict:
        return await self._get(f"/v1/user_hashrate/{self.address}")

    async def user_hashrate_full(self) -> dict:
        return await self._get(f"/v1/user_hashrate_full/{self.address}")

    async def earnpay(self, since_ts: int) -> dict:
        return await self._get(f"/v1/earnpay/{self.address}/{int(since_ts)}")
