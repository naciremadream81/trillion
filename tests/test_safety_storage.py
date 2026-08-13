"""Tier 6: SafetyRepo — the pending-action state machine and the audit log."""

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from agent.safety import storage
from agent.safety.storage import InvalidTransition, SafetyRepo


class SafetyRepoTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = SafetyRepo(os.path.join(self.tmp.name, "safety.db"))

    def _park(self, **overrides):
        kwargs = dict(
            tool_name="write_project_file",
            arguments={"relative_path": "a.md", "content": "hi"},
            summary="write_project_file(relative_path='a.md', content='hi')",
            risk="consequential",
            history_index=2,
            ttl_seconds=900,
        )
        kwargs.update(overrides)
        return self.repo.create_pending(**kwargs)


class CreateAndReadTests(SafetyRepoTestCase):
    def test_create_returns_an_id_and_stores_the_row(self):
        action_id = self._park()
        action = self.repo.get(action_id)
        self.assertEqual(action["status"], storage.PENDING)
        self.assertEqual(action["tool_name"], "write_project_file")
        self.assertEqual(action["history_index"], 2)

    def test_arguments_round_trip_as_a_dict(self):
        action = self.repo.get(self._park())
        self.assertEqual(action["arguments"]["relative_path"], "a.md")

    def test_get_unknown_id_returns_none(self):
        self.assertIsNone(self.repo.get(999))

    def test_list_pending_does_not_mutate(self):
        self._park()
        self.assertEqual(len(self.repo.list_pending()), 1)
        self.assertEqual(len(self.repo.list_pending()), 1)

    def test_digest_is_stable_for_the_same_call(self):
        a = self.repo.get(self._park())
        b = self.repo.get(self._park())
        self.assertEqual(a["digest"], b["digest"])

    def test_digest_changes_when_arguments_change(self):
        a = self.repo.get(self._park())
        b = self.repo.get(self._park(arguments={"relative_path": "b.md"}))
        self.assertNotEqual(a["digest"], b["digest"])

    def test_digest_ignores_key_order(self):
        one = storage.digest_for("t", {"a": 1, "b": 2})
        two = storage.digest_for("t", {"b": 2, "a": 1})
        self.assertEqual(one, two)


class TransitionTests(SafetyRepoTestCase):
    def test_approve_then_execute(self):
        action_id = self._park()
        self.repo.approve(action_id)
        self.assertEqual(self.repo.get(action_id)["status"], storage.APPROVED)
        self.repo.mark_executed(action_id, "wrote 2 bytes")
        self.assertEqual(self.repo.get(action_id)["status"], storage.EXECUTED)

    def test_deny_records_the_reason(self):
        action_id = self._park()
        self.repo.deny(action_id, reason="wrong path")
        action = self.repo.get(action_id)
        self.assertEqual(action["status"], storage.DENIED)
        self.assertEqual(action["reason"], "wrong path")

    def test_cannot_execute_without_approving(self):
        with self.assertRaises(InvalidTransition):
            self.repo.mark_executed(self._park())

    def test_cannot_approve_twice(self):
        action_id = self._park()
        self.repo.approve(action_id)
        with self.assertRaises(InvalidTransition):
            self.repo.approve(action_id)

    def test_denied_is_terminal(self):
        # A denied action is re-asked as a new action, never revived.
        action_id = self._park()
        self.repo.deny(action_id)
        with self.assertRaises(InvalidTransition):
            self.repo.approve(action_id)

    def test_unknown_id_raises_rather_than_silently_passing(self):
        with self.assertRaises(InvalidTransition):
            self.repo.approve(4242)


class ExpiryTests(SafetyRepoTestCase):
    def test_expire_stale_moves_past_due_actions(self):
        stale = self._park(ttl_seconds=-1)
        fresh = self._park(ttl_seconds=900)
        self.assertEqual(self.repo.expire_stale(), 1)
        self.assertEqual(self.repo.get(stale)["status"], storage.EXPIRED)
        self.assertEqual(self.repo.get(fresh)["status"], storage.PENDING)

    def test_expire_stale_is_a_no_op_when_nothing_is_due(self):
        self._park()
        self.assertEqual(self.repo.expire_stale(), 0)

    def test_expired_action_drops_out_of_list_pending(self):
        self._park(ttl_seconds=-1)
        self.repo.expire_stale()
        self.assertEqual(self.repo.list_pending(), [])

    def test_expired_is_terminal(self):
        action_id = self._park(ttl_seconds=-1)
        self.repo.expire_stale()
        with self.assertRaises(InvalidTransition):
            self.repo.approve(action_id)

    def test_expires_at_reflects_the_ttl(self):
        action = self.repo.get(self._park(ttl_seconds=60))
        expires = datetime.fromisoformat(action["expires_at"])
        created = datetime.fromisoformat(action["created_at"])
        self.assertAlmostEqual(
            (expires - created).total_seconds(), 60, delta=1
        )


class AuditLogTests(SafetyRepoTestCase):
    def test_parking_an_action_is_audited(self):
        self._park()
        events = [e["event"] for e in self.repo.recent_audit()]
        self.assertIn(storage.EVENT_GATED, events)

    def test_the_whole_lifecycle_is_audited(self):
        action_id = self._park()
        self.repo.approve(action_id)
        self.repo.mark_executed(action_id)
        events = [e["event"] for e in self.repo.recent_audit()]
        for expected in (
            storage.EVENT_GATED,
            storage.EVENT_APPROVED,
            storage.EVENT_EXECUTED,
        ):
            self.assertIn(expected, events)

    def test_recent_audit_is_newest_first(self):
        action_id = self._park()
        self.repo.approve(action_id)
        self.assertEqual(
            self.repo.recent_audit()[0]["event"], storage.EVENT_APPROVED
        )

    def test_detail_round_trips_as_a_dict(self):
        self.repo.log("custom", tool_name="t", detail={"k": "v"})
        self.assertEqual(self.repo.recent_audit()[0]["detail"], {"k": "v"})

    def test_log_never_raises_on_a_broken_database(self):
        # Losing an audit line is bad; taking down the conversation to report
        # it is worse.
        broken = SafetyRepo(os.path.join(self.tmp.name, "gone.db"))
        broken.db_path = os.path.join(self.tmp.name, "no-such-dir", "x.db")
        broken.log("custom")  # must not raise

    def test_recent_audit_respects_the_limit(self):
        for _ in range(5):
            self.repo.log("custom")
        self.assertEqual(len(self.repo.recent_audit(limit=3)), 3)


class SchemaTests(SafetyRepoTestCase):
    def test_schema_is_applied_idempotently(self):
        path = os.path.join(self.tmp.name, "twice.db")
        SafetyRepo(path)
        SafetyRepo(path)  # must not raise

    def test_default_db_path_honors_the_env_override(self):
        original = os.environ.get("TRILLION_SAFETY_DB")
        os.environ["TRILLION_SAFETY_DB"] = "/tmp/custom-safety.db"
        try:
            self.assertEqual(storage.default_db_path(), "/tmp/custom-safety.db")
        finally:
            if original is None:
                del os.environ["TRILLION_SAFETY_DB"]
            else:
                os.environ["TRILLION_SAFETY_DB"] = original


if __name__ == "__main__":
    unittest.main()
