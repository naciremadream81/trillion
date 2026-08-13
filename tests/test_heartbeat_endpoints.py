"""
Tests for the Tier 5 heartbeat endpoints in serve.py:
    GET  /api/heartbeat/notices
    POST /api/heartbeat/dismiss

Requires aiohttp (a project dependency). Run from the project root:
    python -m unittest tests.test_heartbeat_endpoints
"""

import os
import shutil
import tempfile

from aiohttp.test_utils import AioHTTPTestCase

import serve as serve_module
from agent.heartbeat.storage import HeartbeatRepo
from agent.providers.base import BaseProvider, ProviderResponse, TextChunk, TokenUsage
from agent.tools.registry import ToolRegistry


class FakeProvider(BaseProvider):
    def __init__(self, replies=None):
        self._replies = list(replies or [])

    @property
    def model_name(self):
        return "fake-model"

    async def stream(self, messages, system, tools=None):
        text = self._replies.pop(0) if self._replies else ""
        yield TextChunk(text=text)
        yield ProviderResponse(text=text, tool_calls=[], usage=TokenUsage(), model=self.model_name)


class TestHeartbeatEndpoints(AioHTTPTestCase):
    async def get_application(self):
        self.tmp = tempfile.mkdtemp()

        # build_app() also starts the Agent Factory watcher, the notes index
        # build, and (now) the heartbeat scheduler on startup — point every
        # one of them at this test's temp dir so none touches real project
        # state or the (broken) vault mount, mirroring test_serve_factory_wiring.
        self._prev_env = {
            key: os.environ.get(key)
            for key in (
                "TRILLION_FACTORY_DB",
                "TRILLION_NOTES_VAULT_PATH",
                "TRILLION_NOTES_INDEX_PATH",
                "TRILLION_HEARTBEAT_DB",
                "GITHUB_TOKEN",
                "TRILLION_GITHUB_WATCHED_REPOS",
            )
        }
        os.environ["TRILLION_FACTORY_DB"] = os.path.join(self.tmp, "factory.db")
        os.environ["TRILLION_NOTES_VAULT_PATH"] = os.path.join(self.tmp, "vault")
        os.environ["TRILLION_NOTES_INDEX_PATH"] = os.path.join(self.tmp, "notes_index.db")
        self.db_path = os.path.join(self.tmp, "heartbeat.db")
        os.environ["TRILLION_HEARTBEAT_DB"] = self.db_path
        # No GitHub config -> Code Sentinel checks self-skip; the scheduler
        # still starts with zero checks registered.
        os.environ.pop("GITHUB_TOKEN", None)
        os.environ.pop("TRILLION_GITHUB_WATCHED_REPOS", None)

        serve_module._provider = FakeProvider()
        serve_module._registry = ToolRegistry()
        serve_module._agent = None

        self.repo = HeartbeatRepo(db_path=self.db_path)
        return serve_module.build_app()

    def tearDown(self):
        super().tearDown()
        serve_module._provider = None
        serve_module._registry = None
        serve_module._agent = None
        for key, prev in self._prev_env.items():
            if prev is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = prev
        shutil.rmtree(self.tmp, ignore_errors=True)

    async def test_notices_empty_by_default(self):
        resp = await self.client.request("GET", "/api/heartbeat/notices")
        self.assertEqual(resp.status, 200)
        data = await resp.json()
        self.assertEqual(data, {"notices": []})

    async def test_notices_lists_seeded_notice(self):
        self.repo.add_notice(check_name="ci_failure", severity="critical", message="pytest failed on main.")
        resp = await self.client.request("GET", "/api/heartbeat/notices")
        data = await resp.json()
        self.assertEqual(len(data["notices"]), 1)
        self.assertEqual(data["notices"][0]["severity"], "critical")
        self.assertIn("pytest failed", data["notices"][0]["message"])

    async def test_dismiss_removes_notice_from_active_list(self):
        notice_id = self.repo.add_notice(check_name="ci_failure", severity="critical", message="pytest failed.")
        resp = await self.client.request(
            "POST", "/api/heartbeat/dismiss", json={"id": notice_id}
        )
        self.assertEqual(resp.status, 200)
        data = await resp.json()
        self.assertEqual(data, {"dismissed": True})

        resp = await self.client.request("GET", "/api/heartbeat/notices")
        data = await resp.json()
        self.assertEqual(data["notices"], [])

    async def test_dismiss_unknown_id_returns_false(self):
        resp = await self.client.request("POST", "/api/heartbeat/dismiss", json={"id": 999})
        self.assertEqual(resp.status, 200)
        data = await resp.json()
        self.assertEqual(data, {"dismissed": False})

    async def test_dismiss_missing_id_returns_400(self):
        resp = await self.client.request("POST", "/api/heartbeat/dismiss", json={})
        self.assertEqual(resp.status, 400)

    async def test_dismiss_non_int_id_returns_400(self):
        resp = await self.client.request("POST", "/api/heartbeat/dismiss", json={"id": "not-an-int"})
        self.assertEqual(resp.status, 400)

    async def test_heartbeat_task_started_and_cancelled_on_cleanup(self):
        task = self.app["heartbeat_task"]
        self.assertIsNotNone(task)
        self.assertFalse(task.done())
        await self.app.cleanup()
        self.assertTrue(task.cancelled() or task.done())


if __name__ == "__main__":
    import unittest

    unittest.main()
