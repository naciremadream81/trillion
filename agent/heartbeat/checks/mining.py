"""
Mining heartbeat checks — playbooks/btc-mining-tracker.md, "Two polling
cadences" and "Alerting".

Two Checks implementing the protocol in agent/heartbeat/checks/base.py, so
quiet hours, dedup cursors, dismissible notices, and schedule persistence all
come from the existing scheduler rather than being rebuilt here.

  MiningLiveCheck    ~60s  hashrate, workers, unpaid balance
  MiningPayoutCheck  ~300s payouts, re-polling a sliding window

They run independently on purpose: the playbook notes payout endpoints tend
to be the flakiest, and isolating them keeps live data snappy when payouts
hiccup.

ALERT POLICY, from the interview: **a worker going offline is the only
condition that surfaces a notice.** Hashrate dips, payouts arriving, and
stale data are recorded and readable in the widget but do not interrupt.
That is the playbook's "quiet by default — it earns interruptions, it
doesn't assume them", and it is why a rig that drops is worth a notice while
a payout landing is not: one is money stopping, the other is money arriving.

Severity is deliberately NOT critical, which means quiet hours defer it
(agent/heartbeat/storage.py). A dead rig costs real money and still does not
justify a 3am wake-up — it will be just as dead at 8am, and an assistant
that wakes you for it once is an assistant you mute. The notice is held and
delivered when the window ends, which is the catch-up-on-return behaviour
Tier 5 asks for.
"""

from __future__ import annotations

import logging
import time

from ...mining.client import (
    MiningPoolError,
    parse_payouts,
    parse_statsnap,
    parse_user_hashrate,
    parse_workers,
)
from ...mining.price import get_btc_usd
from ...mining.storage import DEFAULT_RETENTION_DAYS, MiningRepo
from .base import Notice

logger = logging.getLogger(__name__)

# The playbook's suggested re-poll window for payouts. Overlap is free.
PAYOUT_WINDOW_DAYS = 7


class MiningLiveCheck:
    """Fast tick: live state, and the one alert that interrupts."""

    name = "mining_live"

    def __init__(self, client, repo: MiningRepo, cadence_seconds: float = 60.0) -> None:
        self.client = client
        self.repo = repo
        self.cadence_seconds = cadence_seconds

    async def run(self, cursor: dict) -> tuple:
        notices: list = []
        # Whichever workers were already reported offline. Without this the
        # same dead rig produces a notice every 60 seconds all night, which
        # is precisely the "cries wolf" failure the playbook warns about.
        already_offline = set(cursor.get("offline_workers") or [])

        try:
            statsnap = parse_statsnap(await self.client.statsnap())
            hashrate = parse_user_hashrate(await self.client.user_hashrate())
            workers = parse_workers(await self.client.user_hashrate_full())
        except MiningPoolError as e:
            # Errors are data, not exceptions (orchestration.md Tier 3). A
            # pool outage must not stop the heartbeat or the other checks —
            # and must not fire an alert either, or a flaky API becomes a
            # nightly false "your rigs are down".
            logger.warning("mining live check: %s", e)
            return [], cursor

        price = await get_btc_usd()
        wallet_id = self.repo.ensure_wallet(self.client.address)
        self.repo.record_snapshot(
            wallet_id,
            hashrates=hashrate.hashrates,
            unpaid_btc=statsnap.unpaid_btc,
            estimated_next_block_btc=statsnap.estimated_next_block_btc,
            active_worker_count=hashrate.active_worker_count,
            last_share_at=statsnap.last_share_at,
            btc_usd=price,
        )
        self.repo.record_workers(wallet_id, workers)

        offline_now = {w.worker_name for w in workers if w.status == "offline"}
        newly_offline = sorted(offline_now - already_offline)
        if newly_offline:
            shown = ", ".join(newly_offline[:5])
            if len(newly_offline) > 5:
                shown += f", and {len(newly_offline) - 5} more"
            notices.append(
                Notice(
                    severity="warning",
                    message=(
                        f"Mining worker{'s' if len(newly_offline) > 1 else ''} offline: {shown}. "
                        f"{len(offline_now)} of {len(workers)} workers are down."
                    ),
                )
            )

        # Recovered workers drop out of the cursor so a rig that comes back
        # and dies again alerts a second time. Storing "ever been offline"
        # would silently suppress every repeat failure.
        cursor = dict(cursor)
        cursor["offline_workers"] = sorted(offline_now)
        return notices, cursor


class MiningPayoutCheck:
    """Slow tick: payouts, plus retention pruning."""

    name = "mining_payouts"

    def __init__(
        self,
        client,
        repo: MiningRepo,
        cadence_seconds: float = 300.0,
        retention_days: int = DEFAULT_RETENTION_DAYS,
    ) -> None:
        self.client = client
        self.repo = repo
        self.cadence_seconds = cadence_seconds
        self.retention_days = retention_days

    async def run(self, cursor: dict) -> tuple:
        since = int(time.time()) - PAYOUT_WINDOW_DAYS * 86400
        try:
            payload = await self.client.earnpay(since)
        except MiningPoolError as e:
            logger.warning("mining payout check: %s", e)
            return [], cursor

        wallet_id = self.repo.ensure_wallet(self.client.address)
        new_count = self.repo.record_payouts(wallet_id, parse_payouts(payload))

        # Pruning rides on the slow tick rather than getting its own timer:
        # it is cheap, it only needs to happen occasionally, and one fewer
        # scheduled thing is one fewer thing to reason about.
        pruned = self.repo.prune(self.retention_days)
        if pruned:
            logger.info("mining: pruned %s snapshot rows past retention", pruned)

        # A payout arriving is recorded, not announced — see the module
        # docstring's alert policy. It shows up in the widget on the next
        # poll, where money arriving belongs.
        if new_count:
            logger.info("mining: %s new payout(s) recorded", new_count)
        return [], dict(cursor)


def build_mining_checks(settings) -> list:
    """
    Both checks, or nothing at all when no wallet is configured.

    Self-skipping rather than failing, mirroring build_code_sentinel_checks:
    an unconfigured feature should be absent, not an error on every tick.
    """
    address = (getattr(settings, "mining_wallet", "") or "").strip()
    if not address:
        return []
    from ...mining.client import OceanClient

    client = OceanClient(address)
    repo = MiningRepo()
    return [
        MiningLiveCheck(client, repo, cadence_seconds=settings.heartbeat_fast_cadence_seconds),
        MiningPayoutCheck(
            client,
            repo,
            cadence_seconds=settings.heartbeat_slow_cadence_seconds,
            retention_days=getattr(settings, "mining_retention_days", DEFAULT_RETENTION_DAYS),
        ),
    ]
