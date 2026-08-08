# Agent Factory — AI Agent Prompt

A drop-in prompt for your AI coding agent (Claude Code, Cursor, Aider, or similar) that helps it add a **sub-agent spawner** to your project: a meta-agent that designs, prompts, and registers new specialist sub-agents on demand. It encodes a battle-tested architecture and — critically — instructs the agent to **interview you first** so the implementation matches your host runtime, your tool registry, your storage, and your approval workflow.

**How to use it:** copy everything below the horizontal rule into a fresh chat with your AI agent. Answer the interview questions when it asks, then let it build.

---

You are helping me add a **sub-agent spawner** to my project. Treat this whole message as your brief. **Do not write any code yet.** Your first job is to interview me, echo my answers back as a short plan, and only then start building tier by tier with verification at each step.

## The feature, in one paragraph

I want a new sub-agent — call it the **Factory** — whose only job is to mint *other* sub-agents on demand. When I ask the host system "give me a sub-agent that does X," the Factory researches what such an agent should be capable of, drafts its system prompt, picks the tools it should have from my existing catalog, stages a proposed manifest, waits for me to approve, then registers it as a first-class agent the host system can dispatch to — all without a restart. Think "compiler for sub-agents." The Factory itself is just one more sub-agent, but its outputs are persistent, runnable agents.

## Step 1 — Interview me before coding

Ask me the questions in each block below, **wait for my answers**, then summarize back what you heard before moving on. If I'm vague, follow up. If something I describe doesn't exist yet in my codebase, flag it before going further — you may need to scaffold a thin version of it before building the Factory itself.

### A. Host runtime

1. What language and framework does the host system use? (Python + FastAPI, Node + Express, Go, Rust + Axum, something else.)
2. Which LLM provider drives my existing agents? (Anthropic via the official SDK, OpenAI, multiple via LiteLLM/LangChain, a self-hosted endpoint.) The Factory will need to make LLM calls of its own; it should reuse this client.
3. Do my existing agents share a base class / common loop, or is each one hand-rolled? Show me where the canonical "tool-use loop" lives — the Factory's spawned agents will use that same loop.
4. How are sub-agents currently *dispatched* — direct function call, a tool exposed to a parent agent (`dispatch_to_<slug>`), a queue/job runner, something else?

### B. Tool catalog

1. Where does the global tool registry live? File path, please. The Factory needs to read it to pick a tool allowlist for each spawned agent, and to surface tools the agent would want but I don't have ("tool wishlist" — see Tier 1).
2. What shape does a tool definition take? (Name + JSON schema + execute function? A decorator? A Pydantic model?) Show me one example.
3. Do tools have an opt-out flag for "internal-only" — tools the Factory should NOT hand out to spawned agents? (Auth secrets, payment actions, anything destructive.) If not, we'll add a `factory_allowed: bool` field.
4. Is there a `web_search` or equivalent already available? The Factory's research step needs one. If absent, we'll either use the LLM provider's built-in (Anthropic's `web_search_20250305`, OpenAI's browsing) or you'll point me at one of your existing search tools.

### C. Persistence

1. What database backs the host system? (Postgres, SQLite, MySQL, DynamoDB, plain JSON files.) The Factory needs two new tables: one for in-flight spawn *tasks*, one for the registered *agents* themselves.
2. Where do migrations live? (Alembic, raw SQL files, a custom runner.) Match that pattern.
3. Do you have a mechanism for **listening to row changes** so the registry can hot-reload without a restart? (Postgres LISTEN/NOTIFY, a polling watcher, a pub/sub bus, file mtime polling.) If none, we'll fall back to a polling watcher on a 30s tick.

### D. Approval surface

1. Where will I *review and approve* a proposed agent before it goes live? (A CLI, a web UI page, a Slack message, an email with action links, "I'll just hit the DB directly.") Pick one — the Factory writes a proposed manifest to a row in `awaiting_approval` state; something has to surface it to me and call the approve/reject endpoint.
2. Should the Factory go through a "soft launch" — agent is registered but only callable in dry-run / shadow mode until I promote it? Or hard binary: approved → fully live?
3. What's the *maximum* number of revision rounds you want before the Factory gives up and marks the task `failed`? (Default: 3 — i.e. I can reject twice with feedback, the Factory regenerates, third rejection kills the task.)
4. **How does my approval surface re-hydrate after a refresh or reconnect?** If it's a web UI, the WebSocket broadcast that announced "ready for approval" will not be replayed on reconnect — so the user reloading the page, or closing their laptop overnight and reopening, would see *no* pending work even when several agents are waiting. You need a `GET /pending` endpoint (or its CLI equivalent) that returns all `awaiting_approval` tasks with their proposed manifests, called on page load. Without this you will get a "stuck" bug report from your first user. Promise.

