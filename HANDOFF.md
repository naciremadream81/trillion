# Trillion — Build Handoff
*Paste this into a new session to resume exactly where we left off.*

---

## What we're building

A voice-first AI assistant called **Trillion** — Sean's personal AI co-founder.
Not a demo. A real, daily-driver assistant built tier by tier so every layer
is independently testable before the next one starts.

The full spec lives in `AGENT.md` at the project root. Read that first.

---

## Status: Tier 1 complete ✅

All Tier 1 files have been written and are in the `trillion/` directory.
The brain works in plain text. Nothing else has been built yet.

### What's in the project right now

```
trillion/
├── AGENT.md                          ← full spec, source of truth
├── HANDOFF.md                        ← this file
├── .env.example                      ← copy to .env, add API key
├── .gitignore
├── requirements.txt
├── main.py                           ← entry point, REPL
└── agent/
    ├── __init__.py
    ├── core.py                       ← the conversation loop
    ├── system_prompt.py              ← Trillion's personality + system prompt
    └── providers/
        ├── __init__.py               ← get_provider() factory
        ├── base.py                   ← BaseProvider seam + TextChunk/ToolCall types
        ├── claude.py                 ← Anthropic Claude (primary)
        ├── openai_provider.py        ← OpenAI + OpenRouter (same file)
        └── ollama.py                 ← local Ollama (Raspberry Pi target)
```

### How to run it (Tier 1)

```bash
cd trillion
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # then add ANTHROPIC_API_KEY
python main.py
```

Slash commands inside the session: `/reset`, `/history`, `/model`, `/help`, `/quit`.

### Tier 1 verification (must pass before Tier 2)

Run it, hold a short back-and-forth (3+ turns), and confirm it remembers earlier
turns in the same session. Kill it and restart — it should forget everything
(expected; memory is Tier 4). If it ever forgets mid-session, the `self.history`
list in `agent/core.py` isn't threading through correctly.

---

## Key decisions already made

| Decision | Choice | Why |
|----------|--------|-----|
| Language | Python 3.11+ | Best audio/HTTP/AI library support |
| Primary model | Claude (`claude-sonnet-4-6`) | Via Anthropic SDK |
| Alt providers | OpenAI, OpenRouter, Ollama | Same seam, swap with `TRILLION_PROVIDER=` env var |
| Runtime | Laptop-first, Pi-capable | Ollama provider already points at configurable base URL |
| Voice end-state | Push-to-talk → wake word | Text-first always, voice is a layer |
| STT (Tier 3) | Deepgram | Fast, streaming, accurate |
| TTS (Tier 3) | ElevenLabs | Natural voice, streams audio |
| Safety gate | Per-action confirmation | Never send/spend/delete/change without explicit yes |
| Proactive | Yes, quiet by default | Earns interruptions, doesn't assume them |

---

## Architecture principles (hold the line on these)

1. **One core, many adapters.** A typed turn, a spoken turn, and a heartbeat-initiated
   turn all flow through `agent/core.py → Agent.turn()`. Never fork the agent logic
   for voice vs text.

2. **Provider behind a seam.** Nothing outside `agent/providers/` touches an SDK directly.
   Swap models = change one env var.

3. **Build tier by tier.** Don't start Tier N+1 until Tier N verifies. Debugging all
   layers at once is miserable.

4. **Get the brain working in plain text before adding a single line of audio.**
   Voice is a layer. The brain is the foundation.

5. **Tool registry is the extension point.** Adding a new capability = write one
   self-contained tool and register it. Never edit the core loop.

---

## What's next — Tier 2: The hands (tools)

Tier 2 builds the tool registry and the first three tools drawn from Sean's
interview answers.

### The tool registry shape to build

`agent/tools/registry.py` — a class with:
- `register(tool)` — add a tool to the registry
- `schemas()` → `list[dict]` — returns all tool schemas in the format the
  current provider expects (Claude format vs OpenAI format differ slightly)
- `run(tool_call: ToolCall)` → `str` — executes the named tool, returns result

`agent/tools/base.py` — a `BaseTool` class each tool inherits from:
- `name: str` — must match exactly what the model will call
- `description: str` — **write this for a reader, not a compiler.** The model
  picks tools based on this. Vague = wrong tool called.
- `input_schema: dict` — JSON Schema for the tool's inputs
- `async def run(self, **kwargs) -> str` — the actual implementation

### First three tools to build

1. **`web_search`** — business intelligence / opportunity research.
   Sean's shorthand: "make me a million dollars."
   Implementation: Brave Search API or Tavily (both have free tiers,
   both return clean results). Ask Sean which before writing it.
   *Open question: is this right, or does Sean want something more specific
   (lead tracking, KPI dashboard, market monitor)?*

2. **`draft_email`** — take a brief description, return a draft email in Sean's
   voice. Pure LLM, no external API needed. The model already knows Sean's tone
   from the system prompt. Mark as requiring confirmation before send (Tier 6 gate
   already stubbed in `agent/core.py → _run_tool()`).

3. **`search_notes`** — search local markdown files.
   *Open question: where do Sean's notes live? Default: `~/notes/`. Notion MCP
   connector is already connected and ready to wire in as an upgrade.*

### Tier 2 edge cases to build from day one

- A tool will fail. Catch it, return the error as a plain string *to the model*,
  let the model explain it to Sean. Never crash.
- Mark `draft_email` (and any future send/delete/change tools) as `requires_confirmation=True`
  now. The gate is wired in Tier 6 but the flag should be on the tool from birth.
- The tool loop in `agent/core.py` already handles multiple sequential tool calls —
  don't change the core loop, just build the registry and tools.

### Tier 2 verification

Ask for something that needs a tool ("search for SaaS pricing trends in 2025"),
watch it call the tool, get a result, and weave it into a natural reply.
Then break the tool on purpose (bad input, fake API key) and confirm the assistant
explains the problem instead of crashing.

---

## Open questions (answer before Tier 2)

1. **First tool scope:** Is "web search for business intel" right for tool #1,
   or something more specific?

2. **Notes location:** Where do your notes live — `~/notes/`, an Obsidian vault,
   Notion, somewhere else?

3. **Web search API:** Brave Search, Tavily, or something else? (Both have free
   tiers. Tavily is designed for LLM agents. Brave is cheaper at scale.)

4. **ElevenLabs voice (needed by Tier 3):** What should Trillion sound like?
   Describe the feel ("calm male, mid-Atlantic," "warm female, conversational,"
   "crisp British male") and we'll match a voice in the library.

---

## Tier roadmap

| Tier | What | Status |
|------|------|--------|
| 0 | Interview + spec | ✅ Done |
| 1 | Text conversation loop, streaming, provider seam | ✅ Done |
| 2 | Tool registry + 3 tools | 🔜 Next |
| 3 | Voice: Deepgram STT + ElevenLabs TTS, push-to-talk | ⬜ |
| 4 | Persistent memory across restarts | ⬜ |
| 5 | Heartbeat — proactive, scheduled checks | ⬜ |
| 6 | Safety rails: confirmation gate, config file, audit log, kill switch | ⬜ |

---

## How to brief a new session

Paste this into the chat:

> "We're building Trillion, Sean's AI co-founder assistant. Tier 1 is complete
> — the text conversation loop is working. Read HANDOFF.md for full context,
> then pick up at Tier 2: the tool registry and first three tools. The project
> is in the `trillion/` directory. Ask me the open questions in HANDOFF.md
> before writing any Tier 2 code."
