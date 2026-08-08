"""
Deepgram speech-to-text (Voice V1).

One-shot transcription: the browser records a full push-to-talk utterance
with MediaRecorder and posts the whole clip here, rather than streaming —
push-to-talk already waits for the user to finish, so there's no need for
live partial transcripts.
"""

from __future__ import annotations

import aiohttp

DEEPGRAM_URL = "https://api.deepgram.com/v1/listen"


class TranscriptionError(RuntimeError):
    pass


async def transcribe(audio: bytes, content_type: str, api_key: str) -> str:
    if not api_key:
        raise TranscriptionError("Deepgram is not configured (DEEPGRAM_API_KEY missing).")

    headers = {
        "Authorization": f"Token {api_key}",
        "Content-Type": content_type or "audio/webm",
    }
    params = {"model": "nova-2", "smart_format": "true", "language": "en-US"}

    async with aiohttp.ClientSession() as session:
        async with session.post(
            DEEPGRAM_URL, params=params, headers=headers, data=audio
        ) as resp:
            if resp.status != 200:
                body = await resp.text()
                raise TranscriptionError(f"Deepgram error {resp.status}: {body[:200]}")
            data = await resp.json()

    try:
        return data["results"]["channels"][0]["alternatives"][0]["transcript"].strip()
    except (KeyError, IndexError):
        return ""
