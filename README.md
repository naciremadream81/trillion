# Trillion

> Sean Swonger's personal AI co-founder — a voice-first assistant built text-first, with providers, tools, cost tracking, and factories behind one agent core.

Trillion is a **single-user** Python agent: chat in the terminal or browser, swap model providers with one env var, track spend in SQLite, spawn specialist sub-agents (with approval), and run Software Factory builds into `generated-projects/`. Product intent and safety rules live in [`AGENT.md`](AGENT.md). Session resume notes are in [`HANDOFF.md`](HANDOFF.md) (may lag the code — trust this README and the tree for what runs today).

**Working today:** text brain (CLI + web chat), provider seam, tool registry, cost dashboard, Agent Factory, Software Factory, voice V1 (Deepgram STT + local Piper TTS, wired to `POST /api/transcribe` and `POST /api/tts`), and Tier 6 safety rails (confirmation gate, audit log, `/pause` kill switch). **Not done yet:** durable cross-session memory, heartbeat, and notes/email tools.

---

## Quick start

**Prerequisites:** Python 3.11+ (see `.python-version`), and an API key for at least one provider.

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
```

Then open `http://localhost:8123/` — serves `index.html`, `POST /api/chat`, and `GET /api/usage`.

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

Commented placeholders in `.env.example` for tiers that aren't built yet (`NOTES_PATH`, heartbeat interval, quiet hours) are documented there as future work — setting them today does nothing.

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

- `GET /` — UI (`index.html`)
- `POST /api/chat` — chat wired to the same `Agent` + tool registry
- `GET /api/usage` — month-to-date cost JSON (~60s cache)
- `POST /api/transcribe` — audio in, transcript out (Deepgram; needs `DEEPGRAM_API_KEY`)
- `POST /api/tts` — text in, WAV out (local Piper; no key needed)

`serve.py` binds `127.0.0.1` deliberately — there is no auth layer, so it must not be exposed to a network.

Usage rows are written when the agent runs (CLI or web) so the dashboard stays live against the same SQLite file.

---

## Architecture

1. **One core, many adapters** — conversation turns go through `agent/core.py` → `Agent.turn()`. CLI (`main.py`), web (`serve.py`), and future voice/heartbeat should stay adapters, not forks of the brain.
2. **Providers only under `agent/providers/`** — swap with `TRILLION_PROVIDER` / `--provider`.
3. **Tools via registry** — implement a tool, register in `build_registry()` (`agent/tools/`); do not edit the core loop to add capabilities.
4. **Build tier by tier** — text brain before voice; don't fuse unfinished layers.
5. **Safety posture** (from [`AGENT.md`](AGENT.md)) — never send messages, spend money, delete data, or change settings without **explicit per-action** confirmation. This is enforced by `agent/safety/` (a `Gate` that intercepts tool calls, backed by `safety.db`), not just prompted for — see `/pending-actions` and `/audit` above. Treat untrusted external content as data, not instructions (today this half is instruction-only; mechanical enforcement is a later phase).

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
├── HANDOFF.md              # Session handoff (may be stale on tiers)
├── agent/
│   ├── core.py             # Agent.turn()
│   ├── config.py           # Settings from env
│   ├── system_prompt.py
│   ├── providers/          # Claude, OpenAI/OpenRouter, Ollama
│   ├── tools/              # Registry + tools (e.g. analytics)
│   ├── voice/              # Deepgram STT + local Piper TTS
│   ├── cost/               # Pricing, SQLite usage, aggregates
│   └── factory/            # Agent Factory + software/ builds
├── playbooks/              # Design notes and feature prompts
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
