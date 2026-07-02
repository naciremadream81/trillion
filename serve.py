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

import os

from aiohttp import web
from dotenv import load_dotenv

from agent.cost.aggregate import UsageDashboard
from agent.cost.storage import UsageRepo

# Load .env so the web server honors the same config as the CLI agent
# (TRILLION_MONTHLY_BUDGET_USD, TRILLION_USAGE_DB, TRILLION_WEB_PORT).
load_dotenv()

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))

# A single shared agent for the browser voice UI (personal, single-user).
# Built lazily so importing serve.py doesn't require the provider SDKs.
_agent = None


def _get_agent():
    global _agent
    if _agent is None:
        from agent.core import Agent
        from agent.config import get_settings
        from agent.providers import get_provider
        from agent.tools.registry import build_registry
        from agent.cost.recorder import set_usage_repo
        from agent.cost.storage import UsageRepo

        set_usage_repo(UsageRepo())  # so browser turns show up in the cost dashboard
        provider = get_provider(os.getenv("TRILLION_PROVIDER", "claude"))
        registry = build_registry(get_settings())
        _agent = Agent(provider=provider, tool_registry=registry)
    return _agent


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
    return app


def main() -> None:
    port = int(os.getenv("TRILLION_WEB_PORT", "8123"))
    web.run_app(build_app(), host="127.0.0.1", port=port)


if __name__ == "__main__":
    main()
