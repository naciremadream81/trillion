"""
ElevenLabs text-to-speech (Voice V1 TTS) — cloud, paid plan required.

An opt-in alternative to the default Piper provider (see agent/voice/
piper_tts.py, agent/config.py's tts_provider / TTS_PROVIDER). Piper's
synthesize() is CPU-bound local inference and gets offloaded to a thread by
its caller for exactly that reason (see its own docstring). This module does
the opposite: synthesis happens on ElevenLabs' servers, so the work here is
just one network round trip. That's I/O-bound, not CPU-bound, so it follows
agent/voice/deepgram_stt.py's existing pattern instead — an async aiohttp
call awaited directly on the event loop, no thread executor needed. Piping a
network-bound call through a thread would only add a context switch for
nothing in return.

Deliberately no retry logic here, matching deepgram_stt.py's shape: a failed
call surfaces immediately as SynthesisError, which serve.py already turns
into an HTTP 400 with the error text. See docs/superpowers/specs/
2026-08-17-elevenlabs-tts-provider-design.md for the full design rationale
(model/voice defaults, why streaming-latency and output-format overrides
were deliberately left out, etc.).
"""

from __future__ import annotations

import aiohttp

ELEVENLABS_URL = "https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"


class SynthesisError(RuntimeError):
    pass


async def synthesize(text: str, api_key: str, voice_id: str, model_id: str) -> bytes:
    if not api_key:
        raise SynthesisError("ElevenLabs is not configured (ELEVENLABS_API_KEY missing).")

    url = ELEVENLABS_URL.format(voice_id=voice_id)
    headers = {"xi-api-key": api_key}
    body = {"text": text, "model_id": model_id}

    async with aiohttp.ClientSession() as session:
        async with session.post(url, headers=headers, json=body) as resp:
            if resp.status != 200:
                error_body = await resp.text()
                raise SynthesisError(f"ElevenLabs error {resp.status}: {error_body[:200]}")
            return await resp.read()
