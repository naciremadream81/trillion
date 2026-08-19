# Voice V1: ElevenLabs as a selectable TTS provider

*Design doc — 2026-08-17*

## Motivation

Voice V1 TTS currently runs on Piper only ([agent/voice/piper_tts.py](../../../agent/voice/piper_tts.py)) — local, offline, free. That choice was made deliberately: `piper_tts.py`'s own docstring and `agent/config.py`'s comment both record that ElevenLabs' free tier blocks all API voice access (premade *and* custom/cloned voices both require a paid plan — confirmed live, not assumed).

Sean now has a paid ElevenLabs plan and wants it wired in as a second option, not a replacement. This matches a ground rule already written into this project's own playbook ([playbooks/smooth-voice_2.md](../../../playbooks/smooth-voice_2.md), line 19): "never swap out your speech-to-text or text-to-speech provider without asking you first." Piper stays the default; ElevenLabs is opt-in via config.

## Scope

In scope: a new `agent/voice/elevenlabs_tts.py` module, new `Settings` fields, `serve.py`'s `/api/tts` handler dispatching on provider, `.env.example` documentation, and a guard test mirroring the existing Piper/Deepgram ones.

Out of scope: STT (Deepgram is unaffected), a runtime/UI provider switch (this is a deploy-time config choice, not a per-request or in-browser toggle), and streaming synthesis within a single sentence (the existing per-sentence request/response pattern already pipelines speech at the sentence level per the smooth-voice playbook's Tier 4 — true intra-sentence streaming would be a separate future optimization if latency numbers call for it).

## Decisions (resolved during brainstorming)

1. **Selectable via `TTS_PROVIDER`, default stays `"piper"`.** Nothing changes for existing deployments unless `TTS_PROVIDER=elevenlabs` is set explicitly. Matches the playbook rule above and the existing conditional-registration pattern this codebase already uses elsewhere (e.g. `search_provider` in `config.py`).

2. **Model tier: `eleven_flash_v2_5`.** Chosen for latency — the branch this work lands on (`docs/pi-voice-latency-numbers`) is actively instrumenting and tuning voice latency on a Raspberry Pi. Flash is ElevenLabs' lowest-latency model, built for real-time conversational use, at a modest quality trade-off vs. Multilingual v2. Configurable via `ELEVENLABS_MODEL_ID` if that trade-off ever needs revisiting.

3. **Default voice: ElevenLabs' standard premade "Rachel" (`21m00Tcm4TlvDq8ikWAM`), fully overridable.** A concrete default is needed for the code to do anything out of the box; Sean will swap in a real choice via `ELEVENLABS_VOICE_ID` once picked. Not a design commitment to that specific voice.

4. **Async HTTP call via `aiohttp`, not a thread executor.** Piper's `synthesize()` runs CPU-bound local inference and is offloaded to a thread (`loop.run_in_executor`) for that reason — see its own docstring. ElevenLabs is a network call; it follows `agent/voice/deepgram_stt.py`'s existing async `aiohttp` pattern instead, awaited directly on the event loop.

5. **Response format: raw MP3 bytes, `Content-Type: audio/mpeg`.** ElevenLabs' default `output_format` is `mp3_44100_128`; no format conversion is needed because the frontend (`index.html`'s `synthesizeAndPlay`) already reads `Content-Type` off the response and hands the raw bytes to a `Blob`/`Audio` element generically — confirmed by reading that code path, it never assumes WAV.

6. **Shared `SynthesisError` contract.** The new module raises the same exception name Piper's does. `serve.py`'s handler already catches `SynthesisError` and returns HTTP 400 with the message — reused as-is regardless of which provider raised it, preserving the existing "never crash, fail with a clear message" guarantee documented in `config.py`.

## Data flow

```
POST /api/tts { text }
  -> serve.py: synthesize_speech()
  -> settings.tts_provider == "elevenlabs"?
       yes -> agent/voice/elevenlabs_tts.synthesize(text, api_key, voice_id, model_id)
              -> aiohttp POST https://api.elevenlabs.io/v1/text-to-speech/{voice_id}
                   headers: xi-api-key
                   body: { text, model_id }
              -> 200: return MP3 bytes
              -> non-200 / missing api_key: raise SynthesisError(clear message)
              -> response: 200, Content-Type: audio/mpeg, body: MP3 bytes
       no  -> existing Piper path, unchanged (thread executor, WAV bytes)
  -> SynthesisError from either path -> HTTP 400, body: str(error)
```

