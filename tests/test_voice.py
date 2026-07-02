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

from agent.voice.deepgram_stt import TranscriptionError, transcribe
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


if __name__ == "__main__":
    unittest.main()
