"""
Tests for the Agent Factory storage layer (agent/factory/storage.py).

Run from the project root:
    python -m unittest tests.test_factory_storage
"""

import os
import tempfile
import unittest

from agent.factory.storage import (
    APPROVED,
    AWAITING_APPROVAL,
    FAILED,
    PENDING,
    RESEARCHING,
    FactoryRepo,
    InvalidTransition,
)


class TestFactoryStorage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmp, "factory.db")
        self.repo = FactoryRepo(db_path=self.db_path)

    def tearDown(self):
        try:
            os.remove(self.db_path)
        except FileNotFoundError:
            pass

    def test_create_and_get_spawn_task(self):
        task_id = self.repo.create_spawn_task("a specialist that reviews SQL migrations")
        task = self.repo.get_spawn_task(task_id)
        self.assertEqual(task["status"], PENDING)
        self.assertEqual(task["revision_count"], 0)
        self.assertIsNone(task["slug"])

    def test_status_transitions(self):
        task_id = self.repo.create_spawn_task("role")
        self.repo.update_status(task_id, "RESEARCHING")
        self.assertEqual(self.repo.get_spawn_task(task_id)["status"], "RESEARCHING")

    def test_research_report_round_trip(self):
        task_id = self.repo.create_spawn_task("role")
        report_id = self.repo.save_research_report(
            task_id,
            domain="SQL migrations",
            competencies=["schema review", "index analysis"],
            tools_available=["web_search"],
            tools_wishlist=["query_analytics"],
            design_patterns=["idempotent migrations"],
            sources=["https://example.com"],
        )
        report = self.repo.get_research_report(report_id)
        self.assertEqual(report["domain"], "SQL migrations")
        self.assertEqual(report["competencies"], ["schema review", "index analysis"])
        task = self.repo.get_spawn_task(task_id)
        self.assertEqual(task["research_report_id"], report_id)

    def test_set_draft_moves_to_awaiting_approval(self):
        task_id = self.repo.create_spawn_task("role")
        self.repo.set_draft(
            task_id, slug="sql-reviewer", system_prompt="You review SQL.", tool_allowlist=["web_search"]
        )
        task = self.repo.get_spawn_task(task_id)
        self.assertEqual(task["status"], AWAITING_APPROVAL)
        self.assertEqual(task["slug"], "sql-reviewer")
        pending = self.repo.list_pending_approval()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["id"], task_id)

    def test_approve_creates_spawned_agent(self):
        task_id = self.repo.create_spawn_task("role")
        self.repo.set_draft(
            task_id, slug="sql-reviewer", system_prompt="You review SQL.", tool_allowlist=["web_search"]
        )
        agent_id = self.repo.approve(task_id)
        self.assertIsInstance(agent_id, int)
        task = self.repo.get_spawn_task(task_id)
        self.assertEqual(task["status"], APPROVED)
        active = self.repo.list_active_agents()
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["slug"], "sql-reviewer")
        self.assertEqual(active[0]["tool_allowlist"], ["web_search"])

    def test_approve_rejects_reserved_slug(self):
        task_id = self.repo.create_spawn_task("role")
        self.repo.set_draft(task_id, slug="factory", system_prompt="x", tool_allowlist=[])
        with self.assertRaises(ValueError):
            self.repo.approve(task_id)

    def test_approve_rejects_duplicate_slug(self):
        t1 = self.repo.create_spawn_task("role 1")
        self.repo.set_draft(t1, slug="sql-reviewer", system_prompt="x", tool_allowlist=[])
        self.repo.approve(t1)

        t2 = self.repo.create_spawn_task("role 2")
        self.repo.set_draft(t2, slug="sql-reviewer", system_prompt="y", tool_allowlist=[])
        with self.assertRaises(ValueError):
            self.repo.approve(t2)

    def test_approve_requires_awaiting_approval_status(self):
        task_id = self.repo.create_spawn_task("role")
        with self.assertRaises(ValueError):
            self.repo.approve(task_id)

    def test_reject_allows_revision_until_cap(self):
        task_id = self.repo.create_spawn_task("role")
        self.repo.set_draft(task_id, slug="s", system_prompt="x", tool_allowlist=[])
        status = self.repo.reject(task_id, "too vague")
        self.assertEqual(status, PENDING)
        self.assertEqual(self.repo.get_spawn_task(task_id)["revision_count"], 1)

    def test_reject_auto_fails_after_max_revisions(self):
        task_id = self.repo.create_spawn_task("role")
        for _ in range(3):
            self.repo.set_draft(task_id, slug="s", system_prompt="x", tool_allowlist=[])
            status = self.repo.reject(task_id, "still not right")
        self.assertEqual(status, FAILED)
        self.assertEqual(self.repo.get_spawn_task(task_id)["status"], FAILED)

    def test_count_spawns_today(self):
        self.assertEqual(self.repo.count_spawns_today(), 0)
        self.repo.create_spawn_task("role 1")
        self.repo.create_spawn_task("role 2")
        self.assertEqual(self.repo.count_spawns_today(), 2)

    def test_disable_agent_removes_from_active_list(self):
        task_id = self.repo.create_spawn_task("role")
        self.repo.set_draft(task_id, slug="s", system_prompt="x", tool_allowlist=[])
        self.repo.approve(task_id)
        self.repo.disable_agent("s")
        self.assertEqual(self.repo.list_active_agents(), [])

    def test_legal_transition_chain_succeeds(self):
        task_id = self.repo.create_spawn_task("role")
        self.repo.update_status(task_id, RESEARCHING)
        self.repo.update_status(task_id, "DRAFTING_SPEC")
        self.repo.update_status(task_id, "WRITING_PROMPT")
        self.repo.set_draft(task_id, slug="s", system_prompt="x", tool_allowlist=[])
        self.assertEqual(self.repo.get_spawn_task(task_id)["status"], AWAITING_APPROVAL)

    def test_illegal_direct_jump_to_approved_raises(self):
        task_id = self.repo.create_spawn_task("role")
        with self.assertRaises(InvalidTransition):
            self.repo.update_status(task_id, APPROVED)
        # The illegal write must not have landed.
        self.assertEqual(self.repo.get_spawn_task(task_id)["status"], PENDING)

    def test_update_status_from_terminal_status_raises(self):
        task_id = self.repo.create_spawn_task("role")
        self.repo.update_status(task_id, FAILED, failure_reason="boom")
        with self.assertRaises(InvalidTransition):
            self.repo.update_status(task_id, RESEARCHING)


if __name__ == "__main__":
    unittest.main()
