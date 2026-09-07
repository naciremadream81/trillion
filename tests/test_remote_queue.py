"""
Tests for the cross-machine queue — playbook/cloud-to-local.md.

Every tier's stated verification, as a test rather than a shell transcript:

  Tier 1  drain-on-startup: a task enqueued while the worker was down runs
          when it comes back, BEFORE any wake signal — the property that
          makes a request survive the laptop being asleep.
  Tier 2  the worker runs the real runner to completion; an unknown agent
          completes with an error summary rather than crashing.
  Tier 3  the proxy enqueues instead of executing, and is never registered
          alongside the real tool.
  Tier 4  presence is advisory: it changes the wording and never the work.
  Tier 6  every terminal-failure shape maps to failure, including the common
          one where the local agent persists `failed` and returns normally.

Run from the project root:
    python -m unittest tests.test_remote_queue
"""

import asyncio
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from agent.remote.proxy import RemoteDispatchProxy, register_proxies
from agent.remote.runners import head_of_design_runner
from agent.remote.storage import (
    COMPLETED,
    FAILED,
    KIND_NOOP,
    KIND_REMOTE_DISPATCH,
    PENDING,
    RemoteQueue,
)
from agent.remote.worker import RemoteWorker
from agent.tools.registry import ToolRegistry

ROLE = "local_primary"


def run(coro):
    return asyncio.run(coro)


class QueueTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.queue = RemoteQueue(os.path.join(self.tmp, "remote.db"))


# ── Tier 1 ──────────────────────────────────────────────────────────────────


class TestQueue(QueueTestCase):
    def test_enqueue_then_claim_round_trips(self):
        task_id = self.queue.enqueue(ROLE, KIND_NOOP, {"x": 1})
        task = self.queue.claim(ROLE, "worker-a")
        self.assertEqual(task["id"], task_id)
        self.assertEqual(task["payload"], {"x": 1})
        self.assertEqual(task["claimed_by"], "worker-a")

    def test_claiming_an_empty_queue_returns_none(self):
        self.assertIsNone(self.queue.claim(ROLE, "worker-a"))

    def test_a_task_is_claimed_exactly_once(self):
        # The load-bearing detail. Without an atomic claim, two workers (or
        # one woken twice) both grab the row and the work runs twice — which
        # for a paid dispatch means paying twice and doing it twice.
        self.queue.enqueue(ROLE, KIND_NOOP, {})
        first = self.queue.claim(ROLE, "worker-a")
        second = self.queue.claim(ROLE, "worker-b")
        self.assertIsNotNone(first)
        self.assertIsNone(second)

    def test_claims_are_oldest_first(self):
        ids = [self.queue.enqueue(ROLE, KIND_NOOP, {"n": i}) for i in range(3)]
        claimed = [self.queue.claim(ROLE, "w")["id"] for _ in range(3)]
        self.assertEqual(claimed, ids)

    def test_another_role_does_not_see_the_task(self):
        self.queue.enqueue(ROLE, KIND_NOOP, {})
        self.assertIsNone(self.queue.claim("some_other_machine", "w"))

    def test_complete_and_fail_are_terminal(self):
        a = self.queue.enqueue(ROLE, KIND_NOOP, {})
        b = self.queue.enqueue(ROLE, KIND_NOOP, {})
        self.queue.complete(a, {"status": "ok"})
        self.queue.fail(b, "it broke")
        self.assertEqual(self.queue.get(a)["status"], COMPLETED)
        self.assertEqual(self.queue.get(a)["result"], {"status": "ok"})
        self.assertEqual(self.queue.get(b)["status"], FAILED)
        self.assertEqual(self.queue.get(b)["error_message"], "it broke")

    def test_a_stranded_claim_is_returned_to_pending(self):
        # A worker that died mid-run would otherwise strand the task forever.
        task_id = self.queue.enqueue(ROLE, KIND_NOOP, {})
        self.queue.claim(ROLE, "worker-that-died")
        stale = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
        with self.queue._connect() as conn:
            conn.execute("UPDATE remote_tasks SET claimed_at = ? WHERE id = ?", (stale, task_id))
        self.assertEqual(self.queue.release_stale_claims(), 1)
        self.assertEqual(self.queue.get(task_id)["status"], PENDING)

    def test_a_recent_claim_is_not_reclaimed(self):
        # Reclaiming a task that is still running would execute it twice.
        self.queue.enqueue(ROLE, KIND_NOOP, {})
        self.queue.claim(ROLE, "worker-a")
        self.assertEqual(self.queue.release_stale_claims(), 0)


