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
import json
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
from agent.security.csp_reports import CspReportRepo, record_report
from agent.security.headers import security_headers_middleware_factory
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

        settings = get_settings()
        _registry = build_registry(settings)

        # playbook/cloud-to-local.md Tier 3. Registered AFTER build_registry,
        # and only for names it did not produce — on the machine that has the
        # real tool every name is already taken and this registers nothing.
        # That absence check IS the no-double-fire guarantee: which process
        # holds which tool, not a flag anyone can set wrong.
        if getattr(settings, "remote_proxy_enabled", False):
            try:
                from agent.remote.proxy import register_proxies
                from agent.remote.storage import RemoteQueue

                added = register_proxies(
                    _registry, RemoteQueue(), settings.remote_worker_role
                )
                if added:
                    print(f"Remote dispatch proxies registered: {', '.join(added)}")
            except Exception as e:  # noqa: BLE001
                print(f"Remote proxies unavailable ({e}); continuing.")
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
        # Tier 5 handoffs: specialists park proposals here. Best-effort — if
        # safety.db is unreachable the watcher still runs, just without
        # propose_handoff in any specialist's registry.
        try:
            from agent.safety.storage import SafetyRepo

            handoff_safety_repo = SafetyRepo()
        except Exception:
            handoff_safety_repo = None
        watcher = RegistryWatcher(
            repo, _get_provider(), _get_registry(), safety_repo=handoff_safety_repo
        )
        await watcher.sync_once()
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


def _piper_model_path() -> str:
    """
    The configured Piper voice model, resolved against the project root so a
    relative default (voices/en_US-amy-medium.onnx) works regardless of the
    server's working directory. Shared by the /api/tts handler and the
    startup warm-up so the two can never warm one path and synthesize from
    another.
    """
    model_path = get_settings().piper_voice_path
    if not os.path.isabs(model_path):
        model_path = os.path.join(PROJECT_ROOT, model_path)
    return model_path


async def _warm_piper_voice(app: web.Application) -> None:
    """
    Load Piper's ~63MB voice model at boot instead of on the first spoken
    reply (smooth-voice Tier 4).

    Measured cold, that load plus its first inference cost ~4s — the single
    largest number in the Tier 1 latency breakdown, and one every voice turn
    after a restart used to pay. trillion-orb.service starts on boot, so in
    practice it landed on the first thing Sean said each day.

    Scheduled as a background task rather than awaited, deliberately: this
    blocks a CPU core for seconds, and text chat, the cost dashboard, and
    the UI have no reason to wait behind it. A missing model logs and stays
    cold — /api/tts still returns its own clear error, exactly as before.
    """
    app["piper_warmup_task"] = None
    try:
        # Nothing to warm when Piper isn't the configured provider: with
        # TTS_PROVIDER=elevenlabs, /api/tts never touches the ONNX model, so
        # loading 63MB and burning a core for seconds at every boot would buy
        # exactly nothing. (This guard postdates the warm-up itself —
        # ElevenLabs became selectable later; see agent/config.py.)
        if get_settings().tts_provider == "elevenlabs":
            return

        from agent.voice.piper_tts import warm_up

        model_path = _piper_model_path()

        async def _warm() -> None:
            try:
                await asyncio.to_thread(warm_up, model_path)
                print("Piper voice model warm.")
            except Exception as e:  # noqa: BLE001
                print(f"Piper warm-up skipped — {e} First spoken reply will be slower.")

        app["piper_warmup_task"] = asyncio.create_task(_warm())
    except Exception as e:  # noqa: BLE001
        print(f"Piper warm-up unavailable ({e}); continuing.")


async def _stop_piper_warmup(app: web.Application) -> None:
    await _stop_task(app, "piper_warmup_task")


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
        from agent.heartbeat.checks.mining import build_mining_checks

        checks = (
            build_code_sentinel_checks(settings)
            + build_mining_checks(settings)  # empty unless TRILLION_MINING_WALLET is set
            + [cve_check]
            + _build_board_review_checks(settings)
            + _build_remote_completion_checks(settings)
            + _build_revenue_checks(settings)
        )
        scheduler = HeartbeatScheduler(
            checks, repo, settings, background_tasks=app["heartbeat_background_tasks"]
        )
        app["heartbeat_task"] = asyncio.create_task(scheduler.run_forever())
    except Exception as e:  # noqa: BLE001
        print(f"Heartbeat unavailable ({e}); continuing.")


