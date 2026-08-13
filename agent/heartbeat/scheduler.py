"""
Heartbeat scheduler: one tick loop, many checks, each on its own cadence.

Mirrors agent/factory/software/scheduler.py::AutonomousScheduler's
tick_once()/run_forever() shape, adapted for many independent checks
instead of one action. A single base interval
(settings.heartbeat_tick_interval_seconds) drives the loop; each
registered Check's own cadence_seconds decides whether *it* is due, read
from HeartbeatRepo so scheduling survives a restart. Due checks are
spawned as independent tracked tasks so a slow check (e.g. a ~30m
dormant-repo scan) never blocks a fast one (a ~60s CI check) — see
agent/heartbeat/checks/code_sentinel.py. Built generically enough that P11
(BTC mining tracker) reuses this same loop for its own checks.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from .storage import HeartbeatRepo

logger = logging.getLogger(__name__)


class HeartbeatScheduler:
    def __init__(self, checks, repo: HeartbeatRepo, settings, *, background_tasks: set) -> None:
        self.checks = list(checks)
        self.repo = repo
        self.settings = settings
        self.background_tasks = background_tasks
        # In-memory no-overlap guard: a process restart naturally clears
        # any stuck in-flight state, so this doesn't need to be persisted.
        self._running: set[str] = set()

    async def _run_check(self, check) -> None:
        self._running.add(check.name)
        try:
            cursor = self.repo.get_cursor(check.name)
            try:
                notices, new_cursor = await check.run(cursor)
            except Exception:  # noqa: BLE001 — one bad check must never kill the loop
                logger.exception("heartbeat check %r failed", check.name)
                return
            for notice in notices:
                self.repo.add_notice(
                    check_name=check.name,
                    severity=notice.severity,
                    message=notice.message,
                    quiet_hours_start=self.settings.heartbeat_quiet_hours_start,
                    quiet_hours_end=self.settings.heartbeat_quiet_hours_end,
                )
            self.repo.set_cursor(check.name, new_cursor)
        finally:
            self._running.discard(check.name)
            self.repo.set_next_due_at(
                check.name,
                datetime.now(timezone.utc) + timedelta(seconds=check.cadence_seconds),
            )

    async def tick_once(self) -> None:
        # Blocks new checks from starting; conversation and read-only tools
        # are unaffected (agent/config.py's trillion_paused is the same
        # kill switch the Software Factory scheduler respects).
        if self.settings.trillion_paused:
            return
        now = datetime.now(timezone.utc)
        for check in self.checks:
            if check.name in self._running:
                continue
            due_at = self.repo.get_next_due_at(check.name)
            if due_at is not None and due_at > now:
                continue
            task = asyncio.create_task(self._run_check(check))
            self.background_tasks.add(task)
            task.add_done_callback(self.background_tasks.discard)

    async def run_forever(self) -> None:
        while True:
            try:
                await self.tick_once()
            except Exception:  # noqa: BLE001 — a broken tick must never kill the scheduler
                logger.exception("HeartbeatScheduler.tick_once failed")
            await asyncio.sleep(self.settings.heartbeat_tick_interval_seconds)