class TestDrainOnStartup(QueueTestCase):
    def test_a_task_enqueued_while_the_worker_was_down_runs_on_startup(self):
        # THE Tier 1 verification: enqueue, (worker not running), start the
        # worker, and the task completes before any wake signal exists.
        task_id = self.queue.enqueue(ROLE, KIND_NOOP, {"hello": "world"})
        worker = RemoteWorker(self.queue, worker_role=ROLE, claimed_by="w")

        self.assertEqual(run(worker.drain()), 1)
        task = self.queue.get(task_id)
        self.assertEqual(task["status"], COMPLETED)
        self.assertEqual(task["result"]["echo"], {"hello": "world"})

    def test_drain_empties_the_queue_rather_than_taking_one(self):
        # A wake handler that runs exactly one task leaves a backlog.
        for i in range(4):
            self.queue.enqueue(ROLE, KIND_NOOP, {"n": i})
        worker = RemoteWorker(self.queue, worker_role=ROLE, claimed_by="w")
        self.assertEqual(run(worker.drain()), 4)
        self.assertEqual(self.queue.pending_count(ROLE), 0)

    def test_draining_an_empty_queue_is_a_no_op(self):
        worker = RemoteWorker(self.queue, worker_role=ROLE, claimed_by="w")
        self.assertEqual(run(worker.drain()), 0)


# ── Tier 2 ──────────────────────────────────────────────────────────────────


class TestWorkerRouting(QueueTestCase):
    def worker(self, runners=None, deps=None):
        return RemoteWorker(self.queue, worker_role=ROLE, claimed_by="w",
                            runners=runners or {}, deps=deps or {})

    def test_a_dispatch_reaches_the_right_runner_with_its_args(self):
        seen = {}

        async def runner(args, deps):
            seen.update({"args": args, "deps": deps})
            return {"status": "ok", "summary": "composed"}

        task_id = self.queue.enqueue(ROLE, KIND_REMOTE_DISPATCH,
                                     {"agent": "head-of-design", "args": {"screen_name": "home"}})
        run(self.worker({"head-of-design": runner}, {"design_tool": "x"}).drain())

        self.assertEqual(seen["args"], {"screen_name": "home"})
        self.assertEqual(seen["deps"], {"design_tool": "x"})
        self.assertEqual(self.queue.get(task_id)["status"], COMPLETED)

    def test_an_unknown_agent_completes_with_an_error_not_a_crash(self):
        task_id = self.queue.enqueue(ROLE, KIND_REMOTE_DISPATCH,
                                     {"agent": "nobody", "args": {}})
        run(self.worker().drain())   # must not raise
        task = self.queue.get(task_id)
        self.assertEqual(task["status"], FAILED)
        self.assertIn("no runner", task["result"]["error"])

    def test_an_unknown_kind_completes_with_an_error(self):
        task_id = self.queue.enqueue(ROLE, "something_else", {})
        run(self.worker().drain())
        self.assertEqual(self.queue.get(task_id)["status"], FAILED)

    def test_a_runner_that_raises_fails_the_task_not_the_worker(self):
        async def boom(args, deps):
            raise RuntimeError("the CLI died")

        task_id = self.queue.enqueue(ROLE, KIND_REMOTE_DISPATCH,
                                     {"agent": "head-of-design", "args": {}})
        # The second task proves the worker survived the first.
        second = self.queue.enqueue(ROLE, KIND_NOOP, {})
        run(self.worker({"head-of-design": boom}).drain())

        self.assertEqual(self.queue.get(task_id)["status"], FAILED)
        self.assertIn("the CLI died", self.queue.get(task_id)["error_message"])
        self.assertEqual(self.queue.get(second)["status"], COMPLETED)

    def test_a_runner_returning_junk_is_a_failure_not_a_success(self):
        async def junk(args, deps):
            return "not a summary dict"

        task_id = self.queue.enqueue(ROLE, KIND_REMOTE_DISPATCH,
                                     {"agent": "head-of-design", "args": {}})
        run(self.worker({"head-of-design": junk}).drain())
        self.assertEqual(self.queue.get(task_id)["status"], FAILED)


