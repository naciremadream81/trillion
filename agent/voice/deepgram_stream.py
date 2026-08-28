"""
Deepgram streaming speech-to-text (smooth-voice_2 Tier 2, the real signal).

The batch sibling (deepgram_stt.py) posts a finished recording and gets a
transcript back. That is correct for push-to-talk, where the stop-tap *is*
the end-of-turn signal, but it leaves hands-free mode with nothing to lean
on: index.html's VAD has to infer "they're done" from RMS energy alone,
because nothing reaches Deepgram until the recording is already over. The
comment above startVad in index.html spells this out and names this module
as the upgrade path.

Streaming changes that. Deepgram's WebSocket endpoint returns, mid-utterance:

  - interim transcripts, so the UI can show words as they land;
  - `speech_final` on a result — the recognizer's own "this utterance ended",
    derived from the audio it is already transcribing;
  - `UtteranceEnd` — fired after `utterance_end_ms` of silence following the
    last word, which is the belt to speech_final's braces (it still arrives
    when a noisy channel keeps `speech_final` from ever being set).

That is the confidence signal Tier 2 assumes you have. The VAD's own comment
is explicit that a fast/slow layered endpoint should branch on *this*, not on
"they said a lot", which is what an energy-only heuristic was standing in for.

**Why a server-side relay and not a direct browser connection.** Deepgram
authenticates the WebSocket with the API key. A browser connecting directly
would need that key in page JavaScript, which puts a paid credential in
every visitor's devtools. So serve.py relays: the browser talks to us, we
talk to Deepgram, and the key never leaves the process.

`normalize_message` is kept pure and separate from the socket for the same
reason the rest of this codebase splits decisions from I/O — the message
shapes are the part worth testing, and they can be tested without a network.
"""

from __future__ import annotations

import json
from typing import AsyncIterator

import aiohttp

DEEPGRAM_STREAM_URL = "wss://api.deepgram.com/v1/listen"

# How long Deepgram waits after the last word before declaring the utterance
# over. 1000ms is deliberately close to the VAD's own 1200ms silenceMs so the
# two paths feel the same to talk to — the difference is that this number is
# applied to *recognized speech*, not to microphone energy, so a pause full
# of room noise no longer reads as talking and a quiet breath no longer reads
# as finished.
DEFAULT_UTTERANCE_END_MS = 1000

# Silence (in ms) Deepgram requires before finalizing a segment and setting
# speech_final. Smaller is snappier and more likely to cut a slow talker;
# Deepgram's own default is aggressive for conversational use.
DEFAULT_ENDPOINTING_MS = 300


class StreamingTranscriptionError(RuntimeError):
    pass


def stream_params(
    *,
    utterance_end_ms: int = DEFAULT_UTTERANCE_END_MS,
    endpointing_ms: int = DEFAULT_ENDPOINTING_MS,
) -> dict:
    """
    Query parameters for the streaming endpoint.

    `interim_results` is not optional here even though we mostly care about
    finals: Deepgram only emits `UtteranceEnd` when interim results are on,
    and UtteranceEnd is the half of the endpoint signal that survives a noisy
    channel. Turning interims off to save bandwidth would silently disable
    the more reliable of the two signals.
    """
    return {
        "model": "nova-2",
        "smart_format": "true",
        "language": "en-US",
        "interim_results": "true",
        "utterance_end_ms": str(utterance_end_ms),
        "endpointing": str(endpointing_ms),
        "vad_events": "true",
    }


