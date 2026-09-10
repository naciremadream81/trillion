"""
The worker — playbook/cloud-to-local.md Tiers 1, 2 and 4.

Runs in the process that actually has the local capability (the machine with
the `claude` CLI and the project filesystem). Its whole job:

  1. **Drain on startup**, before subscribing to any wake signal. This is the
     tier that makes a request survive the laptop having been asleep when it
     was made. A worker that only reacts to notifications loses every task
     enqueued while it was down.
  2. **Run the REAL local agent**, not a reimplementation. The runner invokes
     the same code path a local request would, so it inherits the real
     behaviour, the real UI events, and the real credentials.
  3. **Run to completion**, so "done" means done — see RUN TO COMPLETION.
  4. Beat a heartbeat so the cloud can word its acknowledgements honestly.

RUN TO COMPLETION (read twice). Trillion's local dispatches are largely
fire-and-forget: start_build() and the design tool hand work to a background
task and return a "started" ack immediately. Awaiting *that* would mark the
task completed before the agent had done anything, and the completion ping
would be a lie. A runner must await the actual end of the run. Where the real
agent ends at a human-approval pause, "completed" means "reached the approval
point", and the summary says so — that is a truthful terminal state, not a
success.

SQLite has no LISTEN/NOTIFY, so the wake signal here is a poll tick — the
same choice agent/factory/dispatch.py's RegistryWatcher makes for the same
reason. Drain-on-startup is what carries the correctness; the tick is only
latency.
"""

from __future__ import annotations

import asyncio
import logging
import socket

from .storage import KIND_NOOP, KIND_REMOTE_DISPATCH, RemoteQueue

logger = logging.getLogger(__name__)

DEFAULT_WORKER_ROLE = "local_primary"
HEARTBEAT_SECONDS = 30.0
POLL_SECONDS = 5.0


def default_claimed_by() -> str:
    """Who this worker is, for the claimed_by column and the presence row."""
    try:
        return f"{socket.gethostname()}"
    except Exception:  # noqa: BLE001
        return "unknown-host"


async def noop_runner(args: dict, deps: dict) -> dict:
    """The Tier 1 verification task: proves the drain path without spending
    money or touching a real agent."""
    return {"status": "ok", "echo": args}