def _build_revenue_checks(settings) -> list:
    """The payment poll — off entirely without a Stripe key, which is the
    same posture every other integration here takes."""
    api_key = os.getenv("STRIPE_API_KEY", "")
    if not api_key:
        return []
    try:
        from agent.heartbeat.checks.revenue import RevenuePollCheck

        return [RevenuePollCheck(api_key)]
    except Exception as e:  # noqa: BLE001
        print(f"Revenue polling unavailable ({e}); continuing.")
        return []


def _build_remote_completion_checks(settings) -> list:
    """
    Tier 6's ping. Registered on the PROXY side — the machine that asked for
    the work is the one that wants telling when it finished. On the worker
    machine the local UI already showed the whole run (Tier 5), so a notice
    there would be announcing something Sean just watched happen.
    """
    if not getattr(settings, "remote_proxy_enabled", False):
        return []
    try:
        from agent.heartbeat.checks.remote_completions import RemoteCompletionCheck
        from agent.remote.storage import RemoteQueue

        return [RemoteCompletionCheck(RemoteQueue(), settings.remote_worker_role)]
    except Exception as e:  # noqa: BLE001
        print(f"Remote completion pings unavailable ({e}); continuing.")
        return []


def _build_board_review_checks(settings) -> list:
    """
    The board's monthly standing review — playbook/the-board.md Tier 7.

    Registered HERE and nowhere else, deliberately. Trillion runs in two
    processes (main.py's terminal chat and this server) and a scheduled job
    needs the surface that is actually awake at 8am, not the one that's
    asleep. Registering it in both would convene twice — four paid model
    calls each — on any day both happen to be running.

    Three gates, all of which must pass, because this spends money without
    being asked: the feature is on, the review is on, and there is a roster.
    """
    if not (getattr(settings, "board_enabled", False)
            and getattr(settings, "board_standing_review", False)):
        return []
    try:
        from agent.board.ask import make_ask_model
        from agent.board.convene import load_seats
        from agent.heartbeat.checks.board_review import BoardStandingReviewCheck

        if not load_seats():
            print("Board standing review is on but no seats are configured; skipping it.")
            return []
        return [BoardStandingReviewCheck(
            make_ask_model(_get_provider()),
            brief_provider=_business_brief,
            ceiling_usd=settings.board_meeting_ceiling_usd,
        )]
    except Exception as e:  # noqa: BLE001 — never let this take down the heartbeat
        print(f"Board standing review unavailable ({e}); continuing.")
        return []


def _business_brief() -> str:
    """
    The live figures the chair reads — and the seats see a short slice of.

    Read fresh on every meeting, never from a file. A board that argues about
    the business without seeing the business is a party trick, and a number
    written down somewhere is a number that was true once.

    Best-effort per source: a board reasoning from three of four figures is
    far better than no meeting at all, so a broken source is omitted rather
    than fatal.
    """
    settings = get_settings()
    lines = []

    def source(label, fn):
        """Run one source. A failure omits its line and SAYS SO — silently
        swallowing it would leave the chair permanently half-blind with
        nothing anywhere to show why."""
        try:
            value = fn()
        except Exception as e:  # noqa: BLE001
            print(f"Board brief: {label} unavailable ({type(e).__name__}: {e})")
            return
        if value:
            lines.append(value)

    def spend():
        from agent.cost.aggregate import UsageDashboard
        from agent.cost.storage import UsageRepo

        payload = UsageDashboard(UsageRepo()).payload()
        return f"API spend month-to-date: ${float(payload.get('month_to_date_usd', 0)):.2f}"

    def factory():
        from agent.factory.software.storage import BuildRepo

        repo = BuildRepo()
        parts = [
            f"Software factory: {len(repo.list_recent_builds(limit=10))} recent builds, "
            f"{repo.count_builds_today()} today"
        ]
        repeats = repo.repeat_sightings(5)
        if repeats:
            parts.append(
                "Problems the scout has seen more than once: "
                + "; ".join(f"{r['problem']} ({r['times_seen']}x)" for r in repeats)
            )
        return "\n".join(parts)

    def mining():
        if not settings.mining_wallet:
            return ""
        from agent.mining.storage import MiningRepo

        summary = MiningRepo().summary(settings.mining_wallet).to_dict()
        # The payout address is deliberately not included — see
        # agent/tools/mining.py. It is Sean's financial identity and the
        # board has no use for it.
        return (
            f"Mining: {summary.get('workers_online', 0)} workers online, "
            f"{summary.get('workers_offline', 0)} offline"
        )

    source("API spend", spend)
    source("software factory", factory)
    source("mining", mining)
    return "\n".join(lines)


