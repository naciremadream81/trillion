"""
Tests for the Agent Factory slash commands wired into main.py
(/spawn, /pending, /approve, /reject).

Uses a FakeProvider and a fake registry — no live API calls.

Run from the project root:
    python -m unittest tests.test_main_factory_commands
"""

import asyncio
import os
import tempfile
import unittest

import main as main_module
from agent.factory.storage import AWAITING_APPROVAL, FactoryRepo
from agent.providers.base import BaseProvider, ProviderResponse, TextChunk, TokenUsage
from agent.tools.base import BaseTool
from agent.tools.registry import ToolRegistry

VALID_REPORT_REPLY = (
    '{"domain": "SQL migration review", "competencies": ["schema review"], '
    '"tools_available": ["web_search"], "tools_wishlist": ["web_search"], '
    '"design_patterns": ["idempotent migrations"], "sources": []}'
)
VALID_PROMPT_REPLY = '{"system_prompt": "You review database schema changes before deployment."}'


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


class TestFactoryCliCommands(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmp, "factory.db")
        self.repo = FactoryRepo(db_path=self.db_path)
        self.registry = ToolRegistry()
        self.registry.register(FakeTool())
        # Keep spec-markdown writes out of the real agent-specs/ directory.
        self._prev_specs_dir = os.environ.get("TRILLION_AGENT_SPECS_DIR")
        os.environ["TRILLION_AGENT_SPECS_DIR"] = os.path.join(self.tmp, "agent-specs")

    def tearDown(self):
        try:
            os.remove(self.db_path)
        except FileNotFoundError:
            pass
        if self._prev_specs_dir is None:
            os.environ.pop("TRILLION_AGENT_SPECS_DIR", None)
        else:
            os.environ["TRILLION_AGENT_SPECS_DIR"] = self._prev_specs_dir

    def test_spawn_then_approve(self):
        async def scenario():
            provider = FakeProvider([VALID_REPORT_REPLY, VALID_PROMPT_REPLY])
            factory = main_module.FactoryContext(
                repo=self.repo, provider=provider, registry=self.registry, background_tasks=set()
            )
            main_module.handle_slash(
                "/spawn a specialist that reviews SQL migrations", None, "claude", factory
            )
            self.assertEqual(len(factory.background_tasks), 1)
            await asyncio.gather(*factory.background_tasks)

            pending = self.repo.list_pending_approval()
            self.assertEqual(len(pending), 1)
            task_id = pending[0]["id"]

            main_module.handle_slash(f"/approve {task_id}", None, "claude", factory)
            active = self.repo.list_active_agents()
            self.assertEqual(len(active), 1)
            self.assertEqual(active[0]["slug"], "sql-migration-review")

        asyncio.run(scenario())

    def test_reject_triggers_background_revision(self):
        async def scenario():
            provider = FakeProvider(
                [VALID_REPORT_REPLY, VALID_PROMPT_REPLY, VALID_REPORT_REPLY, VALID_PROMPT_REPLY]
            )
            factory = main_module.FactoryContext(
                repo=self.repo, provider=provider, registry=self.registry, background_tasks=set()
            )
            main_module.handle_slash(
                "/spawn a specialist that reviews SQL migrations", None, "claude", factory
            )
            await asyncio.gather(*factory.background_tasks)
            task_id = self.repo.list_pending_approval()[0]["id"]

            main_module.handle_slash(f"/reject {task_id} needs more detail on tool usage", None, "claude", factory)
            self.assertEqual(len(factory.background_tasks), 1)  # revision scheduled
            await asyncio.gather(*factory.background_tasks)

            task = self.repo.get_spawn_task(task_id)
            self.assertEqual(task["status"], AWAITING_APPROVAL)
            self.assertEqual(task["revision_count"], 1)

        asyncio.run(scenario())

    def test_approve_unknown_task_reports_error_not_crash(self):
        factory = main_module.FactoryContext(
            repo=self.repo, provider=FakeProvider([]), registry=self.registry, background_tasks=set()
        )
        # Should not raise — errors are caught and printed.
        main_module.handle_slash("/approve 999", None, "claude", factory)

    def test_reject_without_feedback_shows_usage_not_crash(self):
        factory = main_module.FactoryContext(
            repo=self.repo, provider=FakeProvider([]), registry=self.registry, background_tasks=set()
        )
        main_module.handle_slash("/reject 1", None, "claude", factory)
        self.assertEqual(len(factory.background_tasks), 0)

    def test_factory_commands_without_factory_context_dont_crash(self):
        main_module.handle_slash("/pending", None, "claude", None)
        main_module.handle_slash("/spawn something", None, "claude", None)


if __name__ == "__main__":
    unittest.main()
