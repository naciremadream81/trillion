"""
Tests for the Software Factory's autonomous scheduler
(agent/factory/software/scheduler.py) — the self-initiated build path.

This is the load-bearing safety surface for the "fully autonomous, no
per-run approval" design: a tick must never call the provider (i.e. never
run the opportunity scout) once the kill switch, an unset themes list, a
missing search API key, or the daily build cap already rules a build out.
Mirrors tests/test_factory_dispatch.py's TestRegistryWatcher structure.

Run from the project root:
    python -m unittest tests.test_software_scheduler
"""

import asyncio
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from agent.config import Settings
from agent.factory.software.scheduler import AutonomousScheduler
from agent.factory.software.storage import BUILT, BuildRepo
from agent.providers.base import BaseProvider, ProviderResponse, TextChunk, ToolCall, TokenUsage


def run(coro):
    return asyncio.run(coro)


VALID_PLAN_REPLY = (
    '{"project_name": "daily-tool", "tech_stack": "python", '
    '"files": ["main.py"], "entry_point": "main.py", '
    '"test_command": "", "summary": "A small autonomous project.", '
    '"tasks": [{"title": "Implement CLI", "description": "Write main.py.", '
    '"acceptance_criteria": "python main.py works"}]}'
)

ARCHITECTURE_REPLY = "# Architecture\n\nOne module, main.py."
QA_PASS_REPLY = '{"result": "PASS", "feedback": "meets acceptance criteria"}'
INTEGRATION_READY_REPLY = '{"verdict": "READY", "notes": "all good"}'

SCOUT_REPORT_REPLY = json.dumps({
    "candidates": [
        {
            "problem": f"Problem {i}",
            "evidence": f"Evidence {i}",
            "source_url": f"https://example.com/{i}",
        }
        for i in range(5)
    ],
    "selected_index": 2,
    "selection_reasoning": "Clear evidence and a small, buildable scope.",
})


class FakeProvider(BaseProvider):
    """The very first stream() call of a scenario simulates a genuine
    web_search tool round-trip (as the opportunity scout now requires
    evidence of one — see agent/factory/software/opportunity_scout.py's
    _has_search_evidence) before falling back to popping replies off the
    queue as before for every subsequent call (planning, architecture,
    coding, QA, integration, and any scout retries)."""

    def __init__(self, replies):
        self._replies = list(replies)
        self.call_count = 0

    @property
    def model_name(self):
        return "fake-model"

    async def stream(self, messages, system, tools=None):
        self.call_count += 1
        if self.call_count == 1:
            tc = ToolCall(id="tc_1", name="web_search", arguments={"query": "example problem"})
            yield tc
            yield ProviderResponse(text="", tool_calls=[tc], usage=TokenUsage(), model=self.model_name)
            return
        text = self._replies.pop(0) if self._replies else "CODING_COMPLETE"
        yield TextChunk(text=text)
        yield ProviderResponse(text=text, tool_calls=[], usage=TokenUsage(), model=self.model_name)


class ExplodingProvider(BaseProvider):
    """Raises on any call — proves a gated tick never reaches the scout."""

    @property
    def model_name(self):
        return "exploding-model"

    async def stream(self, messages, system, tools=None):
        raise AssertionError("provider should never be called when a tick is gated")
        yield  # pragma: no cover — makes this an async generator