class RemoteWorker:
    """
    Drains `remote_tasks` for one worker_role.

    `runners` maps an agent name to `async runner(args, deps) -> summary dict`.
    `deps` is the bundle the real local agent needs — repos, settings, the
    LOCAL event emitter — passed in from the process that already wires them
    up. A runner whose dependency is missing returns an error summary rather
    than crashing: an unconfigured agent is a task that completes with
    `{"status": "error"}`, not a worker that dies.
    """

    def __init__(
        self,
        queue: RemoteQueue,
        *,
        worker_role: str = DEFAULT_WORKER_ROLE,
        claimed_by: str | None = None,
        runners: dict | None = None,
        deps: dict | None = None,
        poll_seconds: float = POLL_SECONDS,
        heartbeat_seconds: float = HEARTBEAT_SECONDS,
    ) -> None:
        self.queue = queue
        self.worker_role = worker_role
        self.claimed_by = claimed_by or default_claimed_by()
        self.runners = dict(runners or {})
        self.runners.setdefault("noop", noop_runner)
        self.deps = deps or {}
        self.poll_seconds = poll_seconds
        self.heartbeat_seconds = heartbeat_seconds

    # ── Handling one task ────────────────────────────────────────────────────

    async def handle(self, task: dict) -> dict:
        """
        Run one task and return its summary. Never raises — a failure is a
        failed task, not a dead worker.
        """
        kind = task.get("kind")
        payload = task.get("payload") or {}

        if kind == KIND_NOOP:
            return await noop_runner(payload, self.deps)

        if kind != KIND_REMOTE_DISPATCH:
            return {"status": "error", "error": f"unknown task kind {kind!r}"}

        agent = str(payload.get("agent", ""))
        runner = self.runners.get(agent)
        if runner is None:
            # Completes with an error summary rather than crashing: the cloud
            # asked for something this machine doesn't have, which is an
            # answer, not an outage.
            return {"status": "error", "error": f"no runner for agent {agent!r} on this machine"}

        try:
            summary = await runner(payload.get("args") or {}, self.deps)
        except Exception as e:  # noqa: BLE001
            logger.exception("remote worker: runner for %s failed", agent)
            return {"status": "error", "error": f"{type(e).__name__}: {e}"}

        if not isinstance(summary, dict):
            return {"status": "error", "error": "runner returned a non-summary"}
        summary.setdefault("status", "ok")
        summary.setdefault("agent", agent)
        return summary

    async def _run_one(self, task: dict) -> None:
        try:
            summary = await self.handle(task)
        except asyncio.CancelledError:
            # Shutdown (aiohttp cleanup / SIGTERM) cancels the worker task
            # while a runner is awaited. CancelledError is a BaseException,
            # so drain()'s `except Exception` never records a failure and
            # the row stays claimed — claim() will not pick it again.
            try:
                self.queue.release_claim(task["id"])
            except Exception:  # noqa: BLE001
                logger.exception(
                    "remote worker: could not release claim for %s on shutdown",
                    task["id"],
                )
            raise
        # A local agent that persists a `failed` status and returns NORMALLY
        # is common, and treating only exceptions as failure is how you
        # cheerfully report a failure as a success. Every terminal-failure
        # shape maps to fail().
        status = str(summary.get("status", "")).lower()
        if status in ("error", "failed") or summary.get("error"):
            self.queue.fail(task["id"], summary.get("error") or "the run failed", result=summary)
        else:
            self.queue.complete(task["id"], summary)

    # ── The loop ─────────────────────────────────────────────────────────────

    async def drain(self) -> int:
        """
        Claim and run until there is nothing pending. Returns how many ran.

        Called on startup BEFORE any wake signal is subscribed, and again on
        every tick. "Drain" is literally "claim until none left" — a wake
        handler that runs exactly one task leaves a backlog behind it.
        """
        ran = 0
        while True:
            try:
                task = self.queue.claim(self.worker_role, self.claimed_by)
            except Exception:  # noqa: BLE001 — a locked db is a retry, not a death
                logger.exception("remote worker: claim failed")
                return ran
            if task is None:
                return ran
            try:
                await self._run_one(task)
            except Exception as e:  # noqa: BLE001
                logger.exception("remote worker: task %s failed outside the runner", task["id"])
                try:
                    self.queue.fail(task["id"], f"{type(e).__name__}: {e}")
                except Exception:  # noqa: BLE001
                    logger.exception("remote worker: could not even record the failure")
            ran += 1

    async def beat(self) -> None:
        try:
            self.queue.heartbeat(self.worker_role, self.claimed_by)
        except Exception:  # noqa: BLE001 — presence is advisory; never fatal
            logger.warning("remote worker: heartbeat failed")

    def recover_stranded_claims(self) -> int:
        """
        Undo claims this process can no longer be running.

        Two horizons, on purpose:

          * Ours, any age — this worker is starting, so a previous
            incarnation that claimed under our `claimed_by` is dead. A
            deploy or OOM + Restart=always is seconds, not an hour; waiting
            on STALE_CLAIM_SECONDS would strand the row until another
            restart happens after that window, which in practice is never.
          * Anyone's, older than STALE_CLAIM_SECONDS — a different host
            that died and never came back. Must stay generous so we do not
            steal a run that is still in flight there.
        """
        own = self.queue.release_claims_by(self.claimed_by)
        stale = self.queue.release_stale_claims()
        return own + stale

    async def run_forever(self) -> None:
        """
        Startup: reclaim anything a dead worker stranded, beat once so the
        cloud stops saying "offline", then DRAIN — all before the first tick.
        """
        try:
            released = self.recover_stranded_claims()
            if released:
                logger.info("remote worker: returned %d stranded task(s) to pending", released)
        except Exception:  # noqa: BLE001
            logger.exception("remote worker: could not release stale claims")

        await self.beat()
        drained = await self.drain()
        if drained:
            logger.info("remote worker: drained %d task(s) on startup", drained)

        last_beat = 0.0
        loop = asyncio.get_running_loop()
        while True:
            await asyncio.sleep(self.poll_seconds)
            try:
                now = loop.time()
                if now - last_beat >= self.heartbeat_seconds:
                    await self.beat()
                    last_beat = now
                await self.drain()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — a broken tick must not kill the worker
                logger.exception("remote worker: tick failed")
