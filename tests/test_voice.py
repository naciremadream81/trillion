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
from agent.voice import piper_tts
from agent.voice.deepgram_stt import TranscriptionError, transcribe
from agent.voice.elevenlabs_tts import SynthesisError as ElevenLabsSynthesisError
from agent.voice.elevenlabs_tts import synthesize as elevenlabs_synthesize
from agent.voice.piper_tts import SynthesisError, is_warm, synthesize, warm_up


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


class TestPiperWarmUp(unittest.TestCase):
    """
    smooth-voice_2 Tier 4: loading the ~63MB ONNX model lazily meant the first
    spoken reply after every restart paid ~4s for it. warm_up() moves that to
    server startup. Loading a real model needs the (gitignored) voice file, so
    these cover the contract around it — the cache check and the failure mode —
    rather than the load itself.
    """

    def setUp(self):
        self._prev_loaded = piper_tts._loaded

    def tearDown(self):
        piper_tts._loaded = self._prev_loaded

    def test_not_warm_before_anything_is_loaded(self):
        piper_tts._loaded = None
        self.assertFalse(is_warm("/some/voice.onnx"))

    def test_warm_only_for_the_path_actually_loaded(self):
        piper_tts._loaded = ("/loaded/voice.onnx", object())  # stand-in for a loaded PiperVoice
        self.assertTrue(is_warm("/loaded/voice.onnx"))
        self.assertFalse(is_warm("/a/different/voice.onnx"))

    def test_warm_up_is_a_no_op_when_already_warm(self):
        # The path doesn't exist, so reaching the load would raise. Not
        # raising is the proof that it short-circuited on the cache.
        piper_tts._loaded = ("/nonexistent/path/voice.onnx", object())
        warm_up("/nonexistent/path/voice.onnx")

    def test_warm_up_raises_clear_error_for_a_missing_model(self):
        piper_tts._loaded = None
        with self.assertRaises(SynthesisError) as ctx:
            warm_up("/nonexistent/path/voice.onnx")
        self.assertIn("not found", str(ctx.exception))


class TestWarmPiperVoiceSkipsForNonPiperProvider(unittest.IsolatedAsyncioTestCase):
    """
    _warm_piper_voice must do nothing at all when Piper isn't the configured
    provider. With TTS_PROVIDER=elevenlabs, /api/tts never touches the ONNX
    model, so loading 63MB and burning a core for seconds at every boot buys
    nothing. Pinning TTS_PROVIDER here rather than inheriting it also keeps
    this suite honest about the env-leak class of failure that commit e84f7ae
    fixed: tests/__init__.py calls load_dotenv(), so an unpinned test silently
    takes whatever .env happens to say.
    """

    def setUp(self):
        self._prev = os.environ.get("TTS_PROVIDER")
        os.environ["TTS_PROVIDER"] = "elevenlabs"

    def tearDown(self):
        if self._prev is None:
            os.environ.pop("TTS_PROVIDER", None)
        else:
            os.environ["TTS_PROVIDER"] = self._prev

    async def test_skips_warm_up_when_provider_is_not_piper(self):
        app = {}
        await serve._warm_piper_voice(app)
        self.assertIsNone(app.get("piper_warmup_task"))


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
