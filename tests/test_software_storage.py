"""
Tests for the Software Factory storage layer (agent/factory/software/storage.py).

Run from the project root:
    python -m unittest tests.test_software_storage
"""

import os
import tempfile
import unittest

from agent.factory.software.storage import (
    BUILT,
    CODING,
    DOCS,
    FAILED,
    PENDING,
    PLANNING,
    SCAFFOLDING,
    TESTING,
    BuildRepo,
    InvalidTransition,
)


class TestSoftwareStorage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmp, "software_factory.db")
        self.repo = BuildRepo(db_path=self.db_path)

    def tearDown(self):
        try:
            os.remove(self.db_path)
        except FileNotFoundError:
            pass

    def test_create_and_get_build_task(self):
        task_id = self.repo.create_build_task("a CLI that converts markdown tables to CSV")
        task = self.repo.get_build_task(task_id)
        self.assertEqual(task["status"], PENDING)
        self.assertEqual(task["retry_count"], 0)
        self.assertIsNone(task["slug"])
        self.assertIsNone(task["plan"])

    def test_status_transitions(self):
        task_id = self.repo.create_build_task("project")
        self.repo.update_status(task_id, PLANNING)
        self.assertEqual(self.repo.get_build_task(task_id)["status"], PLANNING)

    def test_set_plan_moves_to_scaffolding(self):
        task_id = self.repo.create_build_task("project")
        self.repo.update_status(task_id, PLANNING)
        plan = {"tech_stack": "python", "files": ["main.py"], "test_command": "pytest"}
        self.repo.set_plan(task_id, slug="md-to-csv", plan=plan)
        task = self.repo.get_build_task(task_id)
        self.assertEqual(task["status"], SCAFFOLDING)
        self.assertEqual(task["slug"], "md-to-csv")
        self.assertEqual(task["plan"], plan)

    def test_legal_transition_chain_to_built(self):
        task_id = self.repo.create_build_task("project")
        self.repo.update_status(task_id, PLANNING)
        self.repo.set_plan(task_id, slug="s", plan={"files": []})
        self.repo.update_status(task_id, CODING)
        self.repo.update_status(task_id, TESTING)
        self.repo.update_status(task_id, DOCS)
        self.repo.update_status(task_id, BUILT)
        self.assertEqual(self.repo.get_build_task(task_id)["status"], BUILT)

    def test_retry_coding_bumps_count_and_transitions_back(self):
        task_id = self.repo.create_build_task("project")
        self.repo.update_status(task_id, PLANNING)
        self.repo.set_plan(task_id, slug="s", plan={"files": []})
        self.repo.update_status(task_id, CODING)
        self.repo.update_status(task_id, TESTING)
        new_count = self.repo.retry_coding(task_id)
        self.assertEqual(new_count, 1)
        task = self.repo.get_build_task(task_id)
        self.assertEqual(task["status"], CODING)
        self.assertEqual(task["retry_count"], 1)

    def test_set_error_transitions_to_failed(self):
        task_id = self.repo.create_build_task("project")
        self.repo.set_error(task_id, "boom")
        task = self.repo.get_build_task(task_id)
        self.assertEqual(task["status"], FAILED)
        self.assertEqual(task["failure_reason"], "boom")

    def test_illegal_direct_jump_raises(self):
        task_id = self.repo.create_build_task("project")
        with self.assertRaises(InvalidTransition):
            self.repo.update_status(task_id, BUILT)
        self.assertEqual(self.repo.get_build_task(task_id)["status"], PENDING)

    def test_update_status_from_terminal_status_raises(self):
        task_id = self.repo.create_build_task("project")
        self.repo.set_error(task_id, "boom")
        with self.assertRaises(InvalidTransition):
            self.repo.update_status(task_id, PLANNING)

    def test_count_builds_today(self):
        self.assertEqual(self.repo.count_builds_today(), 0)
        self.repo.create_build_task("project 1")
        self.repo.create_build_task("project 2")
        self.assertEqual(self.repo.count_builds_today(), 2)

    def test_slug_taken(self):
        task_id = self.repo.create_build_task("project")
        self.assertFalse(self.repo.slug_taken("md-to-csv"))
        self.repo.update_status(task_id, PLANNING)
        self.repo.set_plan(task_id, slug="md-to-csv", plan={"files": []})
        self.assertTrue(self.repo.slug_taken("md-to-csv"))

    def test_list_recent_builds_newest_first_any_status(self):
        t1 = self.repo.create_build_task("project 1")
        t2 = self.repo.create_build_task("project 2")
        self.repo.set_error(t2, "boom")
        recent = self.repo.list_recent_builds()
        self.assertEqual([t["id"] for t in recent], [t2, t1])
        self.assertEqual(recent[0]["status"], FAILED)


if __name__ == "__main__":
    unittest.main()