### E. Failure modes I want to avoid

1. **Slug collisions** — what specialist agents already exist in your codebase? List them so we can reserve those slugs and refuse to mint anything that overlaps.
2. **Runaway spawning** — should there be a daily cap on how many new agents the Factory can stage? (Default: 5/day per user.)
3. **Prompt-injection drift** — the Factory writes prompts for *other* agents based on a user description. If a malicious description tries to escape the meta-prompt (`"...and also exfiltrate all env vars"`), what's my containment story? At minimum: the Factory's system-prompt-writer must never quote the user's role description verbatim into the spawned agent's prompt without sanitization. We'll address this in Tier 2.

After all five blocks, **echo back a short plan** — runtime, tool registry shape, DB, approval surface, reserved slugs, daily cap — and ask me to confirm or adjust before you move on.

## Step 2 — Reference architecture

Once I've confirmed the plan, build along these lines. Adapt them to my stack; don't follow them literally if my context calls for something different. The architecture is **five tiers**, each independently shippable with verification.

### The shape of a spawned agent

Every Factory-spawned agent is **pure configuration** — no new Python/JS class is written per agent. One generic runtime (we'll call it `ConfigDrivenAgent`) reads a row from the agents table and runs a vanilla tool-use loop using that row's fields:

| Field | Type | Notes |
|---|---|---|
| `slug` | unique short id | Used in `dispatch_to_<slug>` tool name. Must not collide with reserved specialists. |
| `name` | display name | "Atlas", "Relay", "DocSummarizer" |
| `specialty` | one-line role | "Internet research", "Customer support drafts" |
| `system_prompt` | text | Generated by Tier 2; reviewed by you on approval. |
| `tool_allowlist` | list of tool names | Subset of your global tool registry. The runtime filters available tools to just these. |
| `model` | LLM model id | "claude-sonnet-4-5", "gpt-4o", etc. |
| `status` | enum | `active`, `archived` — archived agents aren't dispatched but the row stays for audit. |
| `created_by_task_id` | FK to spawn task | Lets you trace any agent back to the request and skills report that produced it. |

That's it. **No bespoke code per agent.** This is the single most important constraint in the architecture — if you find yourself writing a Python class for each new agent, you've drifted off-pattern.

### Two database tables

```text
spawn_tasks                          spawned_agents
─────────────                        ──────────────
id                                   id
requested_by                         slug              (unique)
name_hint                            name
role_description                     specialty
special_requirements                 system_prompt
status (enum, below)                 tool_allowlist    (JSON array)
research_report_id  (FK ↓)           model
proposed_manifest   (JSON, nullable) status            ('active' / 'archived')
approval_iterations (int)            created_by_task_id (FK → spawn_tasks.id)
revision_feedback   (nullable text)  created_at
error               (nullable text)
created_at
```

`revision_feedback` is the column that holds the user's last reject-with-feedback message; the Factory reads it back in when regenerating the system prompt. See Tier 4 — Approval gate, below.

Plus a third small table — `research_reports` — to cache the research step's output (24h dedup on normalized query is fine).

### The five-state spawn pipeline

```
PENDING
   │
   ▼
RESEARCHING ──────▶ FAILED
   │
   ▼
DRAFTING_SPEC ────▶ FAILED
   │
   ▼
WRITING_PROMPT ───▶ FAILED
   │
   ▼
AWAITING_APPROVAL ─▶ REJECTED  (or back to WRITING_PROMPT if I asked for revisions)
   │
   ▼
APPROVED   ← terminal
```

Every transition is a DB row update plus an event broadcast (whatever your host system uses — a WebSocket frame, a Slack message, a log line). Encode the transitions as a dict-of-sets and refuse invalid ones at the repo layer:

```python
_TRANSITIONS = {
    State.PENDING:            {State.RESEARCHING, State.FAILED},
    State.RESEARCHING:        {State.DRAFTING_SPEC, State.FAILED},
    State.DRAFTING_SPEC:      {State.WRITING_PROMPT, State.FAILED},
    State.WRITING_PROMPT:     {State.AWAITING_APPROVAL, State.FAILED},
    State.AWAITING_APPROVAL:  {State.APPROVED, State.REJECTED,
                               State.WRITING_PROMPT, State.FAILED},
    State.APPROVED:           set(),  # terminal
    State.REJECTED:           set(),  # terminal
    State.FAILED:             set(),  # terminal
}
```

Adapt syntax to your language. The point is: invalid transitions fail loudly, not silently.

## Step 3 — Build it tier by tier

### Tier 1 — Research subagent (structured Skills Report)

**Goal:** given a one-paragraph role description, produce a JSON-validated report of what the new agent should be capable of, what tools it needs, and what it would want that you don't have yet. This is its own callable thing — you'll want it for the Factory but also potentially as a general-purpose research helper later.

**Schema (Pydantic / Zod / whatever your stack uses):**

```python
class Source(BaseModel):
    url: str
    title: str
    excerpt: str = Field(default="", max_length=400)

class ToolWishlistEntry(BaseModel):
    name: str             # proposed tool name
    purpose: str          # why this agent needs it
    external_dependency: str = ""  # API, library, service

class SkillsReport(BaseModel):
    domain: str
    competencies: list[str]           # 4–8 concrete capabilities
    tools_available: list[str]        # names from your existing catalog
    tools_wishlist: list[ToolWishlistEntry]  # tools to build later
    design_patterns: list[str]        # patterns observed in research
    sources: list[Source]             # 5–15 cited sources
```

**The research loop:**

```text
1. Cache check: if a report for this normalized query is < 24h old, return it.
2. Otherwise, start a Claude/GPT loop with two tools:
     - web_search (provider-native or your own)
     - emit_skills_report  (structured output, schema above)
3. Loop up to ~8 iterations. On the final iteration, FORCE the model
   to call emit_skills_report via tool_choice = {"type": "tool", "name": "emit_skills_report"}.
4. Validate the emitted payload against the Pydantic schema. Persist
   it. Return it.
```

The **forced-tool-call on the last iteration** is the key reliability trick — without it, models sometimes meander past your iteration budget instead of emitting. With it, the worst case is "it emits a slightly under-researched report on the last turn" rather than "it exhausts iterations and crashes."

**The research system prompt (template — adapt the tool list to yours):**

```
You are a research specialist. Your job: research what an agent that
does <DOMAIN> should be capable of, and produce a structured Skills
Report.

You have access to web_search. Use it 3-6 times to gather real
evidence from real sources (vendor docs, open-source projects,
technical blogs).

You MUST end by calling emit_skills_report with these fields:
- domain: the domain you researched
- competencies: 4-8 concrete capabilities the agent should have
- tools_available: tool names from this catalog the agent can use today:
    <PASTE YOUR FACTORY-ALLOWED TOOL NAMES HERE>
- tools_wishlist: tools we DON'T have yet that this agent would need.
- design_patterns: 2-5 real patterns you observed
- sources: 5-15 sources with url + title + short excerpt (<400 chars)

Quote excerpts must be SHORT and clearly attributable.
```

**Verification before moving on:**

```bash
# Call the research function directly with a known domain.
# Expect: a Skills Report with non-empty competencies and ≥3 sources,
# persisted to your research_reports table.
$ <your repo's CLI or REPL invocation> research "PDF text extraction"

# Re-run within 24h — expect a cache hit (no LLM call, sub-100ms response).
```

Ship Tier 1 before touching Tier 2. The research subagent is useful on its own.

### Tier 2 — Spec markdown + system-prompt generation

**Goal:** turn a Skills Report into (a) a human-readable spec markdown file documenting the planned agent and (b) a generated system prompt for the new agent.

**Spec markdown** — write to `<your-project>/agent-specs/<slug>.md`. Include: name, slug, role, special requirements, competencies, granted tools, **tool wishlist** (so future-you can see what to build next), design patterns, and source citations. The spec is for *humans* reviewing the proposed agent; it should read well.

**System-prompt generation** is one Claude/GPT call with a meta-prompt:

```
You write system prompts for AI sub-agents.

Given:
  - the agent's name
  - the agent's role / domain
  - a Skills Report (competencies, tools, design patterns)
  - any special requirements from the user

Produce a system prompt that:
  - addresses the agent in second person ("You are <name>...")
  - states the agent's domain and competencies clearly
  - tells the agent which tools it has and when to use them
  - encodes any special requirements
  - is 200-500 words

Return ONLY the system prompt text. No preamble, no commentary.
```

The user message inlines the report as JSON plus the role/requirements as strings. **Prompt-injection guard:** before inlining the user's `role_description` or `special_requirements`, strip control characters and any backtick-fenced "system:" / "ignore previous instructions" patterns. A small sanitization function with explicit rules is enough — you're not trying to defeat a determined attacker, just nudge sloppy input into a safe shape.

**Verification:**

```bash
# Run the spec writer + prompt generator with a fixture Skills Report
# (capture one from Tier 1 and check it into tests/fixtures).
# Expect: a well-formed markdown file at the expected path and a
# system prompt between 200-500 words.
$ <repo CLI> generate-prompt --report tests/fixtures/pdf_skills.json
```

### Tier 3 — Spawn pipeline (the state machine itself)

**Goal:** wire Tiers 1 and 2 into a single async pipeline that walks a `spawn_tasks` row through every state to `awaiting_approval`.

The pipeline is a class with one method, `run(task_id)`, that:

1. Loads the task row.
2. Picks and validates the slug (slugify the `name_hint`; refuse if it collides with your reserved slugs).
3. Transitions to `RESEARCHING`, calls Tier 1, persists the report id back onto the task.
4. Transitions to `DRAFTING_SPEC`, writes the spec markdown.
5. Transitions to `WRITING_PROMPT`, generates the system prompt.
6. Builds a `proposed_manifest` dict (slug, name, specialty, system_prompt, tool_allowlist from the report, model, etc.) and saves it onto the task row.
7. Transitions to `AWAITING_APPROVAL` and emits an event so your approval surface knows there's something to review.

Wrap the whole method in a try/except that, on any exception, calls `repo.set_error(task_id, str(exc))` and transitions to `FAILED`. **Do not let an exception bubble past the pipeline entry point** — once a spawn task starts, it must end in a terminal state.

**Reserved-slug guard:**

```python
_RESERVED_SLUGS = frozenset({
    # populate from your interview answers in Step 1.A
    "your_agent_a", "your_agent_b", ...
})
```

Check this before any LLM work — failing early saves an API call and gives the user a clean error.

**Verification:**

```bash
# Insert a task row via your repo's CLI / SQL, then run the pipeline.
# Expect: terminal status = awaiting_approval, proposed_manifest
# populated, spec markdown on disk, research report cached.
$ <repo CLI> spawn-task --name "doc-summarizer" \
    --role "summarizes long documents into bullet points"
$ <repo CLI> run-pipeline <task_id>
$ <repo CLI> show-task <task_id>
# → status: awaiting_approval, proposed_manifest: {...}
```

### Tier 4 — Approval gate

**Goal:** human-in-the-loop. The proposed manifest sits in `awaiting_approval`; you review it (via whatever surface you chose in Step 1.D); your approval inserts a row into `spawned_agents` and flips the task to `APPROVED`. Rejection with feedback rolls the task back to `WRITING_PROMPT` (incrementing `approval_iterations`); rejection with no feedback marks it `REJECTED` and terminal.

**The approve handler:**

```python
async def handle_approve(*, task_id, forge_repo, agents_repo,
                        emit_event, notify_registry):
    task = await forge_repo.get(task_id)
    if task.status != "awaiting_approval":
        raise ValueError("task not in approvable state")
    p = task.proposed_manifest

    agent = SpawnedAgent(
        id=uuid4().hex,
        slug=p["slug"],
        name=p["name"],
        specialty=p["specialty"],
        system_prompt=p["system_prompt"],
        tool_allowlist=p["tool_allowlist"],
        model=p["model"],
        status="active",
        created_by_task_id=task_id,
    )
    await agents_repo.save(agent)
    await forge_repo.transition(task_id, State.APPROVED)
    await notify_registry(agent.slug)  # see Tier 5
    await emit_event(kind="agent_added", event={"slug": agent.slug, "name": agent.name})
    return {"status": "approved", "slug": agent.slug}
```

**The reject-with-feedback handler** writes the feedback to the `revision_feedback` column on the task, increments `approval_iterations`, and re-runs Tier 2 (and *only* Tier 2 — research and the avatar are already cached on the task row, so don't burn the API budget regenerating them). The system-prompt generator should accept an optional `(prior_prompt, revision_feedback)` pair and inject them into the user message:

```
The previous draft was:
---
{prior_prompt}
---
The user asked for these changes:
{revision_feedback}
Produce a revised system prompt incorporating the feedback.
```

Cap iterations at the value from Step 1.D.3 — past the cap, the task auto-fails so a user can't keep an LLM call burning on perpetual rejection.

**Echo `created_by_task_id` on the completion event.** When the agent is registered, the event that announces it — your equivalent of `agent_added` — must include the originating spawn task's id, not just the new agent's slug. The UI is rendering a *list* of in-flight builds; without the task id it has no way to clear the right row after approval.

```python
await emit_event(kind="agent_added", event={
    "slug": agent.slug,
    "name": agent.name,
    # ↓ This field is the row key on the approval surface.
    # Without it the UI cannot tell which pending card just resolved.
    "created_by_task_id": task_id,
})
```

**Page-load hydration.** Pair the push event with a poll endpoint — `GET /factory/pending` (or whatever your routing convention is) returning every `awaiting_approval` task with its proposed manifest. Call it on every page load / reconnect. This is the single highest-ROI line of defense against the "I asked the Factory to build something five minutes ago and the UI says nothing happened" bug. The pipeline finished; the WS frame was already broadcast; the browser wasn't there to receive it. Without the poll endpoint, that work is invisible until the user dispatches a fresh task.

**The dock surface renders a list, not a card.** Users will dispatch multiple Factory tasks before approving any of them. Render each `awaiting_approval` task as its own row keyed by `task_id`. Approve / Reject buttons on each. Newest on top.

**Verification:**

```bash
# Approve a task that's in awaiting_approval.
$ <repo CLI> approve <task_id>
$ <repo CLI> list-agents
# → new row visible with status=active

# Reject with feedback — task should re-run prompt generation.
$ <repo CLI> reject <task_id> --feedback "make the tone less formal"
$ <repo CLI> show-task <task_id>
# → status: awaiting_approval, approval_iterations: 1
```

### Tier 5 — Hot-reload registry + config-driven runtime

**Goal:** newly-approved agents become dispatchable **without restarting the host process**.

Two pieces:

**(a) The `ConfigDrivenAgent` runtime** — one class, parameterized by the row:

```python
class ConfigDrivenAgent:
    def __init__(self, row, tool_registry, llm_client):
        self._row = row
        self._tools = tool_registry
        self._llm = llm_client

    def _filtered_tools(self):
        allow = set(self._row.tool_allowlist)
        return [t for t in self._tools.list_all()
                if t.name in allow]

    async def run(self, user_message: str) -> str:
        # Vanilla tool-use loop. Same one your existing agents use.
        # Use self._row.system_prompt as the system message.
        # Use self._row.model as the model.
        # Bound iterations (8 is a fine default).
        ...
```

**No specialist logic.** If you find yourself adding an `if self._row.slug == "foo":` branch, stop — that behavior belongs in the agent's tool allowlist or its system prompt, not in the runtime.

**(b) The registry watcher** — listens for new/archived agent rows and (un)registers the corresponding dispatch tool:

```python
class RegistryWatcher:
    async def refresh(self):
        rows = await self._repo.list_active()
        new_slugs = {r.slug for r in rows}
        for slug in new_slugs - self._known_slugs:
            self._tools.register(build_dispatch_tool(slug, ...))
        for slug in self._known_slugs - new_slugs:
            self._tools.unregister(f"dispatch_to_{slug}")
        self._known_slugs = new_slugs
```

Trigger `refresh()` from whichever change-detection mechanism you picked in Step 1.C.3 — Postgres `LISTEN`, a 30s polling tick, file mtime, etc. Also call it once on host process startup so existing approved agents are loaded.

`build_dispatch_tool(slug, ...)` returns a tool whose `execute` instantiates a `ConfigDrivenAgent` for that slug's row and runs it on the user's message. The dispatch tool's input schema is uniform: `{"message": "string"}`. Resist the urge to give each agent a custom schema — that defeats the "pure config" property.

**Verification:**

```bash
# In one terminal: tail the host process logs.
$ <run your host process in foreground>

# In another: approve a spawn task.
$ <repo CLI> approve <task_id>

# Expect: log line "Registered dispatch_to_<slug>" within ~30s of approval
# (or immediately, if you wired LISTEN/NOTIFY).

# Now call the new agent via the parent agent's dispatch tool, or directly:
$ <repo CLI> dispatch <slug> "test message"
# → the spawned agent runs, with its own system prompt and tool allowlist.
```

## Step 4 — Privacy & safety guardrails

These aren't optional, even if I don't ask:

- **Hold a strong reference to the pipeline task.** If you're in Python and start the pipeline with `asyncio.create_task(pipeline.run(...))` and immediately return — without saving the task object anywhere — the event loop keeps only a *weak* reference. The task can be garbage-collected mid-execution, silently. Symptom: "I dispatched the Factory and nothing ever happened, but no error either." Fix: stash the task in a module-level set, e.g.:

  ```python
  _IN_FLIGHT: set[asyncio.Task] = set()

  task = asyncio.create_task(pipeline.run(task_id=task_id))
  _IN_FLIGHT.add(task)
  task.add_done_callback(_IN_FLIGHT.discard)
  ```

  Cheap insurance. Other runtimes (Node, Go) handle this differently — but if your language's "fire and forget" idiom has any concept of weak references to in-flight work, double-check before you trust it.
- **Never give a spawned agent secrets-bearing tools.** If a tool in your catalog can read env vars, fetch from a credentials store, send email/Slack to a fixed external address, or move money — mark it `factory_allowed=False`. The Skills Report's `tools_available` list is what the Factory hands to the LLM as "tools you may pick from" — keep it clean.
- **The user-supplied `role_description` flows into LLM prompts and into the final spec markdown.** Sanitize before inlining: strip control chars, refuse the task if it contains "ignore previous instructions" / "system:" / fenced-code injection patterns. This is shallow defense — also enforce that the spawned agent's `system_prompt` cannot contain raw user input verbatim; the prompt-generator LLM must paraphrase.
- **Daily cap.** Enforce the value from Step 1.E.2 at the spawn-task creation point, not at approval time — you don't want an attacker queuing 10,000 tasks even if approvals are gated.
- **Reserved slugs** are non-negotiable. The unique constraint on `spawned_agents.slug` will save you at the DB layer, but check at the slug-picking step too so the user gets a clean error before any LLM tokens burn.
- **Audit trail.** Every spawned agent's row stores `created_by_task_id`. Don't truncate that history — for every running agent you should be able to trace back to: who asked for it, what role they described, what the Skills Report said, what feedback led to revisions, who approved it, when.
- **Avatar/visual assets** (optional but tempting). If you add image generation for spawned-agent avatars, treat the generation prompt the same way as the system prompt — never inline raw user input. Generate from `(name, role, role_paraphrase)` only.

## Step 5 — Suggested build order

Once the interview is done and the plan is confirmed, build in this order. Ship a working vertical slice as early as possible, then expand.

1. **Migrations.** Three tables: `spawn_tasks`, `spawned_agents`, `research_reports`. Apply them. Verify with a hand-written INSERT.
2. **Tier 1.** Research subagent with structured output. Ship as a standalone callable function. Verify with one real query end-to-end.
3. **Tier 2.** Spec writer + system-prompt generator. Verify with a fixture Skills Report.
4. **Tier 3.** Wire 1 and 2 into the pipeline state machine. Verify a single task flows from `pending` to `awaiting_approval`.
5. **Tier 4.** Approval handler. Verify approve + reject-with-feedback paths.
6. **Tier 5.** `ConfigDrivenAgent` + registry watcher. Verify hot reload — new agent becomes dispatchable without a restart.
7. **(Stretch)** Tool wishlist surfacing. Every `awaiting_approval` event should also list the wishlist tools — that's your roadmap for what to build next in the tool catalog.

## Step 6 — Final reminder

Do not start coding until the interview is complete and you've echoed my answers back as a plan I've confirmed. Then build the smallest possible end-to-end slice first — one spawn task, one approve action, one running config-driven agent — and expand from there. If you hit something my codebase doesn't have yet (a tool registry, a change-listening mechanism, an approval UI), tell me before working around it; the workaround often shapes the architecture.

Two anti-patterns to refuse no matter what:

- **A bespoke Python/JS class per spawned agent.** That's not an agent factory, it's a code generator that produces a maintenance burden. Stay on the pure-config path.
- **Skipping the human approval gate.** Auto-approving Factory output is how you end up with a registered agent whose system prompt contains the user's prompt-injection payload. The gate is the point.

Ready when you are. Start with the interview.
