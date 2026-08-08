"""
Settings surface.

The repo reads config from environment variables (loaded from .env by
python-dotenv). This module centralizes the ones the tool layer needs so the
registry can decide what to wire up. Add new `supabase_*_url` fields here as
more read-only databases are connected.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float | None) -> float | None:
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_list(name: str) -> list[str]:
    raw = os.getenv(name, "")
    return [item.strip() for item in raw.split(",") if item.strip()]


@dataclass
class Settings:
    # asyncpg DSN for the trillion_analytics role on the analytics DB.
    # Empty string = not configured; the analytics tool is then skipped.
    supabase_analytics_url: str = ""

    # ── Software Factory ──────────────────────────────────────────────────
    # Where built projects land. The autonomy boundary this feature relies on
    # is drawn here: everything the build pipeline writes stays inside this
    # directory (agent/tools/project_fs.py path-jails to it).
    software_factory_root: str = "generated-projects"

    # Hard (not soft) caps, checked before a build starts — there's no
    # per-run human approval gate for this feature, so these are the backstop.
    factory_daily_build_cap: int = 3
    # None = self-initiated (autonomous) building stays off; on-demand
    # /build still works with just the daily build cap.
    factory_daily_budget_usd: float | None = None

    # Kill switch: stops new on-demand and self-initiated builds instantly,
    # no restart needed.
    factory_paused: bool = False

    # Autonomous scheduler: the boundaries Sean sets once, within which the
    # factory proposes its own project briefs. Empty = autonomous triggering
    # off; on-demand /build is unaffected either way.
    factory_autonomous_themes: list[str] = field(default_factory=list)
    factory_autonomous_interval_hours: float = 24.0


def get_settings() -> Settings:
    return Settings(
        supabase_analytics_url=os.getenv("SUPABASE_ANALYTICS_URL", ""),
        software_factory_root=os.getenv("TRILLION_SOFTWARE_FACTORY_ROOT", "generated-projects"),
        factory_daily_build_cap=int(os.getenv("TRILLION_FACTORY_DAILY_BUILD_CAP", "3")),
        factory_daily_budget_usd=_env_float("TRILLION_FACTORY_DAILY_BUDGET_USD", None),
        factory_paused=_env_bool("TRILLION_FACTORY_PAUSED", False),
        factory_autonomous_themes=_env_list("TRILLION_FACTORY_AUTONOMOUS_THEMES"),
        factory_autonomous_interval_hours=_env_float("TRILLION_FACTORY_AUTONOMOUS_INTERVAL_HOURS", 24.0),
    )
