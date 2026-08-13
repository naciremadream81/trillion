"""
Tests for HeartbeatScheduler (agent/heartbeat/scheduler.py).

Run from the project root:
    python -m unittest tests.test_heartbeat_scheduler
"""

import asyncio
import os
import tempfile
import unittest
from datetime import datetime, timezone

from agent.config import Settings
from agent.heartbeat.checks.base import Notice
from agent.heartbeat.scheduler import HeartbeatScheduler
from agent.heartbeat.storage import HeartbeatRepo


def run(coro):
    return asyncio.run(coro)


async def _tick_and_wait(scheduler, background_tasks):
    await scheduler.tick_once()
    if background_tasks:
        await asyncio.gather(*background_tasks)


class FakeCheck:
    def __init__(self, name, cadence_seconds=60.0, notices=None, raises=False):
        self.name = name
        self.cadence_seconds = cadence_seconds
        self._notices = notices or []
        self._raises = raises
        self.run_count = 0

    async def run(self, cursor):
        self.run_count += 1
        if self._raises:
            raise RuntimeError("boom")
        return list(self._notices), {**cursor, "runs": self.run_count}


class TestHeartbeatSchedulerTickOnce(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = HeartbeatRepo(db_path=os.path.join(self._tmp.name, "heartbeat.db"))
        # start == end disables quiet hours (see storage.py::is_quiet_hours) so
        # these tests aren't flaky depending on the wall-clock hour they run at.
        self.settings = Settings(heartbeat_quiet_hours_start=0, heartbeat_quiet_hours_end=0)
        self.background_tasks = set()

    def tearDown(self):
        self._tmp.cleanup()

    def test_due_check_runs_and_notices_land_in_repo(self):
        check = FakeCheck("ci_failure", notices=[Notice(severity="warning", message="build broke")])
        scheduler = HeartbeatScheduler([check], self.repo, self.settings, background_tasks=self.background_tasks)

        run(_tick_and_wait(scheduler, self.background_tasks))

        self.assertEqual(check.run_count, 1)
        active = self.repo.list_active_notices()
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["message"], "build broke")

    def test_not_yet_due_check_is_skipped(self):
        check = FakeCheck("dormant_repo", cadence_seconds=3600.0)
        scheduler = HeartbeatScheduler([check], self.repo, self.settings, background_tasks=self.background_tasks)

        run(_tick_and_wait(scheduler, self.background_tasks))
        self.background_tasks.clear()
        run(_tick_and_wait(scheduler, self.background_tasks))  # second tick: check is now scheduled far in the future

        self.assertEqual(check.run_count, 1)

    def test_paused_settings_skips_all_checks(self):
        check = FakeCheck("ci_failure")
        settings = Settings(trillion_paused=True)
        scheduler = HeartbeatScheduler([check], self.repo, settings, background_tasks=self.background_tasks)

        run(scheduler.tick_once())

        self.assertEqual(check.run_count, 0)
        self.assertEqual(len(self.background_tasks), 0)

    def test_running_check_is_not_started_twice_concurrently(self):
        check = FakeCheck("ci_failure")
        scheduler = HeartbeatScheduler([check], self.repo, self.settings, background_tasks=self.background_tasks)
        scheduler._running.add("ci_failure")

        run(scheduler.tick_once())

        self.assertEqual(len(self.background_tasks), 0)

    def test_failing_check_does_not_raise_and_still_reschedules(self):
        check = FakeCheck("broken_check", raises=True)
        scheduler = HeartbeatScheduler([check], self.repo, self.settings, background_tasks=self.background_tasks)

        run(_tick_and_wait(scheduler, self.background_tasks))

        self.assertEqual(self.repo.list_active_notices(), [])
        self.assertIsNotNone(self.repo.get_next_due_at("broken_check"))

    def test_notices_respect_configured_quiet_hours(self):
        now_hour = datetime.now(timezone.utc).hour
        settings = Settings(
            heartbeat_quiet_hours_start=(now_hour - 1) % 24,
            heartbeat_quiet_hours_end=(now_hour + 1) % 24,
        )
        check = FakeCheck("stale_pr", notices=[Notice(severity="info", message="PR idle 48h")])
        scheduler = HeartbeatScheduler([check], self.repo, settings, background_tasks=self.background_tasks)

        run(_tick_and_wait(scheduler, self.background_tasks))

        self.assertEqual(self.repo.list_active_notices(), [])


if __name__ == "__main__":
    unittest.main()
