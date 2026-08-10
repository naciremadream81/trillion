"""
Trillion web server — serves the UI and the cost dashboard endpoint.

Built on aiohttp (already a project dependency). Reads the same usage.db the
agent writes to, so cost data shows up live.

    GET /api/usage   → month-to-date cost payload (JSON, ~60s cached)
    GET /            → the UI (index.html)

Run:
    python serve.py
    TRILLION_WEB_PORT=8123 python serve.py

This is the server the systemd unit runs in place of `python -m http.server`.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import uuid
from collections import OrderedDict

from aiohttp import web
from dotenv import load_dotenv

from agent.config import get_settings
from agent.cost.aggregate import UsageDashboard
from agent.cost.storage import UsageRepo

# Load .env so the web server honors the same config as the CLI agent
# (TRILLION_MONTHLY_BUDGET_USD, TRILLION_USAGE_DB, TRILLION_WEB_PORT).
load_dotenv()

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))

_SESSION_COOKIE = "trillion_session"
# Bounds memory on an always-on server against abandoned browser tabs, each
# of which owns an Agent (and its growing conversation history) that nothing
# else ever cleans up — oldest session evicted once the cap is hit.
_MAX_CHAT_SESSIONS = 50

# A single shared provider/registry for the browser voice UI (personal,
# single-user). Built lazily so importing serve.py doesn't require the
# provider SDKs. Kept separate from per-session Agents so the Agent
# Factory's RegistryWatcher can share the exact same registry instance
# /api/chat's agents use — otherwise a dispatch_to_<slug> tool registered by
# the watcher would be invisible to chat. Agents themselves are NOT shared:
# one shared Agent's conversation history would interleave two concurrent
# chats (two tabs, or two people) into a single history list.
_provider = None
_registry = None
_cost_recorder_ready = False
_usage_repo = None
_agent_sessions: "OrderedDict[str, object]" = OrderedDict()


def _ensure_cost_tracking():
    """
    Idempotently register the usage repo as agent/cost/recorder.py's global
    write target, returning the same instance every call. Must run before
    ANY agent work — not just /api/chat — because both factories can do
    LLM work at startup, before a browser has ever sent a chat message:
    the Agent Factory's RegistryWatcher.sync_once() runs synchronously in
    _start_factory_watcher, and the Software Factory's AutonomousScheduler
    ticks immediately in run_forever(). Called as its own startup hook
    (first, ahead of both factories) so record_usage() is never a silent
    no-op for autonomous work.
    """
    global _cost_recorder_ready, _usage_repo
    if not _cost_recorder_ready:
        from agent.cost.recorder import set_usage_repo
        from agent.cost.storage import UsageRepo

        _usage_repo = UsageRepo()
        set_usage_repo(_usage_repo)
        _cost_recorder_ready = True
    return _usage_repo


async def _start_cost_tracking(_app: web.Application) -> None:
    _ensure_cost_tracking()


def _get_provider():
    global _provider
    if _provider is None:
        from agent.providers import get_provider

        _provider = get_provider(os.getenv("TRILLION_PROVIDER", "claude"))
    return _provider


def _get_registry():
    global _registry
    if _registry is None:
        from agent.config import get_settings
        from agent.tools.registry import build_registry

        _registry = build_registry(get_settings())
    return _registry


def _get_agent(session_id: str):
    """
    Look up (or create) the Agent for one browser session. Each session gets
    its own Agent, and so its own conversation history — provider/registry
    stay shared since they hold no per-conversation state.
    """
    agent = _agent_sessions.get(session_id)
    if agent is not None:
        _agent_sessions.move_to_end(session_id)
        return agent

    _ensure_cost_tracking()

    from agent.core import Agent

    agent = Agent(provider=_get_provider(), tool_registry=_get_registry())
    _agent_sessions[session_id] = agent
    if len(_agent_sessions) > _MAX_CHAT_SESSIONS:
        _agent_sessions.popitem(last=False)
    return agent


async def _start_factory_watcher(app: web.Application) -> None:
    """
    Best-effort Agent Factory wiring, mirroring main.py's setup — a broken
    factory.db shouldn't stop the browser voice UI from working. Runs
    sync_once() immediately (agents approved via the CLI in a prior session
    are live as soon as this server starts) then schedules run_forever() as
    a background task tied to aiohttp's own event loop, since serve.py has
    no top-level asyncio.run() to hang a task off of directly like main.py does.
    """
    app["factory_watcher_task"] = None
    try:
        from agent.factory.dispatch import RegistryWatcher
        from agent.factory.storage import FactoryRepo

        repo = FactoryRepo()
        watcher = RegistryWatcher(repo, _get_provider(), _get_registry())
        watcher.sync_once()
        app["factory_watcher_task"] = asyncio.create_task(watcher.run_forever())
    except Exception as e:  # noqa: BLE001
        print(f"Agent Factory unavailable ({e}); continuing.")


async def _stop_factory_watcher(app: web.Application) -> None:
    task = app.get("factory_watcher_task")
    if task is not None:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def _start_software_factory(app: web.Application) -> None:
    """
    Best-effort Software Factory wiring, mirroring _start_factory_watcher —
    a broken software_factory.db shouldn't stop the browser voice UI from
    working. The AutonomousScheduler only starts ticking if
    TRILLION_FACTORY_AUTONOMOUS_THEMES is set; serve.py is what actually runs
    24/7 (via trillion-orb.service), so this is the process that needs to own
    it — main.py's CLI wiring only ticks while a REPL session is open.
    """
    app["sf_scheduler_task"] = None
    app["sf_background_tasks"] = set()
    try:
        from agent.config import get_settings
        from agent.factory.software.scheduler import AutonomousScheduler
        from agent.factory.software.storage import BuildRepo

        settings = get_settings()
        if not settings.factory_autonomous_themes:
            return  # autonomous triggering is off; on-demand builds are unaffected

        sf_repo = BuildRepo()
        scheduler = AutonomousScheduler(
            sf_repo, _get_provider(), settings,
            background_tasks=app["sf_background_tasks"], usage_repo=_ensure_cost_tracking(),
        )
        app["sf_scheduler_task"] = asyncio.create_task(scheduler.run_forever())
    except Exception as e:  # noqa: BLE001
        print(f"Software Factory autonomous scheduler unavailable ({e}); continuing.")


async def _stop_software_factory(app: web.Application) -> None:
    task = app.get("sf_scheduler_task")
    if task is not None:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


def _monthly_budget_from_env() -> float | None:
    """Read the optional soft monthly budget (USD) from $TRILLION_MONTHLY_BUDGET_USD."""
    raw = os.getenv("TRILLION_MONTHLY_BUDGET_USD")
    if not raw:
        return None
    try:
        value = float(raw)
        return value if value > 0 else None
    except ValueError:
        return None


def build_app(dashboard: UsageDashboard | None = None) -> web.Application:
    """
    Construct the aiohttp app. Pass a dashboard in tests; in production it's
    built from the default usage database.
    """
    dash = dashboard or UsageDashboard(
        UsageRepo(), monthly_budget=_monthly_budget_from_env()
    )

    async def usage(_request: web.Request) -> web.Response:
        # dash.payload() is best-effort-cached and pure-read; if aggregation
        # ever raised it would 500, but it's designed to return a zeroed
        # payload on an empty table rather than error.
        return web.json_response(dash.payload())

    async def index(_request: web.Request) -> web.FileResponse:
        return web.FileResponse(os.path.join(PROJECT_ROOT, "index.html"))

    async def chat(request: web.Request) -> web.StreamResponse:
        # Browser voice: receives transcribed text, streams the agent's reply
        # text back chunk-by-chunk. STT/TTS happen in the browser (V0).
        try:
            data = await request.json()
        except Exception:
            data = {}
        message = (data.get("message") or "").strip()

        session_id = request.cookies.get(_SESSION_COOKIE)
        is_new_session = session_id is None
        if is_new_session:
            session_id = uuid.uuid4().hex

        resp = web.StreamResponse(
            status=200,
            headers={"Content-Type": "text/plain; charset=utf-8", "Cache-Control": "no-store"},
        )
        if is_new_session:
            resp.set_cookie(_SESSION_COOKIE, session_id, httponly=True, samesite="Strict")
        await resp.prepare(request)
        if message:
            try:
                agent = _get_agent(session_id)
                async for piece in agent.turn(message):
                    await resp.write(piece.encode("utf-8"))
            except Exception as e:  # surface the real error to the client
                await resp.write(f"\n[agent error: {type(e).__name__}: {e}]".encode("utf-8"))
        await resp.write_eof()
        return resp

    async def transcribe_audio(request: web.Request) -> web.Response:
        # Voice V1 STT: browser posts one recorded push-to-talk clip, we
        # forward it to Deepgram and hand back the transcript.
        from agent.voice.deepgram_stt import TranscriptionError, transcribe

        settings = get_settings()
        audio = await request.read()
        content_type = request.headers.get("Content-Type", "audio/webm")
        try:
            text = await transcribe(audio, content_type, settings.deepgram_api_key)
        except TranscriptionError as e:
            return web.json_response({"error": str(e)}, status=400)
        return web.json_response({"text": text})

    async def synthesize_speech(request: web.Request) -> web.Response:
        # Voice V1 TTS: one sentence in, one WAV clip out. Called once per
        # sentence as the agent's reply streams, so playback can start early.
        # Piper runs on-device and is CPU-bound/blocking, so it's offloaded
        # to a thread rather than awaited directly on the event loop.
        from agent.voice.piper_tts import SynthesisError, synthesize

        settings = get_settings()
        model_path = settings.piper_voice_path
        if not os.path.isabs(model_path):
            model_path = os.path.join(PROJECT_ROOT, model_path)
        try:
            data = await request.json()
        except Exception:
            data = {}
        text = (data.get("text") or "").strip()
        if not text:
            return web.Response(status=400, text="missing text")
        loop = asyncio.get_running_loop()
        try:
            audio = await loop.run_in_executor(None, synthesize, text, model_path)
        except SynthesisError as e:
            return web.Response(status=400, text=str(e))
        return web.Response(body=audio, content_type="audio/wav")

    app = web.Application()
    app.router.add_get("/api/usage", usage)
    app.router.add_post("/api/chat", chat)
    app.router.add_post("/api/transcribe", transcribe_audio)
    app.router.add_post("/api/tts", synthesize_speech)
    app.router.add_get("/", index)
    app.router.add_get("/index.html", index)
    app.on_startup.append(_start_cost_tracking)
    app.on_startup.append(_start_factory_watcher)
    app.on_cleanup.append(_stop_factory_watcher)
    app.on_startup.append(_start_software_factory)
    app.on_cleanup.append(_stop_software_factory)
    return app


def main() -> None:
    port = int(os.getenv("TRILLION_WEB_PORT", "8123"))
    web.run_app(build_app(), host="127.0.0.1", port=port)


if __name__ == "__main__":
    main()
