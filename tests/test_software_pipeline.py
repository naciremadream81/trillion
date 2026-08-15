"""
Tests for the Software Factory build pipeline (agent/factory/software/pipeline.py).

Uses a FakeProvider that returns canned replies — no live API calls. Mirrors
tests/test_factory_pipeline.py's structure.

Run from the project root:
    python -m unittest tests.test_software_pipeline
"""

import asyncio
import os
import tempfile
import unittest
from unittest.mock import patch

from agent.config import Settings
from agent.factory.software.pipeline import (
    CODING_DONE_SENTINEL,
    BudgetCapExceeded,
    BuildCapExceeded,
    FactoryPaused,
    run_build_pipeline,
    start_build,
)
from agent.factory.software.storage import BUILT, FAILED, BuildRepo
from agent.providers.base import BaseProvider, ProviderResponse, TextChunk, TokenUsage


def run(coro):
    return asyncio.run(coro)


ARCHITECTURE_REPLY = "# Architecture\n\nA single main.py handles parsing and CSV output."
QA_PASS_REPLY = '{"result": "PASS", "feedback": "meets acceptance criteria"}'
QA_FAIL_REPLY = '{"result": "FAIL", "feedback": "missing CSV header row"}'
INTEGRATION_READY_REPLY = '{"verdict": "READY", "notes": "all tasks passed, tests green"}'
INTEGRATION_NEEDS_WORK_REPLY = '{"verdict": "NEEDS_WORK", "notes": "tests are failing"}'
CODING_DONE_REPLY = CODING_DONE_SENTINEL

VALID_PLAN_REPLY = (
    '{"project_name": "md-to-csv", "tech_stack": "python", '
    '"files": ["main.py"], "entry_point": "main.py", '
    '"test_command": "python3 -c \\"print(\'ok\')\\"", "summary": "Converts markdown tables to CSV.", '
    '"tasks": [{"title": "Implement CLI", "description": "Write main.py.", '
    '"acceptance_criteria": "python main.py works"}]}'
)
VALID_PLAN_REPLY_FAILING_TESTS = (
    '{"project_name": "md-to-csv", "tech_stack": "python", '
    '"files": ["main.py"], "entry_point": "main.py", '
    '"test_command": "python3 -c \\"import sys; sys.exit(1)\\"", "summary": "Converts markdown tables to CSV.", '
    '"tasks": [{"title": "Implement CLI", "description": "Write main.py.", '
    '"acceptance_criteria": "python main.py works"}]}'
)
VALID_PLAN_REPLY_NO_TESTS = (
    '{"project_name": "md-to-csv", "tech_stack": "python", '
    '"files": ["main.py"], "entry_point": "main.py", '
    '"test_command": "", "summary": "Converts markdown tables to CSV.", '
    '"tasks": [{"title": "Implement CLI", "description": "Write main.py.", '
    '"acceptance_criteria": "python main.py works"}]}'
)
MULTI_TASK_PLAN_REPLY = (
    '{"project_name": "md-to-csv", "tech_stack": "python", '
    '"files": ["main.py", "csv_writer.py"], "entry_point": "main.py", '
    '"test_command": "", "summary": "Converts markdown tables to CSV.", '
    '"tasks": ['
    '{"title": "Parse markdown", "description": "Write the parser.", "acceptance_criteria": "parses a table"}, '
    '{"title": "Write CSV", "description": "Write the CSV writer.", "acceptance_criteria": "writes valid CSV"}'
    ']}'
)


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


def make_settings(tmp, **overrides):
    kwargs = dict(
        software_factory_root=os.path.join(tmp, "generated-projects"),
        factory_daily_build_cap=3,
        factory_daily_budget_usd=None,
        factory_paused=False,
    )
    kwargs.update(overrides)
    return Settings(**kwargs)


class FakeUsageRepo:
    def __init__(self, cost_usd: float):
        self.cost_usd = cost_usd

    def usage_since(self, start, end):
        return {"cost_usd": self.cost_usd}