class TestTerminalFailureShapes(QueueTestCase):
    """
    Tier 6's trap: a local agent that persists `failed` and returns NORMALLY
    is the common case. Treating only exceptions as failure is exactly how a
    failure gets cheerfully reported as a success.
    """

    def failing_summary(self, summary):
        async def runner(args, deps):
            return summary

        task_id = self.queue.enqueue(ROLE, KIND_REMOTE_DISPATCH,
                                     {"agent": "a", "args": {}})
        worker = RemoteWorker(self.queue, worker_role=ROLE, claimed_by="w",
                              runners={"a": runner})
        run(worker.drain())
        return self.queue.get(task_id)

    def test_status_failed_is_a_failure(self):
        self.assertEqual(self.failing_summary({"status": "failed"})["status"], FAILED)

    def test_status_error_is_a_failure(self):
        self.assertEqual(self.failing_summary({"status": "error"})["status"], FAILED)

    def test_an_error_key_alongside_an_ok_status_is_still_a_failure(self):
        task = self.failing_summary({"status": "ok", "error": "actually it broke"})
        self.assertEqual(task["status"], FAILED)

    def test_a_genuine_success_is_a_success(self):
        self.assertEqual(self.failing_summary({"status": "ok", "summary": "done"})["status"],
                         COMPLETED)


class TestDesignRunner(unittest.TestCase):
    def test_an_unconfigured_machine_returns_an_error_summary(self):
        # The cloud asked for something this machine cannot do. That is an
        # answer, not an outage.
        result = run(head_of_design_runner({}, {}))
        self.assertEqual(result["status"], "error")
        self.assertIn("not configured", result["error"])

    def test_a_refusal_string_is_mapped_to_failure(self):
        # GenerateMockupTool reports its budget refusals in the RESULT and
        # returns normally — the exact shape that reads as success if you
        # only catch exceptions.
        class RefusingTool:
            async def run(self, **kwargs):
                return "[generate_mockup refused: daily ceiling reached]"

        result = run(head_of_design_runner({}, {"design_tool": RefusingTool()}))
        self.assertEqual(result["status"], "failed")
        self.assertIn("ceiling", result["error"])

    def test_a_real_result_is_a_success(self):
        class GoodTool:
            async def run(self, **kwargs):
                return "Composed home.tsx and the build passed."

        result = run(head_of_design_runner({"screen_name": "home"},
                                           {"design_tool": GoodTool()}))
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["screen"], "home")

    def test_a_tool_that_raises_is_caught(self):
        class BoomTool:
            async def run(self, **kwargs):
                raise RuntimeError("subprocess died")

        result = run(head_of_design_runner({}, {"design_tool": BoomTool()}))
        self.assertEqual(result["status"], "error")


# ── Tier 3 ──────────────────────────────────────────────────────────────────


