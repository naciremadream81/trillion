"""
Tests for HeartbeatRepo (agent/heartbeat/storage.py).

Run from the project root:
    python -m unittest tests.test_heartbeat_storage
"""

import os
import tempfile
import unittest
from datetime import datetime, timezone

from agent.heartbeat.storage import HeartbeatRepo, is_quiet_hours, _next_quiet_hours_end


def _dt(hour: int) -> datetime:
    return datetime(2026, 1, 1, hour, 30, tzinfo=timezone.utc)


class TestIsQuietHours(unittest.TestCase):
    def test_disabled_when_start_equals_end(self):
        self.assertFalse(is_quiet_hours(22, 22, _dt(23)))

    def test_simple_range(self):
        self.assertTrue(is_quiet_hours(1, 8, _dt(3)))
        self.assertFalse(is_quiet_hours(1, 8, _dt(9)))

    def test_wraps_past_midnight(self):
        self.assertTrue(is_quiet_hours(22, 8, _dt(23)))
        self.assertTrue(is_quiet_hours(22, 8, _dt(2)))
        self.assertFalse(is_quiet_hours(22, 8, _dt(12)))


class TestNextQuietHoursEnd(unittest.TestCase):
    def test_later_today(self):
        result = _next_quiet_hours_end(8, _dt(2))
        self.assertEqual(result.hour, 8)
        self.assertEqual(result.day, 1)

    def test_rolls_to_tomorrow_when_already_past(self):
        result = _next_quiet_hours_end(8, _dt(10))
        self.assertEqual(result.hour, 8)
        self.assertEqual(result.day, 2)


class TestHeartbeatRepoNotices(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = HeartbeatRepo(db_path=os.path.join(self._tmp.name, "heartbeat.db"))

    def tearDown(self):
        self._tmp.cleanup()

    def test_non_quiet_hours_notice_delivers_immediately(self):
        notice_id = self.repo.add_notice(
            check_name="ci_failure",
            severity="warning",
            message="build broke",
            quiet_hours_start=22,
            quiet_hours_end=22,  # disabled
        )
        active = self.repo.list_active_notices()
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["id"], notice_id)

    def test_critical_notice_bypasses_quiet_hours(self):
        now = datetime.now(timezone.utc)
        self.repo.add_notice(
            check_name="ci_failure",
            severity="critical",
            message="production is down",
            quiet_hours_start=(now.hour - 1) % 24,
            quiet_hours_end=(now.hour + 1) % 24,
        )
        self.assertEqual(len(self.repo.list_active_notices()), 1)

    def test_non_critical_notice_held_during_quiet_hours(self):
        now = datetime.now(timezone.utc)
        self.repo.add_notice(
            check_name="stale_pr",
            severity="info",
            message="PR open 48h",
            quiet_hours_start=(now.hour - 1) % 24,
            quiet_hours_end=(now.hour + 1) % 24,
        )
        self.assertEqual(self.repo.list_active_notices(now=now), [])

    def test_dismiss_hides_notice(self):
        notice_id = self.repo.add_notice(
            check_name="dormant_repo", severity="info", message="quiet for 30 days",
            quiet_hours_start=0, quiet_hours_end=0,
        )
        self.assertTrue(self.repo.dismiss(notice_id))
        self.assertEqual(self.repo.list_active_notices(), [])

    def test_dismiss_unknown_notice_returns_false(self):
        self.assertFalse(self.repo.dismiss(999))

    def test_dismiss_twice_returns_false_second_time(self):
        notice_id = self.repo.add_notice(
            check_name="x", severity="info", message="y",
            quiet_hours_start=0, quiet_hours_end=0,
        )
        self.assertTrue(self.repo.dismiss(notice_id))
        self.assertFalse(self.repo.dismiss(notice_id))


class TestHeartbeatRepoScheduling(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = HeartbeatRepo(db_path=os.path.join(self._tmp.name, "heartbeat.db"))

    def tearDown(self):
        self._tmp.cleanup()

    def test_next_due_at_defaults_to_none(self):
        self.assertIsNone(self.repo.get_next_due_at("ci_failure"))

    def test_set_and_get_next_due_at(self):
        when = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
        self.repo.set_next_due_at("ci_failure", when)
        self.assertEqual(self.repo.get_next_due_at("ci_failure"), when)

    def test_set_next_due_at_overwrites(self):
        first = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
        second = datetime(2026, 1, 2, 12, tzinfo=timezone.utc)
        self.repo.set_next_due_at("ci_failure", first)
        self.repo.set_next_due_at("ci_failure", second)
        self.assertEqual(self.repo.get_next_due_at("ci_failure"), second)


class TestHeartbeatRepoCursor(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = HeartbeatRepo(db_path=os.path.join(self._tmp.name, "heartbeat.db"))

    def tearDown(self):
        self._tmp.cleanup()

    def test_cursor_defaults_to_empty_dict(self):
        self.assertEqual(self.repo.get_cursor("ci_failure"), {})

    def test_set_and_get_cursor(self):
        self.repo.set_cursor("ci_failure", {"last_sha": "abc123"})
        self.assertEqual(self.repo.get_cursor("ci_failure"), {"last_sha": "abc123"})

    def test_set_cursor_overwrites(self):
        self.repo.set_cursor("ci_failure", {"last_sha": "abc123"})
        self.repo.set_cursor("ci_failure", {"last_sha": "def456"})
        self.assertEqual(self.repo.get_cursor("ci_failure"), {"last_sha": "def456"})


if __name__ == "__main__":
    unittest.main()