def normalize_message(raw: str | bytes | dict) -> dict | None:
    """
    Turn one Deepgram message into the small shape the browser consumes, or
    None for messages it has no use for (Metadata, keepalives, unknown types).

    The browser deliberately never sees Deepgram's own message shape. Keeping
    the wire format of a third party out of index.html means a Deepgram
    change is a change to this function, not to the voice controller — the
    same reason the provider seam exists for models.

    Returned shapes:
      {"type": "transcript", "text": str, "is_final": bool, "speech_final": bool}
      {"type": "utterance_end"}
      {"type": "speech_started"}
      {"type": "error", "message": str}
    """
    if isinstance(raw, (str, bytes)):
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            return None
    else:
        data = raw
    if not isinstance(data, dict):
        return None

    kind = data.get("type")

    if kind == "Results":
        channel = data.get("channel") or {}
        alternatives = channel.get("alternatives") or []
        transcript = ""
        if alternatives and isinstance(alternatives[0], dict):
            transcript = (alternatives[0].get("transcript") or "").strip()
        is_final = bool(data.get("is_final"))
        speech_final = bool(data.get("speech_final"))
        # An empty interim is pure noise — Deepgram emits them constantly
        # while listening. An empty *final* is dropped too, but an empty
        # result carrying speech_final is NOT: that is the endpoint signal
        # arriving on a segment that happened to transcribe to nothing, and
        # swallowing it would strand the turn open.
        if not transcript and not speech_final:
            return None
        return {
            "type": "transcript",
            "text": transcript,
            "is_final": is_final,
            "speech_final": speech_final,
        }

    if kind == "UtteranceEnd":
        return {"type": "utterance_end"}

    if kind == "SpeechStarted":
        return {"type": "speech_started"}

    if kind == "Error" or data.get("error"):
        message = data.get("description") or data.get("message") or data.get("error") or "unknown"
        return {"type": "error", "message": str(message)[:300]}

    # Metadata, KeepAlive acks, anything Deepgram adds later.
    return None


class DeepgramStream:
    """
    One live streaming connection. Async context manager.

        async with DeepgramStream(api_key) as stream:
            await stream.send_audio(chunk)
            ...
            await stream.finish()
            async for event in stream:
                ...

    In practice serve.py runs the send and receive halves concurrently, since
    audio keeps arriving while transcripts come back.
    """

    def __init__(
        self,
        api_key: str,
        *,
        session: aiohttp.ClientSession | None = None,
        utterance_end_ms: int = DEFAULT_UTTERANCE_END_MS,
        endpointing_ms: int = DEFAULT_ENDPOINTING_MS,
    ) -> None:
        if not api_key:
            raise StreamingTranscriptionError(
                "Deepgram is not configured (DEEPGRAM_API_KEY missing)."
            )
        self._api_key = api_key
        self._params = stream_params(
            utterance_end_ms=utterance_end_ms, endpointing_ms=endpointing_ms
        )
        self._session = session
        self._owns_session = session is None
        self._ws: aiohttp.ClientWebSocketResponse | None = None

    async def __aenter__(self) -> "DeepgramStream":
        if self._session is None:
            self._session = aiohttp.ClientSession()
        try:
            self._ws = await self._session.ws_connect(
                DEEPGRAM_STREAM_URL,
                params=self._params,
                headers={"Authorization": f"Token {self._api_key}"},
                heartbeat=10,
            )
        except Exception as e:
            if self._owns_session and self._session is not None:
                await self._session.close()
                self._session = None
            raise StreamingTranscriptionError(f"Could not open Deepgram stream: {e}") from e
        return self

    async def __aexit__(self, *exc) -> None:
        if self._ws is not None and not self._ws.closed:
            await self._ws.close()
        if self._owns_session and self._session is not None:
            await self._session.close()
        self._ws = None
        self._session = None

    async def send_audio(self, chunk: bytes) -> None:
        if self._ws is None or self._ws.closed or not chunk:
            return
        await self._ws.send_bytes(chunk)

    async def finish(self) -> None:
        """
        Tell Deepgram the audio is done so it flushes any held segment.

        Without this, the tail of the last utterance can sit unfinalized
        until the socket times out — the transcript arrives seconds late or
        not at all.
        """
        if self._ws is None or self._ws.closed:
            return
        try:
            await self._ws.send_str(json.dumps({"type": "CloseStream"}))
        except Exception:
            # A socket that died before we could say goodbye needs no goodbye.
            pass

    async def __aiter__(self) -> AsyncIterator[dict]:
        if self._ws is None:
            return
        async for message in self._ws:
            if message.type == aiohttp.WSMsgType.TEXT:
                event = normalize_message(message.data)
                if event is not None:
                    yield event
            elif message.type == aiohttp.WSMsgType.ERROR:
                yield {"type": "error", "message": "Deepgram socket error"}
                return
            elif message.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED):
                return
