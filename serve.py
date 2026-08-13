"""
Trillion web server — serves the UI and the cost dashboard endpoint.

Built on aiohttp (already a project dependency). Reads the same usage.db the
agent writes to, so cost data shows up live.

    GET /api/usage              → month-to-date cost payload (JSON, ~60s cached)
    GET /api/heartbeat/notices  → active (undismissed) heartbeat notices (JSON)
    POST /api/heartbeat/dismiss → dismiss a notice by id
    POST /api/security/csp-report → browser CSP-violation reports (report-only mode)
    GET /api/security/cve-status → latest pip-audit scan result (JSON)
    POST /api/security/cve-scan  → run a fresh pip-audit scan and persist it
    GET /api/security/status    → self-audit security shield score (§3.5, JSON)
    GET /                       → the UI (index.html)

Every response carries the security_headers_middleware (agent/security/
headers.py, §2.2): X-Content-Type-Options, Referrer-Policy, X-Frame-Options,
Permissions-Policy, and a report-only Content-Security-Policy.

Run:
    python serve.py
    TRILLION_WEB_PORT=8123 python serve.py

Binds to TRILLION_WEB_HOST (default 127.0.0.1). Binding anything else
requires TRILLION_WEB_AUTH_TOKEN to be set — see agent/security/
startup_guard.py — since there is no auth middleware to protect a public
listener yet.

This is the server the systemd unit runs in place of `python -m http.server`.
"""

from __future__ import annotations

import asyncio
import contextlib
import os

from aiohttp import web
from dotenv import load_dotenv

from agent.config import get_settings
from agent.cost.aggregate import UsageDashboard
from agent.cost.storage import UsageRepo
from agent.security.headers import security_headers_middleware

# Load .env so the web server honors the same config as the CLI agent
# (TRILLION_MONTHLY_BUDGET_USD, TRILLION_USAGE_DB, TRILLION_WEB_PORT).
load_dotenv()

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))

# A single shared provider/registry/agent for the browser voice UI (personal,
# single-user). Built lazily so importing serve.py doesn't require the
# provider SDKs. Split into three singletons (rather than one on _get_agent)
# so the Agent Factory's RegistryWatcher can share the exact same registry
# instance _get_agent() hands to /api/chat — otherwise a dispatch_to_<slug>
# tool registered by the watcher would be invisible to the chat agent.
_provider = None
_registry = None
_agent = None
_gate = None


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


def _get_gate():
    """
    Tier 6 safety rails, best-effort like everything else here — a broken
    safety.db must not stop the browser voice UI, only leave it ungated
    (Agent and handle_slash-equivalent callers already treat gate=None as
    "unavailable").
    """
    global _gate
    if _gate is None:
        try:
            from agent.safety.approval import Gate
            from agent.safety.storage import SafetyRepo

            settings = get_settings()
            safety_repo = SafetyRepo()
            registry = _get_registry()
            _gate = Gate(
                safety_repo, registry,
                mode=settings.confirmation_mode,
                ttl_seconds=settings.confirmation_ttl_seconds,
                paused=settings.trillion_paused,
            )
            registry.set_audit_sink(safety_repo.log)
        except Exception as e:  # noqa: BLE001
            print(f"Safety rails unavailable ({e}); continuing ungated.")
    return _gate


def _get_agent():
    global _agent
    if _agent is None:
        from agent.core import Agent
        from agent.cost.recorder import set_usage_repo
        from agent.cost.storage import UsageRepo
        from agent.memory import load_facts

        set_usage_repo(UsageRepo())  # so browser turns show up in the cost dashboard
        settings = get_settings()
        memory_facts: list[str] = []
        try:
            memory_facts = load_facts(settings.memory_path)
        except Exception as e:  # noqa: BLE001
            print(f"Memory unavailable ({e}); continuing with no facts.")
        _agent = Agent(
            provider=_get_provider(), tool_registry=_get_registry(), gate=_get_gate(),
            memory_facts=memory_facts, memory_path=settings.memory_path,
        )
    return _agent


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


async def _build_notes_index(_app: web.Application) -> None:
    """
    Best-effort Tier 2 notes index build, same posture as the sections above.
    Bounded with a timeout because the vault is an rclone FUSE mount that has
    been observed to hang/error on read while still reporting mounted (see
    agent/notes/index.py) — a broken mount must not stall server startup.
    """
    try:
        from agent.notes.index import build_index

        settings = get_settings()
        indexed = await asyncio.wait_for(
            asyncio.to_thread(build_index, settings.notes_vault_path, settings.notes_index_path),
            timeout=10.0,
        )
        print(f"Notes index: {indexed} file(s).")
    except Exception as e:  # noqa: BLE001
        print(f"Notes index unavailable ({e}); continuing with stale/no index.")


