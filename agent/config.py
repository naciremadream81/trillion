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


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        return int(raw)
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

    # Voice V1: Deepgram STT (cloud) + a selectable TTS provider.
    # TTS_PROVIDER picks between "piper" (local, offline, free — the default,
    # and what every existing deployment keeps using unless this is set
    # explicitly) and "elevenlabs" (cloud, paid plan required — ElevenLabs'
    # free tier blocks all API voice access, premade and custom/cloned voices
    # alike). Both are real options now that Sean has a paid ElevenLabs plan;
    # see docs/superpowers/specs/2026-08-17-elevenlabs-tts-provider-design.md
    # for why Piper stays the default (never swap the active voice provider
    # without asking first — playbooks/smooth-voice_2.md, line 19).
    # An unrecognized TTS_PROVIDER value (a typo, e.g. "elevenlab") is not
    # rejected — it silently falls through to the Piper path in serve.py's
    # synthesize_speech(), same as unset. That's deliberate per the design
    # spec, but it means a typo won't announce itself; it'll just look like
    # ElevenLabs was never turned on. Double-check this value if TTS seems to
    # be ignoring an ElevenLabs config that looks otherwise correct.
    # Empty deepgram key = STT not configured; missing Piper model file (or,
    # on the elevenlabs path, empty elevenlabs_api_key) = TTS not configured.
    # Every one of those cases 400s with a clear message instead of crashing.
    deepgram_api_key: str = ""
    piper_voice_path: str = "voices/en_US-amy-medium.onnx"
    tts_provider: str = "piper"
    elevenlabs_api_key: str = ""
    # ElevenLabs' standard premade "Rachel" voice — a concrete default so the
    # elevenlabs path does something out of the box. Not a design commitment
    # to this specific voice; override once a real choice is picked.
    elevenlabs_voice_id: str = "21m00Tcm4TlvDq8ikWAM"
    # Flash: ElevenLabs' lowest-latency model, built for real-time
    # conversational use, at a modest quality trade-off vs. Multilingual v2 —
    # chosen because this branch is actively tuning voice latency on the Pi.
    elevenlabs_model_id: str = "eleven_flash_v2_5"

    # Provider-agnostic web search — the model calls it, Trillion makes the
    # HTTP request, so it works the same regardless of which LLM provider is
    # configured. Used by the main chat registry (when set) and the Software
    # Factory's autonomous scheduler's opportunity scout. Two backends are
    # supported (Brave Search, Firecrawl); empty = the web_search tool isn't
    # offered anywhere. search_provider picks explicitly ("brave" |
    # "firecrawl"); empty = auto-pick from whichever key is present,
    # preferring Firecrawl if both are set (see resolve_search_provider()).
    brave_search_api_key: str = ""
    firecrawl_api_key: str = ""
    search_provider: str = ""
    # Override for a self-hosted Firecrawl instance (e.g.
    # http://localhost:3002) instead of the managed api.firecrawl.dev.
    # Same pattern as OLLAMA_BASE_URL for a local Ollama server.
    firecrawl_base_url: str = "https://api.firecrawl.dev"

    # ── Tier 4: memory ───────────────────────────────────────────────────
    # Markdown facts store — plain and human-readable so Sean can read or
    # hand-edit it directly. Relative paths resolve against the process's
    # working directory (matches software_factory_root's convention above).
    memory_path: str = "memory/facts.md"

    # ── Tier 2: notes search ────────────────────────────────────────────
    # The Aires Ai Brain vault (mounted via rclone — see CLAUDE.md) and the
    # local SQLite FTS5 index built from it. search_notes only ever reads
    # notes_index_path; notes_vault_path is where startup wiring (best-effort,
    # like Tier 4's memory block) rebuilds that index from, so queries never
    # depend on the mount being alive. See agent/notes/index.py.
    notes_vault_path: str = field(default_factory=lambda: os.path.expanduser("~/AiresAiBrain"))
    notes_index_path: str = "memory/notes_index.db"

    # ── Tier 6: safety rails ───────────────────────────────────────────────
    # How aggressively the confirmation gate fires: "off" | "smart" | "manual".
    # Even "off" cannot clear the hardline list (agent/safety/risk.py).
    confirmation_mode: str = "smart"

    # How long a parked action stays approvable. This is what makes the gate
    # safe with nobody present: an action the heartbeat parks at 3am is not
    # still sitting there approvable at noon.
    confirmation_ttl_seconds: int = 900

    # The main kill switch. Stops Trillion from *acting* — gated actions,
    # background dispatch, builds — while leaving conversation and read-only
    # tools alone. A kill switch that also mutes the conversation is one Sean
    # won't reach for, which makes it worse than no kill switch at all.
    trillion_paused: bool = False

    # ── Tier 5: heartbeat ────────────────────────────────────────────────
    # Base tick-loop interval. Each registered check has its own cadence
    # (fast/slow below); this is just how often the loop wakes up to check
    # whether anything is due. Reuses the pre-existing HEARTBEAT_INTERVAL_
    # SECONDS env var name (was a "not built yet" placeholder), lowered from
    # its old 300s default to support the faster per-check cadences.
    heartbeat_tick_interval_seconds: float = 30.0
    # Cadence for CI-style checks that should surface within about a minute.
    heartbeat_fast_cadence_seconds: float = 60.0
    # Cadence for everything else (stale PRs, dormant repos, dependency
    # alerts) — no need to poll GitHub every 60s for things that change
    # over days.
    heartbeat_slow_cadence_seconds: float = 1800.0
    # Quiet hours (both UTC, single-user Pi deployment — see
    # agent/heartbeat/storage.py::is_quiet_hours for the wrap-past-midnight
    # and disabled-when-equal semantics). Reuses the pre-existing QUIET_
    # HOURS_START/END env var names.
    heartbeat_quiet_hours_start: int = 22
    heartbeat_quiet_hours_end: int = 8

    # The Code Sentinel (agent/heartbeat/checks/code_sentinel.py) polls
    # these repos via the GitHub REST API. Empty token or repo list = the
    # Code Sentinel checks self-skip (mirrors resolve_search_provider()'s
    # None-means-skip convention) rather than registering and failing.
    github_token: str = ""
    github_username: str = ""
    github_watched_repos: list[str] = field(default_factory=list)

    # ── Web server bind (§1.5 startup guard) ────────────────────────────────
    # serve.py's bind host. Defaults to loopback-only; agent/security/
    # startup_guard.py refuses to start on anything else unless
    # web_auth_token is set. The token is also enforced per-request on /api/
    # routes by agent/security/auth.py's bearer_auth_middleware, so rotating
    # it immediately revokes anyone holding the old value. When it's empty
    # (the loopback-only default) that middleware is a no-op.
    web_host: str = "127.0.0.1"
    web_auth_token: str = ""
    # §2.1 rotation overlap: the outgoing token, accepted alongside the
    # current one for as long as this is set. Empty in the steady state —
    # it exists only for the window in which callers are being moved over
    # one at a time. agent/security/auth.py::is_authorized honours it;
    # startup_guard deliberately does not, because a bind is only safe on
    # the strength of the *current* token.
    web_auth_token_prev: str = ""
    # §2.2: CSP enforcement. Off means report-only, which is where a policy
    # starts and stays until GET /api/security/csp-violations shows a clean
    # session. Flipping this is the last step of that process, never the
    # first — see agent/security/headers.py.
    csp_enforce: bool = False

    def builds_paused(self) -> bool:
        """
        Whether the Software Factory should refuse to start work.

        factory_paused is a *child scope* of trillion_paused, not a sibling:
        pausing Trillion pauses builds too. The narrower flag stays because
        pausing builds while leaving the rest of Trillion acting is a real
        thing to want.
        """
        return self.trillion_paused or self.factory_paused


