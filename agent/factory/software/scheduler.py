"""
Autonomous scheduler: on an interval, researches real problems within Sean's
own configured themes (TRILLION_FACTORY_AUTONOMOUS_THEMES) via the
opportunity scout, then starts a build via the exact same start_build() the
/build command uses — no separate code path for self-initiated vs. requested
builds past the one research-and-select step.

Same run_forever()/poll-and-reconcile idiom as
agent/factory/dispatch.py's RegistryWatcher: main.py and serve.py are
separate processes with no shared memory, so periodic ticking is the
simplest correct option, not a hot-reload/event system.
"""

from __future__ import annotations

import asyncio
import logging

from ...tools.web_search import resolve_search_provider
from .opportunity_scout import OpportunityScoutError, run_opportunity_scout
from .pipeline import BudgetCapExceeded, BuildCapExceeded, FactoryPaused, start_build

logger = logging.getLogger(__name__)


def _render_brief(report: dict) -> str:
    """Turn a validated opportunity-scout report into the build's
    description text — folds the chosen candidate and why it was picked
    into the one field /builds and each project's README already surface,
    rather than adding new storage for it."""
    candidate = report["candidates"][report["selected_index"]]
    return (
        f"{candidate['problem']}\n\n"
        f"Why this one: {report['selection_reasoning']}\n\n"
        f"(Source: {candidate['source_url']})"
    )


class AutonomousScheduler:
    """
    Ticks on settings.factory_autonomous_interval_hours. Each tick: checks
    the kill switch, the themes, the search-tool config, and the daily
    build cap first (no LLM/search calls if any of those is already
    blocking), runs the opportunity scout to research and select one
    project idea constrained to factory_autonomous_themes, then calls
    start_build() with it.
    """

    def __init__(self, repo, provider, settings, *, background_tasks: set, usage_repo=None) -> None:
        self.repo = repo
        self.provider = provider
        self.settings = settings
        self.background_tasks = background_tasks
        self.usage_repo = usage_repo

    async def tick_once(self) -> None:
        # builds_paused(), not factory_paused: pausing Trillion as a whole
        # pauses builds too (agent/config.py).
        if self.settings.builds_paused():
            return
        if not self.settings.factory_autonomous_themes:
            return  # autonomous triggering is off; on-demand /build is unaffected
        resolved = resolve_search_provider(self.settings)
        if resolved is None:
            # No fallback to a non-researched guess — skip rather than degrade.
            logger.info("autonomous scheduler skipped a tick: no search provider configured")
            return
        if self.repo.count_builds_today() >= self.settings.factory_daily_build_cap:
            return

        search_provider, search_api_key = resolved
        try:
            report = await run_opportunity_scout(
                self.settings.factory_autonomous_themes,
                self.provider,
                search_api_key,
                search_provider=search_provider,
                firecrawl_base_url=self.settings.firecrawl_base_url,
            )
        except OpportunityScoutError as e:
            logger.info("autonomous scheduler skipped a tick: opportunity scout failed: %s", e)
            return

        brief = _render_brief(report)

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
