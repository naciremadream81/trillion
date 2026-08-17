"""
Tests for Voice V1 (Deepgram STT + Piper TTS) — the missing-config guards.

Real transcription needs a live Deepgram API key and network access, so
that path isn't covered here; this locks in the "never crash, fail with a
clear message" contract when the key or voice model is missing. Piper
itself runs locally, so its guard test doesn't need network access — it
just points at a model path that doesn't exist.

Run: python -m unittest tests.test_voice
"""

import unittest

from agent.voice import piper_tts
from agent.voice.deepgram_stt import TranscriptionError, transcribe
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
        self._prev_voice = piper_tts._voice
        self._prev_path = piper_tts._voice_model_path

    def tearDown(self):
        piper_tts._voice = self._prev_voice
        piper_tts._voice_model_path = self._prev_path

    def test_not_warm_before_anything_is_loaded(self):
        piper_tts._voice = None
        piper_tts._voice_model_path = None
        self.assertFalse(is_warm("/some/voice.onnx"))

    def test_warm_only_for_the_path_actually_loaded(self):
        piper_tts._voice = object()  # stand-in for a loaded PiperVoice
        piper_tts._voice_model_path = "/loaded/voice.onnx"
        self.assertTrue(is_warm("/loaded/voice.onnx"))
        self.assertFalse(is_warm("/a/different/voice.onnx"))

    def test_warm_up_is_a_no_op_when_already_warm(self):
        # The path doesn't exist, so reaching the load would raise. Not
        # raising is the proof that it short-circuited on the cache.
        piper_tts._voice = object()
        piper_tts._voice_model_path = "/nonexistent/path/voice.onnx"
        warm_up("/nonexistent/path/voice.onnx")

    def test_warm_up_raises_clear_error_for_a_missing_model(self):
        piper_tts._voice = None
        piper_tts._voice_model_path = None
        with self.assertRaises(SynthesisError) as ctx:
            warm_up("/nonexistent/path/voice.onnx")
        self.assertIn("not found", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