async def _stop_factory_watcher(app: web.Application) -> None:
    task = app.get("factory_watcher_task")
    if task is not None:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def _start_heartbeat_scheduler(app: web.Application) -> None:
    """
    Best-effort Tier 5 heartbeat wiring, mirroring _start_factory_watcher —
    a broken heartbeat.db shouldn't stop the browser voice UI. Code Sentinel
    checks self-skip (empty list) when GitHub isn't configured, so this
    always constructs the scheduler even with zero checks registered.
    """
    app["heartbeat_task"] = None
    try:
        from agent.heartbeat.checks.code_sentinel import build_code_sentinel_checks
        from agent.heartbeat.checks.cve_scan import CveScanCheck
        from agent.heartbeat.scheduler import HeartbeatScheduler
        from agent.heartbeat.storage import HeartbeatRepo

        settings = get_settings()
        repo = HeartbeatRepo()
        checks = build_code_sentinel_checks(settings) + [CveScanCheck()]
        scheduler = HeartbeatScheduler(checks, repo, settings, background_tasks=set())
        app["heartbeat_task"] = asyncio.create_task(scheduler.run_forever())
    except Exception as e:  # noqa: BLE001
        print(f"Heartbeat unavailable ({e}); continuing.")


async def _stop_heartbeat_scheduler(app: web.Application) -> None:
    task = app.get("heartbeat_task")
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

        resp = web.StreamResponse(
            status=200,
            headers={"Content-Type": "text/plain; charset=utf-8", "Cache-Control": "no-store"},
        )
        await resp.prepare(request)
        if message:
            try:
                agent = _get_agent()
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

    async def heartbeat_notices(_request: web.Request) -> web.Response:
        # Polled by the browser (see index.html's fetchHeartbeatNotices),
        # mirroring /api/usage's read-only, best-effort-cached shape.
        from agent.heartbeat.storage import HeartbeatRepo

        notices = HeartbeatRepo().list_active_notices()
        return web.json_response({"notices": notices})

    async def dismiss_notice(request: web.Request) -> web.Response:
        from agent.heartbeat.storage import HeartbeatRepo

        try:
            data = await request.json()
        except Exception:
            data = {}
        notice_id = data.get("id")
        if not isinstance(notice_id, int):
            return web.json_response({"error": "missing or invalid 'id'"}, status=400)
        ok = HeartbeatRepo().dismiss(notice_id)
        return web.json_response({"dismissed": ok})

    async def cve_status(_request: web.Request) -> web.Response:
        # GET-only read of the last scan — never triggers pip-audit itself,
        # mirroring /api/usage's read-a-cached-answer shape.
        from agent.security.cve_scan import CveScanRepo

        latest = CveScanRepo().latest()
        if latest is None:
            return web.json_response(
                {
                    "cve_count": 0,
                    "findings": [],
                    "scanner_version": None,
                    "error_message": "no scan has run yet",
                    "generated_at": None,
                }
            )
        return web.json_response(latest)

    async def cve_scan(_request: web.Request) -> web.Response:
        from agent.security.cve_scan import scan_and_persist

        result = await scan_and_persist()
        return web.json_response(result)

    async def security_status(_request: web.Request) -> web.Response:
        # GET-only aggregate of every safety-rail signal (§3.5) — read-only,
        # no persistence, same shape as cve_status's read-a-cached-answer.
        from agent.security.audit import audit

        return web.json_response(audit(get_settings(), _get_registry()))

    async def csp_report(request: web.Request) -> web.Response:
        # Browser-sent CSP-violation reports while the policy runs in
        # report-only mode (agent-security.md §2.2). Best-effort logging,
        # same posture as every other print() in this file — a malformed
        # report body must not 500. 204 No Content is what the Reporting
        # API expects back.
        try:
            data = await request.json()
        except Exception:
            data = {}
        print(f"[csp-violation] {data}")
        return web.Response(status=204)

    app = web.Application(middlewares=[security_headers_middleware])
    app.router.add_get("/api/usage", usage)
    app.router.add_post("/api/chat", chat)
    app.router.add_post("/api/transcribe", transcribe_audio)
    app.router.add_post("/api/tts", synthesize_speech)
    app.router.add_get("/api/heartbeat/notices", heartbeat_notices)
    app.router.add_post("/api/heartbeat/dismiss", dismiss_notice)
    app.router.add_post("/api/security/csp-report", csp_report)
    app.router.add_get("/api/security/cve-status", cve_status)
    app.router.add_post("/api/security/cve-scan", cve_scan)
    app.router.add_get("/api/security/status", security_status)
    app.router.add_get("/", index)
    app.router.add_get("/index.html", index)
    # Vendored Three.js (see vendor/three/) — served locally instead of the
    # unpkg CDN so the UI works offline and P7's CSP doesn't need a
    # third-party script-src origin.
    app.router.add_static("/vendor/", os.path.join(PROJECT_ROOT, "vendor"))
    app.on_startup.append(_start_factory_watcher)
    app.on_startup.append(_build_notes_index)
    app.on_startup.append(_start_heartbeat_scheduler)
    app.on_cleanup.append(_stop_factory_watcher)
    app.on_cleanup.append(_stop_heartbeat_scheduler)
    return app


def main() -> None:
    from agent.security.startup_guard import check_bind_safety

    port = int(os.getenv("TRILLION_WEB_PORT", "8123"))
    settings = get_settings()
    check_bind_safety(settings.web_host, auth_configured=bool(settings.web_auth_token))
    web.run_app(build_app(), host=settings.web_host, port=port)


if __name__ == "__main__":
    main()
