"""
The completion ping — playbook/cloud-to-local.md Tier 6.

When a remotely-dispatched task reaches a terminal state, tell Sean on the
surface he is actually looking at. Written as a heartbeat Check so it reuses
the existing notice channel rather than inventing a new one, and so quiet
hours, dedup and schedule persistence all come from the scheduler.

SQLite has no LISTEN/NOTIFY, so "when complete() fires the results signal" is
a short poll here. The cursor carries the last task id already reported,
which is also what stops a restart from re-announcing a week of finished
work.

**Surface the failure honestly.** The playbook's trap: a local agent that
persists a `failed` status and returns NORMALLY is the common case, so a
handler that only reacts to exceptions cheerfully reports failures as
successes. Every terminal-failure shape maps to the "ran into trouble" line —
the task's own status, an `error` key in the result summary, or a non-empty
error_message.
"""

from __future__ import annotations

import logging

from ...remote.storage import COMPLETED, FAILED, KIND_REMOTE_DISPATCH, RemoteQueue
from .base import Notice

logger = logging.getLogger(__name__)

CADENCE_SECONDS = 60.0

# Never announce more than this in one tick. A worker that drained a long
# backlog should not produce forty notices at once — the tail is history, and
# the cursor still advances past all of it.
MAX_PER_TICK = 5


def describe(task: dict) -> str:
    """The one line Sean reads. Derived from the summary, not assumed."""
    result = task.get("result") or {}
    agent = str(result.get("agent") or (task.get("payload") or {}).get("agent") or "That job")
    status = str(result.get("status", "")).lower()

    failed = (
        task.get("status") == FAILED
        or status in ("error", "failed")
        or bool(result.get("error"))
        or bool(task.get("error_message"))
    )
    if failed:
        return f"{agent} ran into trouble on your computer."
    if status == "awaiting_approval":
        name = result.get("screen") or result.get("name") or ""
        made = f" — created {name}" if name else ""
        return f"{agent} finished{made}. Approve it on your desktop."
    return f"{agent} finished on your computer."


class RemoteCompletionCheck:
    """Announces newly-finished remote dispatches."""

    name = "remote_completions"
    cadence_seconds = CADENCE_SECONDS

    def __init__(self, queue: RemoteQueue | None = None, worker_role: str = "local_primary"):
        self._queue = queue
        self._worker_role = worker_role

    def _q(self) -> RemoteQueue:
        if self._queue is None:
            self._queue = RemoteQueue()
        return self._queue

    async def run(self, cursor: dict) -> tuple[list[Notice], dict]:
        seen = cursor.get("announced") or []
        if not isinstance(seen, list):
            seen = []
        already = set(seen)

        try:
            tasks = self._q().recent(limit=40)
        except Exception:  # noqa: BLE001 — a broken read is not worth a dead heartbeat
            logger.exception("remote completions: could not read the queue")
            return [], cursor

        fresh = [
            task for task in tasks
            if task.get("kind") == KIND_REMOTE_DISPATCH
            and task.get("status") in (COMPLETED, FAILED)
            and task.get("id") not in already
        ]
        if not fresh:
            return [], cursor

        # Oldest first, so a drained backlog reads in the order it happened.
        fresh.reverse()
        notices = [
            Notice(severity="info", message=describe(task))
            for task in fresh[:MAX_PER_TICK]
        ]
        # The cursor advances past EVERY fresh task, including ones beyond
        # the per-tick cap — they are history, and re-announcing them later
        # would be worse than never mentioning them.
        announced = ([task["id"] for task in fresh] + seen)[:200]
        return notices, {**cursor, "announced": announced}
