"""
Tests for the per-agent model override — orchestration.md Tier 2's
"a declared model per agent", plus the routing policy from Tier 1.

The column existed in the spawned_agents schema but was never read or
written by anything, which is the gap these cover.

Run from the project root:
    python -m unittest tests.test_agent_model
"""

import os
import shutil
import tempfile
import unittest

from agent.factory.dispatch import ConfigDrivenAgent, RegistryWatcher, dispatch_tool_name
from agent.factory.storage import FactoryRepo
from agent.providers import get_provider
from agent.providers.base import BaseProvider, ProviderResponse, TextChunk, TokenUsage
from agent.system_prompt import build_system_prompt
from agent.tools.registry import ToolRegistry


class FakeProvider(BaseProvider):
    def __init__(self, model="fake-default"):
        self._model = model

    @property
    def model_name(self):
        return self._model

    async def stream(self, messages, system, tools=None):
        yield TextChunk(text="")
        yield ProviderResponse(text="", tool_calls=[], usage=TokenUsage(), model=self._model)


class TestProviderModelOverride(unittest.TestCase):
    def setUp(self):
        self._prev = os.environ.get("ANTHROPIC_API_KEY")
        os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")

    def tearDown(self):
        if self._prev is None:
            os.environ.pop("ANTHROPIC_API_KEY", None)
        else:
            os.environ["ANTHROPIC_API_KEY"] = self._prev

    def test_none_uses_the_env_default(self):
        self.assertEqual(get_provider("claude").model_name, os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6"))

    def test_override_is_used(self):
        self.assertEqual(
            get_provider("claude", "claude-haiku-4-5-20251001").model_name,
            "claude-haiku-4-5-20251001",
        )

    def test_empty_string_falls_back_to_the_default(self):
        # "" must behave like None, not like a model literally named "".
        self.assertEqual(
            get_provider("claude", "").model_name, os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6")
        )


class TestSetAgentModel(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._prev = os.environ.get("TRILLION_FACTORY_DB")
        os.environ["TRILLION_FACTORY_DB"] = os.path.join(self.tmp, "factory.db")
        self.repo = FactoryRepo()
        task_id = self.repo.create_spawn_task("an analyst")
        self.repo.set_draft(
            task_id, slug="analyst", system_prompt="you analyse", tool_allowlist=[]
        )
        self.repo.approve(task_id)

    def tearDown(self):
        if self._prev is None:
            os.environ.pop("TRILLION_FACTORY_DB", None)
        else:
            os.environ["TRILLION_FACTORY_DB"] = self._prev
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _row(self):
        return next(r for r in self.repo.list_active_agents() if r["slug"] == "analyst")

    def test_model_defaults_to_null(self):
        self.assertFalse(self._row().get("model"))

    def test_setting_and_reading_back(self):
        self.assertTrue(self.repo.set_agent_model("analyst", "claude-haiku-4-5-20251001"))
        self.assertEqual(self._row()["model"], "claude-haiku-4-5-20251001")

    def test_clearing_restores_the_default(self):
        self.repo.set_agent_model("analyst", "claude-haiku-4-5-20251001")
        self.repo.set_agent_model("analyst", "")
        self.assertIsNone(self._row()["model"])

    def test_unknown_slug_reports_failure(self):
        self.assertFalse(self.repo.set_agent_model("nope", "x"))

    def test_whitespace_only_clears_rather_than_storing_blanks(self):
        self.repo.set_agent_model("analyst", "   ")
        self.assertIsNone(self._row()["model"])


class TestConfigDrivenAgentHonoursModel(unittest.TestCase):
    def test_no_declared_model_uses_the_shared_provider(self):
        shared = FakeProvider("shared-model")
        row = {"slug": "a", "name": "a", "system_prompt": "p", "tool_allowlist": []}
        agent = ConfigDrivenAgent(row, shared, ToolRegistry())
        self.assertIs(agent._agent.provider, shared)

    def test_a_declared_model_builds_a_provider_of_the_same_family(self):
        shared = FakeProvider("shared-model")
        row = {
            "slug": "a", "name": "a", "system_prompt": "p", "tool_allowlist": [],
            "model": "cheap-model",
        }
        agent = ConfigDrivenAgent(row, shared, ToolRegistry())
        self.assertIsNot(agent._agent.provider, shared)
        self.assertIsInstance(agent._agent.provider, FakeProvider)
        self.assertEqual(agent._agent.provider.model_name, "cheap-model")

    def test_an_unbuildable_model_falls_back_to_the_shared_provider(self):
        # A specialist running on the default model is a far better failure
        # than a specialist that won't run at all.
        class Unbuildable(FakeProvider):
            def __init__(self, model=None):
                if model:
                    raise RuntimeError("no such model")
                super().__init__("shared-model")

        shared = Unbuildable()
        row = {
            "slug": "a", "name": "a", "system_prompt": "p", "tool_allowlist": [],
            "model": "no-such-model",
        }
        agent = ConfigDrivenAgent(row, shared, ToolRegistry())
        self.assertIs(agent._agent.provider, shared)


class TestWatcherRebuildsOnModelChange(unittest.TestCase):
    """A field baked in at construction must be in the change fingerprint."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._prev = os.environ.get("TRILLION_FACTORY_DB")
        os.environ["TRILLION_FACTORY_DB"] = os.path.join(self.tmp, "factory.db")
        self.repo = FactoryRepo()
        task_id = self.repo.create_spawn_task("an analyst")
        self.repo.set_draft(task_id, slug="analyst", system_prompt="p", tool_allowlist=[])
        self.repo.approve(task_id)
        self.registry = ToolRegistry()

    def tearDown(self):
        if self._prev is None:
            os.environ.pop("TRILLION_FACTORY_DB", None)
        else:
            os.environ["TRILLION_FACTORY_DB"] = self._prev
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_changing_the_model_replaces_the_live_dispatch_tool(self):
        watcher = RegistryWatcher(self.repo, FakeProvider(), self.registry)
        watcher.sync_once()
        first = self.registry.get(dispatch_tool_name("analyst"))

        self.repo.set_agent_model("analyst", "claude-haiku-4-5-20251001")
        watcher.sync_once()
        second = self.registry.get(dispatch_tool_name("analyst"))
        self.assertIsNot(first, second, "model change did not force a rebuild")

    def test_an_unchanged_agent_is_not_rebuilt(self):
        watcher = RegistryWatcher(self.repo, FakeProvider(), self.registry)
        watcher.sync_once()
        first = self.registry.get(dispatch_tool_name("analyst"))
        watcher.sync_once()
        self.assertIs(first, self.registry.get(dispatch_tool_name("analyst")))


class TestRoutingPolicy(unittest.TestCase):
    """orchestration.md Tier 1 — the policy is explicit, not implied."""

    def setUp(self):
        self.prompt = build_system_prompt()

    def test_the_policy_is_present(self):
        self.assertIn("## Routing work to specialists", self.prompt)

    def test_it_states_the_clarify_dont_guess_rule(self):
        self.assertIn("ambiguous", self.prompt)

    def test_it_states_the_decomposition_rule(self):
        self.assertIn("several dispatches", self.prompt)

    def test_it_forbids_chaining_on_a_handoff(self):
        # Tier 5's rule restated where the orchestrator will actually read it.
        self.assertIn("not permission for you to chain", self.prompt)

    def test_the_prompt_stays_byte_identical_across_builds(self):
        # The cached-prefix property tests/test_caching.py locks in — the
        # routing policy is static text and must not break it.
        self.assertEqual(build_system_prompt(), build_system_prompt())


if __name__ == "__main__":
    unittest.main()
