# Trillion

> Sean Swonger's personal AI co-founder — a voice-first assistant built text-first, with providers, tools, cost tracking, and factories behind one agent core.

Trillion is a **single-user** Python agent: chat in the terminal or browser, swap model providers with one env var, track spend in SQLite, spawn specialist sub-agents (with approval), and run Software Factory builds into `generated-projects/`. Product intent and safety rules live in [`AGENT.md`](AGENT.md). Session resume notes are in [`HANDOFF.md`](HANDOFF.md) (may lag the code — trust this README and the tree for what runs today).

**Working today:** text brain (CLI + web chat), provider seam, tool registry, cost dashboard, Agent Factory, Software Factory, voice V1 (Deepgram STT + selectable TTS: local Piper by default or ElevenLabs via `TTS_PROVIDER=elevenlabs`, wired to `POST /api/transcribe` and `POST /api/tts`), hands-free voice mode (optional; push-to-talk remains default), Tier 6 safety rails (confirmation gate, audit log, `/pause` kill switch), durable cross-session memory (`agent/memory.py` + `remember_fact`/`forget_fact`), the heartbeat scheduler with quiet hours and the Code Sentinel, the `search_notes` and `draft_email` tools, untrusted-content sanitization on every tool result, and the security shield (`GET /api/security/status`).

