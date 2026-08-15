"""
Tests for the Agent Factory wiring in serve.py (item D) — an agent approved
via FactoryRepo should be dispatchable from the browser voice server's
registry without restarting it, mirroring what RegistryWatcher.sync_once()
already gives main.py's CLI.

Requires aiohttp (a project dependency). Run from the project root:
    python -m unittest tests.test_serve_factory_wiring
"""

import os
import shutil
import tempfile

from aiohttp.test_utils import AioHTTPTestCase

import serve as serve_module
from agent.factory.storage import FactoryRepo
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


class TestFactoryWiring(AioHTTPTestCase):
    async def get_application(self):
        self.tmp = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmp, "factory.db")
        self._prev_db = os.environ.get("TRILLION_FACTORY_DB")
        os.environ["TRILLION_FACTORY_DB"] = self.db_path

        # build_app() also registers the notes-index on_startup hook, which
        # AioHTTPTestCase fires for real. Point it at this test's temp dir
        # so it doesn't walk the real (broken) vault mount or write to the
        # project's real memory/notes_index.db.
        self._prev_vault = os.environ.get("TRILLION_NOTES_VAULT_PATH")
        self._prev_index = os.environ.get("TRILLION_NOTES_INDEX_PATH")
        os.environ["TRILLION_NOTES_VAULT_PATH"] = os.path.join(self.tmp, "vault")
        os.environ["TRILLION_NOTES_INDEX_PATH"] = os.path.join(self.tmp, "notes_index.db")

        # Reset serve.py's lazy singletons and inject test doubles so
        # build_app() doesn't need real API keys or a configured tool set.
        serve_module._provider = FakeProvider()
        serve_module._registry = ToolRegistry()
        serve_module._agent_sessions.clear()

        # Approve an agent directly via FactoryRepo, pointed at the same DB
        # serve.py's RegistryWatcher reads on startup — mirrors an agent
        # approved via the CLI in a prior session, before this server started.
        repo = FactoryRepo(db_path=self.db_path)
        task_id = repo.create_spawn_task("a specialist that reviews SQL migrations")
        repo.set_draft(
            task_id, slug="sql-migration-review", system_prompt="You review SQL.", tool_allowlist=[]
        )
        repo.approve(task_id)

        return serve_module.build_app()

    def tearDown(self):
        super().tearDown()
        serve_module._provider = None
        serve_module._registry = None
        serve_module._agent_sessions.clear()
        if self._prev_db is None:
            os.environ.pop("TRILLION_FACTORY_DB", None)
        else:
            os.environ["TRILLION_FACTORY_DB"] = self._prev_db
        for key, prev in (
            ("TRILLION_NOTES_VAULT_PATH", self._prev_vault),
            ("TRILLION_NOTES_INDEX_PATH", self._prev_index),
        ):
            if prev is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = prev
        shutil.rmtree(self.tmp, ignore_errors=True)

    async def test_agent_approved_before_startup_is_live_in_registry(self):
        # AioHTTPTestCase's client setup already ran app.startup(), which
        # calls RegistryWatcher.sync_once() before the first request lands —
        # no poll-interval wait needed.
        self.assertIn("dispatch_to_sql_migration_review", serve_module._registry.names())

    async def test_watcher_task_is_cancelled_on_cleanup(self):
        task = self.app["factory_watcher_task"]
        self.assertIsNotNone(task)
        self.assertFalse(task.done())
        await self.app.cleanup()
        self.assertTrue(task.cancelled() or task.done())


if __name__ == "__main__":
    import unittest

    unittest.main()
