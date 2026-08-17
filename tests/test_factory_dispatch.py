"""
Tests for the Agent Factory hot-reload dispatch layer (agent/factory/dispatch.py).

Uses a FakeProvider that returns canned replies — no live API calls.

Run from the project root:
    python -m unittest tests.test_factory_dispatch
"""

import asyncio
import os
import tempfile
import unittest

from agent.factory.dispatch import (
    ConfigDrivenAgent,
    DispatchActivity,
    DispatchTool,
    RegistryWatcher,
    dispatch_tool_name,
    get_dispatch_activity,
)
from agent.factory.storage import FactoryRepo
from agent.providers.base import BaseProvider, ProviderResponse, TextChunk, TokenUsage
from agent.tools.base import BaseTool
from agent.tools.registry import ToolRegistry


def run(coro):
    return asyncio.run(coro)


class FakeTool(BaseTool):
    name = "web_search"
    description = "fake"
    input_schema = {}

    async def run(self, **kwargs):
        return "ok"


class FakeProvider(BaseProvider):
    def __init__(self, replies):
        self._replies = list(replies)

    @property
    def model_name(self):
        return "fake-model"

    async def stream(self, messages, system, tools=None):
        text = self._replies.pop(0) if self._replies else ""
        yield TextChunk(text=text)
        yield ProviderResponse(text=text, tool_calls=[], usage=TokenUsage(), model=self.model_name)


def make_row(slug="sql-migration-review", tool_allowlist=None):
    return {
        "slug": slug,
        "name": slug,
        "system_prompt": "You review database schema changes before deployment.",
        "tool_allowlist": tool_allowlist if tool_allowlist is not None else ["web_search"],
    }


class TestConfigDrivenAgent(unittest.TestCase):
    def test_run_produces_reply_via_scratch_agent(self):
        row = make_row()
        base_registry = ToolRegistry()
        base_registry.register(FakeTool())
        provider = FakeProvider(["Looks safe to deploy."])
        agent = ConfigDrivenAgent(row, provider, base_registry)
        reply = run(agent.run("Is this migration safe?"))
        self.assertEqual(reply, "Looks safe to deploy.")
        self.assertEqual(agent._agent.system, row["system_prompt"])

    def test_restricted_registry_only_has_allowlisted_tools(self):
        row = make_row(tool_allowlist=["web_search"])
        base_registry = ToolRegistry()
        base_registry.register(FakeTool())
        agent = ConfigDrivenAgent(row, FakeProvider([]), base_registry)
        self.assertEqual(agent._agent.tool_registry.names(), ["web_search"])

    def test_none_base_registry_yields_no_tools(self):
        row = make_row()
        agent = ConfigDrivenAgent(row, FakeProvider([]), None)
        self.assertIsNone(agent._agent.tool_registry)


class TestDispatchTool(unittest.TestCase):
    def test_name_and_schema_shape(self):
        row = make_row(slug="sql-migration-review")
        tool = DispatchTool(row, FakeProvider([]), None)
        self.assertEqual(tool.name, "dispatch_to_sql_migration_review")
        self.assertIn("message", tool.input_schema["properties"])
        definition = tool.definition()
        self.assertEqual(definition["name"], tool.name)

    def test_run_delegates_to_sub_agent(self):
        row = make_row()
        tool = DispatchTool(row, FakeProvider(["delegated reply"]), None)
        result = run(tool.run(message="check this migration"))
        self.assertEqual(result, "delegated reply")

    def test_run_never_raises_on_sub_agent_failure(self):
        row = make_row()
        tool = DispatchTool(row, FakeProvider([]), None)

        async def boom(message):
            raise RuntimeError("sub-agent exploded")

        tool._sub_agent.run = boom
        result = run(tool.run(message="hi"))
        self.assertIn("failed", result)

    def test_factory_allowed_is_false(self):
        row = make_row()
        tool = DispatchTool(row, FakeProvider([]), None)
        self.assertFalse(tool.factory_allowed)

    def test_dispatch_tool_name_replaces_hyphens(self):
        self.assertEqual(dispatch_tool_name("sql-migration-review"), "dispatch_to_sql_migration_review")


