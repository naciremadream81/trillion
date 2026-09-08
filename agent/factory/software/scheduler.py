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
from datetime import datetime, timezone

from ...tools.web_search import resolve_search_provider
from .doctrine import DocumentOverrides, lane_for
from .opportunity_scout import OpportunityScoutError, fingerprint, run_opportunity_scout
from .pipeline import BudgetCapExceeded, BuildCapExceeded, FactoryPaused, start_build

logger = logging.getLogger(__name__)

# How many previously-sighted candidates to show the scout as "already seen".
# Enough to steer it off the obvious repeats, short enough that it stays a
# note rather than half the system prompt.
SEEN_MEMORY_LIMIT = 40


def _render_brief(report: dict) -> str:
    """Turn a validated opportunity-scout report into the build's
    description text — folds the chosen candidate and why it was picked
    into the one field /builds and each project's README already surface,
    rather than adding new storage for it.

    The lane label rides along in the same field for the same reason: it is
    the record of which hunting strategy found this, and it costs nothing to
    carry it where a human is already looking."""
    candidate = report["candidates"][report["selected_index"]]
    lines = [
        candidate["problem"],
        "",
        f"Why this one: {report['selection_reasoning']}",
        "",
        f"(Source: {candidate['source_url']})",
    ]
    if report.get("lane"):
        lines.append(f"(Lane: {report['lane']})")
    return "\n".join(lines)


def _record_sightings(repo, report: dict, lane_label: str | None) -> int:
    """
    Persist EVERY candidate the scout examined — Tier 7 — and return how many
    actually landed.

    Two rules from the playbook, both learned the expensive way:

    Persist per item, not per report. Each write gets its own try/except, so
    one malformed candidate cannot discard the four good ones beside it. A
    single failed insert used to mean the whole run left no trace.

    Count what actually persisted and report *that*. Telling the operator
    "5 new finds" when zero were saved sends them looking for rows that do
    not exist, and they find the storage layer instead of the bug.
    """
    selected_index = report.get("selected_index")
    saved = 0
    for i, candidate in enumerate(report.get("candidates", [])):
        try:
            repo.record_sighting(
                fingerprint=fingerprint(candidate),
                problem=candidate.get("problem", ""),
                evidence=candidate.get("evidence", ""),
                source_url=candidate.get("source_url", ""),
                lane=lane_label,
                selected=(i == selected_index),
            )
            saved += 1
        except Exception:  # noqa: BLE001 — one bad row must not lose the others
            logger.exception("could not record scout sighting %d; continuing with the rest", i)
    return saved


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
        # Operator document overrides (opportunity-scout.md Tier 4). Reads go
        # through a TTL'd in-process cache, so an edit written by the other
        # process lands on the next tick without restarting this one.
        self.overrides = DocumentOverrides(repo)

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
        # One lane per run, rotation positional on the weekday. lane_for()
        # never raises and never returns None — a deleted lanes document
        # degrades to one generic lane rather than killing the tick.
        lane = lane_for(datetime.now(timezone.utc), self.overrides)

        # Repetition memory (Tier 6). A failed read here is not worth losing
        # a run over — the scout is simply less informed for one tick.
        try:
            already_seen = self.repo.recent_sightings(SEEN_MEMORY_LIMIT)
        except Exception:  # noqa: BLE001
            logger.exception("could not load scout repetition memory; running without it")
            already_seen = []

        try:
            report = await run_opportunity_scout(
                self.settings.factory_autonomous_themes,
                self.provider,
                search_api_key,
                search_provider=search_provider,
                firecrawl_base_url=self.settings.firecrawl_base_url,
                lane=lane,
                overrides=self.overrides,
                already_seen=already_seen,
            )
        except OpportunityScoutError as e:
            logger.info("autonomous scheduler skipped a tick: opportunity scout failed: %s", e)
            return

        # Telemetry before the build: the record of what was examined is
        # worth keeping even if start_build() below hits a cap and skips.
        saved = _record_sightings(self.repo, report, lane.label if lane else None)
        logger.info(
            "scout lane %s examined %d candidates, %d recorded",
            lane.label if lane else "-",
            len(report.get("candidates", [])),
            saved,
        )

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
