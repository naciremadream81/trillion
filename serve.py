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

from aiohttp import web
from dotenv import load_dotenv

from agent.cost.aggregate import UsageDashboard
from agent.cost.storage import UsageRepo

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


def _get_agent():
    global _agent
    if _agent is None:
        from agent.core import Agent
        from agent.cost.recorder import set_usage_repo
        from agent.cost.storage import UsageRepo

        set_usage_repo(UsageRepo())  # so browser turns show up in the cost dashboard
        _agent = Agent(provider=_get_provider(), tool_registry=_get_registry())
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


async def _stop_factory_watcher(app: web.Application) -> None:
    task = app.get("factory_watcher_task")
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

    app = web.Application()
    app.router.add_get("/api/usage", usage)
    app.router.add_post("/api/chat", chat)
    app.router.add_get("/", index)
    app.router.add_get("/index.html", index)
    app.on_startup.append(_start_factory_watcher)
    app.on_cleanup.append(_stop_factory_watcher)
    return app


def main() -> None:
    port = int(os.getenv("TRILLION_WEB_PORT", "8123"))
    web.run_app(build_app(), host="127.0.0.1", port=port)


if __name__ == "__main__":
    main()
