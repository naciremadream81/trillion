"""
BTC/USD price, cached — playbooks/btc-mining-tracker.md, "Data sources".

The playbook asks for a reputable keyless feed, a short TTL (~60s), and an
in-memory lock to avoid stampedes. The lock is the part that is easy to skip
and shouldn't be: the fast tick, the widget's poll, and a voice question can
all want a price in the same second, and without it that is three identical
requests to a public API that will start refusing them.

A price is never load-bearing. Every caller takes None and carries on
showing BTC without a fiat conversion — a mining widget with no dollar
figure is mildly less useful, one that fails to render because a price
lookup timed out is broken.
"""

from __future__ import annotations

import asyncio
import time

import aiohttp

PRICE_URL = "https://api.coingecko.com/api/v3/simple/price"
CACHE_TTL_SECONDS = 60.0

_cached_price: float | None = None
_cached_at: float = 0.0
_lock = asyncio.Lock()


async def get_btc_usd(*, force: bool = False) -> float | None:
    """Current BTC/USD, or None if it can't be fetched. Never raises."""
    global _cached_price, _cached_at

    now = time.monotonic()
    if not force and _cached_price is not None and (now - _cached_at) < CACHE_TTL_SECONDS:
        return _cached_price

    async with _lock:
        # Re-check inside the lock: while we waited, whoever held it has
        # very likely just refreshed the value we were about to fetch.
        now = time.monotonic()
        if not force and _cached_price is not None and (now - _cached_at) < CACHE_TTL_SECONDS:
            return _cached_price
        try:
            timeout = aiohttp.ClientTimeout(total=10)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(
                    PRICE_URL, params={"ids": "bitcoin", "vs_currencies": "usd"}
                ) as resp:
                    if resp.status != 200:
                        return _cached_price
                    data = await resp.json(content_type=None)
            price = float(((data or {}).get("bitcoin") or {}).get("usd") or 0)
            if price > 0:
                _cached_price = price
                _cached_at = time.monotonic()
        except Exception:
            # Stale beats absent: a price from a minute ago is a fine basis
            # for a dashboard figure, and the alternative is the widget
            # losing its dollar column every time the feed hiccups.
            return _cached_price
    return _cached_price


def reset_cache() -> None:
    """Test hook — the module-level cache would otherwise leak across tests."""
    global _cached_price, _cached_at
    _cached_price = None
    _cached_at = 0.0
