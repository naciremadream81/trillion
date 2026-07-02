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

    # Voice V1: Deepgram STT (cloud) + Piper TTS (local, offline, free).
    # ElevenLabs' free tier blocks all API voice access — premade voices AND
    # custom/cloned ones both require a paid plan — so TTS runs on-device
    # instead. Empty deepgram key = STT not configured; missing Piper model
    # file = TTS not configured. Both endpoints then 400 with a clear
    # message instead of crashing.
    deepgram_api_key: str = ""
    piper_voice_path: str = "voices/en_US-amy-medium.onnx"


def get_settings() -> Settings:
    return Settings(
        supabase_analytics_url=os.getenv("SUPABASE_ANALYTICS_URL", ""),
        deepgram_api_key=os.getenv("DEEPGRAM_API_KEY", ""),
        piper_voice_path=os.getenv("PIPER_VOICE_PATH", "voices/en_US-amy-medium.onnx"),
    )
