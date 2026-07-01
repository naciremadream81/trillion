"""
Month-to-date aggregation for the cost dashboard.

`build_month_to_date` is a pure function of (repo, now) — inject `now` in
tests, leave it out in production. `UsageDashboard` wraps it in a short
in-memory cache so a UI polling once a minute is effectively free.

Cache-savings is the number people actually love: what the cache-read tokens
WOULD have cost at the full input rate, minus what they actually cost at the
cache-read rate. It's derived straight from the pricing table, so it stays
correct when rates change.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

from .pricing import compute_cost


def _month_start(dt: datetime) -> datetime:
    return dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _prev_month_start(month_start: datetime) -> datetime:
    # One microsecond before the 1st lands in the previous month; snap to its 1st.
    last_day_prev = month_start.replace(hour=0) - _one_day()
    return last_day_prev.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _one_day():
    from datetime import timedelta
    return timedelta(days=1)


def _round(x: float) -> float:
    return round(x, 6)


# Fraction of the monthly budget at which the indicator starts warning.
BUDGET_WARN_FRACTION = 0.8


def _budget_block(total_cost: float, monthly_budget: float | None) -> dict:
    """
    Budget status for the payload. When no budget is configured, state is
    'none' and the UI shows no alert coloring.
    """
    if not monthly_budget or monthly_budget <= 0:
        return {"limit_usd": None, "used_pct": None, "state": "none"}

    used_pct = total_cost / monthly_budget * 100.0
    if used_pct >= 100.0:
        state = "over"
    elif used_pct >= BUDGET_WARN_FRACTION * 100.0:
        state = "warn"
    else:
        state = "ok"
    return {
        "limit_usd": _round(monthly_budget),
        "used_pct": _round(used_pct),
        "state": state,
    }


def _cache_savings(by_model: list[dict]) -> float:
    """Sum, per model, (full-input cost − cache-read cost) of cache-read tokens."""
    savings = 0.0
    for m in by_model:
        cr = m.get("cache_read_tokens", 0) or 0
        if cr <= 0:
            continue
        model = m["model"]
        full = compute_cost(model, input_tokens=cr)
        cached = compute_cost(model, cache_read_tokens=cr)
        savings += full - cached
    return savings


def build_month_to_date(
    repo,
    now: datetime | None = None,
    monthly_budget: float | None = None,
) -> dict:
    """
    Build the dashboard payload. Never raises on an empty table — it returns a
    clean zeroed payload. `monthly_budget` (USD) drives the soft budget alert;
    None disables it.
    """
    now = now or datetime.now(timezone.utc)
    month_start = _month_start(now)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    now_iso = now.isoformat()
    month_start_iso = month_start.isoformat()
    today_start_iso = today_start.isoformat()

    # Month-to-date and today.
    mtd = repo.usage_since(month_start_iso, now_iso)
    today = repo.usage_since(today_start_iso, now_iso)
    by_day = repo.usage_by_day(month_start_iso, now_iso)

    # Same-elapsed window in the previous month, for a fair month-over-month
    # delta (month-to-date vs the equivalent point last month).
    prev_month_start = _prev_month_start(month_start)
    prev_period_end = prev_month_start + (now - month_start)
    prev = repo.usage_since(prev_month_start.isoformat(), prev_period_end.isoformat())

    total_cost = mtd["cost_usd"]
    prev_cost = prev["cost_usd"]
    change_usd = total_cost - prev_cost
    change_pct = (change_usd / prev_cost * 100.0) if prev_cost > 0 else None

    return {
        "month": now.strftime("%Y-%m"),
        "generated_at": now_iso,
        "total_cost_usd": _round(total_cost),
        "calls": mtd["calls"],
        "input_tokens": mtd["input_tokens"],
        "output_tokens": mtd["output_tokens"],
        "cache_write_tokens": mtd["cache_write_tokens"],
        "cache_read_tokens": mtd["cache_read_tokens"],
        "cache_savings_usd": _round(_cache_savings(mtd["by_model"])),
        "today_cost_usd": _round(today["cost_usd"]),
        "by_model": [
            {
                "model": m["model"],
                "cost_usd": _round(m["cost_usd"]),
                "calls": m["calls"],
                "input_tokens": m["input_tokens"],
                "output_tokens": m["output_tokens"],
                "cache_write_tokens": m["cache_write_tokens"],
                "cache_read_tokens": m["cache_read_tokens"],
            }
            for m in mtd["by_model"]
        ],
        "by_source": [
            {"source": s["source"], "cost_usd": _round(s["cost_usd"]), "calls": s["calls"]}
            for s in mtd["by_source"]
        ],
        "by_day": [
            {"day": d["day"], "cost_usd": _round(d["cost_usd"]), "calls": d["calls"]}
            for d in by_day
        ],
        "delta": {
            "prev_period_cost_usd": _round(prev_cost),
            "change_usd": _round(change_usd),
            "change_pct": (_round(change_pct) if change_pct is not None else None),
        },
        "budget": _budget_block(total_cost, monthly_budget),
    }


class UsageDashboard:
    """build_month_to_date + a short in-memory TTL cache (default 60s)."""

    def __init__(
        self,
        repo,
        ttl_seconds: float = 60.0,
        monthly_budget: float | None = None,
    ) -> None:
        self.repo = repo
        self.ttl = ttl_seconds
        self.monthly_budget = monthly_budget
        self._cached: dict | None = None
        self._cached_at: float | None = None

    def payload(self, now: datetime | None = None) -> dict:
        # Explicit `now` (tests) always bypasses the cache.
        if now is None and self._cached is not None and self._cached_at is not None:
            if (time.monotonic() - self._cached_at) < self.ttl:
                return self._cached

        data = build_month_to_date(
            self.repo, now=now, monthly_budget=self.monthly_budget
        )

        if now is None:
            self._cached = data
            self._cached_at = time.monotonic()
        return data
