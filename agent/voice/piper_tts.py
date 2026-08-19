"""
Piper text-to-speech (Voice V1 TTS) — local, offline, free.

ElevenLabs' free tier blocks all API voice access (premade library voices
need a paid plan; so does creating a custom voice via Voice Design or
Instant Cloning — confirmed live, not assumed). Piper runs the model
on-device instead: no API key, no per-character cost, no plan gate, works
without internet. Trade-off is voice quality — synthetic, not a human
clone — in exchange for zero ongoing cost or dependency on a vendor.

The voice model is loaded once and reused across requests; loading the
~63MB ONNX model per-request would make every reply noticeably slower.
Synthesis is CPU-bound and blocking, so callers should run it in a thread
(see serve.py, which uses loop.run_in_executor).

That "loaded once" is lazy, which used to mean the *first* voice turn
after every process restart paid the load cost — measured at ~4s on this
Pi, and by far the largest single number in the smooth-voice_2 Tier 1
breakdown. warm_up() moves that cost to server startup, where nobody is
waiting on it; serve.py calls it in the background at boot.
"""

from __future__ import annotations

import io
import os
import threading
import wave

from piper.voice import PiperVoice

# The loaded voice and the path it came from are published as ONE tuple in a
# single global, rather than as two. That keeps the pair atomic for the
# unlocked fast-path read in _load_voice: two separate writes would let a
# reader land between them and see (new voice, old path), handing a caller
# asking for the OLD path the NEW voice object. Unreachable today
# (PIPER_VOICE_PATH doesn't change mid-process, and this starts as None so the
# first load is safe either way), but one tuple closes it permanently.
_loaded: tuple[str, PiperVoice] | None = None
_voice_lock = threading.Lock()

# Short, cheap, and never heard by anyone — just enough audio to force the
# first ONNX inference during warm_up(). Loading the model and *running* it
# are separate costs; only paying the first is a half-warm cache.
_WARM_UP_TEXT = "Ready."


class SynthesisError(RuntimeError):
    pass


def _load_voice(model_path: str) -> PiperVoice:
    global _loaded
    # Fast path: no lock once a voice is loaded and matches. This runs on every
    # synthesis call, and locking unconditionally would serialize otherwise
    # independent requests on the common case. One tuple read is atomic, so it
    # can never observe a half-published pair.
    loaded = _loaded
    if loaded is not None and loaded[0] == model_path:
        return loaded[1]
    with _voice_lock:
        # Re-check under the lock: another thread may have loaded this exact
        # model while we waited. Without this, serve.py's default multi-worker
        # executor lets two concurrent first-requests — two browser tabs, or
        # the startup warm-up racing a real user turn — both see None and both
        # load the ~63MB model, leaving one copy unreachable.
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


def is_warm(model_path: str) -> bool:
    """Whether synthesize() would skip the model load for this path."""
    loaded = _loaded
    return loaded is not None and loaded[0] == model_path


def warm_up(model_path: str) -> None:
    """
    Load the voice model and run one throwaway synthesis, so the first real
    request doesn't have to.

    Blocking and CPU-bound like synthesize() — call it from a thread. Raises
    SynthesisError when the model is missing so a caller can report *why* it
    stayed cold; serve.py logs that and carries on, since TTS being cold is a
    slow first reply, not a broken server.
    """
    if is_warm(model_path):
        return
    synthesize(_WARM_UP_TEXT, model_path)