class TestProxy(QueueTestCase):
    DEFINITION = {
        "name": "generate_mockup",
        "description": "Compose one screen.",
        "input_schema": {"type": "object", "properties": {"screen_name": {"type": "string"}}},
    }

    def proxy(self):
        return RemoteDispatchProxy("head-of-design", self.DEFINITION, self.queue, ROLE)

    def test_the_proxy_enqueues_rather_than_running(self):
        import json

        result = json.loads(run(self.proxy().run(screen_name="home")))
        self.assertTrue(result["queued"])
        task = self.queue.get(result["task_id"])
        self.assertEqual(task["status"], PENDING)
        self.assertEqual(task["kind"], KIND_REMOTE_DISPATCH)
        self.assertEqual(task["payload"], {"agent": "head-of-design",
                                           "args": {"screen_name": "home"}})

    def test_the_proxy_mirrors_the_real_tools_name_and_schema(self):
        # A doppelgänger, not a new tool: the cloud model already knows how
        # to call this, so zero prompts change. Drift and you edit system
        # prompts forever.
        proxy = self.proxy()
        self.assertEqual(proxy.name, "generate_mockup")
        self.assertEqual(proxy.definition(), self.DEFINITION)
        self.assertEqual(proxy.input_schema, self.DEFINITION["input_schema"])

    def test_a_proxy_is_never_handed_to_a_spawned_agent(self):
        self.assertFalse(self.proxy().factory_allowed)

    def test_no_double_fire_when_the_real_tool_is_present(self):
        # The guarantee is structural: only one of the two exists per
        # process, because the proxy is registered only into a registry that
        # lacks the real name.
        from agent.tools.base import BaseTool

        class RealTool(BaseTool):
            name = "generate_mockup"
            description = "the real one"
            input_schema = {"type": "object", "properties": {}}

            async def run(self, **kwargs):
                return "ran locally"

        registry = ToolRegistry()
        registry.register(RealTool())
        registered = register_proxies(registry, self.queue, ROLE,
                                      proxied={"generate_mockup": "head-of-design"})
        self.assertEqual(registered, [])
        self.assertIsInstance(registry.get("generate_mockup"), RealTool)

    def test_the_proxy_registers_when_the_real_tool_is_absent(self):
        registry = ToolRegistry()
        registered = register_proxies(registry, self.queue, ROLE,
                                      proxied={"generate_mockup": "head-of-design"})
        self.assertEqual(registered, ["generate_mockup"])
        self.assertIsInstance(registry.get("generate_mockup"), RemoteDispatchProxy)


# ── Tier 4 ──────────────────────────────────────────────────────────────────


class TestPresence(QueueTestCase):
    def test_an_unknown_worker_is_offline(self):
        self.assertFalse(self.queue.is_online(ROLE))

    def test_a_fresh_heartbeat_is_online(self):
        self.queue.heartbeat(ROLE, "w")
        self.assertTrue(self.queue.is_online(ROLE))

    def test_a_stale_heartbeat_is_offline(self):
        self.queue.heartbeat(ROLE, "w")
        old = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        with self.queue._connect() as conn:
            conn.execute("UPDATE worker_presence SET last_seen = ? WHERE worker_role = ?",
                         (old, ROLE))
        self.assertFalse(self.queue.is_online(ROLE))

    def test_an_unparseable_timestamp_reads_as_offline_not_an_exception(self):
        self.queue.heartbeat(ROLE, "w")
        with self.queue._connect() as conn:
            conn.execute("UPDATE worker_presence SET last_seen = ? WHERE worker_role = ?",
                         ("not a date", ROLE))
        self.assertFalse(self.queue.is_online(ROLE))   # must not raise

    def test_heartbeat_upserts_rather_than_duplicating(self):
        for _ in range(3):
            self.queue.heartbeat(ROLE, "w")
        with self.queue._connect() as conn:
            rows = conn.execute("SELECT COUNT(*) AS n FROM worker_presence").fetchone()
        self.assertEqual(rows["n"], 1)

    def test_offline_changes_the_wording_but_never_the_work(self):
        import json

        # Presence is advisory. Both paths must enqueue.
        proxy = RemoteDispatchProxy("head-of-design", TestProxy.DEFINITION, self.queue, ROLE)

        offline = json.loads(run(proxy.run(screen_name="a")))
        self.assertIn("when your computer's online", offline["spoken"])
        self.assertTrue(offline["queued"])

        self.queue.heartbeat(ROLE, "w")
        online = json.loads(run(proxy.run(screen_name="b")))
        self.assertIn("starting on your computer", online["spoken"])
        self.assertTrue(online["queued"])

        self.assertEqual(self.queue.pending_count(ROLE), 2)

    def test_a_broken_presence_read_still_enqueues(self):
        import json

        class BrokenPresence(RemoteQueue):
            def is_online(self, worker_role, max_age_seconds=90.0):
                raise RuntimeError("presence table is gone")

        broken = BrokenPresence(os.path.join(self.tmp, "broken.db"))
        proxy = RemoteDispatchProxy("head-of-design", TestProxy.DEFINITION, broken, ROLE)
        result = json.loads(run(proxy.run(screen_name="a")))
        self.assertTrue(result["queued"])
        self.assertIn("when your computer's online", result["spoken"])


if __name__ == "__main__":
    unittest.main()