class TestRunBuildPipeline(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmp, "software_factory.db")
        self.repo = BuildRepo(db_path=self.db_path)
        self.settings = make_settings(self.tmp)

    def tearDown(self):
        try:
            os.remove(self.db_path)
        except FileNotFoundError:
            pass

    def test_success_reaches_built(self):
        provider = FakeProvider([
            VALID_PLAN_REPLY, ARCHITECTURE_REPLY, CODING_DONE_REPLY, QA_PASS_REPLY, INTEGRATION_READY_REPLY,
        ])
        task_id = self.repo.create_build_task("a CLI that converts markdown tables to CSV")
        with patch(
            "agent.factory.software.pipeline._run_testing",
            return_value=(True, "exit_code=0\nok\n"),
        ):
            run(run_build_pipeline(
                task_id, "a CLI that converts markdown tables to CSV", self.repo, provider, self.settings
            ))
        task = self.repo.get_build_task(task_id)
        self.assertEqual(task["status"], BUILT)
        self.assertEqual(task["slug"], "md-to-csv")

        project_dir = os.path.join(self.settings.software_factory_root, "md-to-csv")
        self.assertTrue(os.path.isfile(os.path.join(project_dir, "main.py")))
        self.assertTrue(os.path.isfile(os.path.join(project_dir, "README.md")))
        self.assertTrue(os.path.isfile(os.path.join(project_dir, "ARCHITECTURE.md")))

        with open(os.path.join(project_dir, "README.md")) as f:
            readme = f.read()
        self.assertIn("## Architecture", readme)
        self.assertIn("## Tasks", readme)
        self.assertIn("Implement CLI", readme)
        self.assertIn("PASSED", readme)
        self.assertIn("## Integration review", readme)
        self.assertIn("READY", readme)

        task_results = task["plan"]["task_results"]
        self.assertEqual(len(task_results), 1)
        self.assertEqual(task_results[0]["status"], "PASSED")
        self.assertEqual(task_results[0]["attempts"], 1)

    def test_no_test_command_reaches_built_without_provider_call(self):
        provider = FakeProvider([
            VALID_PLAN_REPLY_NO_TESTS, ARCHITECTURE_REPLY, CODING_DONE_REPLY, QA_PASS_REPLY, INTEGRATION_READY_REPLY,
        ])
        task_id = self.repo.create_build_task("project")
        run(run_build_pipeline(task_id, "project", self.repo, provider, self.settings))
        task = self.repo.get_build_task(task_id)
        self.assertEqual(task["status"], BUILT)
        self.assertEqual(task["retry_count"], 0)

    def test_failing_tests_trigger_one_corrective_retry_then_builds(self):
        provider = FakeProvider([
            VALID_PLAN_REPLY_FAILING_TESTS, ARCHITECTURE_REPLY,
            CODING_DONE_REPLY, QA_PASS_REPLY,   # task loop
            CODING_DONE_REPLY,                  # whole-project corrective retry (_run_coding, unchanged)
            INTEGRATION_NEEDS_WORK_REPLY,
        ])
        task_id = self.repo.create_build_task("project")
        run(run_build_pipeline(task_id, "project", self.repo, provider, self.settings))
        task = self.repo.get_build_task(task_id)
        self.assertEqual(task["status"], BUILT)
        self.assertEqual(task["retry_count"], 1)

        project_dir = os.path.join(self.settings.software_factory_root, "md-to-csv")
        with open(os.path.join(project_dir, "README.md")) as f:
            readme = f.read()
        self.assertIn("FAILED", readme)
        self.assertIn("NEEDS_WORK", readme)

    def test_task_fails_qa_once_then_passes_on_retry(self):
        provider = FakeProvider([
            VALID_PLAN_REPLY_NO_TESTS, ARCHITECTURE_REPLY,
            CODING_DONE_REPLY, QA_FAIL_REPLY,   # attempt 1: dev, QA fails
            CODING_DONE_REPLY, QA_PASS_REPLY,   # attempt 2: dev, QA passes
            INTEGRATION_READY_REPLY,
        ])
        task_id = self.repo.create_build_task("project")
        run(run_build_pipeline(task_id, "project", self.repo, provider, self.settings))
        task = self.repo.get_build_task(task_id)
        self.assertEqual(task["status"], BUILT)
        task_results = task["plan"]["task_results"]
        self.assertEqual(task_results[0]["status"], "PASSED")
        self.assertEqual(task_results[0]["attempts"], 2)

    def test_task_blocked_after_max_retries_still_reaches_built(self):
        provider = FakeProvider([
            VALID_PLAN_REPLY_NO_TESTS, ARCHITECTURE_REPLY,
            CODING_DONE_REPLY, QA_FAIL_REPLY,   # attempt 1
            CODING_DONE_REPLY, QA_FAIL_REPLY,   # attempt 2
            CODING_DONE_REPLY, QA_FAIL_REPLY,   # attempt 3
            INTEGRATION_NEEDS_WORK_REPLY,
        ])
        task_id = self.repo.create_build_task("project")
        run(run_build_pipeline(task_id, "project", self.repo, provider, self.settings))
        task = self.repo.get_build_task(task_id)
        self.assertEqual(task["status"], BUILT)  # a blocked task never aborts the build
        task_results = task["plan"]["task_results"]
        self.assertEqual(task_results[0]["status"], "BLOCKED")
        self.assertEqual(task_results[0]["attempts"], 3)

    def test_multi_task_build_records_each_task_result(self):
        provider = FakeProvider([
            MULTI_TASK_PLAN_REPLY, ARCHITECTURE_REPLY,
            CODING_DONE_REPLY, QA_PASS_REPLY,  # task 1
            CODING_DONE_REPLY, QA_PASS_REPLY,  # task 2
            INTEGRATION_READY_REPLY,
        ])
        task_id = self.repo.create_build_task("project")
        run(run_build_pipeline(task_id, "project", self.repo, provider, self.settings))
        task = self.repo.get_build_task(task_id)
        self.assertEqual(task["status"], BUILT)
        task_results = task["plan"]["task_results"]
        self.assertEqual(len(task_results), 2)
        self.assertTrue(all(r["status"] == "PASSED" for r in task_results))

    def test_planning_failure_marks_task_failed(self):
        provider = FakeProvider(["not json", "still not json"])
        task_id = self.repo.create_build_task("project")
        run(run_build_pipeline(task_id, "project", self.repo, provider, self.settings))
        task = self.repo.get_build_task(task_id)
        self.assertEqual(task["status"], FAILED)
        self.assertIsNotNone(task["failure_reason"])

    def test_budget_exceeded_mid_pipeline_marks_task_failed(self):
        settings = make_settings(self.tmp, factory_daily_budget_usd=1.0)
        usage_repo = FakeUsageRepo(cost_usd=5.0)
        provider = FakeProvider([VALID_PLAN_REPLY])
        task_id = self.repo.create_build_task("project")
        run(run_build_pipeline(task_id, "project", self.repo, provider, settings, usage_repo=usage_repo))
        task = self.repo.get_build_task(task_id)
        self.assertEqual(task["status"], FAILED)
        self.assertIn("budget", task["failure_reason"].lower())


