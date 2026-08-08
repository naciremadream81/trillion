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

from agent.factory.dispatch import ConfigDrivenAgent, DispatchTool, RegistryWatcher, dispatch_tool_name
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