## Module changes

### `agent/voice/elevenlabs_tts.py` (new)

```python
class SynthesisError(RuntimeError):
    pass

async def synthesize(text: str, api_key: str, voice_id: str, model_id: str) -> bytes:
    # missing api_key -> raise SynthesisError immediately, no request made
    # POST to ELEVENLABS_URL.format(voice_id=voice_id), xi-api-key header,
    # JSON body {"text": text, "model_id": model_id}
    # non-200 -> raise SynthesisError(f"ElevenLabs error {status}: {body[:200]}")
    # 200 -> return response bytes (MP3)
```

Mirrors `deepgram_stt.py`'s shape: module-level constant URL, one async function, one exception class, no client-side retry logic (matches this codebase's existing "fail fast" posture for voice providers).

### `agent/config.py`

New fields, adjacent to the existing Voice V1 block:

```python
tts_provider: str = "piper"          # "piper" | "elevenlabs"
elevenlabs_api_key: str = ""
elevenlabs_voice_id: str = "21m00Tcm4TlvDq8ikWAM"   # ElevenLabs premade "Rachel"; override once picked
elevenlabs_model_id: str = "eleven_flash_v2_5"
```

The existing comment block above `deepgram_api_key`/`piper_voice_path` gets rewritten — it currently states ElevenLabs is blocked outright, which is now stale (Sean has a paid plan). Replaced with a comment describing both providers as selectable, Piper as default.

`from_env()` (or equivalent) reads `TTS_PROVIDER`, `ELEVENLABS_API_KEY`, `ELEVENLABS_VOICE_ID`, `ELEVENLABS_MODEL_ID` — no `TRILLION_` prefix, matching `DEEPGRAM_API_KEY`/`PIPER_VOICE_PATH`'s existing convention in this same section.

### `serve.py`

`synthesize_speech()` branches on `settings.tts_provider` immediately after parsing `text`:

- `"elevenlabs"`: import and await `agent.voice.elevenlabs_tts.synthesize(...)` directly (no executor); catch its `SynthesisError` the same way; return with `content_type="audio/mpeg"`.
- anything else (including unset/unrecognized): existing Piper path, byte-for-byte unchanged.

### `.env.example`

New lines under the existing "Voice V1" section, commented out by default (matching how `PIPER_VOICE_PATH` is presented — optional override, not required):

```
# Optional: switch TTS provider to ElevenLabs (requires a paid ElevenLabs
# plan — the free tier blocks all API voice access, premade and custom
# voices alike). Default stays "piper" (local, free, offline) if unset.
# TTS_PROVIDER=elevenlabs
# ELEVENLABS_API_KEY=...
# ELEVENLABS_VOICE_ID=21m00Tcm4TlvDq8ikWAM
# ELEVENLABS_MODEL_ID=eleven_flash_v2_5
```

## Testing

Extend `tests/test_voice.py` with `TestElevenLabsGuard`, matching `TestDeepgramGuard`'s shape (an `IsolatedAsyncioTestCase`, since the new function is async):

```python
class TestElevenLabsGuard(unittest.IsolatedAsyncioTestCase):
    async def test_missing_key_raises_clear_error(self):
        with self.assertRaises(SynthesisError) as ctx:
            await synthesize("hello", api_key="", voice_id="x", model_id="eleven_flash_v2_5")
        self.assertIn("ELEVENLABS_API_KEY", str(ctx.exception))
```

No live-network test, same reasoning the file's own docstring already gives for Deepgram: real synthesis needs a live paid API key and network access, so only the "never crash, fail clearly" contract is locked in here.

## Error handling

Identical posture to the existing Piper/Deepgram guards: missing config never crashes the process — `/api/tts` returns HTTP 400 with a specific, actionable message (`"ElevenLabs is not configured (ELEVENLABS_API_KEY missing)."`) instead of a stack trace or a silent fallback to Piper. A silent fallback was considered and rejected — if `TTS_PROVIDER=elevenlabs` is set but misconfigured, Sean should see that immediately, not have it quietly degrade to a different voice.
