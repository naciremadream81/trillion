"""
Autonomous scheduler: on an interval, proposes a project brief constrained to
Sean's own configured themes (TRILLION_FACTORY_AUTONOMOUS_THEMES) and starts a
build via the exact same start_build() the /build command uses — no separate
code path for self-initiated vs. requested builds past the one proposal step.

Same run_forever()/poll-and-reconcile idiom as
agent/factory/dispatch.py's RegistryWatcher: main.py and serve.py are
separate processes with no shared memory, so periodic ticking is the
simplest correct option, not a hot-reload/event system.
"""

from __future__ import annotations

import asyncio
import logging

from ...core import Agent
from .pipeline import BudgetCapExceeded, BuildCapExceeded, FactoryPaused, start_build

logger = logging.getLogger(__name__)


def _proposal_system_prompt(themes: list[str]) -> str:
    return (
        "You are the Trillion Software Factory's autonomous project scout. "
        "Sean has authorized self-initiated builds within these themes only: "
        f"{', '.join(themes)}. Propose exactly one small, concretely scoped "
        "software project that fits within one of these themes. Reply with "
        "ONLY a one-to-three sentence project brief, no preamble, no options "
        "list — a single project."
    )


class AutonomousScheduler:
    """
    Ticks on settings.factory_autonomous_interval_hours. Each tick: checks the
    kill switch and both hard caps first (no LLM call if either is already
    blocking), proposes one project brief via a single LLM call constrained
    to factory_autonomous_themes, then calls start_build() with it.
    """

    def __init__(self, repo, provider, settings, *, background_tasks: set, usage_repo=None) -> None:
        self.repo = repo
        self.provider = provider
        self.settings = settings
        self.background_tasks = background_tasks
        self.usage_repo = usage_repo

    async def propose_brief(self) -> str | None:
        agent = Agent(provider=self.provider, tool_registry=None)
        agent.system = _proposal_system_prompt(self.settings.factory_autonomous_themes)
        reply = ""
        async for chunk in agent.turn("Propose today's project."):
            reply += chunk
        reply = reply.strip()
        return reply or None

    async def tick_once(self) -> None:
        if self.settings.factory_paused:
            return
        if not self.settings.factory_autonomous_themes:
            return  # autonomous triggering is off; on-demand /build is unaffected
        if self.repo.count_builds_today() >= self.settings.factory_daily_build_cap:
            return

        brief = await self.propose_brief()
        if not brief:
            return

        try:
            start_build(
                brief,
                self.repo,
                self.provider,
                self.settings,
                background_tasks=self.background_tasks,
                usage_repo=self.usage_repo,
                created_by="factory-auto",
            )
        except (FactoryPaused, BuildCapExceeded, BudgetCapExceeded) as e:
            # A cap could have been hit between the checks above and now
            # (e.g. a concurrent /build) — skip this tick rather than crash it.
            logger.info("autonomous scheduler skipped a tick: %s", e)

    async def run_forever(self) -> None:
        while True:
            try:
                await self.tick_once()
            except Exception:  # noqa: BLE001 — a broken tick must never kill the scheduler
                logger.exception("AutonomousScheduler.tick_once failed")
            await asyncio.sleep(self.settings.factory_autonomous_interval_hours * 3600)
