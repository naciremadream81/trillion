"""
Trillion web server — serves the UI and the cost dashboard endpoint.

Built on aiohttp (already a project dependency). Reads the same usage.db the
agent writes to, so cost data shows up live.

    GET /api/usage              → month-to-date cost payload (JSON, ~60s cached)
    GET /api/agents             → active Agent Factory specialists + working state (JSON)
    GET /api/heartbeat/notices  → active (undismissed) heartbeat notices (JSON)
    POST /api/heartbeat/dismiss → dismiss a notice by id
    POST /api/security/csp-report → browser CSP-violation reports (report-only mode)
    GET /api/security/cve-status → latest pip-audit scan result (JSON)
    POST /api/security/cve-scan  → run a fresh pip-audit scan and persist it
    GET /api/security/status    → self-audit security shield score (§3.5, JSON)
    GET /                       → the UI (index.html)

Every response carries the security_headers_middleware (agent/security/
headers.py, §2.2): X-Content-Type-Options, Referrer-Policy, X-Frame-Options,
Permissions-Policy, and a report-only Content-Security-Policy. Every /api/
request (except the CSP report endpoint) is also checked by
bearer_auth_middleware (agent/security/auth.py) against
TRILLION_WEB_AUTH_TOKEN, when that token is set, and every state-changing one
by origin_check_middleware (agent/security/origin.py), which refuses
cross-origin POSTs so a page you happen to be visiting can't drive /api/chat
or /api/security/cve-scan on your behalf.

Run:
    python serve.py
    TRILLION_WEB_PORT=8123 python serve.py
    TRILLION_WEB_STRICT_PORT=1 TRILLION_WEB_PORT=8123 python serve.py

Binds to TRILLION_WEB_HOST (default 127.0.0.1). Binding anything else
requires TRILLION_WEB_AUTH_TOKEN to be set — see agent/security/
startup_guard.py.

This is the server the systemd unit runs in place of `python -m http.server`.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import socket
import uuid
from collections import OrderedDict
from datetime import datetime, timedelta, timezone

from aiohttp import web
from dotenv import load_dotenv

from agent.config import get_settings
from agent.cost.aggregate import UsageDashboard
from agent.cost.storage import UsageRepo
from agent.security.auth import bearer_auth_middleware
from agent.security.headers import security_headers_middleware
from agent.security.origin import origin_check_middleware

# Load .env so the web server honors the same config as the CLI agent
# (TRILLION_MONTHLY_BUDGET_USD, TRILLION_USAGE_DB, TRILLION_WEB_PORT).
load_dotenv()

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))

_SESSION_COOKIE = "trillion_session"
# Bounds memory on an always-on server against abandoned browser tabs, each
# of which owns an Agent (and its growing conversation history) that nothing
# else ever cleans up — oldest session evicted once the cap is hit.
_MAX_CHAT_SESSIONS = 50

# Cap on how much of a CSP violation report we read and log. Real reports from
# Chrome and Firefox run a few hundred bytes; 4 KiB is generous for a genuine
# one and small enough that a hostile page POSTing in a loop can't fill the
# journal. The endpoint is unauthenticated by necessity — see csp_report().
CSP_REPORT_MAX_BYTES = 4096

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
# Compatibility for older tests/helpers that reset serve._agent. Browser chat
# now uses _agent_sessions instead.
_agent = None


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


class _SessionToolRegistry:
    """
    Per-chat-session overlay for tools that close over one Agent's history,
    delegating everything else to the shared server registry. That keeps
    confirm_action / memory tools session-local while Factory-dispatch tools
    registered by RegistryWatcher remain visible to existing browser sessions.
    """

    def __init__(self, shared_registry) -> None:
        from agent.tools.registry import ToolRegistry

        self._shared = shared_registry
        self._local = ToolRegistry()

    def set_audit_sink(self, sink) -> None:
        self._shared.set_audit_sink(sink)
        self._local.set_audit_sink(sink)

    def register(self, tool) -> None:
        self._local.register(tool)

    def unregister(self, name: str) -> None:
        self._local.unregister(name)

    def names(self) -> list[str]:
        return self._local.names() + [
            name for name in self._shared.names() if name not in self._local.names()
        ]

    def get(self, name: str):
        return self._local.get(name) or self._shared.get(name)

    def schemas(self) -> list[dict]:
        local_names = set(self._local.names())
        return self._local.schemas() + [
            schema
            for schema in self._shared.schemas()
            if schema.get("name") not in local_names
        ]

    def factory_allowed_names(self) -> set[str]:
        return self._shared.factory_allowed_names()

    async def run(self, tool_call) -> str:
        if self._local.get(tool_call.name) is not None:
            return await self._local.run(tool_call)
        return await self._shared.run(tool_call)


def _make_gate(registry):
    """
    Tier 6 safety rails, best-effort like everything else here — a broken
    safety.db must not stop the browser voice UI, only leave it ungated
    (Agent and handle_slash-equivalent callers already treat gate=None as
    "unavailable").
    """
    try:
        from agent.safety.approval import Gate
        from agent.safety.storage import SafetyRepo

        settings = get_settings()
        safety_repo = SafetyRepo()
        gate = Gate(
            safety_repo, registry,
            mode=settings.confirmation_mode,
            ttl_seconds=settings.confirmation_ttl_seconds,
            paused=settings.trillion_paused,
        )
        registry.set_audit_sink(safety_repo.log)
        return gate
    except Exception as e:  # noqa: BLE001
        print(f"Safety rails unavailable ({e}); continuing ungated.")
        return None


def _get_agent(session_id: str):
    """
    Look up (or create) the Agent for one browser session. Each session gets
    its own Agent, and so its own conversation history. The shared registry
    remains the live home for server-wide tools; per-agent tools use a small
    session overlay so their callbacks don't bleed across tabs.
    """
    agent = _agent_sessions.get(session_id)
    if agent is not None:
        _agent_sessions.move_to_end(session_id)
        return agent

    _ensure_cost_tracking()

    from agent.core import Agent
    from agent.memory import load_facts

    settings = get_settings()
    memory_facts: list[str] = []
    try:
        memory_facts = load_facts(settings.memory_path)
    except Exception as e:  # noqa: BLE001
        print(f"Memory unavailable ({e}); continuing with no facts.")
    registry = _SessionToolRegistry(_get_registry())
    agent = Agent(
        provider=_get_provider(),
        tool_registry=registry,
        gate=_make_gate(registry),
        memory_facts=memory_facts,
        memory_path=settings.memory_path,
    )
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


async def _warm_tts(app: web.Application) -> None:
    """
    Best-effort Piper warm-up, same posture as _build_notes_index above.

    README.md records ~3.5s of extra latency on the *first* Piper synthesis
    after process start: loading the ~63MB ONNX model plus first-inference
    allocation, neither of which happens again once the voice is cached (see
    piper_tts.py). Left alone, that 3.5s tax lands on whichever user happens
    to speak first after a deploy or restart. Paying it here at startup
    instead means it's gone before anyone's listening.

    This is launched as a background task (app["tts_warm_task"]), NOT
    awaited inline, and that's the whole point: blocking on_startup on this
    would just move the 3.5s from "first user turn" to "server refuses
    connections for 3.5s after every restart" — trading one bad latency
    number for a worse one. Mirrors _start_heartbeat_scheduler's pattern of
    stashing a task on `app` for on_cleanup to cancel via _stop_task.

    Skipped when settings.tts_provider != "piper": there is nothing to warm
    on the ElevenLabs path (no local model, no first-inference cost — it's a
    network call every time), so starting this thread would just burn a
    little startup CPU for no latency win.

    Warms with "Ready." rather than a bare "." on purpose: a bare period can
    phonemize to nothing and short-circuit before the actual inference path
    runs, which would pay for ONNX session init but skip the first-inference
    allocation this warm-up exists to pre-pay. A short real word forces the
    full synthesize() path Piper actually runs on a live request.
    """
    app["tts_warm_task"] = None
    try:
        settings = get_settings()
        if settings.tts_provider != "piper":
            return
        from agent.voice.piper_tts import synthesize

        model_path = settings.piper_voice_path
        if not os.path.isabs(model_path):
            model_path = os.path.join(PROJECT_ROOT, model_path)
        app["tts_warm_task"] = asyncio.create_task(
            asyncio.to_thread(synthesize, "Ready.", model_path)
        )
    except Exception as e:  # noqa: BLE001
        print(f"Piper warm-up unavailable ({e}); continuing.")


async def _stop_tts_warm(app: web.Application) -> None:
    """
    Dedicated cleanup for tts_warm_task — deliberately NOT routed through
    _stop_task (used by the three other background tasks on this app).
    _stop_task's `await task` sits inside `contextlib.suppress(asyncio.
    CancelledError)` only, which is correct for those three: they're
    run_forever() loops that never end except by cancellation. tts_warm_task
    is the one task on this app that is both finite (it completes on its own,
    seconds after startup — see _warm_tts) AND fallible (it raises
    SynthesisError whenever voices/en_US-amy-medium.onnx hasn't been
    downloaded yet, e.g. a fresh clone or a deploy that hasn't run that
    README-documented step). By the time on_cleanup runs, the task is
    typically already finished: task.cancel() on a completed task is a
    no-op, and `await task` then re-raises whatever it finished with. A
    stored SynthesisError is not a CancelledError, so _stop_task's
    suppress() would NOT catch it — it would escape runner.cleanup() and
    blow up every `systemctl stop`/restart of trillion-orb.service. The
    failure itself is already handled where it happened (_warm_tts's own
    try/except logs it); cleanup's only job is making sure it doesn't
    surface a second time here. So Exception is suppressed broadly, and
    CancelledError is suppressed alongside it (it's a BaseException, not an
    Exception, so it needs listing explicitly) in case the task was instead
    still running at shutdown and cancel() actually did something.
    """
    task = app.get("tts_warm_task")
    if task is not None:
        task.cancel()
        with contextlib.suppress(Exception, asyncio.CancelledError):
            await task


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
    await _stop_task(app, "sf_scheduler_task")
    await _stop_software_factory_background_tasks(app)


async def _start_heartbeat_scheduler(app: web.Application) -> None:
    """
    Best-effort Tier 5 heartbeat wiring, mirroring _start_factory_watcher —
    a broken heartbeat.db shouldn't stop the browser voice UI. Code Sentinel
    checks self-skip (empty list) when GitHub isn't configured, so this
    always constructs the scheduler even with zero checks registered.
    """
    app["heartbeat_task"] = None
    app["heartbeat_background_tasks"] = set()
    try:
        from agent.heartbeat.checks.code_sentinel import build_code_sentinel_checks
        from agent.heartbeat.checks.cve_scan import CveScanCheck
        from agent.heartbeat.scheduler import HeartbeatScheduler
        from agent.heartbeat.storage import HeartbeatRepo

        settings = get_settings()
        repo = HeartbeatRepo()
        cve_check = CveScanCheck()
        if repo.get_next_due_at(cve_check.name) is None:
            repo.set_next_due_at(
                cve_check.name,
                datetime.now(timezone.utc) + timedelta(seconds=cve_check.cadence_seconds),
            )
        checks = build_code_sentinel_checks(settings) + [cve_check]
        scheduler = HeartbeatScheduler(
            checks, repo, settings, background_tasks=app["heartbeat_background_tasks"]
        )
        app["heartbeat_task"] = asyncio.create_task(scheduler.run_forever())
    except Exception as e:  # noqa: BLE001
        print(f"Heartbeat unavailable ({e}); continuing.")


async def _stop_heartbeat_scheduler(app: web.Application) -> None:
    await _stop_task(app, "heartbeat_task")
    await _stop_heartbeat_background_tasks(app)


def _cancel_task_set(tasks: set) -> None:
    for task in list(tasks):
        task.cancel()


async def _wait_for_task_set(tasks: set) -> None:
    for task in list(tasks):
        with contextlib.suppress(asyncio.CancelledError):
            await task
    tasks.clear()


async def _stop_software_factory_background_tasks(app: web.Application) -> None:
    tasks = app.get("sf_background_tasks")
    if tasks:
        _cancel_task_set(tasks)
        await _wait_for_task_set(tasks)


async def _stop_heartbeat_background_tasks(app: web.Application) -> None:
    tasks = app.get("heartbeat_background_tasks")
    if tasks:
        _cancel_task_set(tasks)
        await _wait_for_task_set(tasks)


async def _stop_task(app: web.Application, key: str) -> None:
    task = app.get(key)
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
    settings = get_settings()
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
        # Voice V1 TTS: one sentence in, one audio clip out. Called once per
        # sentence as the agent's reply streams, so playback can start early.
        # Provider is a deploy-time choice (settings.tts_provider /
        # TTS_PROVIDER), not a per-request one — see docs/superpowers/specs/
        # 2026-08-17-elevenlabs-tts-provider-design.md. text is parsed first,
        # before either provider branch, since both need it and neither needs
        # the other's setup.
        try:
            data = await request.json()
        except Exception:
            data = {}
        text = (data.get("text") or "").strip()
        if not text:
            return web.Response(status=400, text="missing text")

        settings = get_settings()
        if settings.tts_provider == "elevenlabs":
            # Cloud call: I/O-bound, so it's awaited directly rather than
            # offloaded to a thread — see elevenlabs_tts.py's docstring.
            from agent.voice.elevenlabs_tts import SynthesisError, synthesize

            try:
                audio = await synthesize(
                    text,
                    settings.elevenlabs_api_key,
                    settings.elevenlabs_voice_id,
                    settings.elevenlabs_model_id,
                )
            except SynthesisError as e:
                return web.Response(status=400, text=str(e))
            return web.Response(body=audio, content_type="audio/mpeg")

        # Default (including unset or an unrecognized TTS_PROVIDER value —
        # see the comment above tts_provider in agent/config.py): Piper,
        # on-device. CPU-bound and blocking, so it's offloaded to a thread
        # rather than awaited directly on the event loop.
        from agent.voice.piper_tts import SynthesisError, synthesize

        model_path = settings.piper_voice_path
        if not os.path.isabs(model_path):
            model_path = os.path.join(PROJECT_ROOT, model_path)
        loop = asyncio.get_running_loop()
        try:
            audio = await loop.run_in_executor(None, synthesize, text, model_path)
        except SynthesisError as e:
            return web.Response(status=400, text=str(e))
        return web.Response(body=audio, content_type="audio/wav")

    async def active_agents(_request: web.Request) -> web.Response:
        # Polled by the browser (cosmic-orb-ui's sub-agent constellation,
        # index.html), mirroring /api/usage's read-only, best-effort-cached
        # shape. `working`/`dispatch_count` come from agent/factory/
        # dispatch.py's in-process DispatchActivity tracker — real signals,
        # not simulated ones. dispatch_count is what lets the browser catch
        # a dispatch that started and finished between two polls (see that
        # class's docstring): it's monotonic, so a diff against the last
        # poll's value never misses one, even though `working` alone would.
        from agent.factory.dispatch import get_dispatch_activity
        from agent.factory.storage import FactoryRepo

        rows = FactoryRepo().list_active_agents()
        activity = get_dispatch_activity()
        working = activity.snapshot()
        agents = [
            {
                "slug": r["slug"],
                "name": r["name"],
                "specialty": (r["system_prompt"] or "").strip().splitlines()[0][:140],
                "working": r["slug"] in working,
                "dispatch_count": activity.total_dispatches(r["slug"]),
            }
            for r in rows
        ]
        return web.json_response({"agents": agents})

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
        #
        # This is the one write surface exempt from *both* the bearer gate and
        # the origin gate, because the browser posts these itself and we can't
        # influence the headers it attaches. So it is also the one place where
        # an attacker fully controls what reaches a print(): unbounded, that is
        # a journald flood and a log-injection vector. Read a bounded number of
        # bytes and log a single truncated line.
        raw = await request.content.read(CSP_REPORT_MAX_BYTES + 1)
        truncated = len(raw) > CSP_REPORT_MAX_BYTES
        try:
            body = raw[:CSP_REPORT_MAX_BYTES].decode("utf-8", "replace")
        except Exception:
            body = ""
        # Collapse newlines so a crafted report can't forge extra log lines.
        line = " ".join(body.split())[:CSP_REPORT_MAX_BYTES]
        print(f"[csp-violation] {line}{' …[truncated]' if truncated else ''}")
        return web.Response(status=204)

    # Middlewares wrap in reverse list order, so the first entry is outermost.
    # Headers go outside everything, which is what puts them on 403s and 401s
    # too. The origin gate sits outside the bearer gate deliberately: a forged
    # cross-origin request should be refused on that basis whether or not it
    # also carried a token, and the browser-attested evidence is cheaper to
    # check than a constant-time token compare.
    app = web.Application(
        middlewares=[
            security_headers_middleware,
            origin_check_middleware(settings.web_host),
            bearer_auth_middleware(settings.web_auth_token),
        ]
    )
    app.router.add_get("/api/usage", usage)
    app.router.add_get("/api/agents", active_agents)
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
    app.on_startup.append(_start_cost_tracking)
    app.on_startup.append(_start_factory_watcher)
    app.on_startup.append(_build_notes_index)
    app.on_startup.append(_start_software_factory)
    app.on_startup.append(_start_heartbeat_scheduler)
    app.on_startup.append(_warm_tts)
    app.on_cleanup.append(_stop_factory_watcher)
    app.on_cleanup.append(_stop_software_factory)
    app.on_cleanup.append(_stop_heartbeat_scheduler)
    app.on_cleanup.append(_stop_tts_warm)
    return app


def _can_bind(host: str, port: int) -> bool:
    with contextlib.closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as probe:
        try:
            probe.bind((host, port))
        except OSError:
            return False
    return True


def _select_web_port(
    host: str,
    preferred_port: int,
    *,
    strict: bool = False,
    search_limit: int = 10,
) -> int:
    if preferred_port == 0 or _can_bind(host, preferred_port):
        return preferred_port

    if strict:
        next_port = preferred_port + 1
        raise SystemExit(
            f"Port {preferred_port} is already in use on {host}. "
            f"Stop the other server or run TRILLION_WEB_PORT={next_port} trillion serve."
        )

    for port in range(preferred_port + 1, preferred_port + search_limit + 1):
        if _can_bind(host, port):
            print(f"Port {preferred_port} is busy on {host}; starting on {port} instead.")
            return port

    raise SystemExit(
        f"No free web port found on {host} from {preferred_port} "
        f"through {preferred_port + search_limit}."
    )


def main() -> None:
    from agent.security.startup_guard import check_bind_safety

    raw_port = os.getenv("TRILLION_WEB_PORT")
    port = int(raw_port or "8123")
    strict_port = os.getenv("TRILLION_WEB_STRICT_PORT", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    settings = get_settings()
    check_bind_safety(settings.web_host, auth_configured=bool(settings.web_auth_token))
    port = _select_web_port(settings.web_host, port, strict=strict_port)
    web.run_app(build_app(), host=settings.web_host, port=port)


if __name__ == "__main__":
    main()
