"""
Tests that the Software Factory's hard caps (daily build cap, kill switch,
daily budget cap) refuse a new build before any provider (LLM) call happens —
the load-bearing safety property, since this feature has no per-run human
approval gate. A provider that raises on any call proves the refusal never
reaches it.

Run from the project root:
    python -m unittest tests.test_software_caps
"""

import asyncio
import os
import tempfile
import unittest

from agent.config import Settings
from agent.factory.software.pipeline import BudgetCapExceeded, BuildCapExceeded, FactoryPaused, start_build
from agent.factory.software.storage import BuildRepo
from agent.providers.base import BaseProvider


class ExplodingProvider(BaseProvider):
    """Raises on any call — proves a refused start_build() never touches it."""

    @property
    def model_name(self):
        return "exploding-model"

    async def stream(self, messages, system, tools=None):
        raise AssertionError("provider should never be called when a cap refuses the build")
        yield  # pragma: no cover — makes this an async generator


class FakeUsageRepo:
    def __init__(self, cost_usd: float):
        self.cost_usd = cost_usd

    def usage_since(self, start, end):
        return {"cost_usd": self.cost_usd}


class TestSoftwareFactoryCaps(unittest.TestCase):
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
        )
        kwargs.update(overrides)
        return Settings(**kwargs)

    def test_paused_refuses_without_touching_provider(self):
        settings = self._settings(factory_paused=True)
        bg = set()
        with self.assertRaises(FactoryPaused):
            start_build("project", self.repo, ExplodingProvider(), settings, background_tasks=bg)
        self.assertEqual(bg, set())
        self.assertEqual(self.repo.count_builds_today(), 0)

    def test_daily_build_cap_refuses_without_touching_provider(self):
        settings = self._settings(factory_daily_build_cap=2)
        self.repo.create_build_task("filler 1")
        self.repo.create_build_task("filler 2")
        bg = set()
        with self.assertRaises(BuildCapExceeded):
            start_build("one too many", self.repo, ExplodingProvider(), settings, background_tasks=bg)
        self.assertEqual(bg, set())
        self.assertEqual(self.repo.count_builds_today(), 2)

    def test_daily_build_cap_of_zero_refuses_first_build(self):
        settings = self._settings(factory_daily_build_cap=0)
        bg = set()
        with self.assertRaises(BuildCapExceeded):
            start_build("project", self.repo, ExplodingProvider(), settings, background_tasks=bg)
        self.assertEqual(self.repo.count_builds_today(), 0)

    def test_daily_budget_cap_refuses_without_touching_provider(self):
        settings = self._settings(factory_daily_budget_usd=1.0)
        usage_repo = FakeUsageRepo(cost_usd=1.0)  # at (not over) the cap — still refused
        bg = set()
        with self.assertRaises(BudgetCapExceeded):
            start_build(
                "project", self.repo, ExplodingProvider(), settings,
                background_tasks=bg, usage_repo=usage_repo,
            )
        self.assertEqual(bg, set())
        self.assertEqual(self.repo.count_builds_today(), 0)

    def test_budget_cap_unset_does_not_refuse(self):
        # factory_daily_budget_usd=None means the budget check is a no-op —
        # only the build cap gates on-demand /build in that configuration.
        settings = self._settings(factory_daily_budget_usd=None)
        usage_repo = FakeUsageRepo(cost_usd=999.0)

        async def scenario():
            bg = set()
            # Should get past the cap checks and create the row (then the
            # provider explodes inside the background pipeline, not here —
            # the pipeline's own except-Exception catch swallows it and
            # marks the task FAILED, which is irrelevant to what this test
            # is checking).
            task_id = start_build(
                "project", self.repo, ExplodingProvider(), settings,
                background_tasks=bg, usage_repo=usage_repo,
            )
            self.assertEqual(len(bg), 1)
            self.assertIsNotNone(task_id)
            await asyncio.gather(*bg)

        asyncio.run(scenario())

    def test_budget_under_cap_does_not_refuse(self):
        settings = self._settings(factory_daily_budget_usd=10.0)
        usage_repo = FakeUsageRepo(cost_usd=2.0)

        async def scenario():
            bg = set()
            task_id = start_build(
                "project", self.repo, ExplodingProvider(), settings,
                background_tasks=bg, usage_repo=usage_repo,
            )
            self.assertEqual(len(bg), 1)
            self.assertIsNotNone(task_id)
            await asyncio.gather(*bg)

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
