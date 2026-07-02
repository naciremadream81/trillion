# Trillion — Build Handoff
*Paste this into a new session to resume exactly where we left off.*

---

## What we're building

A voice-first AI assistant called **Trillion** — Sean's personal AI co-founder.
Not a demo. A real, daily-driver assistant built tier by tier so every layer
is independently testable before the next one starts.

The full spec lives in `AGENT.md` at the project root. Read that first.

Repo: https://github.com/naciremadream81/trillion (pushed, `main` branch).

---

## Status: Tier 1 + Tier 2 (partial) + cost dashboard + voice-smoothness (Tier 3/5) + browser voice V1 — all built, verified live

The text brain, tool-calling, a full cost/usage dashboard, prompt caching,
sign-off detection, and browser-based voice are all in and working. Voice
went through two pivots before landing: V0 (free browser STT/TTS) hit a hard
Chromium/Pi limitation; V1a (Deepgram + ElevenLabs) hit ElevenLabs' free-tier
paywall on *all* API voice access, premade or self-cloned; **V1 as shipped is
Deepgram (STT, cloud) + Piper (TTS, local/offline/free)** — see "Known issue"
below for the full history. Voice is fully working end-to-end and verified
live, not just built.

### What's in the project right now

```
trillion/
├── AGENT.md                          ← full spec, source of truth
├── HANDOFF.md                        ← this file
├── README.md
├── .env / .env.example               ← API keys + SUPABASE_ANALYTICS_URL (gitignored)
├── .gitignore
├── requirements.txt
├── main.py                           ← CLI entry point (REPL)
├── serve.py                          ← web server: UI + /api/usage + /api/chat
├── index.html                        ← orb UI, glass shell, cost panel, voice controller
├── usage.db                          ← SQLite cost/usage ledger (gitignored)
├── voices/
│   └── en_US-amy-medium.onnx(.json)  ← Piper voice model (gitignored, ~63MB — see below to re-download)
├── context/
│   └── analytics-supabase-schema.md  ← schema doc auto-loaded into system prompt
├── agent/
│   ├── core.py                       ← the conversation loop (turn-taking + tool loop)
│   ├── system_prompt.py              ← personality + system prompt + context/*.md loader
│   ├── turn_taking.py                ← Tier 5 sign-off detection
│   ├── config.py                     ← Settings dataclass, get_settings()
│   ├── voice/
│   │   ├── deepgram_stt.py           ← one-shot transcription via Deepgram REST API
│   │   └── piper_tts.py              ← local TTS via the piper-tts Python package
│   ├── providers/
│   │   ├── __init__.py               ← get_provider() factory (lazy imports per provider)
│   │   ├── base.py                   ← BaseProvider seam, TextChunk/ToolCall/ProviderResponse/TokenUsage
│   │   ├── _caching.py                ← Tier 3 Anthropic prompt-caching helper
│   │   ├── claude.py                 ← Anthropic Claude (primary, tool-use + caching + usage)
│   │   ├── openai_provider.py        ← OpenAI + OpenRouter
│   │   └── ollama.py                 ← local Ollama
│   ├── tools/
│   │   ├── base.py                   ← BaseTool ABC
│   │   ├── registry.py               ← ToolRegistry + build_registry(settings)
│   │   └── analytics_tool.py         ← QueryAnalyticsTool (read-only Supabase Postgres)
│   └── cost/
│       ├── pricing.py                ← $/M-token table, model matching
│       ├── storage.py                ← SQLite UsageRepo
│       ├── recorder.py               ← record_usage(), best-effort, never raises
│       └── aggregate.py              ← UsageDashboard (month-to-date, cache savings, budget alert)
└── tests/                            ← 58 tests, all passing (1 live-Supabase skip without env var)
```

Re-downloading the Piper voice model (gitignored, not in the repo):
```bash
mkdir -p voices && cd voices
curl -sL -o en_US-amy-medium.onnx "https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/amy/medium/en_US-amy-medium.onnx"
curl -sL -o en_US-amy-medium.onnx.json "https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/amy/medium/en_US-amy-medium.onnx.json"
```

### How to run it

```bash
cd /home/archie/Projects/trillion
python3 main.py               # text CLI, or just run: trillion   (installed launcher)
python3 serve.py               # web UI + orb + voice, on $TRILLION_WEB_PORT (default 8123)
```

`trillion-orb.service` (systemd --user) already runs `serve.py` on boot via
`loginctl enable-linger archie` — no login required. Check with
`systemctl --user status trillion-orb`.