def get_settings() -> Settings:
    return Settings(
        supabase_analytics_url=os.getenv("SUPABASE_ANALYTICS_URL", ""),
        software_factory_root=os.getenv("TRILLION_SOFTWARE_FACTORY_ROOT", "generated-projects"),
        factory_daily_build_cap=_env_int("TRILLION_FACTORY_DAILY_BUILD_CAP", 3),
        factory_daily_budget_usd=_env_float("TRILLION_FACTORY_DAILY_BUDGET_USD", None),
        factory_paused=_env_bool("TRILLION_FACTORY_PAUSED", False),
        factory_autonomous_themes=_env_list("TRILLION_FACTORY_AUTONOMOUS_THEMES"),
        factory_autonomous_interval_hours=_env_float("TRILLION_FACTORY_AUTONOMOUS_INTERVAL_HOURS", 24.0),
        deepgram_api_key=os.getenv("DEEPGRAM_API_KEY", ""),
        piper_voice_path=os.getenv("PIPER_VOICE_PATH", "voices/en_US-amy-medium.onnx"),
        tts_provider=os.getenv("TTS_PROVIDER", "piper"),
        elevenlabs_api_key=os.getenv("ELEVENLABS_API_KEY", ""),
        elevenlabs_voice_id=os.getenv("ELEVENLABS_VOICE_ID", "21m00Tcm4TlvDq8ikWAM"),
        elevenlabs_model_id=os.getenv("ELEVENLABS_MODEL_ID", "eleven_flash_v2_5"),
        brave_search_api_key=os.getenv("BRAVE_SEARCH_API_KEY", ""),
        firecrawl_api_key=os.getenv("FIRECRAWL_API_KEY", ""),
        search_provider=os.getenv("TRILLION_SEARCH_PROVIDER", ""),
        firecrawl_base_url=os.getenv("FIRECRAWL_BASE_URL", "https://api.firecrawl.dev"),
        memory_path=os.getenv("TRILLION_MEMORY_PATH", "memory/facts.md"),
        notes_vault_path=os.getenv("TRILLION_NOTES_VAULT_PATH", os.path.expanduser("~/AiresAiBrain")),
        notes_index_path=os.getenv("TRILLION_NOTES_INDEX_PATH", "memory/notes_index.db"),
        confirmation_mode=os.getenv("TRILLION_CONFIRMATION_MODE", "smart"),
        confirmation_ttl_seconds=_env_int("TRILLION_CONFIRMATION_TTL_SECONDS", 900),
        trillion_paused=_env_bool("TRILLION_PAUSED", False),
        heartbeat_tick_interval_seconds=_env_float("HEARTBEAT_INTERVAL_SECONDS", 30.0),
        heartbeat_fast_cadence_seconds=_env_float("HEARTBEAT_FAST_CADENCE_SECONDS", 60.0),
        heartbeat_slow_cadence_seconds=_env_float("HEARTBEAT_SLOW_CADENCE_SECONDS", 1800.0),
        heartbeat_quiet_hours_start=_env_int("QUIET_HOURS_START", 22),
        heartbeat_quiet_hours_end=_env_int("QUIET_HOURS_END", 8),
        github_token=os.getenv("GITHUB_TOKEN", ""),
        github_username=os.getenv("GITHUB_USERNAME", ""),
        github_watched_repos=_env_list("TRILLION_GITHUB_WATCHED_REPOS"),
        web_host=os.getenv("TRILLION_WEB_HOST", "127.0.0.1"),
        web_auth_token=os.getenv("TRILLION_WEB_AUTH_TOKEN", ""),
        web_auth_token_prev=os.getenv("TRILLION_WEB_AUTH_TOKEN_PREV", ""),
        csp_enforce=_env_bool("TRILLION_CSP_ENFORCE"),
    )
