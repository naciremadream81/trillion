"""
Settings surface.

The repo reads config from environment variables (loaded from .env by
python-dotenv). This module centralizes the ones the tool layer needs so the
registry can decide what to wire up. Add new `supabase_*_url` fields here as
more read-only databases are connected.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass
class Settings:
    # asyncpg DSN for the trillion_analytics role on the analytics DB.
    # Empty string = not configured; the analytics tool is then skipped.
    supabase_analytics_url: str = ""


def get_settings() -> Settings:
    return Settings(
        supabase_analytics_url=os.getenv("SUPABASE_ANALYTICS_URL", ""),
    )
