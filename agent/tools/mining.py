"""
query_mining — read-only mining status for the conversation.

Lets Sean ask "how are the miners doing?" out loud and get a real answer,
which is the playbook's optional Step 4 (voice/agent integration).

Reads the local database only. It never calls the pool: the heartbeat is
already polling on a schedule, so a tool that fetched would add a second,
unthrottled caller to a public API every time the model felt curious.

The wallet address is deliberately NOT included in the output. The model has
no use for it, and the playbook's privacy guardrail is that a payout address
is Sean's financial identity — putting it in a prompt sends it to a model
provider for no benefit. See agent/security/log_redact.py for the same
reasoning applied to logs.
"""

from __future__ import annotations

from ..safety.risk import READ_ONLY
from .base import BaseTool


def _format_hashrate(hashes_per_second: int) -> str:
    """Hashes/sec into the unit a miner actually speaks in."""
    value = float(hashes_per_second or 0)
    for unit in ("H/s", "KH/s", "MH/s", "GH/s", "TH/s", "PH/s", "EH/s"):
        if value < 1000:
            return f"{value:.2f} {unit}"
        value /= 1000
    return f"{value:.2f} ZH/s"


class QueryMiningTool(BaseTool):
    name = "query_mining"
    description = (
        "Check how Sean's Bitcoin mining is doing right now: hashrate, how many "
        "workers are online or offline, unpaid balance, and recent payouts. Use "
        "this whenever he asks about the miners, mining income, or whether a rig "
        "is down. Returns locally-recorded data from the last poll, not a live "
        "query to the pool."
    )
    input_schema = {"type": "object", "properties": {}}

    factory_allowed = True   # read-only; safe for a spawned specialist
    risk = READ_ONLY
    requires_confirmation = False
    # Output is assembled here from local database values, not from anything
    # the pool sent as free text.
    trusted_output = True

    def __init__(self, repo, address: str) -> None:
        self.repo = repo
        self.address = address

    async def run(self, **kwargs) -> str:
        try:
            summary = self.repo.summary(self.address)
        except Exception as e:  # noqa: BLE001 — errors cross the boundary as data
            return f"[Could not read mining data: {type(e).__name__}: {e}]"

        if not summary.captured_at:
            return (
                "No mining data recorded yet. The heartbeat polls the pool on its "
                "own schedule — if this persists, the pool may be unreachable or "
                "the wallet may not be configured."
            )

        lines = [
            f"Hashrate: {_format_hashrate(summary.hashrate_60s)} "
            f"(24h average {_format_hashrate(summary.hashrate_86400s)})",
            f"Workers: {summary.workers_online} online, "
            f"{summary.workers_degraded} degraded, {summary.workers_offline} offline "
            f"(pool reports {summary.active_worker_count} active)",
            f"Unpaid balance: {summary.unpaid_btc:.8f} BTC",
            f"Estimated next block: {summary.estimated_next_block_btc:.8f} BTC",
        ]
        if summary.btc_usd:
            lines.append(
                f"Payouts in the last 30 days: {summary.payouts_30d_btc:.8f} BTC "
                f"(about ${summary.payouts_30d_btc * summary.btc_usd:,.2f} at "
                f"${summary.btc_usd:,.0f}/BTC)"
            )
        else:
            lines.append(f"Payouts in the last 30 days: {summary.payouts_30d_btc:.8f} BTC")
        if summary.last_payout_at:
            lines.append(
                f"Last payout: {summary.last_payout_btc:.8f} BTC at {summary.last_payout_at}"
            )
        if summary.last_share_at:
            lines.append(f"Last share accepted: {summary.last_share_at}")
        lines.append(f"Data captured: {summary.captured_at}")
        return "\n".join(lines)
