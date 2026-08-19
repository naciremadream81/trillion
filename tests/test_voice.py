"""
Tests for Voice V1 (Deepgram STT + Piper/ElevenLabs TTS) — the missing-config
guards, plus the never-crash-startup contract for Piper's warm-up path.

Real transcription/synthesis needs a live API key (Deepgram, ElevenLabs) and
network access, so those paths aren't covered here; this locks in the "never
crash, fail with a clear message" contract when a key or the voice model is
missing. Piper itself runs locally, so its guard test doesn't need network
access — it just points at a model path that doesn't exist.

Run: python -m unittest tests.test_voice
"""

import contextlib
import os
import unittest
from unittest import mock

from aiohttp.test_utils import AioHTTPTestCase

import serve
from agent.voice.deepgram_stt import TranscriptionError, transcribe
from agent.voice.elevenlabs_tts import SynthesisError as ElevenLabsSynthesisError
from agent.voice.elevenlabs_tts import synthesize as elevenlabs_synthesize
from agent.voice.piper_tts import SynthesisError, synthesize


class TestDeepgramGuard(unittest.IsolatedAsyncioTestCase):
    async def test_missing_key_raises_clear_error(self):
        with self.assertRaises(TranscriptionError) as ctx:
            await transcribe(b"fake-audio", "audio/webm", api_key="")
        self.assertIn("DEEPGRAM_API_KEY", str(ctx.exception))


class TestPiperGuard(unittest.TestCase):
    def test_missing_model_raises_clear_error(self):
        with self.assertRaises(SynthesisError) as ctx:
            synthesize("hello", model_path="/nonexistent/path/voice.onnx")
        self.assertIn("not found", str(ctx.exception))


class TestPiperWarmUp(unittest.IsolatedAsyncioTestCase):
    # Locks in serve.py's _warm_tts never-crash-startup contract: a missing/
    # misconfigured Piper model must never take the startup sequence down,
    # even though the background synthesis it kicks off will itself fail
    # with SynthesisError (that failure lives on the task object, not on
    # _warm_tts's own call stack — see that function's docstring on why it's
    # launched as a background task rather than awaited inline).
    def setUp(self):
        self._prev = os.environ.get("PIPER_VOICE_PATH")
        self._prev_provider = os.environ.get("TTS_PROVIDER")
        os.environ["PIPER_VOICE_PATH"] = "/nonexistent/path/voice.onnx"
        # Pin TTS_PROVIDER too, not just PIPER_VOICE_PATH: tests/__init__.py
        # calls load_dotenv() before collection, so a real .env value (e.g.
        # TTS_PROVIDER=elevenlabs, the whole point of this batch) would
        # otherwise leak in here. _warm_tts skips straight past the Piper
        # warm-up and leaves app["tts_warm_task"] as None when tts_provider
        # != "piper", which is exactly what this test asserts against — see
        # commit e84f7ae for the same env-leak failure class.
        os.environ["TTS_PROVIDER"] = "piper"

    def tearDown(self):
        if self._prev is None:
            os.environ.pop("PIPER_VOICE_PATH", None)
        else:
            os.environ["PIPER_VOICE_PATH"] = self._prev
        if self._prev_provider is None:
            os.environ.pop("TTS_PROVIDER", None)
        else:
            os.environ["TTS_PROVIDER"] = self._prev_provider

    async def test_warm_up_does_not_raise_on_missing_model(self):
        app = {}
        await serve._warm_tts(app)  # must not raise
        task = app.get("tts_warm_task")
        self.assertIsNotNone(task)
        # The task itself is allowed to fail (missing model -> SynthesisError
        # inside the thread) — that failure must stay contained to the task,
        # never propagate out and crash the server. Retrieve it so the
        # exception doesn't get flagged as "never retrieved" by asyncio.
        with contextlib.suppress(SynthesisError):
            await task


class TestWarmTtsSkipsForNonPiperProvider(unittest.IsolatedAsyncioTestCase):
    # The assertion that would have caught the env-leak regression above:
    # _warm_tts must leave app["tts_warm_task"] as None (and do no work at
    # all) when settings.tts_provider != "piper" — there's nothing to warm
    # on the ElevenLabs path (see _warm_tts's own docstring). This is the
    # opposite side of TestPiperWarmUp: that class exercises the
    # tts_provider == "piper" path, this exercises everything else.
    def setUp(self):
        self._prev = os.environ.get("TTS_PROVIDER")
        os.environ["TTS_PROVIDER"] = "elevenlabs"

    def tearDown(self):
        if self._prev is None:
            os.environ.pop("TTS_PROVIDER", None)
        else:
            os.environ["TTS_PROVIDER"] = self._prev

    async def test_skips_piper_warm_up_when_provider_is_not_piper(self):
        app = {}
        await serve._warm_tts(app)
        self.assertIsNone(app.get("tts_warm_task"))


def _fake_provider_and_registry():
    """
    Shared helper for the AioHTTPTestCase classes below: wires serve.py's
    module-level _provider/_registry globals to lightweight fakes before
    build_app() runs, so on_startup hooks that call _get_provider() (e.g.
    _start_factory_watcher) construct nothing real and no network/API-key
    setup is needed just to exercise /api/tts or shutdown cleanup.
    """
    from agent.providers.base import BaseProvider, ProviderResponse, TokenUsage
    from agent.tools.registry import ToolRegistry

    class FakeProvider(BaseProvider):
        @property
        def model_name(self):
            return "fake-model"

        async def stream(self, messages, system, tools=None):
            yield ProviderResponse(text="ok", tool_calls=[], usage=TokenUsage(), model=self.model_name)

    serve._provider = FakeProvider()
    serve._registry = ToolRegistry()


