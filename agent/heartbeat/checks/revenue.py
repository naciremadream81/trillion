"""
The payment poll — playbook/money-celebration.md Phase 1.

A heartbeat Check so the cadence, the persistence and the failure isolation
come from the existing scheduler.

It returns NO notices, deliberately, and that is the whole design decision
here. A celebration is a thing that happens on screen, not a notification:
routing it through the notice store would put it behind quiet hours, and the
playbook's named trap is that a deferred notification gets released through a
different code path that forgets to celebrate. There is no deferral here — a
detected payment sits uncelebrated in the revenue database until a screen
asks for it, which is the same mechanism that handles the tab having been
closed. One path, not two.
"""

from __future__ import annotations

import logging

from ...revenue.storage import RevenueRepo
from ...revenue.stripe_client import (
    DEFAULT_LOOKBACK_SECONDS,
    DEFAULT_POLL_SECONDS,
    StripeError,
    fetch_recent_charges,
)
from .base import Notice

logger = logging.getLogger(__name__)


class RevenuePollCheck:
    """Polls Stripe for new payments and records them."""

    name = "revenue_poll"

    def __init__(self, api_key: str, repo: RevenueRepo | None = None,
                 *, poll_seconds: float = DEFAULT_POLL_SECONDS,
                 lookback_seconds: float = DEFAULT_LOOKBACK_SECONDS,
                 fetch=fetch_recent_charges):
        self._api_key = api_key
        self._repo = repo or RevenueRepo()
        self.cadence_seconds = poll_seconds
        self._lookback = lookback_seconds
        self._fetch = fetch

    async def run(self, cursor: dict) -> tuple[list[Notice], dict]:
        try:
            payments = await self._fetch(self._api_key, lookback_seconds=self._lookback)
        except StripeError as e:
            # A failed poll is a poll that runs again shortly. Nothing is
            # lost: the next one looks back over the same window.
            logger.info("revenue poll: %s", e)
            return [], cursor
        except Exception:  # noqa: BLE001
            logger.exception("revenue poll failed")
            return [], cursor

        new = 0
        for payment in payments:
            try:
                if self._repo.record_payment(**payment):
                    new += 1
            except Exception:  # noqa: BLE001 — one bad row must not lose the rest
                logger.exception("revenue poll: could not record %s", payment.get("charge_id"))
        if new:
            logger.info("revenue poll: %d new payment(s) awaiting celebration", new)
        # No notices, on purpose — see the module docstring.
        return [], {**cursor, "last_new": new}
