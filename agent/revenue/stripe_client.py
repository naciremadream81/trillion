"""
Payment detection — playbook/money-celebration.md Phase 1.

Short-window polling rather than webhooks, and that is a deliberate reading
of this deployment: Trillion runs on a Pi behind a home NAT with no public
endpoint, so a webhook would need a tunnel to exist before payments could be
detected at all. Polling has no such dependency. Swap this module for a
webhook handler if the deployment ever gets a public address — everything
downstream keys off `record_payment()` and does not care which produced it.

Three rules, each of which is a way this goes wrong:

**Only real money in.** Successful, captured, non-zero, not refunded. A
refund that celebrates is worse than a missed celebration.

**Re-check the status on every record.** Stripe's charge list does not honour
a status filter the way you would expect, so the query is a hint and the
per-record check is the actual filter. Trusting the query is how a failed
attempt gets a party thrown for it.

**Look back further than the poll interval.** A lookback equal to the
interval leaves a gap at every boundary, and a payment that lands in one is
never seen. The default is comfortably more than double.

Uses the REST API over aiohttp rather than the `stripe` SDK: one authenticated
GET, no new dependency on a Pi that already has aiohttp.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import aiohttp

logger = logging.getLogger(__name__)

STRIPE_CHARGES_URL = "https://api.stripe.com/v1/charges"

# Comfortably longer than the poll interval below — see the docstring.
DEFAULT_LOOKBACK_SECONDS = 900.0
DEFAULT_POLL_SECONDS = 300.0
REQUEST_TIMEOUT_SECONDS = 15.0
PAGE_LIMIT = 50


class StripeError(RuntimeError):
    """The API call failed. Never fatal to the caller — a poll that fails is
    a poll that runs again in five minutes."""


def is_real_money_in(charge: dict) -> bool:
    """
    Whether this charge is a payment worth celebrating.

    Every clause is a filter the list query cannot be trusted to have
    applied. `amount_captured` rather than `amount` because an authorized-
    but-uncaptured charge is not money yet, and a partial capture should
    celebrate what was actually taken.
    """
    if not isinstance(charge, dict):
        return False
    if charge.get("status") != "succeeded":
        return False
    if not charge.get("paid"):
        return False
    if charge.get("refunded"):
        return False
    if charge.get("disputed"):
        return False
    # A partially refunded charge still brought money in; a fully refunded
    # one is caught above.
    if int(charge.get("amount_captured") or 0) <= 0:
        return False
    return True


def customer_label(charge: dict) -> str:
    """A human label, best-effort. Never an id — an id is not a name."""
    details = charge.get("billing_details") or {}
    for candidate in (details.get("name"), details.get("email"),
                      charge.get("receipt_email"), charge.get("description")):
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()[:120]
    return ""


def to_payment(charge: dict) -> dict:
    """The fields the celebration needs, and nothing else."""
    created = charge.get("created")
    paid_at = (
        datetime.fromtimestamp(int(created), tz=timezone.utc).isoformat()
        if isinstance(created, (int, float))
        else datetime.now(timezone.utc).isoformat()
    )
    return {
        "charge_id": str(charge.get("id", "")),
        "amount_minor": int(charge.get("amount_captured") or 0),
        "currency": str(charge.get("currency") or "usd").lower(),
        "customer_label": customer_label(charge),
        "paid_at": paid_at,
    }


async def fetch_recent_charges(api_key: str, *, lookback_seconds: float = DEFAULT_LOOKBACK_SECONDS,
                               session: aiohttp.ClientSession | None = None) -> list[dict]:
    """
    Charges created inside the lookback window, filtered to real money in.

    Raises StripeError on any transport or API failure — the caller treats
    that as "try again next tick", never as a reason to stop polling.
    """
    if not api_key:
        return []
    since = int((datetime.now(timezone.utc) - timedelta(seconds=lookback_seconds)).timestamp())
    params = {"limit": str(PAGE_LIMIT), "created[gte]": str(since)}
    headers = {"Authorization": f"Bearer {api_key}"}

    owns_session = session is None
    session = session or aiohttp.ClientSession()
    try:
        async with session.get(
            STRIPE_CHARGES_URL, params=params, headers=headers,
            timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS),
        ) as response:
            if response.status != 200:
                body = (await response.text())[:200]
                raise StripeError(f"HTTP {response.status}: {body}")
            payload = await response.json()
    except aiohttp.ClientError as e:
        raise StripeError(f"{type(e).__name__}: {e}") from e
    finally:
        if owns_session:
            await session.close()

    data = payload.get("data")
    if not isinstance(data, list):
        raise StripeError("unexpected response shape: no data list")

    # The per-record re-check — see the module docstring.
    return [to_payment(c) for c in data if is_real_money_in(c) and c.get("id")]
