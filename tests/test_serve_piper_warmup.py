"""
Tests for the Piper warm-up startup hook in serve.py (smooth-voice_2 Tier 4).

The measured cost this removes is a real model load, which needs the
gitignored ~63MB voice file — so these cover the wiring around it: that the
hook is registered, that it doesn't block startup, that a missing model
leaves the server fully working, and that /api/tts and the warm-up agree on
which model path they mean. The load itself is covered in tests/test_voice.py.

Run from the project root:
    python -m unittest tests.test_serve_piper_warmup
"""

import asyncio
import os
import shutil
import tempfile
import unittest

from aiohttp.test_utils import AioHTTPTestCase

import serve as serve_module
from agent.providers.base import BaseProvider, ProviderResponse, TextChunk, TokenUsage
from agent.tools.registry import ToolRegistry


class FakeProvider(BaseProvider):
    @property
    def model_name(self):
        return "fake-model"

    async def stream(self, messages, system, tools=None):
        yield TextChunk(text="")
        yield ProviderResponse(text="", tool_calls=[], usage=TokenUsage(), model=self.model_name)


class TestPiperWarmUpWiring(AioHTTPTestCase):
    async def get_application(self):
        self.tmp = tempfile.mkdtemp()

        # Point the warm-up at a model that doesn't exist: this asserts the
        # degraded path, which is also what a fresh checkout hits before
        # anyone downloads the voice file.
        self._prev_env = {
            k: os.environ.get(k)
            for k in (
                "PIPER_VOICE_PATH",
                "TRILLION_NOTES_VAULT_PATH",
                "TRILLION_NOTES_INDEX_PATH",
                "TTS_PROVIDER",
            )
        }
        # Pin TTS_PROVIDER, not just the model path. _warm_piper_voice now
        # returns early when Piper isn't the configured provider (ElevenLabs
        # became selectable after this test was written), so an inherited
        # TTS_PROVIDER=elevenlabs would make every assertion here vacuous —
        # no warm-up task, no degraded path, nothing to assert on.
        # tests/__init__.py calls load_dotenv(), so "inherited" includes
        # whatever .env happens to say. Same env-leak class as e84f7ae.
        os.environ["TTS_PROVIDER"] = "piper"
        os.environ["PIPER_VOICE_PATH"] = os.path.join(self.tmp, "missing-voice.onnx")
        os.environ["TRILLION_NOTES_VAULT_PATH"] = os.path.join(self.tmp, "vault")
        os.environ["TRILLION_NOTES_INDEX_PATH"] = os.path.join(self.tmp, "notes_index.db")

        serve_module._provider = FakeProvider()
        serve_module._registry = ToolRegistry()
        serve_module._agent_sessions.clear()
        return serve_module.build_app()

    def tearDown(self):
        super().tearDown()
        serve_module._provider = None
        serve_module._registry = None
        serve_module._agent_sessions.clear()
        for key, prev in self._prev_env.items():
            if prev is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = prev
        shutil.rmtree(self.tmp, ignore_errors=True)

    async def test_warmup_task_is_scheduled_at_startup(self):
        # Scheduled, not awaited: a multi-second model load must not hold up
        # the UI, text chat, or the cost dashboard.
        self.assertIn("piper_warmup_task", self.app)
        self.assertIsNotNone(self.app["piper_warmup_task"])

    async def test_missing_model_does_not_break_the_server(self):
        task = self.app["piper_warmup_task"]
        await asyncio.wait_for(task, timeout=5.0)
        self.assertIsNone(task.exception())

        resp = await self.client.get("/api/usage")
        self.assertEqual(resp.status, 200)

    async def test_tts_still_reports_the_missing_model_itself(self):
        # A cold TTS is a slower reply, not a silent one — /api/tts keeps
        # returning its own clear error rather than depending on the warm-up.
        resp = await self.client.post("/api/tts", json={"text": "hello"})
        self.assertEqual(resp.status, 400)
        self.assertIn("not found", await resp.text())

    async def test_warmup_and_tts_resolve_the_same_model_path(self):
        # Warming one path while synthesizing from another would look like a
        # working warm-up and still pay the load cost on the first reply.
        expected = os.environ["PIPER_VOICE_PATH"]
        self.assertEqual(serve_module._piper_model_path(), expected)

    async def test_relative_model_path_resolves_against_the_project_root(self):
        # The default is relative, so resolving it against the CWD would warm
        # nothing whenever the server is started from another directory —
        # which is exactly how trillion-orb.service starts it.
        previous = os.environ["PIPER_VOICE_PATH"]
        os.environ["PIPER_VOICE_PATH"] = "voices/en_US-amy-medium.onnx"
        try:
            self.assertEqual(
                serve_module._piper_model_path(),
                os.path.join(serve_module.PROJECT_ROOT, "voices/en_US-amy-medium.onnx"),
            )
        finally:
            os.environ["PIPER_VOICE_PATH"] = previous

    async def test_cleanup_cancels_a_warmup_still_running(self):
        await self.app.cleanup()
        task = self.app["piper_warmup_task"]
        self.assertTrue(task.done())


if __name__ == "__main__":
    unittest.main()
