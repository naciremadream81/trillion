"""
Tests for the Software Factory planning subagent (agent/factory/software/planning.py).

Run from the project root:
    python -m unittest tests.test_software_planning
"""

import asyncio
import unittest

from agent.factory.software.planning import PlanningError, run_planning
from agent.providers.base import BaseProvider, ProviderResponse, TextChunk, TokenUsage


def run(coro):
    return asyncio.run(coro)


VALID_PLAN_REPLY = (
    '{"project_name": "md-to-csv", "tech_stack": "python", '
    '"files": ["main.py", "tests/test_main.py"], "entry_point": "main.py", '
    '"test_command": "pytest", "summary": "Converts markdown tables to CSV."}'
)


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


class TestRunPlanning(unittest.TestCase):
    def test_valid_reply_returns_plan(self):
        provider = FakeProvider([VALID_PLAN_REPLY])
        plan = run(run_planning("a CLI that converts markdown tables to CSV", provider))
        self.assertEqual(plan["project_name"], "md-to-csv")
        self.assertEqual(plan["files"], ["main.py", "tests/test_main.py"])
        self.assertEqual(plan["test_command"], "pytest")

    def test_invalid_json_then_valid_recovers_on_retry(self):
        provider = FakeProvider(["not json at all", VALID_PLAN_REPLY])
        plan = run(run_planning("a CLI that converts markdown tables to CSV", provider))
        self.assertEqual(plan["project_name"], "md-to-csv")

    def test_missing_fields_raises_after_retry(self):
        provider = FakeProvider(['{"project_name": "x"}', '{"project_name": "x"}'])
        with self.assertRaises(PlanningError):
            run(run_planning("project", provider))

    def test_empty_files_list_raises_after_retry(self):
        bad_reply = (
            '{"project_name": "x", "tech_stack": "python", "files": [], '
            '"entry_point": "main.py", "test_command": "", "summary": "s"}'
        )
        provider = FakeProvider([bad_reply, bad_reply])
        with self.assertRaises(PlanningError):
            run(run_planning("project", provider))

    def test_empty_description_raises_immediately(self):
        provider = FakeProvider([VALID_PLAN_REPLY])
        with self.assertRaises(PlanningError):
            run(run_planning("   ", provider))


if __name__ == "__main__":
    unittest.main()
