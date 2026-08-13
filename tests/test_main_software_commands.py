"""
Tests for the Software Factory slash commands wired into main.py
(/build, /builds).

Uses a FakeProvider — no live API calls. Mirrors
tests/test_main_factory_commands.py's structure.

Run from the project root:
    python -m unittest tests.test_main_software_commands
"""

import asyncio
import os
import tempfile
import unittest

import main as main_module
from agent.config import Settings
from agent.factory.software.pipeline import CODING_DONE_SENTINEL
from agent.factory.software.storage import BUILT, BuildRepo
from agent.providers.base import BaseProvider, ProviderResponse, TextChunk, TokenUsage

VALID_PLAN_REPLY = (
    '{"project_name": "md-to-csv", "tech_stack": "python", '
    '"files": ["main.py"], "entry_point": "main.py", '
    '"test_command": "", "summary": "Converts markdown tables to CSV.", '
    '"tasks": [{"title": "Implement CLI", "description": "Write main.py.", '
    '"acceptance_criteria": "python main.py works"}]}'
)

ARCHITECTURE_REPLY = "# Architecture\n\nOne module, main.py."
QA_PASS_REPLY = '{"result": "PASS", "feedback": "meets acceptance criteria"}'
INTEGRATION_READY_REPLY = '{"verdict": "READY", "notes": "all good"}'


class FakeProvider(BaseProvider):
    def __init__(self, replies):
        self._replies = list(replies)

    @property
    def model_name(self):
        return "fake-model"

    async def stream(self, messages, system, tools=None):
        text = self._replies.pop(0) if self._replies else CODING_DONE_SENTINEL
        yield TextChunk(text=text)
        yield ProviderResponse(text=text, tool_calls=[], usage=TokenUsage(), model=self.model_name)


class TestSoftwareFactoryCliCommands(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmp, "software_factory.db")
        self.repo = BuildRepo(db_path=self.db_path)
        self.settings = Settings(
            software_factory_root=os.path.join(self.tmp, "generated-projects"),
            factory_daily_build_cap=3,
            factory_daily_budget_usd=None,
            factory_paused=False,
        )

    def tearDown(self):
        try:
            os.remove(self.db_path)
        except FileNotFoundError:
            pass

    def test_build_then_builds_lists_it(self):
        async def scenario():
            provider = FakeProvider([
                VALID_PLAN_REPLY, ARCHITECTURE_REPLY, CODING_DONE_SENTINEL, QA_PASS_REPLY, INTEGRATION_READY_REPLY,
            ])
            sf = main_module.SoftwareFactoryContext(
                repo=self.repo, provider=provider, settings=self.settings, background_tasks=set()
            )
            main_module.handle_slash(
                "/build a CLI that converts markdown tables to CSV", None, "claude", None, sf
            )
            self.assertEqual(len(sf.background_tasks), 1)
            await asyncio.gather(*sf.background_tasks)

            task = self.repo.get_build_task(1)
            self.assertEqual(task["status"], BUILT)

            main_module.handle_slash("/builds", None, "claude", None, sf)  # should not crash

        asyncio.run(scenario())

    def test_builds_shows_task_summary_line(self):
        async def scenario():
            provider = FakeProvider([
                VALID_PLAN_REPLY, ARCHITECTURE_REPLY, CODING_DONE_SENTINEL, QA_PASS_REPLY, INTEGRATION_READY_REPLY,
            ])
            sf = main_module.SoftwareFactoryContext(
                repo=self.repo, provider=provider, settings=self.settings, background_tasks=set()
            )
            main_module.handle_slash(
                "/build a CLI that converts markdown tables to CSV", None, "claude", None, sf
            )
            await asyncio.gather(*sf.background_tasks)

            with main_module.console.capture() as capture:
                main_module.handle_slash("/builds", None, "claude", None, sf)
            self.assertIn("tasks: 1/1 passed, 0 blocked", capture.get())

        asyncio.run(scenario())

    def test_build_without_description_shows_usage_not_crash(self):
        sf = main_module.SoftwareFactoryContext(
            repo=self.repo, provider=FakeProvider([]), settings=self.settings, background_tasks=set()
        )
        main_module.handle_slash("/build", None, "claude", None, sf)
        self.assertEqual(len(sf.background_tasks), 0)

    def test_build_refused_when_paused_reports_error_not_crash(self):
        paused_settings = Settings(
            software_factory_root=self.settings.software_factory_root,
            factory_daily_build_cap=3,
            factory_daily_budget_usd=None,
            factory_paused=True,
        )
        sf = main_module.SoftwareFactoryContext(
            repo=self.repo, provider=FakeProvider([]), settings=paused_settings, background_tasks=set()
        )
        main_module.handle_slash("/build a project", None, "claude", None, sf)
        self.assertEqual(len(sf.background_tasks), 0)
        self.assertEqual(self.repo.count_builds_today(), 0)

    def test_builds_with_no_history_reports_none_not_crash(self):
        sf = main_module.SoftwareFactoryContext(
            repo=self.repo, provider=FakeProvider([]), settings=self.settings, background_tasks=set()
        )
        main_module.handle_slash("/builds", None, "claude", None, sf)

    def test_software_factory_commands_without_context_dont_crash(self):
        main_module.handle_slash("/build something", None, "claude", None, None)
        main_module.handle_slash("/builds", None, "claude", None, None)


if __name__ == "__main__":
    unittest.main()