class TestAutonomousScheduler(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmp, "software_factory.db")
        self.repo = BuildRepo(db_path=self.db_path)

    def tearDown(self):
        try:
            os.remove(self.db_path)
        except FileNotFoundError:
            pass

    def _settings(self, **overrides):
        kwargs = dict(
            software_factory_root=os.path.join(self.tmp, "generated-projects"),
            factory_daily_build_cap=3,
            factory_daily_budget_usd=None,
            factory_paused=False,
            factory_autonomous_themes=["cli productivity tools"],
            factory_autonomous_interval_hours=24.0,
            brave_search_api_key="fake-brave-key",
        )
        kwargs.update(overrides)
        return Settings(**kwargs)

    def test_tick_skips_when_paused_without_touching_provider(self):
        settings = self._settings(factory_paused=True)
        scheduler = AutonomousScheduler(
            self.repo, ExplodingProvider(), settings, background_tasks=set()
        )
        run(scheduler.tick_once())
        self.assertEqual(self.repo.count_builds_today(), 0)

    def test_tick_skips_when_themes_unset_without_touching_provider(self):
        settings = self._settings(factory_autonomous_themes=[])
        scheduler = AutonomousScheduler(
            self.repo, ExplodingProvider(), settings, background_tasks=set()
        )
        run(scheduler.tick_once())
        self.assertEqual(self.repo.count_builds_today(), 0)

    def test_tick_skips_when_search_key_not_configured_without_touching_provider(self):
        settings = self._settings(brave_search_api_key="")
        scheduler = AutonomousScheduler(
            self.repo, ExplodingProvider(), settings, background_tasks=set()
        )
        run(scheduler.tick_once())
        self.assertEqual(self.repo.count_builds_today(), 0)

    def test_tick_skips_when_build_cap_already_reached_without_touching_provider(self):
        settings = self._settings(factory_daily_build_cap=1)
        self.repo.create_build_task("filler")
        scheduler = AutonomousScheduler(
            self.repo, ExplodingProvider(), settings, background_tasks=set()
        )
        run(scheduler.tick_once())
        self.assertEqual(self.repo.count_builds_today(), 1)

    def test_tick_researches_and_starts_a_build_when_clear(self):
        settings = self._settings()
        provider = FakeProvider([
            SCOUT_REPORT_REPLY, VALID_PLAN_REPLY, ARCHITECTURE_REPLY,
            "CODING_COMPLETE", QA_PASS_REPLY, INTEGRATION_READY_REPLY,
        ])
        bg = set()
        scheduler = AutonomousScheduler(self.repo, provider, settings, background_tasks=bg)

        async def scenario():
            await scheduler.tick_once()
            self.assertEqual(len(bg), 1)
            await asyncio.gather(*bg)

        run(scenario())
        self.assertEqual(self.repo.count_builds_today(), 1)
        task = self.repo.get_build_task(1)
        self.assertEqual(task["status"], BUILT)
        self.assertEqual(task["created_by"], "factory-auto")
        self.assertIn("Problem 2", task["description"])
        self.assertIn("Clear evidence", task["description"])

    def test_tick_skips_when_opportunity_scout_fails(self):
        settings = self._settings()
        provider = FakeProvider(["not json", "still not json"])
        scheduler = AutonomousScheduler(self.repo, provider, settings, background_tasks=set())
        run(scheduler.tick_once())
        self.assertEqual(self.repo.count_builds_today(), 0)

    def test_tick_swallows_a_cap_race_between_check_and_start(self):
        # Simulate a concurrent /build exhausting the cap between tick_once's
        # own pre-check and its call to start_build() — should log and
        # return, not raise.
        settings = self._settings(factory_daily_build_cap=1)
        provider = FakeProvider([])  # never actually consulted — the scout call is patched below

        async def fake_scout_then_fill_cap(themes, provider, api_key):
            self.repo.create_build_task("a concurrent /build got here first")
            return {
                "candidates": [
                    {"problem": "p", "evidence": "e", "source_url": "https://example.com"}
                    for _ in range(5)
                ],
                "selected_index": 0,
                "selection_reasoning": "reasoning",
            }

        scheduler = AutonomousScheduler(self.repo, provider, settings, background_tasks=set())
        with patch(
            "agent.factory.software.scheduler.run_opportunity_scout",
            new=fake_scout_then_fill_cap,
        ):
            run(scheduler.tick_once())  # must not raise
        self.assertEqual(self.repo.count_builds_today(), 1)


if __name__ == "__main__":
    unittest.main()