**Not done yet:** streaming STT (which would let end-of-turn detection lean on the recognizer's own endpoint signal instead of audio energy alone), acoustic barge-in (talking over Trillion mid-reply — deliberately not implemented, since on an open-speaker Pi its own output re-enters the mic and can self-trigger an endpoint), and server-side cancellation when a client aborts (today an aborted `/api/chat` keeps generating until a write hits the dropped connection, so the tail of an interrupted reply is still billed). A provider swap (model, STT, TTS) still needs Sean's say-so first.

Self-knowledge (`agent/selfknowledge/`, generating `context/self/trillion.md`) and cosmic-orb UI tiers 4-6 (sub-agent constellation, dispatch beams/rings, performance mode, `prefers-reduced-motion`) are built — the orb UI change couldn't be visually verified against a real WebGL context in this session's sandboxed preview browser (no GPU there), so treat it as code-reviewed and unit-tested but not yet eyeballed running; check it in a real browser before relying on it.

Voice latency instrumentation (smooth-voice_2 Tier 1, measure-only) is built into `index.html`'s voice flow: `console.log`s a per-turn breakdown (stop speaking → transcript final → first model token → first audio byte → first sound playing) and leaves it at `window.trillionVoiceLatency`. Real numbers, measured against the actual deployed `trillion-orb.service` on the Pi 5 itself (not a separate dev machine — a prior pass through this README mistakenly assumed otherwise):

| Leg | Real, measured |
|---|---|
| STT (Deepgram nova-2) | ~670ms–1.2s, scales with clip length (verified via a real TTS→STT round trip, transcript matched the source text) |
| Model (Claude, first token) | ~1.2–1.4s for a short reply |
| TTS (Piper, local) | ~180–620ms/sentence once warm, scales with sentence length. There used to be **~3.5s extra on the very first synthesis after the process starts** while the ~63MB voice model loaded; that cost is now paid at server startup instead — see below |

Straight add of the three "first token/byte" legs is ~2.05–3.22s before the first sound plays once the process is warm (STT + model + TTS-warm ranges above).

### The cold-start tax is gone (measured before/after)

`serve.py`'s `_warm_tts` startup hook synthesizes one short word in a background
thread as the server comes up, so the ONNX session init and first-inference
allocation are already done before anyone speaks. It is best-effort and never
blocks startup — a missing voice model prints one line and the server continues.

Measured with `scripts/voice_bench.py` on the Pi, same machine, same load, on the
first `/api/tts` request of a fresh process:

| | first-byte |
|---|---|
| before (cold) | **4822ms** |
| after (warmed at startup) | **1158ms** |

**~3.7s removed from the first voice turn after any restart or deploy.** The
1158ms figure is marginally *faster* than the same build's warm reading, i.e. the
first request now behaves as a warm one — the tax is eliminated, not reduced.
A fresh process is therefore ~2.05–3.22s to first sound, the same as a warm one,
rather than the ~5.55–6.72s it used to be.

> **Caveat on the per-sentence numbers above.** The ~180–620ms/sentence figures
> are the earlier idle-machine measurements and are deliberately left as-is. A
> re-run during this work reported 1338–4839ms/sentence, but the Pi was saturated
> at the time (load average 4.18 on 4 cores, mostly from the coding session doing
> the work), so those readings are not comparable and were not published. The
> cold-start delta above *is* safe to publish from the same loaded run, because a
> before/after comparison on one machine cancels the load out — an absolute
> per-sentence number does not. **The per-sentence row still wants an idle
> re-measure.**

### `scripts/voice_bench.py`

Read-only benchmark of the **server** legs only — `/api/tts`, `/api/chat`, and an
`/api/transcribe` round trip that synthesizes a known sentence and transcribes it
back. It cannot measure mic capture, end-of-turn detection, or browser playback;
those live in the browser and are what `window.trillionVoiceLatency` reports. Use
the script to tell whether a slowdown is server-side, and the browser breakdown
for the number a waiting person actually feels. It spends real Deepgram and model
credits on every run.

```bash
python scripts/voice_bench.py
```

**Tiers 2-6, acted on against those numbers:**

- **Tier 2 (end-of-turn detection) — hands-free VAD, opt-in.** An earlier pass recorded this tier as N/A because voice was push-to-talk: the stop-tap *was* the end-of-turn signal. That note also said "if voice ever goes hands-free, this tier comes back" — it has. A header toggle (default off) opens a browser-side `AnalyserNode` detector at 20Hz that ends the turn on silence; push-to-talk is untouched when it's off, and a tap still works when it's on. It uses **one** silence threshold (1200ms), not the layered fast/slow endpointing the playbook describes. Tier 2 assumes you can lean on the recognizer's own end-of-utterance signal for confidence, and `/api/transcribe` is batch Deepgram, so there isn't one. A layered version was built and measured: its confidence signal — a long final speech burst — turned out to mean "they said a lot", not "they finished", and it cut people off mid-sentence. Removed in favour of one honest number. Tunables live on `window.trillionVoiceVad` and are read every tick, so they can be adjusted mid-conversation from the console.
- **Tier 3 (prompt-caching hygiene) — audited, found already correct, now guarded.** The classic failure (caching on, but something that changes every turn sits inside the cached prefix) isn't present: the system prompt is byte-identical build to build, and `append_voice_cue()` runs strictly *after* `apply_prompt_caching()`, so the per-turn cue lands after every breakpoint. Working code left alone; `tests/test_caching.py::TestCachedPrefixStaysStable` locks the property in so it can't silently regress.
- **Tier 4 (the biggest measured number) — Piper's cold start moved off the critical path.** `warm_up()` loads the voice model *and* runs one throwaway inference (loading and first-inference are separate costs); `serve.py` schedules it as a background startup task, so it never blocks the server binding — confirmed live on the Pi, where the bind line prints *before* `Piper voice model warm.` A missing model logs and stays cold, and `/api/tts` still returns its own clear error. The before/after numbers are above. `_load_voice` also takes a lock: `synthesize` runs on a multi-worker executor, so two concurrent first-requests could otherwise both load the ~63MB model.
- **Tier 4 (provider choice) — ElevenLabs is selectable.** `TTS_PROVIDER=elevenlabs` (default stays `piper`), model `eleven_flash_v2_5`, requires a paid plan. Piper remains the default and its path is unchanged.
- **Tier 6 (polish/protect) — barge-in actually interrupts, and sentences stop fragmenting.** Barge-in bugs: the in-flight `/api/chat` stream kept running after an interrupt (fixed with `AbortController`, treating `AbortError` as intentional); resetting the speech queue didn't cancel the already-running chain, so the next sentence played over the new turn (fixed with per-turn identity checked at every await boundary); `audio.pause()` fires neither `ended` nor `error`, so the awaited promise never settled and the object URL leaked (fixed with both an abort listener and `audio.onpause = resolve`); and stale `.finally()` handlers drove the pending-speech counter negative, leaving the orb stuck on `processing` for the rest of the session. Separately, the sentence splitter anchored its end-of-buffer match to the *partial* stream, so a chunk ending just after a `.` spoke a fragment — `"That costs $1."`, then `"5 million."` — now fixed with abbreviation guards and a final-flush pass. Both new pure functions ship console self-tests (`window.trillionSplitSentencesSelfTest()`, `window.trillionVadSelfTest()`); there is no JS test runner in this repo, so those are the executable record.


---

## Quick start

**Prerequisites:** Python 3.11+ (see `.python-version`), an API key for at least one provider, and [`bubblewrap`](https://github.com/containers/bubblewrap) (`apt install bubblewrap` / `dnf install bubblewrap`) if you want the Software Factory to actually run project test suites — without it, `run_project_tests` refuses to run rather than executing untrusted, LLM-authored test commands unsandboxed.

```bash
cd trillion
python3 -m venv .venv && source .venv/bin/activate
pip install -e .        # installs deps from requirements.txt + registers the `trillion` command
cp .env.example .env   # add keys — never commit .env
trillion
```

`pip install -e .` also still leaves `python main.py` / `python serve.py` working exactly as before — `trillion` is an additive shortcut, not a replacement (`pip install -r requirements.txt` on its own, without the editable install, skips registering the command).

Optional provider override on the CLI:

```bash
trillion --provider openai   # or ollama
```

Web UI + cost dashboard (default port `8123`):

```bash
trillion serve
# or
TRILLION_WEB_PORT=8123 trillion serve
# fixed-port service mode
TRILLION_WEB_STRICT_PORT=1 TRILLION_WEB_PORT=8123 trillion serve
```

Then open the URL printed by the server — by default `http://localhost:8123/`, falling forward to the next free local port if `8123` is already in use. It serves `index.html`, `POST /api/chat`, and `GET /api/usage`.

---

## Configuration

Copy [`.env.example`](.env.example) → `.env` and fill in secrets. The CLI and `serve.py` both load dotenv.

### Providers

| Variable | Purpose |
|----------|---------|
| `TRILLION_PROVIDER` | `claude` (default), `openai`, or `ollama` |
| `ANTHROPIC_API_KEY` | Required for Claude |
| `CLAUDE_MODEL` | Default `claude-sonnet-4-6` |
| `OPENAI_API_KEY` / `OPENAI_MODEL` | OpenAI (default model `gpt-4o`) |
| `OPENAI_BASE_URL` | Set for OpenRouter (e.g. `https://openrouter.ai/api/v1`) with an OpenRouter key/model |
| `OLLAMA_BASE_URL` / `OLLAMA_MODEL` | Local Ollama (defaults `http://localhost:11434`, `llama3.2`) |

### Cost dashboard

| Variable | Default / notes |
|----------|-----------------|
| `TRILLION_USAGE_DB` | `usage.db` (SQLite) |
| `TRILLION_WEB_PORT` | `8123` |
| `TRILLION_MONTHLY_BUDGET_USD` | Soft alert only (amber ~80%, red ~100%) — **does not** block spending |

### Tools & factories

| Variable | Purpose |
|----------|---------|
| `SUPABASE_ANALYTICS_URL` | asyncpg DSN; if set, registers the read-only analytics tool (`agent/config.py`) |
| `BRAVE_SEARCH_API_KEY` / `FIRECRAWL_API_KEY` | Either registers the `web_search` tool (main chat) and enables the Software Factory's opportunity scout (autonomous scheduler); set both for a fallback option |
| `TRILLION_SEARCH_PROVIDER` | Optional `brave` or `firecrawl` to pick explicitly; if unset and both keys are set, Firecrawl is used |
| `FIRECRAWL_BASE_URL` | Override for a self-hosted Firecrawl instance (default `https://api.firecrawl.dev`) |
| `TRILLION_SOFTWARE_FACTORY_ROOT` | Build output root (default `generated-projects/`; path-jailed) |
| `TRILLION_FACTORY_DAILY_BUILD_CAP` | Hard daily build cap (default `3`) |
| `TRILLION_FACTORY_DAILY_BUDGET_USD` | Optional hard daily $ cap for builds |
| `TRILLION_FACTORY_PAUSED` | Kill switch (`1`/`true`/…) — stops new builds without restart |
| `TRILLION_FACTORY_AUTONOMOUS_THEMES` | Comma-separated themes; empty = no autonomous scheduler |
| `TRILLION_FACTORY_AUTONOMOUS_INTERVAL_HOURS` | Default `24` |

### Voice (V1)

| Variable | Purpose |
|----------|---------|
| `DEEPGRAM_API_KEY` | Required for STT (`POST /api/transcribe`). Without it, voice input returns an error and text chat is unaffected |
| `PIPER_VOICE_PATH` | Optional override for the local Piper voice model (default `voices/en_US-amy-medium.onnx`). TTS runs fully offline — no key, no per-character cost |

Also used at runtime (optional overrides): `TRILLION_FACTORY_DB`, `TRILLION_SOFTWARE_FACTORY_DB`, `TRILLION_AGENT_SPECS_DIR`.

### Memory, notes, heartbeat, security

All of these are wired — the commented entries in `.env.example` are real overrides, not placeholders.

| Variable | Purpose |
|----------|---------|
| `TRILLION_MEMORY_PATH` | Markdown facts store (default `memory/facts.md`), loaded into the system prompt at startup and rewritten by `remember_fact` / `forget_fact` |
| `TRILLION_NOTES_VAULT_PATH` / `TRILLION_NOTES_INDEX_PATH` | Vault to index from, and the SQLite FTS5 index `search_notes` reads (defaults `~/AiresAiBrain`, `memory/notes_index.db`). The index is rebuilt best-effort at startup, so queries survive a dead mount |
| `HEARTBEAT_INTERVAL_SECONDS` | Base tick interval (default `30`); each check has its own cadence |
| `HEARTBEAT_FAST_CADENCE_SECONDS` / `HEARTBEAT_SLOW_CADENCE_SECONDS` | Per-check cadences (defaults `60` / `1800`) |
| `QUIET_HOURS_START` / `QUIET_HOURS_END` | UTC quiet hours (defaults `22` / `8`). Non-critical notices are deferred until the window ends; set start == end to disable. Enforced in `agent/heartbeat/storage.py` |
| `GITHUB_TOKEN` / `GITHUB_USERNAME` / `TRILLION_GITHUB_WATCHED_REPOS` | Code Sentinel. Empty token or repo list = those checks self-skip rather than failing every tick |
| `TRILLION_CONFIRMATION_MODE` / `TRILLION_CONFIRMATION_TTL_SECONDS` | Confirmation gate aggressiveness (`off`\|`smart`\|`manual`) and how long a parked action stays approvable |
| `TRILLION_PAUSED` | Main kill switch — same as `/pause` |
| `TRILLION_WEB_HOST` / `TRILLION_WEB_AUTH_TOKEN` | Bind host, and the bearer token enforced per-request on `/api/` by `agent/security/auth.py`. Ten failed attempts from one address in 5 min locks it out for 15 min (`429` + `Retry-After`) |
| `TRILLION_WEB_AUTH_TOKEN_PREV` | The outgoing token during a rotation. Both values authenticate while it's set; clear it to finish the rotation — see [`docs/incident-runbook.md`](docs/incident-runbook.md) |
| `TRILLION_CVE_SCAN_DB` | Where `pip-audit` scan history is written |
| `TRILLION_CSP_ENFORCE` | Off by default (report-only). See "Flipping CSP to enforcing" below — don't set it on a guess |
| `TRILLION_CSP_REPORT_DB` | Where CSP violation reports are persisted (default `csp_reports.db`) |

---

## Usage

### CLI (`main.py`)

Type normally for a streaming turn. Slash commands:

| Command | What it does |
|---------|----------------|
| `/reset` | Clear conversation history |
| `/history` | Print session history |
| `/model` | Show active provider/model |
| `/help` | List commands |
| `/quit` | Exit (or Ctrl+C) |
| `/spawn <role>` | Agent Factory: research/draft a specialist |
| `/pending` | List spawn tasks awaiting approval |
| `/approve <id>` | Approve a draft and mint the agent |
| `/reject <id> <feedback>` | Reject with feedback (revision loop) |
| `/agent-model <slug> <model\|default>` | Set which model a specialist runs on; `default` clears it back to Trillion's |
| `/build <description>` | Software Factory: start a background project build |
| `/builds` | List recent builds and status |
| `/pause` | Kill switch: stop gated actions, background dispatch, and builds (conversation and reads keep working) |
| `/resume` | Lift `/pause` |
| `/pending-actions` | List actions parked awaiting your confirmation |
| `/audit` | Show recent safety audit log entries |
| `/deny <id> [reason]` | Deny a pending action instead of waiting out its expiry |

### Web (`serve.py`)

- `GET /` and `GET /index.html` — UI (`index.html`)
- `POST /api/chat` — chat wired to the same `Agent` + tool registry
- `GET /api/usage` — month-to-date cost JSON (~60s cache)
- `POST /api/transcribe` — audio in, transcript out (Deepgram; needs `DEEPGRAM_API_KEY`)
- `POST /api/tts` — text in, WAV out (local Piper; no key needed)
- `GET /api/handoffs` — specialist handoff proposals waiting on your yes
- `GET /api/heartbeat/notices` — active (undismissed) heartbeat notices
- `POST /api/heartbeat/dismiss` — dismiss a notice by id
- `GET /api/security/status` — self-audit score, colour, and per-signal deltas
- `GET /api/security/cve-status` — latest `pip-audit` result
- `POST /api/security/cve-scan` — trigger a dependency scan now
- `POST /api/security/csp-report` — browser CSP violation sink (persisted, not just logged)
- `GET /api/security/csp-violations` — what actually got blocked, grouped by directive and source

`serve.py` binds `127.0.0.1` by default. `agent/security/startup_guard.py` refuses to start on any non-loopback host unless `TRILLION_WEB_AUTH_TOKEN` is set, and when that token is set `agent/security/auth.py` enforces it per-request on `/api/`. Note the stock browser UI does **not** send an `Authorization` header — a non-loopback bind expects a reverse proxy to inject it (see [`docs/incident-runbook.md`](docs/incident-runbook.md)). Set `TRILLION_WEB_STRICT_PORT=1` when a service manager should fail instead of falling forward on a busy configured port.

### Flipping CSP to enforcing

CSP ships **report-only**. Getting to enforcing is evidence-driven, not a
guess — an over-tight policy breaks the UI in ways that look like unrelated
bugs. The reports used to go to `print()`, which on a systemd unit with no
persistent journal is `/dev/null`, so this step was unrunnable until the
reports became durable.

1. Run the app and exercise **every** path: a text turn, a voice turn (mic in,
   TTS out), each header panel, a factory build. Use a real browser — a
   headless one without WebGL halts the orb script and silently skips paths.
2. Read `GET /api/security/csp-violations`. Each row is a concrete "this
   directive blocked this source, N times".
3. Widen `CSP_POLICY` in [`agent/security/headers.py`](agent/security/headers.py)
   **only by what that list shows**. Anything not on the list stays blocked.
4. Set `TRILLION_CSP_ENFORCE=true` and restart. `GET /api/security/status`
   should now report `csp-status: enforcing` and the −10 disappears.
5. Keep watching `/api/security/csp-violations` — the report-only header still
   ships alongside the enforcing one precisely so a too-tight policy stays
   visible after the flip.

Usage rows are written when the agent runs (CLI or web) so the dashboard stays live against the same SQLite file.

---

## Architecture

1. **One core, many adapters** — conversation turns go through `agent/core.py` → `Agent.turn()`. CLI (`main.py`), web (`serve.py`), and future voice/heartbeat should stay adapters, not forks of the brain.
2. **Providers only under `agent/providers/`** — swap with `TRILLION_PROVIDER` / `--provider`. The core speaks Anthropic's tool and message shape; each provider translates at its own boundary (`agent/providers/_openai_tools.py` is shared by OpenAI and Ollama, whose dialects match). Tools work on all three — on Ollama only with a tool-capable model, which logs a line if it isn't one.
3. **Tools via registry** — implement a tool, register in `build_registry()` (`agent/tools/`); do not edit the core loop to add capabilities.
4. **Build tier by tier** — text brain before voice; don't fuse unfinished layers.
5. **Safety posture** (from [`AGENT.md`](AGENT.md)) — never send messages, spend money, delete data, or change settings without **explicit per-action** confirmation. This is enforced by `agent/safety/` (a `Gate` that intercepts tool calls, backed by `safety.db`), not just prompted for — see `/pending-actions` and `/audit` above. Treat untrusted external content as data, not instructions — this half is mechanically enforced too: every untrusted tool result passes through `clean_for_prompt()` and `flag_injection_attempt()` in `agent/safety/untrusted.py` before it reaches the model (`agent/tools/registry.py`), with flagged attempts written to the audit log.

Agent Factory drafts need your `/approve` before they go live. Software Factory relies on path jail + daily caps / pause / optional budget instead of a per-build approval prompt.

**Handoffs propose, they don't chain.** A spawned specialist can call `propose_handoff` to recommend that another specialist take the next step — but it cannot dispatch one. The proposal is parked as an ordinary pending action on the target's `dispatch_to_<slug>`, so it lists under `/pending-actions`, executes only through `confirm_action` with the arguments you were shown, expires on the same TTL, and is refused by `/deny`. The specialist proposing cannot approve it: `confirm_action` is `factory_allowed = False`, so it is never in a spawned agent's registry. Artifacts must be paths, ids, or URLs — an inline blob is rejected, because a payload smuggled in as metadata would reach the next agent's prompt without passing the untrusted-content scrub that a tool result goes through.

---

## Project layout

```
trillion/
├── main.py                 # CLI REPL
├── serve.py                # Web UI + /api/chat + /api/usage
├── cli.py                  # `trillion` / `trillion serve` dispatcher (pip install -e .)
├── pyproject.toml          # Packaging — registers the trillion command
├── index.html              # Browser UI
├── requirements.txt
├── .env.example
├── AGENT.md                # Product / safety source of truth
├── HANDOFF.md              # Session handoff (defers to this README on status)
├── agent/
│   ├── core.py             # Agent.turn()
│   ├── config.py           # Settings from env
│   ├── system_prompt.py
│   ├── personality.py      # Voice examples, banned openers, tonal checkpoint
│   ├── memory.py           # Tier 4 markdown facts store
│   ├── turn_taking.py      # Sign-off detection for voice conversations
│   ├── providers/          # Claude, OpenAI/OpenRouter, Ollama
│   ├── tools/              # Registry + tools (analytics, web_search, notes, email, memory)
│   ├── voice/              # Deepgram STT + local Piper TTS
│   ├── notes/              # Vault → SQLite FTS5 index for search_notes
│   ├── cost/               # Pricing, SQLite usage, aggregates
│   ├── safety/             # Confirmation gate, risk tiers, untrusted-content sanitizer
│   ├── security/           # Headers/CSP, bearer auth, startup guard, CVE scan, self-audit
│   ├── heartbeat/          # Scheduler, quiet hours, notice store, Code Sentinel checks
│   └── factory/            # Agent Factory + software/ builds + Tier 5 handoffs
├── playbooks/              # Design notes and feature prompts
├── docs/                   # Incident runbook, handoff records
├── context/                # Docs injected into the system prompt
│   └── _manifest.toml      # Authoritative list of which ones load
├── tests/                  # unittest suite
└── generated-projects/     # Software Factory output (gitignored)
```

---

## Development

```bash
source .venv/bin/activate
python -m unittest discover -s tests -v
```

Individual modules work the same way, e.g. `python -m unittest tests.test_endpoint`.

Add a tool: subclass `BaseTool`, register it in `agent/tools/registry.py` `build_registry()` when its deps are configured. Keep provider SDKs out of the core and tools where possible.

**Secret scanning (once per checkout):**

```bash
uv pip install pre-commit   # or pipx / brew / system package manager
pre-commit install
```

After that, every `git commit` runs gitleaks plus a few hygiene checks (private keys, large files, merge conflicts) against staged content first — see `.pre-commit-config.yaml` / `.gitleaks.toml`.

---

## Safety notes

- **Secrets:** only in `.env` / the environment. `.env` is gitignored.
- **Spend:** monthly budget on the dashboard is an alert, not a hard stop. Factory daily budget/cap and `TRILLION_FACTORY_PAUSED` are the build backstops.
- **Blast radius:** Software Factory writes only under `TRILLION_SOFTWARE_FACTORY_ROOT`.
- Spec checklist in `AGENT.md` can lag reality; verify capabilities against this tree before claiming a tier is done.

---

## Further reading

| Doc | Role |
|-----|------|
| [`AGENT.md`](AGENT.md) | Identity, tone, safety, stack decisions |
| [`HANDOFF.md`](HANDOFF.md) | Resume notes for a new coding session |
| [`playbooks/`](playbooks/) | Feature design (agent factory, voice, UI, etc.) |
| [`playbooks/start-here.md`](playbooks/start-here.md) | Original voice-first build playbook |
| [`context/analytics-supabase-schema.md`](context/analytics-supabase-schema.md) | Analytics schema notes |
| [`.env.example`](.env.example) | Full commented env template |