The `trillion` command (`~/.local/bin/trillion`) and a desktop icon
(`~/.local/share/applications/trillion.desktop`) both launch `main.py` (the
text CLI) directly — no manual venv activation needed.

Slash commands inside the CLI: `/reset`, `/history`, `/model`, `/help`, `/quit`.

---

## What's actually been built (beyond the original Tier 1 plan)

1. **Tier 1 — text conversation loop.** Streaming, session history, three
   providers behind one seam. Done, verified.

2. **Tier 2 (partial) — tools.** Only one tool exists so far:
   `query_analytics`, a **read-only** Supabase/Postgres query tool (see below).
   The originally planned `web_search`, `draft_email`, `search_notes` were
   **never built** — Supabase access became the priority instead. Those three
   are still open (see "Open questions").

3. **Cost/usage dashboard (not in the original roadmap at all).** Full
   month-to-date cost tracking: per-model breakdown, per-source breakdown,
   daily sparkline, cache-savings math, month-over-month delta, soft budget
   alert (`TRILLION_MONTHLY_BUDGET_USD` env var, warns at 80%). Lives in
   `agent/cost/*` + the `#tr-cost` header button/panel in `index.html`.
   Every API call from both `main.py` and `serve.py` records a row.

4. **Read-only Supabase integration.** A `trillion_analytics` Postgres role
   (LOGIN, SELECT-only, `BYPASSRLS`, 5s statement timeout) connects via the
   **Shared/Transaction Pooler** (`*.pooler.supabase.com`, free, IPv4 — the
   Direct connection is IPv6-only unless you pay, so pooler is correct).
   `agent/tools/analytics_tool.py` validates SQL (SELECT/WITH only, no
   semicolon-chaining, keyword blocklist) before it ever reaches the DB —
   defense in depth on top of the role's own read-only grant. Verified live
   against a real `contacts` table (5 rows). Schema is documented in
   `context/analytics-supabase-schema.md`, which `system_prompt.py` loads
   automatically into every conversation.

5. **Fixed a live crash:** tool-calling used to throw
   `TypeError: Object of type ToolCall is not JSON serializable` — root cause
   was `agent/core.py` using OpenAI-style tool messages instead of Anthropic's
   real `tool_use`/`tool_result` block format. Fixed and verified with a real
   "how many contacts do we have?" round-trip.

6. **Voice-smoothness Tier 3 (prompt caching) and Tier 5 (sign-off
   detection)** — the two tiers from the "make your voice AI feel human"
   playbook that were actionable on a still-text-only agent:
   - `agent/providers/_caching.py` adds Anthropic `cache_control` breakpoints
     to the system prompt and the last message. Verified live: first call
     `cache_write_tokens=1505`, later calls showed growing
     `cache_read_tokens` (1505→1620→1647).
   - `agent/turn_taking.py`'s `is_signoff()` — deterministic, conservative,
     biased toward replying when unsure (never triggers on questions, long
     messages, commands, or the first utterance of a session). Wired into
     `agent/core.py → turn()` so a real goodbye ends the turn with no API
     call and no printed reply.

7. **Browser voice V1 (done, verified live end-to-end).** Chosen surface:
   the existing orb UI in the browser. Chosen interaction: push-to-talk (tap
   to start recording, tap again to stop-and-send — `MediaRecorder` has no
   built-in pause auto-finalize like V0's `SpeechRecognition` did, so both
   ends are now an explicit tap).
   - `serve.py` gained `/api/chat` (unchanged from V0: POST `{message}` →
     streams the real `Agent.turn()` reply as plain text), plus two new
     endpoints: `POST /api/transcribe` (audio blob in → Deepgram → `{text}`
     out) and `POST /api/tts` (one sentence in → WAV audio out, via Piper).
   - `agent/voice/deepgram_stt.py` posts the full recorded clip to
     Deepgram's one-shot REST endpoint (`nova-2` model). Needs
     `DEEPGRAM_API_KEY` in `.env`; cloud-based, has a real (small) per-minute
     cost.
   - `agent/voice/piper_tts.py` runs TTS **locally on the Pi** via the
     `piper-tts` Python package — no API key, no per-character cost, no
     internet dependency for this half of the pipeline. The ONNX voice model
     loads once into a module-level singleton and is reused across requests
     (reloading it per-request would make every reply noticeably slower).
     Synthesis is CPU-bound/blocking, so `serve.py` runs it via
     `loop.run_in_executor()` rather than awaiting it directly. Measured on
     this Pi 5: ~0.2–0.35x realtime factor (faster than real-time) once the
     model is warm; first request after a restart pays a one-time model-load
     cost (~4-6s).
   - `index.html`'s mic button runs a full voice controller: records via
     `MediaRecorder`, posts to `/api/transcribe`, sends the transcript to
     `/api/chat`, buffers the streamed reply and speaks complete sentences as
     they arrive (regex-split on `.!?`) by POSTing each to `/api/tts` and
     playing the returned WAV through an `Audio` element, queued via a
     promise chain so playback stays in order even if responses resolve out
     of sequence. Ties playback start/end to the orb's `setVoiceBright()`
     glow, and barge-in (new mic tap cancels any in-progress speech and
     drains the queue).
   - Every piece verified live, not assumed: Deepgram confirmed end-to-end
     with a real audio file; Piper confirmed via direct `/api/tts` curl
     calls returning real playable WAV audio at ~0.2-0.35x realtime, and via
     the full pipeline in the user's actual browser.

