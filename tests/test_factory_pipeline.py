"""
Tests for the Agent Factory spawn pipeline (agent/factory/pipeline.py).

Uses a FakeProvider that returns canned replies — no live API calls.

Run from the project root:
    python -m unittest tests.test_factory_pipeline
"""

import asyncio
import os
import tempfile
import unittest

from agent.factory.pipeline import DAILY_SPAWN_CAP, SpawnCapExceeded, resume_spawn, run_pipeline, start_spawn
from agent.factory.storage import AWAITING_APPROVAL, FAILED, FactoryRepo
from agent.providers.base import BaseProvider, ProviderResponse, TextChunk, TokenUsage


def run(coro):
    return asyncio.run(coro)


VALID_REPORT_REPLY = (
    '{"domain": "SQL migration review", "competencies": ["schema review"], '
    '"tools_available": ["web_search"], "tools_wishlist": ["web_search"], '
    '"design_patterns": ["idempotent migrations"], "sources": []}'
)
VALID_PROMPT_REPLY = '{"system_prompt": "You review database schema changes before deployment."}'


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


class TestRunPipeline(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmp, "factory.db")
        self.repo = FactoryRepo(db_path=self.db_path)
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

    def test_success_reaches_awaiting_approval(self):
        provider = FakeProvider([VALID_REPORT_REPLY, VALID_PROMPT_REPLY])
        task_id = self.repo.create_spawn_task("a specialist that reviews SQL migrations")
        run(
            run_pipeline(
                task_id, "a specialist that reviews SQL migrations", self.repo, provider,
                None, ["web_search"], {"web_search"},
            )
        )
        task = self.repo.get_spawn_task(task_id)
        self.assertEqual(task["status"], AWAITING_APPROVAL)
        self.assertEqual(task["slug"], "sql-migration-review")
        self.assertIn("database schema", task["system_prompt"])
        pending = self.repo.list_pending_approval()
        self.assertEqual(len(pending), 1)

        spec_path = os.path.join(os.environ["TRILLION_AGENT_SPECS_DIR"], "sql-migration-review.md")
        self.assertTrue(os.path.exists(spec_path))
        with open(spec_path) as f:
            content = f.read()
        self.assertIn("sql-migration-review", content)
        self.assertIn("database schema", content)

    def test_research_failure_marks_task_failed(self):
        provider = FakeProvider(["not json", "still not json"])
        task_id = self.repo.create_spawn_task("role")
        run(run_pipeline(task_id, "role", self.repo, provider, None, [], set()))
        task = self.repo.get_spawn_task(task_id)
        self.assertEqual(task["status"], FAILED)
        self.assertIsNotNone(task["failure_reason"])

    def test_draft_failure_marks_task_failed(self):
        provider = FakeProvider([VALID_REPORT_REPLY, "not json", "still not json"])
        task_id = self.repo.create_spawn_task("role")
        run(run_pipeline(task_id, "role", self.repo, provider, None, [], set()))
        task = self.repo.get_spawn_task(task_id)
        self.assertEqual(task["status"], FAILED)


class TestResumeSpawn(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmp, "factory.db")
        self.repo = FactoryRepo(db_path=self.db_path)
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

    def test_resume_after_rejection_reuses_task_id(self):
        async def scenario():
            provider = FakeProvider([VALID_REPORT_REPLY, VALID_PROMPT_REPLY, VALID_REPORT_REPLY, VALID_PROMPT_REPLY])
            bg = set()
            task_id = start_spawn(
                "a specialist that reviews SQL migrations",
                self.repo, provider, None, ["web_search"], {"web_search"},
                background_tasks=bg,
            )
            await asyncio.gather(*bg)
            self.assertEqual(self.repo.count_spawns_today(), 1)

            status = self.repo.reject(task_id, "needs more detail on tool usage")
            self.assertEqual(status, "PENDING")

            resume_spawn(
                task_id, self.repo, provider, None, ["web_search"], {"web_search"},
                background_tasks=bg,
            )
            await asyncio.gather(*bg)
            return task_id

        task_id = run(scenario())
        task = self.repo.get_spawn_task(task_id)
        self.assertEqual(task["status"], AWAITING_APPROVAL)
        # Revision reused the same row rather than creating a new spawn.
        self.assertEqual(self.repo.count_spawns_today(), 1)


class TestStartSpawn(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmp, "factory.db")
        self.repo = FactoryRepo(db_path=self.db_path)
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

    def test_cap_exceeded_raises_before_creating_task(self):
        for _ in range(DAILY_SPAWN_CAP):
            self.repo.create_spawn_task("filler role")
        bg = set()
        with self.assertRaises(SpawnCapExceeded):
            start_spawn(
                "one too many", self.repo, FakeProvider([]), None, [], set(), background_tasks=bg
            )
        self.assertEqual(bg, set())

    def test_start_spawn_completes_in_background(self):
        async def scenario():
            provider = FakeProvider([VALID_REPORT_REPLY, VALID_PROMPT_REPLY])
            bg = set()
            task_id = start_spawn(
                "a specialist that reviews SQL migrations",
                self.repo, provider, None, ["web_search"], {"web_search"},
                background_tasks=bg,
            )
            self.assertEqual(len(bg), 1)
            await asyncio.gather(*bg)
            return task_id

        task_id = run(scenario())
        task = self.repo.get_spawn_task(task_id)
        self.assertEqual(task["status"], AWAITING_APPROVAL)


if __name__ == "__main__":
    unittest.main()
