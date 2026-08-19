"""
Piper text-to-speech (Voice V1 TTS) — local, offline, free.

The default TTS provider (see agent/config.py's tts_provider / TTS_PROVIDER).
ElevenLabs (agent/voice/elevenlabs_tts.py) is now available as an opt-in
alternative for Sean's paid plan, but Piper stays the default: no API key, no
per-character cost, no plan gate, works without internet. Trade-off is voice
quality — synthetic, not a human clone — in exchange for zero ongoing cost or
dependency on a vendor.

The voice model is loaded once and reused across requests; loading the
~63MB ONNX model per-request would make every reply noticeably slower.
Synthesis is CPU-bound and blocking, so callers should run it in a thread
(see serve.py, which uses loop.run_in_executor).

serve.py's asyncio default executor is a *multi-worker* thread pool, not a
single background thread — so two first-requests can race here. Two browser
tabs opened at once, or the startup warm-up task (serve.py's _warm_tts)
racing a real user's first turn, can both see `_loaded is None` at the same
time and both call PiperVoice.load(). That's not just wasted work: it loads
the 63MB model twice concurrently and leaves `_loaded` set to whichever
PiperVoice.load() call happens to finish (assign) last, while any synthesis
already in flight on the other one keeps using an object nothing else can
reach. The lock below closes that window with the standard double-checked
pattern: check unlocked (the fast path, once warm), then re-check inside the
lock before paying for a load, since another thread may have finished loading
while this one was waiting to acquire it.
"""

from __future__ import annotations

import io
import os
import threading
import wave

from piper.voice import PiperVoice

# The loaded voice and the path it was loaded from are held as ONE tuple in a
# single global, rather than as two separate globals. That keeps the pair
# atomic for the unlocked fast-path read in _load_voice below: two separate
# global writes would let a reader land between them and observe (new voice,
# old path) — i.e. a caller asking for the OLD path would be handed the NEW
# voice object. Unreachable today (PIPER_VOICE_PATH never changes mid-process,
# and this starts as None so the very first load is safe either way), but one
# tuple closes the window permanently at no cost.
_loaded: tuple[str, PiperVoice] | None = None
_voice_lock = threading.Lock()


class SynthesisError(RuntimeError):
    pass


def _load_voice(model_path: str) -> PiperVoice:
    global _loaded
    # Fast path: no lock once a voice is loaded and matches. This runs on every
    # synthesis call, and taking the lock unconditionally would serialize
    # otherwise-independent requests on the common case. One tuple read is
    # atomic, so this can never see a half-published pair.
    loaded = _loaded
    if loaded is not None and loaded[0] == model_path:
        return loaded[1]
    with _voice_lock:
        # Re-check inside the lock: another thread may have loaded this exact
        # model while we waited to acquire it, in which case reuse its work
        # rather than loading a second 63MB copy. Without this, serve.py's
        # default multi-worker executor lets two concurrent first-requests
        # (two browser tabs, or the startup warm-up racing a real user turn)
        # both observe None and both call PiperVoice.load.
        loaded = _loaded
        if loaded is not None and loaded[0] == model_path:
            return loaded[1]
        if not os.path.isfile(model_path):
            raise SynthesisError(f"Piper voice model not found at {model_path}.")
        config_path = model_path + ".json"
        if not os.path.isfile(config_path):
            raise SynthesisError(f"Piper voice config not found at {config_path}.")
        voice = PiperVoice.load(model_path, config_path=config_path)
        _loaded = (model_path, voice)
        return voice


def synthesize(text: str, model_path: str) -> bytes:
    voice = _load_voice(model_path)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav_file:
        voice.synthesize_wav(text, wav_file)
    return buf.getvalue()