class TestStopTtsWarmCleanup(AioHTTPTestCase):
    """
    Regression test for the shutdown crash fixed in serve.py's
    _stop_tts_warm: runs a REAL web.AppRunner (aiohttp.test_utils.TestServer
    wraps one) through both startup and cleanup with PIPER_VOICE_PATH
    pointed at a model that doesn't exist, so tts_warm_task fails with
    SynthesisError shortly after startup — the exact scenario the reviewer
    reproduced ("RUNNER CLEANUP RAISED: SynthesisError: ..."). Before the
    fix, tearing the runner down (which asyncTearDown does automatically via
    self.client.close()) would let that SynthesisError escape
    runner.cleanup() and fail this test during teardown.
    """

    def setUp(self):
        self._prev_path = os.environ.get("PIPER_VOICE_PATH")
        self._prev_provider = os.environ.get("TTS_PROVIDER")
        os.environ["PIPER_VOICE_PATH"] = "/nonexistent/path/voice.onnx"
        os.environ["TTS_PROVIDER"] = "piper"

    def tearDown(self):
        if self._prev_path is None:
            os.environ.pop("PIPER_VOICE_PATH", None)
        else:
            os.environ["PIPER_VOICE_PATH"] = self._prev_path
        if self._prev_provider is None:
            os.environ.pop("TTS_PROVIDER", None)
        else:
            os.environ["TTS_PROVIDER"] = self._prev_provider
        serve._provider = None
        serve._registry = None

    async def get_application(self):
        _fake_provider_and_registry()
        return serve.build_app()

    async def test_cleanup_does_not_raise_when_warm_task_failed(self):
        task = self.app.get("tts_warm_task")
        self.assertIsNotNone(task)
        # Let the warm-up actually finish (and fail) before cleanup runs,
        # mirroring the reviewer's repro ("seconds after startup") rather
        # than racing a still-pending task.
        with contextlib.suppress(SynthesisError):
            await task
        # No explicit assertion beyond this point: asyncTearDown() (run
        # automatically right after this method returns) closes the
        # TestClient, which tears down the real AppRunner underneath it and
        # runs every on_cleanup hook, including _stop_tts_warm. Before the
        # fix, that raised SynthesisError out of runner.cleanup() and this
        # test would error during teardown instead of passing normally.


class TestSynthesizeSpeechProviderRouting(AioHTTPTestCase):
    """
    End-to-end coverage of /api/tts's provider routing — config.py spends
    three paragraphs documenting tts_provider's branches (piper / elevenlabs
    / anything else falls through to piper) with zero prior assertions on
    any of it.
    """

    def setUp(self):
        self._prev_provider = os.environ.get("TTS_PROVIDER")
        self._prev_path = os.environ.get("PIPER_VOICE_PATH")

    def tearDown(self):
        if self._prev_provider is None:
            os.environ.pop("TTS_PROVIDER", None)
        else:
            os.environ["TTS_PROVIDER"] = self._prev_provider
        if self._prev_path is None:
            os.environ.pop("PIPER_VOICE_PATH", None)
        else:
            os.environ["PIPER_VOICE_PATH"] = self._prev_path
        serve._provider = None
        serve._registry = None

    async def get_application(self):
        _fake_provider_and_registry()
        return serve.build_app()

    async def test_elevenlabs_provider_returns_audio_mpeg(self):
        os.environ["TTS_PROVIDER"] = "elevenlabs"

        async def fake_synthesize(text, api_key, voice_id, model_id):
            return b"fake-mp3-bytes"

        with mock.patch("agent.voice.elevenlabs_tts.synthesize", fake_synthesize):
            resp = await self.client.post("/api/tts", json={"text": "hi"})
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.headers.get("Content-Type"), "audio/mpeg")
            body = await resp.read()
            self.assertEqual(body, b"fake-mp3-bytes")

    async def test_unrecognized_provider_falls_through_to_piper(self):
        # A typo (config.py documents this exact case) must land on Piper,
        # not error out or silently no-op.
        os.environ["TTS_PROVIDER"] = "elevenlab"
        os.environ["PIPER_VOICE_PATH"] = "/nonexistent/path/voice.onnx"

        resp = await self.client.post("/api/tts", json={"text": "hi"})
        # Piper's synthesize() 400s with a clear "not found" message when
        # the model file is missing. That specific 400 (not a 200 with
        # mpeg, not a 500) is the signal that routing actually fell through
        # to the Piper branch rather than doing nothing or hitting
        # ElevenLabs.
        self.assertEqual(resp.status, 400)
        text = await resp.text()
        self.assertIn("not found", text)


class TestElevenLabsGuard(unittest.IsolatedAsyncioTestCase):
    async def test_missing_key_raises_clear_error(self):
        with self.assertRaises(ElevenLabsSynthesisError) as ctx:
            await elevenlabs_synthesize("hello", api_key="", voice_id="x", model_id="eleven_flash_v2_5")
        self.assertIn("ELEVENLABS_API_KEY", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