### Known issue — voice provider history (resolved, kept for context)

Voice went through three iterations before landing. Documented here so a
future session doesn't re-litigate decisions that are already made:

1. **V0 — browser-native `SpeechRecognition` + `speechSynthesis` (free, zero
   API keys).** Worked in principle, but the user's actual Raspberry Pi
   Chromium install failed STT with a `network` error. Root-caused (not
   guessed) via a direct `curl` against Google's real speech-recognition
   endpoint using the Debian Chromium package's own bundled API key —
   confirmed `403 Invalid key`. This is a **hard, permanent limitation**:
   Google restricts that endpoint to official Chrome-branded builds, and
   distro-packaged Chromium is not on the allowlist. Also checked whether
   Google Chrome's newly-announced ARM64 Linux build was a fix — it is not
   yet actually downloadable at chrome.com/download despite the
   announcement (user confirmed directly), so that path was a dead end too,
   at least for now.

2. **V1a — Deepgram (STT) + ElevenLabs (TTS), paid APIs.** Built in full:
   both provider modules, both `serve.py` endpoints, the `index.html`
   `MediaRecorder`-based rewrite. Deepgram worked immediately. ElevenLabs
   did not: the free tier returns `402 payment_required` /
   `paid_plan_required` for **any** premade "library" voice via the API —
   confirmed live, not a bug on our end. Investigated whether a
   *self-created* voice (Voice Design or Instant Cloning, as opposed to a
   library voice) would be exempt, since the error text specifically named
   "library voices." It is not exempt — also confirmed live: Voice Design
   returns a clean `403 feature_not_available`
   (`"Creating a voice through the API is only available on a paid plan"`),
   and Instant Cloning returns a permission error before even reaching that
   check (the free-tier key isn't provisioned with the
   `create_instant_voice_clone` scope at all). **There is no free path
   through ElevenLabs' API**, premade or custom.

3. **V1 as shipped — Deepgram (STT) + Piper (TTS, local).** Given both free
   paths were exhausted, researched whether a fully local/offline TTS engine
   was viable on a Pi 5 before deciding between "pay ElevenLabs" and "run
   something locally." Piper (open-source, ONNX-based, the default TTS in
   Home Assistant) measured at ~0.2-0.35x realtime on this hardware — fast
   enough to feel responsive. Chose Piper over an ElevenLabs upgrade: zero
   ongoing cost, zero vendor dependency, works offline, in exchange for a
   more synthetic-sounding voice than a premium cloud TTS. `elevenlabs_tts.py`
   was deleted (not deprecated in place) since it's fully unused now — see
   `git log` if a paid TTS provider is ever wanted again later.

This is the fork in the road the original voice playbook called out —
*"never swap out your speech-to-text or text-to-speech provider without
asking first."* Every swap above (V0→V1a, ElevenLabs→Piper) was confirmed
with the user via explicit choice before being built, not decided
unilaterally. Keep doing this if voice needs to change again (e.g. if Piper's
voice quality turns out to be unacceptable in daily use, or if
`DEEPGRAM_API_KEY` usage costs become a concern).

---

## Key decisions already made

