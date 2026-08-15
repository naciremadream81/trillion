"""
Tests for the Software Factory autonomous scheduler wiring in serve.py —
serve.py (not main.py) is what actually runs 24/7 via trillion-orb.service,
so it's the process that must own AutonomousScheduler.run_forever() for
self-initiated builds to happen at all. Mirrors
tests/test_serve_factory_wiring.py's structure for the Agent Factory watcher.

Run from the project root:
    python -m unittest tests.test_serve_software_factory_wiring
"""

import os
import tempfile
import unittest

from aiohttp.test_utils import AioHTTPTestCase

import serve as serve_module
from agent.providers.base import BaseProvider, ProviderResponse, TextChunk, TokenUsage


class FakeProvider(BaseProvider):
    @property
    def model_name(self):
        return "fake-model"

    async def stream(self, messages, system, tools=None):
        yield TextChunk(text="")
        yield ProviderResponse(text="", tool_calls=[], usage=TokenUsage(), model=self.model_name)


class _BaseCase(AioHTTPTestCase):
    async def get_application(self):
        self.tmp = tempfile.mkdtemp()
        self._prev_db = os.environ.get("TRILLION_SOFTWARE_FACTORY_DB")
        os.environ["TRILLION_SOFTWARE_FACTORY_DB"] = os.path.join(self.tmp, "software_factory.db")
        self._prev_themes = os.environ.get("TRILLION_FACTORY_AUTONOMOUS_THEMES")
        os.environ["TRILLION_FACTORY_AUTONOMOUS_THEMES"] = self.themes_env

        serve_module._provider = FakeProvider()
        return serve_module.build_app()

    def tearDown(self):
        super().tearDown()
        serve_module._provider = None

        def _restore(key, prev):
            if prev is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = prev

        _restore("TRILLION_SOFTWARE_FACTORY_DB", self._prev_db)
        _restore("TRILLION_FACTORY_AUTONOMOUS_THEMES", self._prev_themes)


class TestSchedulerStartsWhenThemesSet(_BaseCase):
    themes_env = "small internal utilities"

    async def test_scheduler_task_is_created_and_cancelled_on_cleanup(self):
        task = self.app["sf_scheduler_task"]
        self.assertIsNotNone(task)
        self.assertFalse(task.done())
        await self.app.cleanup()
        self.assertTrue(task.cancelled() or task.done())


class TestSchedulerStaysOffWithoutThemes(_BaseCase):
    themes_env = ""

    async def test_no_scheduler_task_when_themes_unset(self):
        self.assertIsNone(self.app["sf_scheduler_task"])


if __name__ == "__main__":
    unittest.main()
