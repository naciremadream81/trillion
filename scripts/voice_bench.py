#!/usr/bin/env python3
"""
Voice server-leg benchmark — a read-only script, NOT a test.

Measures only the SERVER-SIDE legs of a Trillion voice turn: how long
POST /api/tts, POST /api/chat, and POST /api/transcribe take once a request
reaches this process. It cannot and does not measure mic capture, end-of-turn
(sign-off) detection, or browser-side audio playback — those all happen in
the browser, and this script never opens one. It is a SUBSET of the Tier 1
browser breakdown already instrumented in index.html (window.
trillionVoiceLatency logs stop-speaking -> transcript-final -> first-model-
token -> first-audio-byte -> first-sound-playing for a real turn end to end)
rather than a replacement for it. Use this to isolate whether a slowdown is
server-side or browser-side; use the browser instrumentation for the number
that actually matters to a person waiting for a reply.

This script spends REAL money on every run: /api/chat calls whatever model
provider is configured, /api/tts calls ElevenLabs if TTS_PROVIDER=elevenlabs
is set, and /api/transcribe calls Deepgram. Prompts here are kept short on
purpose. Don't loop this in a tight cron job.

Run against an already-running `python serve.py` (or the trillion-orb
systemd service):

    python scripts/voice_bench.py
    python scripts/voice_bench.py --url http://localhost:8123

If TRILLION_WEB_AUTH_TOKEN is set in the environment, it's sent as
`Authorization: Bearer <token>` on every request, matching what serve.py's
bearer_auth_middleware requires once that token is configured (agent/
security/auth.py). No Origin or Sec-Fetch-Site header is sent — this is a
plain Python HTTP client, and agent/security/origin.py's check_origin() rule
3 explicitly allows requests carrying neither header (curl, systemd, the
CLI): the browser-forgery attack that gate defends against doesn't apply to
a script run by hand against your own server, so no CSRF workaround is
needed here.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import time

import aiohttp
from dotenv import load_dotenv

# Same as every other entrypoint in this repo (main.py, serve.py,
# tests/__init__.py): TRILLION_WEB_AUTH_TOKEN is documented in .env.example,
# so "read it from the environment" has to mean the environment *after* .env
# is loaded. Without this, the day that token gets set in .env every leg here
# would 401 and the script would quietly report auth failures as latency data.
load_dotenv()

DEFAULT_URL = "http://localhost:8123"

# Kept short deliberately (see module docstring): every run of this script
# is a real, billed request to whichever chat provider is configured.
_CHAT_PROMPT = "Say OK."

# Three lengths so the TTS numbers show how synthesis time scales with
# sentence length, matching the shape of the numbers already recorded in
# README.md's latency table (which was gathered the same way, by hand).
_TTS_SENTENCES = [
    ("short (~40 chars)", "The quick brown fox jumps over the fence."),
    (
        "medium (~120 chars)",
        "Trillion runs on a Raspberry Pi and handles voice input, a model "
        "reply, and speech output for every single turn.",
    ),
    (
        "long (~250 chars)",
        "This is a longer sentence meant to approximate a full assistant "
        "reply, long enough to show whether synthesis time keeps scaling "
        "roughly linearly with text length or whether some fixed per-request "
        "overhead starts to dominate the total instead.",
    ),
]

# Which of the three TTS sentences gets round-tripped through /api/transcribe
# to check STT accuracy. Picked to be the shortest — this leg alone spends
# real Deepgram money, no reason to pay for the long one too.
_ROUND_TRIP_INDEX = 0


def _auth_headers() -> dict[str, str]:
    # Never hardcode the token — bearer_auth_middleware (agent/security/
    # auth.py) is a no-op when settings.web_auth_token is empty (the
    # loopback default), so most runs of this script send no auth header at
    # all. Only attach one if the environment says the server actually
    # requires it.
    token = os.getenv("TRILLION_WEB_AUTH_TOKEN")
    if not token:
        return {}
    return {"Authorization": f"Bearer {token}"}


async def _read_streamed(
    resp: aiohttp.ClientResponse, start: float
) -> tuple[float | None, bytes]:
    """
    Consume a response body chunk by chunk, returning the seconds from
    `start` until the *first* body chunk arrived (or None if the body was
    empty) alongside the full concatenated body.

    `start` is passed in by the caller, taken BEFORE session.post — it is not
    sampled here. That distinction is the whole point of this helper. aiohttp's
    `async with session.post(...) as resp:` returns once headers arrive, so
    sampling the clock on entry here would measure headers -> first body byte
    rather than request -> first body byte. For /api/chat that gap is real and
    interesting. For /api/tts it is ~0 and meaningless, because /api/tts is a
    fully buffered web.Response (serve.py builds the entire WAV/MP3 before
    returning): its headers and body leave the process together, so an
    entry-sampled clock would report ~0ms no matter how long synthesis took.
    Note that "sentence-at-a-time" audio describes the *client's* pattern of
    issuing one /api/tts request per sentence (see index.html), not response
    streaming — a distinction worth keeping straight, because conflating the
    two is exactly what produced a bogus 0ms first-byte number here once
    already. Only /api/chat is a true StreamResponse.

    The body is still read via iter_any() rather than resp.read() so that on
    the endpoint that genuinely does stream, the first-byte number reflects a
    real chunk arrival instead of the completed download.
    """
    first_byte_at: float | None = None
    chunks: list[bytes] = []
    async for chunk in resp.content.iter_any():
        if first_byte_at is None:
            first_byte_at = time.perf_counter()
        chunks.append(chunk)
    first_byte_s = (first_byte_at - start) if first_byte_at is not None else None
    return first_byte_s, b"".join(chunks)


async def bench_tts(
    session: aiohttp.ClientSession, base_url: str, label: str, text: str
) -> dict:
    start = time.perf_counter()
    async with session.post(f"{base_url}/api/tts", json={"text": text}) as resp:
        content_type = resp.headers.get("Content-Type", "")
        status = resp.status
        first_byte_s, body = await _read_streamed(resp, start)
    total_s = time.perf_counter() - start
    return {
        "label": label,
        "chars": len(text),
        "status": status,
        "content_type": content_type,
        "first_byte_s": first_byte_s,
        "total_s": total_s,
        "audio": body,
        "text": text,
    }


async def bench_chat(session: aiohttp.ClientSession, base_url: str, message: str) -> dict:
    start = time.perf_counter()
    async with session.post(f"{base_url}/api/chat", json={"message": message}) as resp:
        status = resp.status
        first_byte_s, body = await _read_streamed(resp, start)
    total_s = time.perf_counter() - start
    return {
        "status": status,
        "first_byte_s": first_byte_s,
        "total_s": total_s,
        "reply": body.decode("utf-8", errors="replace"),
    }


async def bench_transcribe(
    session: aiohttp.ClientSession,
    base_url: str,
    audio: bytes,
    source_text: str,
    content_type: str,
) -> dict:
    # Same trick README.md's STT numbers were gathered with: synthesize a
    # known sentence, transcribe it back, and check the transcript matches —
    # a cheap way to get a real, live-API STT number without a human in the
    # loop reading a sentence into a mic.
    #
    # content_type is whatever /api/tts actually returned, not a hardcoded
    # audio/wav: with TTS_PROVIDER=elevenlabs the bytes are MP3, and labelling
    # them WAV would leave Deepgram to sniff its way out of our own mistake.
    start = time.perf_counter()
    headers = {"Content-Type": content_type or "audio/wav"}
    async with session.post(f"{base_url}/api/transcribe", data=audio, headers=headers) as resp:
        status = resp.status
        data = await resp.json()
    total_s = time.perf_counter() - start
    transcript = (data.get("text") or "").strip()
    # Loose containment check, not equality — Deepgram may add punctuation,
    # change casing, or drop a trailing period that Piper's TTS output never
    # had a chance to make audible in the first place.
    matched = source_text.strip().lower().rstrip(".") in transcript.lower()
    return {
        "status": status,
        "total_s": total_s,
        "transcript": transcript,
        "matched": matched,
    }


def _fmt(seconds: float | None) -> str:
    return f"{seconds * 1000:.0f}ms" if seconds is not None else "n/a"


def _print_table(title: str, rows: list[list[str]], headers: list[str]) -> None:
    print(f"\n{title}")
    widths = [
        max(len(h), *(len(r[i]) for r in rows)) if rows else len(h)
        for i, h in enumerate(headers)
    ]
    line = "  ".join(h.ljust(w) for h, w in zip(headers, widths))
    print(line)
    print("-" * len(line))
    for row in rows:
        print("  ".join(c.ljust(w) for c, w in zip(row, widths)))


async def run(base_url: str) -> None:
    headers = _auth_headers()
    async with aiohttp.ClientSession(headers=headers) as session:
        print(f"Benchmarking server legs at {base_url} (server-side only — see module docstring)")

        tts_results = []
        for label, text in _TTS_SENTENCES:
            result = await bench_tts(session, base_url, label, text)
            tts_results.append(result)

        chat_result = await bench_chat(session, base_url, _CHAT_PROMPT)

        round_trip_source = tts_results[_ROUND_TRIP_INDEX]
        transcribe_result = None
        if round_trip_source["status"] == 200 and round_trip_source["audio"]:
            transcribe_result = await bench_transcribe(
                session,
                base_url,
                round_trip_source["audio"],
                round_trip_source["text"],
                round_trip_source["content_type"],
            )

    tts_rows = [
        [
            r["label"],
            str(r["chars"]),
            str(r["status"]),
            r["content_type"],
            _fmt(r["first_byte_s"]),
            _fmt(r["total_s"]),
        ]
        for r in tts_results
    ]
    _print_table(
        "POST /api/tts",
        tts_rows,
        ["sentence", "chars", "status", "content-type", "first-byte", "total"],
    )

    _print_table(
        "POST /api/chat (first model token)",
        [[str(chat_result["status"]), _fmt(chat_result["first_byte_s"]), _fmt(chat_result["total_s"])]],
        ["status", "first-byte", "total"],
    )
    print(f"  reply: {chat_result['reply'][:80]!r}")

    if transcribe_result is not None:
        _print_table(
            "POST /api/transcribe (TTS -> STT round trip)",
            [
                [
                    str(transcribe_result["status"]),
                    _fmt(transcribe_result["total_s"]),
                    "yes" if transcribe_result["matched"] else "no",
                ]
            ],
            ["status", "total", "transcript matched source"],
        )
        print(f"  source:     {round_trip_source['text']!r}")
        print(f"  transcript: {transcribe_result['transcript']!r}")
    else:
        print(
            "\nPOST /api/transcribe: skipped (the TTS call it depends on didn't "
            "return audio — see the /api/tts table above for its status)."
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--url",
        default=DEFAULT_URL,
        help=f"Base URL of a running Trillion server (default: {DEFAULT_URL}, "
        "matching serve.py's default TRILLION_WEB_PORT of 8123).",
    )
    args = parser.parse_args()
    asyncio.run(run(args.url.rstrip("/")))


if __name__ == "__main__":
    main()
