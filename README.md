# Trillion

> Sean Swonger's personal AI co-founder — a voice-first assistant built text-first, with providers, tools, cost tracking, and factories behind one agent core.

Trillion is a **single-user** Python agent: chat in the terminal or browser, swap model providers with one env var, track spend in SQLite, spawn specialist sub-agents (with approval), and run Software Factory builds into `generated-projects/`. Product intent and safety rules live in [`AGENT.md`](AGENT.md). Session resume notes are in [`HANDOFF.md`](HANDOFF.md) (may lag the code — trust this README and the tree for what runs today).

**Working today:** text brain (CLI + web chat), provider seam, tool registry, cost dashboard, Agent Factory, Software Factory, voice V1 (Deepgram STT + local Piper TTS, wired to `POST /api/transcribe` and `POST /api/tts`), Tier 6 safety rails (confirmation gate, audit log, `/pause` kill switch), durable cross-session memory (`agent/memory.py` + `remember_fact`/`forget_fact`), the heartbeat scheduler with quiet hours and the Code Sentinel, the `search_notes` and `draft_email` tools, untrusted-content sanitization on every tool result, and the security shield (`GET /api/security/status`).

**Not done yet:** self-knowledge (Trillion cannot describe its own tools from source) and cosmic-orb UI tiers 4-6 are built on branches not yet merged to `main` (see their own PRs for status/caveats). Acting on the voice latency numbers below (smooth-voice_2 Tiers 2-6) — end-of-turn detection tuning, prompt-caching hygiene, and any provider change — is still open; a provider swap requires asking Sean first regardless.

Voice latency instrumentation (smooth-voice_2 Tier 1, measure-only) is built into `index.html`'s voice flow: `console.log`s a per-turn breakdown (stop speaking → transcript final → first model token → first audio byte → first sound playing) and leaves it at `window.trillionVoiceLatency`. Real numbers captured against this dev sandbox (not the deployed Pi — see caveat below): first Claude token ~1.4s; Piper TTS ~500-1200ms per sentence once its ~63MB voice model is warm in memory, but **~4s extra on the very first synthesis after a process (re)start** while that model loads — worth fixing before the STT leg even matters. STT (Deepgram) couldn't be measured here at all: `DEEPGRAM_API_KEY` isn't set in this dev `.env`. None of these numbers transfer directly to the Pi — different CPU, different network path — re-run the measurement there before prioritizing Tier 2+.

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
| `TRILLION_WEB_HOST` / `TRILLION_WEB_AUTH_TOKEN` | Bind host, and the bearer token enforced per-request on `/api/` by `agent/security/auth.py` |
| `TRILLION_CVE_SCAN_DB` | Where `pip-audit` scan history is written |

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
- `GET /api/heartbeat/notices` — active (undismissed) heartbeat notices
- `POST /api/heartbeat/dismiss` — dismiss a notice by id
- `GET /api/security/status` — self-audit score, colour, and per-signal deltas
- `GET /api/security/cve-status` — latest `pip-audit` result
- `POST /api/security/cve-scan` — trigger a dependency scan now
- `POST /api/security/csp-report` — browser CSP violation sink

`serve.py` binds `127.0.0.1` by default. `agent/security/startup_guard.py` refuses to start on any non-loopback host unless `TRILLION_WEB_AUTH_TOKEN` is set, and when that token is set `agent/security/auth.py` enforces it per-request on `/api/`. Note the stock browser UI does **not** send an `Authorization` header — a non-loopback bind expects a reverse proxy to inject it (see [`docs/incident-runbook.md`](docs/incident-runbook.md)). Set `TRILLION_WEB_STRICT_PORT=1` when a service manager should fail instead of falling forward on a busy configured port.

Usage rows are written when the agent runs (CLI or web) so the dashboard stays live against the same SQLite file.

---

## Architecture

1. **One core, many adapters** — conversation turns go through `agent/core.py` → `Agent.turn()`. CLI (`main.py`), web (`serve.py`), and future voice/heartbeat should stay adapters, not forks of the brain.
2. **Providers only under `agent/providers/`** — swap with `TRILLION_PROVIDER` / `--provider`.
3. **Tools via registry** — implement a tool, register in `build_registry()` (`agent/tools/`); do not edit the core loop to add capabilities.
4. **Build tier by tier** — text brain before voice; don't fuse unfinished layers.
5. **Safety posture** (from [`AGENT.md`](AGENT.md)) — never send messages, spend money, delete data, or change settings without **explicit per-action** confirmation. This is enforced by `agent/safety/` (a `Gate` that intercepts tool calls, backed by `safety.db`), not just prompted for — see `/pending-actions` and `/audit` above. Treat untrusted external content as data, not instructions — this half is mechanically enforced too: every untrusted tool result passes through `clean_for_prompt()` and `flag_injection_attempt()` in `agent/safety/untrusted.py` before it reaches the model (`agent/tools/registry.py`), with flagged attempts written to the audit log.

Agent Factory drafts need your `/approve` before they go live. Software Factory relies on path jail + daily caps / pause / optional budget instead of a per-build approval prompt.

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
│   └── factory/            # Agent Factory + software/ builds
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