class TestDispatchActivity(unittest.TestCase):
    def test_mark_started_then_snapshot_contains_slug(self):
        activity = DispatchActivity()
        activity.mark_started("x")
        self.assertIn("x", activity.snapshot())

    def test_mark_finished_removes_slug(self):
        activity = DispatchActivity()
        activity.mark_started("x")
        activity.mark_finished("x")
        self.assertNotIn("x", activity.snapshot())

    def test_mark_finished_on_unknown_slug_is_a_no_op(self):
        DispatchActivity().mark_finished("never-started")  # must not raise

    def test_snapshot_is_a_copy_not_a_live_view(self):
        activity = DispatchActivity()
        activity.mark_started("x")
        snap = activity.snapshot()
        activity.mark_started("y")
        self.assertNotIn("y", snap)

    def test_get_dispatch_activity_returns_the_same_singleton(self):
        self.assertIs(get_dispatch_activity(), get_dispatch_activity())

    def test_overlapping_dispatches_to_the_same_slug_stay_active_until_both_finish(self):
        # Codex review finding on PR #14: two concurrent dispatches to one
        # slug used to collapse into one set entry, so the first to finish
        # discarded it while the second was still running.
        activity = DispatchActivity()
        activity.mark_started("x")
        activity.mark_started("x")  # a second, overlapping call
        activity.mark_finished("x")  # the first call finishes
        self.assertIn("x", activity.snapshot())  # still active — the second isn't done
        activity.mark_finished("x")  # the second call finishes
        self.assertNotIn("x", activity.snapshot())

    def test_mark_finished_never_goes_negative_on_an_unbalanced_call(self):
        activity = DispatchActivity()
        activity.mark_started("x")
        activity.mark_finished("x")
        activity.mark_finished("x")  # an extra finish shouldn't happen, but must not corrupt state
        activity.mark_started("x")
        self.assertIn("x", activity.snapshot())

    def test_total_dispatches_counts_every_start_and_never_decreases(self):
        # Codex review finding on PR #14: a dispatch short enough to start
        # and finish between two browser polls needs a signal that stays
        # observable until acknowledged — total_dispatches is that signal,
        # a monotonic counter the browser diffs against, not the current
        # active/inactive snapshot.
        activity = DispatchActivity()
        self.assertEqual(activity.total_dispatches("x"), 0)
        activity.mark_started("x")
        activity.mark_finished("x")
        activity.mark_started("x")
        activity.mark_finished("x")
        self.assertEqual(activity.total_dispatches("x"), 2)

    def test_total_dispatches_is_per_slug(self):
        activity = DispatchActivity()
        activity.mark_started("x")
        self.assertEqual(activity.total_dispatches("y"), 0)


class TestDispatchToolActivityTracking(unittest.TestCase):
    """
    Covers what serve.py's GET /api/agents reads: DispatchTool.run() must
    mark its slug working for the duration of the sub-agent call, real
    signal for the browser's constellation "working" pulse — and must clear
    it even when the sub-agent blows up, so a crash doesn't strand an agent
    permanently shown as busy.
    """

    def tearDown(self):
        # Belt and braces: every test below pairs a start with a finish via
        # DispatchTool.run()'s own try/finally, but clear defensively so a
        # failed assertion mid-test can't leak state into the next test.
        activity = get_dispatch_activity()
        for slug in list(activity.snapshot()):
            activity.mark_finished(slug)

    def test_slug_is_working_during_the_call_and_clear_after(self):
        row = make_row(slug="probe-agent")
        tool = DispatchTool(row, FakeProvider([]), None)
        seen = {}

        async def probe(message):
            seen["working_during_call"] = "probe-agent" in get_dispatch_activity().snapshot()
            return "done"

        tool._sub_agent.run = probe
        result = run(tool.run(message="hi"))

        self.assertTrue(seen["working_during_call"])
        self.assertNotIn("probe-agent", get_dispatch_activity().snapshot())
        self.assertEqual(result, "done")

    def test_working_state_clears_even_when_sub_agent_raises(self):
        row = make_row(slug="boom-agent")
        tool = DispatchTool(row, FakeProvider([]), None)

        async def boom(message):
            raise RuntimeError("sub-agent exploded")

        tool._sub_agent.run = boom
        run(tool.run(message="hi"))
        self.assertNotIn("boom-agent", get_dispatch_activity().snapshot())


class TestRegistryWatcher(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmp, "factory.db")
        self.repo = FactoryRepo(db_path=self.db_path)
        self.registry = ToolRegistry()
        self.registry.register(FakeTool())

    def tearDown(self):
        try:
            os.remove(self.db_path)
        except FileNotFoundError:
            pass

    def _approve_agent(self, slug="sql-migration-review"):
        task_id = self.repo.create_spawn_task(f"a specialist: {slug}")
        self.repo.set_draft(task_id, slug=slug, system_prompt="You are a specialist.", tool_allowlist=["web_search"])
        return self.repo.approve(task_id)

    def test_sync_once_registers_newly_approved_agent(self):
        self._approve_agent()
        watcher = RegistryWatcher(self.repo, FakeProvider([]), self.registry)
        watcher.sync_once()
        self.assertIn("dispatch_to_sql_migration_review", self.registry.names())

    def test_sync_once_is_idempotent(self):
        self._approve_agent()
        watcher = RegistryWatcher(self.repo, FakeProvider([]), self.registry)
        watcher.sync_once()
        first = self.registry._tools["dispatch_to_sql_migration_review"]
        watcher.sync_once()
        second = self.registry._tools["dispatch_to_sql_migration_review"]
        self.assertIs(first, second)

    def test_sync_once_unregisters_disabled_agent(self):
        self._approve_agent()
        watcher = RegistryWatcher(self.repo, FakeProvider([]), self.registry)
        watcher.sync_once()
        self.assertIn("dispatch_to_sql_migration_review", self.registry.names())

        self.repo.disable_agent("sql-migration-review")
        watcher.sync_once()
        self.assertNotIn("dispatch_to_sql_migration_review", self.registry.names())

    def test_sync_once_registers_multiple_agents(self):
        self._approve_agent("sql-migration-review")
        self._approve_agent("api-doc-writer")
        watcher = RegistryWatcher(self.repo, FakeProvider([]), self.registry)
        watcher.sync_once()
        self.assertIn("dispatch_to_sql_migration_review", self.registry.names())
        self.assertIn("dispatch_to_api_doc_writer", self.registry.names())


if __name__ == "__main__":
    unittest.main()
