# Trillion

> Sean Swonger's personal AI co-founder — a voice-first assistant built text-first, with providers, tools, cost tracking, and factories behind one agent core.

Trillion is a **single-user** Python agent: chat in the terminal or browser, swap model providers with one env var, track spend in SQLite, spawn specialist sub-agents (with approval), and run Software Factory builds into `generated-projects/`. Product intent and safety rules live in [`AGENT.md`](AGENT.md). Session resume notes are in [`HANDOFF.md`](HANDOFF.md) (may lag the code — trust this README and the tree for what runs today).

**Working today:** text brain (CLI + web chat), provider seam, tool registry, cost dashboard, Agent Factory, Software Factory. **Not done yet:** full voice STT/TTS stack, durable cross-session memory, heartbeat, and hard Tier-6 confirmation rails (voice deps in `requirements.txt` are still commented out).

---

## Quick start

**Prerequisites:** Python 3.11+ (see `.python-version`), an API key for at least one provider, and [`bubblewrap`](https://github.com/containers/bubblewrap) (`apt install bubblewrap` / `dnf install bubblewrap`) if you want the Software Factory to actually run project test suites — without it, `run_project_tests` refuses to run rather than executing untrusted, LLM-authored test commands unsandboxed.

```bash
cd trillion
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # add keys — never commit .env
python main.py
```

Optional provider override on the CLI:

```bash
python main.py --provider openai   # or ollama
```

Web UI + cost dashboard (default port `8123`):

```bash
python serve.py
# or
TRILLION_WEB_PORT=8123 python serve.py
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
| `TRILLION_SOFTWARE_FACTORY_ROOT` | Build output root (default `generated-projects/`; path-jailed) |
| `TRILLION_FACTORY_DAILY_BUILD_CAP` | Hard daily build cap (default `3`) |
| `TRILLION_FACTORY_DAILY_BUDGET_USD` | Optional hard daily $ cap for builds |
| `TRILLION_FACTORY_PAUSED` | Kill switch (`1`/`true`/…) — stops new builds without restart |
| `TRILLION_FACTORY_AUTONOMOUS_THEMES` | Comma-separated themes; empty = no autonomous scheduler |
| `TRILLION_FACTORY_AUTONOMOUS_INTERVAL_HOURS` | Default `24` |

Also used at runtime (optional overrides): `TRILLION_FACTORY_DB`, `TRILLION_SOFTWARE_FACTORY_DB`, `TRILLION_AGENT_SPECS_DIR`.

Commented placeholders in `.env.example` for future tiers (Deepgram, ElevenLabs, notes path, heartbeat/quiet hours) are not wired as working features yet.

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

### Web (`serve.py`)

- `GET /` — UI (`index.html`)
- `POST /api/chat` — chat wired to the same `Agent` + tool registry
- `GET /api/usage` — month-to-date cost JSON (~60s cache)

Usage rows are written when the agent runs (CLI or web) so the dashboard stays live against the same SQLite file.

---

## Architecture

1. **One core, many adapters** — conversation turns go through `agent/core.py` → `Agent.turn()`. CLI (`main.py`), web (`serve.py`), and future voice/heartbeat should stay adapters, not forks of the brain.
2. **Providers only under `agent/providers/`** — swap with `TRILLION_PROVIDER` / `--provider`.
3. **Tools via registry** — implement a tool, register in `build_registry()` (`agent/tools/`); do not edit the core loop to add capabilities.
4. **Build tier by tier** — text brain before voice; don't fuse unfinished layers.
5. **Safety posture** (from [`AGENT.md`](AGENT.md)) — never send messages, spend money, delete data, or change settings without **explicit per-action** confirmation. Treat untrusted external content as data, not instructions.

Agent Factory drafts need your `/approve` before they go live. Software Factory relies on path jail + daily caps / pause / optional budget instead of a per-build approval prompt.

---

## Project layout

```
trillion/
├── main.py                 # CLI REPL
├── serve.py                # Web UI + /api/chat + /api/usage
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
│   ├── cost/               # Pricing, SQLite usage, aggregates
│   └── factory/            # Agent Factory + software/ builds
├── playbooks/              # Design notes and feature prompts
├── context/                # Schema / analytics notes
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