async def _start_remote_worker(app: web.Application) -> None:
    """
    Drain the cross-machine queue — playbook/cloud-to-local.md Tiers 1, 2, 5.

    Runs in THIS process on purpose. The worker invokes the real local tool
    instance out of the shared registry, so a remotely-dispatched run emits
    exactly the same lifecycle events a locally-initiated one does and the
    local UI lights up natively, with no new UI code (Tier 5). A separate
    worker process would need those events bridged back.
    """
    app["remote_worker_task"] = None
    settings = get_settings()
    if not getattr(settings, "remote_worker_enabled", False):
        return
    try:
        from agent.remote.runners import build_deps, build_runners
        from agent.remote.storage import RemoteQueue
        from agent.remote.worker import RemoteWorker

        registry = _get_registry()
        worker = RemoteWorker(
            RemoteQueue(),
            worker_role=settings.remote_worker_role,
            runners=build_runners(),
            deps=build_deps(settings, registry),
        )
        app["remote_worker_task"] = asyncio.create_task(worker.run_forever())
    except Exception as e:  # noqa: BLE001
        print(f"Remote worker unavailable ({e}); continuing.")


async def _stop_remote_worker(app: web.Application) -> None:
    await _stop_task(app, "remote_worker_task")


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
        # no-store on the shell — playbook/mobile-pwa.md §7. iOS PWA cache is
        # sticky enough that without this an installed home-screen app can
        # keep serving a shell from a previous deploy for days, with no
        # reload gesture available in a standalone window to escape it.
        return web.FileResponse(
            os.path.join(PROJECT_ROOT, "index.html"),
            headers={"Cache-Control": "no-store"},
        )

    async def service_worker(_request: web.Request) -> web.FileResponse:
        # Served from the root so its scope covers the whole origin — a
        # worker under /static/ could only control /static/. It caches
        # nothing (see sw.js); no-store here keeps the worker script itself
        # from becoming the stale thing.
        return web.FileResponse(
            os.path.join(PROJECT_ROOT, "sw.js"),
            headers={
                "Cache-Control": "no-store",
                "Content-Type": "application/javascript; charset=utf-8",
            },
        )

    async def web_manifest(_request: web.Request) -> web.FileResponse:
        return web.FileResponse(
            os.path.join(PROJECT_ROOT, "static", "manifest.json"),
            headers={"Content-Type": "application/manifest+json"},
        )

    def _client_gone(request: web.Request) -> bool:
        """
        Whether the browser has dropped this request.

        smooth-voice Tier 6 / README's "not done yet": an aborted /api/chat
        used to keep generating until a write happened to hit the dropped
        connection. aiohttp buffers, so that can be the whole rest of the
        reply — every token of it billed, for a turn nobody will ever read.
        Barge-in aborts the fetch on the client (index.html's turnAbort), so
        this is not a rare path; it fires every time Sean talks over Trillion.

        transport.is_closing() is the earliest honest signal available here.
        A write raising ConnectionResetError is the backstop, not the primary
        check — by the time it raises, the buffer has already drained.
        """
        transport = request.transport
        return transport is None or transport.is_closing()

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
            turn = None
            try:
                agent = _get_agent(session_id)
                # Recheck after the per-Agent lock is acquired (inside
                # turn()), not only on each yielded chunk: a rapid second
                # barge-in aborts this POST while it is still queued, and a
                # tool-only first round yields nothing for _client_gone to
                # see. Starting that abandoned utterance would still run
                # LOW-risk tools (remember_fact, dispatch) and bill a
                # provider call the user already talked over.
                turn = agent.turn(
                    message, should_abort=lambda: _client_gone(request)
                )
                async for piece in turn:
                    if _client_gone(request):
                        # Stop generating — and stop paying — for a reply
                        # nobody is listening to. Breaking here closes the
                        # generator in the finally below, which unwinds into
                        # the provider stream and ends the API call.
                        break
                    try:
                        await resp.write(piece.encode("utf-8"))
                    except (ConnectionResetError, ConnectionAbortedError):
                        break
            except Exception as e:  # surface the real error to the client
                try:
                    await resp.write(f"\n[agent error: {type(e).__name__}: {e}]".encode("utf-8"))
                except (ConnectionResetError, ConnectionAbortedError):
                    pass
            finally:
                # Explicit rather than left to garbage collection: the point
                # is to end the upstream API call *now*, and GC timing is not
                # a billing policy.
                if turn is not None:
                    await turn.aclose()
        try:
            await resp.write_eof()
        except (ConnectionResetError, ConnectionAbortedError):
            pass
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

    async def transcribe_stream(request: web.Request) -> web.WebSocketResponse:
        """
        Streaming STT relay (smooth-voice Tier 2).

        The browser sends audio chunks as MediaRecorder produces them and
        gets back normalized events — interim transcripts, and crucially the
        recognizer's own end-of-utterance signal, which is what lets
        hands-free stop guessing from microphone energy.

        A relay rather than a direct browser->Deepgram connection because
        Deepgram authenticates with the API key: a direct connection would
        put a paid credential in page JavaScript. The key never leaves this
        process.

        Both directions are pumped concurrently — audio keeps arriving while
        transcripts come back, so a sequential loop would deadlock the moment
        the speaker said two things.
        """
        from agent.voice.deepgram_stream import DeepgramStream, StreamingTranscriptionError

        ws = web.WebSocketResponse(heartbeat=20)
        await ws.prepare(request)

        settings = get_settings()
        try:
            stream_cm = DeepgramStream(settings.deepgram_api_key)
        except StreamingTranscriptionError as e:
            await ws.send_json({"type": "error", "message": str(e)})
            await ws.close()
            return ws

        try:
            async with stream_cm as stream:

                async def pump_downstream() -> None:
                    # Deepgram -> browser.
                    async for event in stream:
                        if ws.closed:
                            return
                        await ws.send_json(event)

                downstream = asyncio.create_task(pump_downstream())
                try:
                    # Browser -> Deepgram.
                    async for message in ws:
                        if message.type == web.WSMsgType.BINARY:
                            await stream.send_audio(message.data)
                        elif message.type == web.WSMsgType.TEXT:
                            # The browser's only verb. Anything else is
                            # ignored rather than parsed: this socket carries
                            # audio, and giving it a command vocabulary is
                            # attack surface for no gain.
                            if message.data.strip() == "close":
                                await stream.finish()
                                # STOP READING and go wait for the flush.
                                # Without this break the loop keeps waiting on
                                # a browser that has already said everything
                                # it intends to, so the flushed tail is only
                                # collected once the *browser* hangs up — and
                                # a browser that hangs up promptly loses the
                                # last segment of the utterance, which is the
                                # one word the turn usually hinges on. The
                                # browser keeps its socket open to receive;
                                # we close it below once Deepgram is done.
                                break
                        elif message.type == web.WSMsgType.ERROR:
                            break
                    else:
                        # The browser hung up without saying "close" — flush
                        # anyway so a held final segment isn't stranded.
                        await stream.finish()
                    # Give Deepgram a moment to emit the flushed tail rather
                    # than tearing the socket down the instant audio stops.
                    try:
                        await asyncio.wait_for(downstream, timeout=5.0)
                    except asyncio.TimeoutError:
                        pass
                finally:
                    downstream.cancel()
        except StreamingTranscriptionError as e:
            if not ws.closed:
                await ws.send_json({"type": "error", "message": str(e)})
        except Exception as e:  # noqa: BLE001 — never let a socket crash the server
            if not ws.closed:
                await ws.send_json(
                    {"type": "error", "message": f"{type(e).__name__}: {e}"}
                )
        finally:
            if not ws.closed:
                await ws.close()
        return ws

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

        model_path = _piper_model_path()
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

    async def design_preview(request: web.Request) -> web.StreamResponse:
        """
        Serve a project's exported mockups — playbook/design-subagent.md
        Tier 2's serving endpoint.

        generate_mockup hands back URLs under this prefix, and the exported
        Next.js app emits its own asset URLs under it too (that is what
        basePath + assetPrefix are for). Without this route every advertised
        preview and every script it loads returns 404.

        Three shapes map onto one handler because they are all "a file inside
        this project's out/ directory":
            /api/design/<project>/preview/                     -> out/index.html
            /api/design/<project>/preview/_next/<path>          -> out/_next/<path>
            /api/design/<project>/preview/<feature>/<screen>/   -> out/<feature>/<screen>/index.html

        Containment is the whole job: `tail` is attacker-shaped in principle,
        so it goes through the same jail as everything else and a directory
        request is resolved to index.html rather than listed.
        """
        from agent.design.docs import DesignDocError, assert_within_project, resolve_project_root

        settings = get_settings()
        if not settings.design_agent_enabled:
            return web.json_response({"error": "design agent is not enabled"}, status=404)

        project = request.match_info.get("project", "")
        tail = request.match_info.get("tail", "") or ""
        try:
            project_root = resolve_project_root(project, settings)
            out_root = assert_within_project(project_root, os.path.join(".prism", "preview", "out"))
            # A trailing-slash route (which is what trailingSlash: true makes
            # Next emit) resolves to that directory's index.html.
            relative = tail if tail and not tail.endswith("/") else os.path.join(tail, "index.html")
            target = assert_within_project(out_root, relative.lstrip("/"))
        except DesignDocError:
            return web.json_response({"error": "not found"}, status=404)

        if os.path.isdir(target):
            target = os.path.join(target, "index.html")
        if not os.path.isfile(target):
            return web.json_response(
                {"error": "no such preview — has this screen been generated?"}, status=404
            )
        return web.FileResponse(target)

    async def mining_status(_request: web.Request) -> web.Response:
        # Read-only view of the last recorded poll. Like query_mining, this
        # never calls the pool — the heartbeat owns that cadence, and a
        # browser poll that fetched would add an unthrottled second caller.
        settings = get_settings()
        if not settings.mining_wallet:
            return web.json_response({"configured": False})
        try:
            from agent.mining.storage import MiningRepo

            summary = MiningRepo().summary(settings.mining_wallet).to_dict()
            # The address is Sean's financial identity and the browser has no
            # use for it — the widget shows hashrate and payouts, not which
            # address they belong to.
            summary.pop("address", None)
            summary["configured"] = True
            return web.json_response(summary)
        except Exception as e:  # noqa: BLE001
            return web.json_response({"error": f"{type(e).__name__}: {e}"}, status=500)

    async def pending_handoffs(_request: web.Request) -> web.Response:
        # orchestration.md Tier 5, read side: proposals waiting on Sean.
        # These are ordinary pending_actions rows whose tool is a dispatch —
        # surfaced next to the heartbeat notices so an offer waiting for an
        # answer is something he glances at, not something buried in a
        # transcript he has scrolled past.
        try:
            from agent.factory.dispatch import DISPATCH_PREFIX
            from agent.safety.storage import SafetyRepo

            repo = SafetyRepo()
            repo.expire_stale()
            rows = [
                {
                    "id": row["id"],
                    "target": row["tool_name"][len(DISPATCH_PREFIX):],
                    "summary": row["summary"],
                    "task": (row["arguments"] or {}).get("message", ""),
                    "expires_at": row["expires_at"],
                }
                for row in repo.list_pending()
                if row["tool_name"].startswith(DISPATCH_PREFIX)
            ]
            return web.json_response({"handoffs": rows})
        except Exception as e:  # noqa: BLE001
            return web.json_response({"error": f"{type(e).__name__}: {e}"}, status=500)

    # ── Revenue celebration (money-celebration.md Phase 2) ───────────────────

    async def revenue_catchup(_request: web.Request) -> web.Response:
        """
        What this screen should celebrate right now.

        Called on every connect and reconnect, which is the whole fix for
        the "nobody was watching" gap: a payment that landed with the tab
        closed is still uncelebrated, so it finally gets its moment when a
        screen next opens. `withheld` is how many are being left in history
        rather than replayed, so a burst is never a silent truncation.
        """
        try:
            from agent.revenue.storage import RevenueRepo

            return web.json_response(RevenueRepo().read_catchup())
        except Exception as e:  # noqa: BLE001
            return web.json_response({"error": f"{type(e).__name__}: {e}"}, status=500)

    async def revenue_celebrated(request: web.Request) -> web.Response:
        """
        Mark payments as shown.

        Both the live path and the catch-up path post here, which is what
        keeps them in agreement — a payment celebrated live is not replayed
        on the next reload because the live path recorded it too.
        """
        try:
            body = await request.json()
            ids = body.get("charge_ids")
            if not isinstance(ids, list):
                return web.json_response({"error": "expected charge_ids: []"}, status=400)

            from agent.revenue.storage import RevenueRepo

            return web.json_response({"marked": RevenueRepo().mark_celebrated(ids)})
        except json.JSONDecodeError:
            return web.json_response({"error": "invalid JSON body"}, status=400)
        except Exception as e:  # noqa: BLE001
            return web.json_response({"error": f"{type(e).__name__}: {e}"}, status=500)

    async def revenue_test(request: web.Request) -> web.Response:
        """
        Fire a test celebration at a given amount.

        The playbook calls a manual trigger invaluable and it is right: every
        tier of the animation, the sound, and the orb reaction is otherwise
        only testable by taking real money. Writes a real (test-marked) row
        so it travels the exact same catch-up path a Stripe charge does —
        a trigger that bypasses the pipeline tests nothing about it.
        """
        try:
            body = await request.json() if request.can_read_body else {}
            amount = int(body.get("amount_minor", 130000))

            from agent.revenue.storage import RevenueRepo

            charge_id = f"test_{uuid.uuid4().hex[:16]}"
            RevenueRepo().record_payment(
                charge_id=charge_id, amount_minor=amount,
                currency=str(body.get("currency", "usd")),
                customer_label="Test celebration",
            )
            return web.json_response({"charge_id": charge_id, "amount_minor": amount})
        except Exception as e:  # noqa: BLE001
            return web.json_response({"error": f"{type(e).__name__}: {e}"}, status=500)

    async def board_meetings(_request: web.Request) -> web.Response:
        """
        Stored meetings, newest first — playbook/the-board.md Tier 6.

        A read surface, not a new channel: a meeting Sean asked for already
        reaches him through the conversation, and the standing review reaches
        him as a heartbeat notice. This exists so a meeting from three weeks
        ago is still readable, and it renders from the citations snapshotted
        onto the meeting rather than from a dossier that may have changed.
        """
        try:
            from agent.board.storage import BoardRepo

            return web.json_response({"meetings": BoardRepo().recent_meetings(20)})
        except Exception as e:  # noqa: BLE001
            return web.json_response({"error": f"{type(e).__name__}: {e}"}, status=500)

    # ── Scout doctrine (opportunity-scout.md Tiers 5 and 7) ──────────────────

    def _scout_repo_and_overrides():
        """A BuildRepo plus a document cache over it.

        Built per request rather than held on the app: these are cheap SQLite
        handles, and a request that builds its own cannot serve another
        request's stale snapshot after an edit."""
        from agent.factory.software.doctrine import DocumentOverrides
        from agent.factory.software.storage import BuildRepo

        repo = BuildRepo()
        return repo, DocumentOverrides(repo)

    async def scout_documents(_request: web.Request) -> web.Response:
        """Every editable scout document, with its effective and default text."""
        try:
            from agent.factory.software.doctrine import active_lanes, all_document_states

            repo, overrides = _scout_repo_and_overrides()
            documents = all_document_states(overrides)
            return web.json_response({
                "documents": documents,
                # The broad question — "does the scout have ANY override?" —
                # answered separately from each document's own is_overridden,
                # because the two drive different controls.
                "any_overridden": any(d["is_overridden"] for d in documents),
                "lanes": [
                    {"label": lane.label, "title": lane.title} for lane in active_lanes(overrides)
                ],
                "repeats": repo.repeat_sightings(10),
            })
        except Exception as e:  # noqa: BLE001
            return web.json_response({"error": f"{type(e).__name__}: {e}"}, status=500)

    async def save_scout_documents(request: web.Request) -> web.Response:
        """
        Save one or more scout document overrides.

        Validates EVERY document before writing ANY of them. A save that
        writes the first textarea and then rejects the second leaves the
        operator with no idea which half landed.
        """
        try:
            from agent.factory.software.doctrine import find_editable, validate_document

            body = await request.json()
            updates = body.get("documents")
            if not isinstance(updates, dict) or not updates:
                return web.json_response({"error": "expected a non-empty 'documents' object"}, status=400)

            # Pass 1 — resolve and validate. Routing is by the registry, not
            # by the key the caller sent, so an unknown key is refused rather
            # than written into the same table under a name nothing reads.
            resolved = []
            errors = {}
            for key, text in updates.items():
                document = find_editable(key)
                if document is None:
                    errors[key] = "not an editable document"
                    continue
                if not isinstance(text, str):
                    errors[key] = "expected a string"
                    continue
                problem = validate_document(key, text)
                if problem:
                    errors[key] = problem
                    continue
                resolved.append((document, text))
            if errors:
                return web.json_response({"error": "validation failed", "errors": errors}, status=400)

            # Pass 2 — write.
            repo, overrides = _scout_repo_and_overrides()
            for document, text in resolved:
                repo.set_document(document.key, text)
            # The writing process shouldn't have to wait out its own TTL.
            overrides.refresh()
            return web.json_response({"saved": [d.key for d, _ in resolved]})
        except json.JSONDecodeError:
            return web.json_response({"error": "invalid JSON body"}, status=400)
        except Exception as e:  # noqa: BLE001
            return web.json_response({"error": f"{type(e).__name__}: {e}"}, status=500)

    async def revert_scout_document(request: web.Request) -> web.Response:
        """Drop one document's override so the shipped file default returns."""
        try:
            from agent.factory.software.doctrine import find_editable

            body = await request.json()
            document = find_editable(str(body.get("key", "")))
            if document is None:
                return web.json_response({"error": "not an editable document"}, status=400)

            repo, overrides = _scout_repo_and_overrides()
            repo.delete_document(document.key)
            overrides.refresh()
            return web.json_response({"reverted": document.key})
        except json.JSONDecodeError:
            return web.json_response({"error": "invalid JSON body"}, status=400)
        except Exception as e:  # noqa: BLE001
            return web.json_response({"error": f"{type(e).__name__}: {e}"}, status=500)

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
        # ...and persist it. The print above goes to stdout, which on a
        # systemd unit with no persistent journal (this host) is /dev/null —
        # so for the entire life of this endpoint the collection step of
        # agent-security.md §2.2 produced nothing to read. record_report is
        # best-effort and never raises: this route is unauthenticated, and a
        # storage error must not become a 500 an attacker can trigger.
        record_report(line)
        return web.Response(status=204)

    async def csp_violations(request: web.Request) -> web.Response:
        # The read side of the above — "what actually got blocked", grouped
        # by directive and source. This is the evidence you widen the policy
        # from before setting TRILLION_CSP_ENFORCE; see
        # agent/security/headers.py for why the order matters.
        try:
            limit = min(200, max(1, int(request.query.get("limit", "50"))))
        except ValueError:
            limit = 50
        try:
            repo = CspReportRepo()
            return web.json_response(
                {
                    "enforcing": get_settings().csp_enforce,
                    "total": repo.count(),
                    "directives": repo.directive_summary(limit),
                }
            )
        except Exception as e:
            return web.json_response({"error": f"{type(e).__name__}: {e}"}, status=500)

    # Middlewares wrap in reverse list order, so the first entry is outermost.
    # Headers go outside everything, which is what puts them on 403s and 401s
    # too. The origin gate sits outside the bearer gate deliberately: a forged
    # cross-origin request should be refused on that basis whether or not it
    # also carried a token, and the browser-attested evidence is cheaper to
    # check than a constant-time token compare.
    app = web.Application(
        middlewares=[
            security_headers_middleware_factory(settings.csp_enforce),
            origin_check_middleware(settings.web_host),
            bearer_auth_middleware(settings.web_auth_token, settings.web_auth_token_prev),
        ]
    )
    app.router.add_get("/api/usage", usage)
    app.router.add_get("/api/agents", active_agents)
    app.router.add_post("/api/chat", chat)
    app.router.add_post("/api/transcribe", transcribe_audio)
    app.router.add_get("/api/transcribe/stream", transcribe_stream)
    app.router.add_post("/api/tts", synthesize_speech)
    app.router.add_get("/api/design/{project}/preview/", design_preview)
    app.router.add_get("/api/design/{project}/preview/{tail:.*}", design_preview)
    app.router.add_get("/api/mining", mining_status)
    app.router.add_get("/api/handoffs", pending_handoffs)
    app.router.add_get("/api/revenue/catchup", revenue_catchup)
    app.router.add_post("/api/revenue/celebrated", revenue_celebrated)
    app.router.add_post("/api/revenue/test", revenue_test)
    app.router.add_get("/api/board/meetings", board_meetings)
    app.router.add_get("/api/scout/documents", scout_documents)
    app.router.add_post("/api/scout/documents", save_scout_documents)
    app.router.add_post("/api/scout/documents/revert", revert_scout_document)
    app.router.add_get("/api/heartbeat/notices", heartbeat_notices)
    app.router.add_post("/api/heartbeat/dismiss", dismiss_notice)
    app.router.add_post("/api/security/csp-report", csp_report)
    app.router.add_get("/api/security/csp-violations", csp_violations)
    app.router.add_get("/api/security/cve-status", cve_status)
    app.router.add_post("/api/security/cve-scan", cve_scan)
    app.router.add_get("/api/security/status", security_status)
    app.router.add_get("/", index)
    app.router.add_get("/index.html", index)
    # Vendored Three.js (see vendor/three/) — served locally instead of the
    # unpkg CDN so the UI works offline and P7's CSP doesn't need a
    # third-party script-src origin.
    # PWA surface (playbook/mobile-pwa.md). All three are public for the same
    # reason index.html is: they are markup and icons, not a capability.
    app.router.add_get("/sw.js", service_worker)
    app.router.add_get("/manifest.json", web_manifest)
    app.router.add_static("/static/", os.path.join(PROJECT_ROOT, "static"))
    app.router.add_static("/vendor/", os.path.join(PROJECT_ROOT, "vendor"))
    app.on_startup.append(_start_cost_tracking)
    app.on_startup.append(_start_factory_watcher)
    app.on_startup.append(_build_notes_index)
    app.on_startup.append(_start_software_factory)
    app.on_startup.append(_start_remote_worker)
    app.on_startup.append(_start_heartbeat_scheduler)
    app.on_startup.append(_warm_piper_voice)
    app.on_cleanup.append(_stop_factory_watcher)
    app.on_cleanup.append(_stop_software_factory)
    app.on_cleanup.append(_stop_heartbeat_scheduler)
    app.on_cleanup.append(_stop_remote_worker)
    app.on_cleanup.append(_stop_piper_warmup)
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