class TestStartBuild(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmp, "software_factory.db")
        self.repo = BuildRepo(db_path=self.db_path)
        self.settings = make_settings(self.tmp)

    def tearDown(self):
        try:
            os.remove(self.db_path)
        except FileNotFoundError:
            pass

    def test_start_build_completes_in_background(self):
        async def scenario():
            provider = FakeProvider([
                VALID_PLAN_REPLY, ARCHITECTURE_REPLY, CODING_DONE_REPLY, QA_PASS_REPLY, INTEGRATION_READY_REPLY,
            ])
            bg = set()
            task_id = start_build(
                "a CLI that converts markdown tables to CSV", self.repo, provider, self.settings,
                background_tasks=bg,
            )
            self.assertEqual(len(bg), 1)
            with patch(
                "agent.factory.software.pipeline._run_testing",
                return_value=(True, "exit_code=0\nok\n"),
            ):
                await asyncio.gather(*bg)
            return task_id

        task_id = run(scenario())
        task = self.repo.get_build_task(task_id)
        self.assertEqual(task["status"], BUILT)

    def test_paused_refuses_before_creating_task(self):
        settings = make_settings(self.tmp, factory_paused=True)
        bg = set()
        with self.assertRaises(FactoryPaused):
            start_build("project", self.repo, FakeProvider([]), settings, background_tasks=bg)
        self.assertEqual(bg, set())
        self.assertEqual(self.repo.count_builds_today(), 0)

    def test_build_cap_exceeded_refuses_before_creating_task(self):
        settings = make_settings(self.tmp, factory_daily_build_cap=2)
        self.repo.create_build_task("filler 1")
        self.repo.create_build_task("filler 2")
        bg = set()
        with self.assertRaises(BuildCapExceeded):
            start_build("one too many", self.repo, FakeProvider([]), settings, background_tasks=bg)
        self.assertEqual(bg, set())
        self.assertEqual(self.repo.count_builds_today(), 2)

    def test_budget_cap_exceeded_refuses_before_creating_task(self):
        settings = make_settings(self.tmp, factory_daily_budget_usd=1.0)
        usage_repo = FakeUsageRepo(cost_usd=5.0)
        bg = set()
        with self.assertRaises(BudgetCapExceeded):
            start_build(
                "project", self.repo, FakeProvider([]), settings,
                background_tasks=bg, usage_repo=usage_repo,
            )
        self.assertEqual(bg, set())
        self.assertEqual(self.repo.count_builds_today(), 0)


if __name__ == "__main__":
    unittest.main()