| Decision | Choice | Why |
|----------|--------|-----|
| Language | Python 3.11+ (running on 3.13 in prod) | Best audio/HTTP/AI library support |
| Primary model | Claude (`claude-sonnet-4-6`) | Via Anthropic SDK |
| Alt providers | OpenAI, OpenRouter, Ollama | Same seam, swap with `TRILLION_PROVIDER=` env var |
| Runtime | Raspberry Pi 5 (aarch64, Debian 13), always-on via systemd | `trillion-orb.service` + linger |
| Voice end-state | Push-to-talk → wake word | Text-first always, voice is a layer |
| Voice V0 | Browser-native SpeechRecognition + speechSynthesis | Free, zero setup — **hit a permanent Pi/Chromium key-entitlement limitation** |
| Voice STT | Deepgram (cloud, paid) | Free browser STT was a hardware dead end on this Pi's Chromium |
| Voice TTS | Piper (local, free, offline) | ElevenLabs' free tier blocks **all** API voice access, premade or self-cloned — confirmed live; Piper avoids both the cost and the vendor gate |
| Safety gate | Per-action confirmation | Never send/spend/delete/change without explicit yes |
| Proactive | Yes, quiet by default | Earns interruptions, doesn't assume them |
| DB access | Read-only `trillion_analytics` role via Supabase Shared Pooler | Free tier, IPv4, defense in depth (role + SQL validator) |

---

## Architecture principles (hold the line on these)

1. **One core, many adapters.** A typed turn, a spoken turn, and a heartbeat-initiated
   turn all flow through `agent/core.py → Agent.turn()`. Never fork the agent logic
   for voice vs text.

2. **Provider behind a seam.** Nothing outside `agent/providers/` touches an SDK directly.
   Swap models = change one env var. `get_provider()` uses lazy imports so
   missing SDKs don't break unrelated code paths.

3. **Build tier by tier.** Don't start Tier N+1 until Tier N verifies. Debugging all
   layers at once is miserable.

4. **Tool registry is the extension point.** Adding a new capability = write one
   self-contained `BaseTool` and register it in `build_registry()`. Never edit
   the core loop.

5. **Never claim "should work."** Every feature above was proven with a real,
   observed output (a curl response, a live query, an actual token count) —
   not an assumption. Keep doing this.

6. **Never print secrets.** `.env`, DB passwords, connection strings — inspect
   only via safe metadata (length, prefix, host substring), never paste raw.

---

## Open questions (still unanswered)

1. **First "real" tool beyond analytics:** `web_search`, `draft_email`, and
   `search_notes` were the original Tier 2 plan and none exist yet. Still
   worth building, or has priority shifted?

2. **Notes location:** where do Sean's notes live — `~/notes/`, an Obsidian
   vault, Notion? (Note: a separate, unrelated Obsidian vault at
   `~/obsidian-vault/` is used for *Claude Code's own memory* — don't conflate
   the two if/when `search_notes` gets built.)

3. **Web search API:** Brave Search vs. Tavily, if/when that tool gets built.

4. **Piper voice quality in daily use:** chosen for zero cost/offline
   operation over ElevenLabs' quality. Worth checking in on after some real
   usage — if it feels too synthetic day-to-day, an ElevenLabs Starter
   upgrade (~$5/mo) is a small, already-scoped change (the old
   `elevenlabs_tts.py` code is in `git log` if this comes back up).

---

## Tier roadmap

| Tier | What | Status |
|------|------|--------|
| 0 | Interview + spec | ✅ Done |
| 1 | Text conversation loop, streaming, provider seam | ✅ Done |
| 2 | Tool registry + tools | 🟡 Partial — registry + `query_analytics` only |
| — | Cost/usage dashboard | ✅ Done (not in original roadmap) |
| — | Voice-smoothness: prompt caching + sign-off detection | ✅ Done |
| 3 | Voice V0: browser STT/TTS, push-to-talk | ⛔ Abandoned — permanent Pi/Chromium key limitation |
| 3 | Voice V1: Deepgram (STT) + Piper (TTS, local) | ✅ Done, verified live end-to-end |
| 4 | Persistent memory across restarts | ⬜ |
| 5 | Heartbeat — proactive, scheduled checks | ⬜ |
| 6 | Safety rails: confirmation gate, config file, audit log, kill switch | ⬜ |

---

## How to brief a new session

Paste this into the chat:

> "We're building Trillion. Read HANDOFF.md for full context. The brain,
> tools (Supabase read-only query), cost dashboard, voice-smoothness, and
> browser voice (Deepgram STT + local Piper TTS) are all built and verified
> live end-to-end. Next open items are the unbuilt Tier 2 tools
> (web_search/draft_email/search_notes) and persistent memory (Tier 4).
> Don't swap any provider (model, STT, TTS) without asking me first."
