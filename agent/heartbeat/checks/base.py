"""
The Check abstraction: one unit of heartbeat work.

A Check declares its own cadence (how often it's due) and, when run,
returns zero or more Notices plus its updated dedup state. It never talks
to storage directly — the scheduler persists what a Check returns, so a
Check stays a pure "look at the world, report what's interesting" function
and is trivially testable without a database.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass
class Notice:
    """One thing worth surfacing to Sean. severity="critical" bypasses
    quiet hours (agent/heartbeat/storage.py::add_notice); anything else
    waits until quiet hours end."""

    severity: str
    message: str


class Check(Protocol):
    """A single heartbeat check.

    name             stable identifier — used as the check_state/check_cursor
                      key, so renaming a Check resets its schedule and dedup
                      state.
    cadence_seconds  how long after this check runs before it's due again.
    """

    name: str
    cadence_seconds: float

    async def run(self, cursor: dict) -> tuple[list[Notice], dict]:
        """
        Look at the world once. Returns (notices, updated_cursor).

        cursor is this check's dedup state from the last run (e.g. the last
        commit SHA it already notified about) — read it to decide what's
        new, and return whatever the next run should see instead. Returning
        the same cursor back is fine when nothing changed.
        """
        ...
