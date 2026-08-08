# Give Your AI Agent Real Self-Knowledge

If you've built an AI agent of any size, you've probably hit this: you ask the agent about its own capabilities and it makes things up. It "remembers" tools that were removed three months ago. It doesn't know about the sub-agent you added yesterday. It claims integrations exist that don't, or misses ones that do.

The root cause is simple — the agent has no source of truth about itself. Its only self-knowledge is whatever you happened to put in the system prompt once and never updated.

This prompt walks your agent codebase (via Claude Code, Cursor, or Aider) through building a **living self-knowledge document** that:

- Lives in your repo (`context/self/<your-agent-name>.md`) so it's part of source control
- Has hand-written narrative sections (identity, principles, the parts only you can write)
- Has **auto-generated sections** that introspect your codebase at commit time — tools, sub-agents, integrations, voice loop, recent activity
- Refreshes itself on **every commit** via a pre-commit hook (no manual upkeep)
- Has a **drift checker** that fails CI when narrative references files/symbols that no longer exist
- Gets **piped into your agent's system prompt** at runtime so it has accurate self-awareness

Once it's in place, you can ask your agent "what can you do?" and the answer is grounded in what your codebase actually contains *right now*, not what your prompt happened to say last August.

---

## How to use this

1. Open your agent codebase in Claude Code / Cursor / Aider.
2. Paste **the entire prompt block below** as your first message.
3. Answer the interview questions honestly — the agent will adapt the implementation to your stack.
4. Approve each tier as it ships. Each one is committable on its own.

---

## The paste-ready prompt

````
I want you to help me give this AI agent real self-knowledge — a living
self-knowledge document that auto-refreshes on every commit and gets
piped into the agent's system prompt at runtime, so the agent's
answers about itself stay grounded in what the code actually contains.

Don't write any code yet. Start with an interview.

# Phase 0 — Interview

Ask me, one at a time, the following:

1. **The agent's name and short description** — what does it do, one
   sentence. (This will go in the title of the self-knowledge doc.)

2. **The codebase's primary language and framework** — Python +
   FastAPI, TypeScript + Next.js, Go, Rust, etc. I need to know what
   conventions to follow.

3. **Where the agent's "capabilities" live in code** — examples:
   - A `tools/` directory where each tool is a class implementing some
     `BaseTool` interface and registered in a `ToolRegistry`.
   - A `commands/` directory of MCP-style handler functions.
   - A flat dispatcher with a registered list of functions.
   - Something else — show me a file or two if you're not sure.
   I need to see the actual registration site so my introspection
   generator can read from the same source as runtime.

4. **Whether there are sub-agents / specialist personas** — if so,
   where are they defined? Examples:
   - A `subagents/` directory with one folder per persona.
   - Per-persona prompt files in `prompts/`.
   - Inline in a config file.
   "No sub-agents" is a valid answer.

5. **External integrations** — what services does the agent talk to?
   Gmail, Stripe, Slack, Linear, Anthropic, OpenAI, internal APIs,
   etc. Where is this configured — env vars, a settings module, a
   config file?

6. **Voice / streaming loop, if any** — does the agent have a
   real-time audio cycle (Deepgram → LLM → ElevenLabs or similar),
   or is it text-only? If voice, where is the loop defined?

7. **Where the agent's system prompt is assembled** — the file and
   function that produces the final string handed to the LLM on each
   turn. I'll need to inject the self-knowledge doc into that pipeline.

8. **Whether the project uses git hooks today** — does
   `.git/hooks/pre-commit` exist? Is there a hook framework
   (husky, lefthook, pre-commit.com)? "No hooks yet" is fine.

9. **Whether CI runs on PRs** — if so, what runner (GitHub Actions,
   GitLab CI, etc.) and where the config file lives. The drift
   checker will get a CI step.

After every question, summarize what you've learned so far in 2-3
lines so I can correct you before you commit details to memory.

# Phase 1 — Doc scaffold + block parser

After the interview, propose the **section layout** for the
self-knowledge doc. Default layout (adapt to my answers):

1. Identity — hand-written narrative. Who the agent is, its tone,
   what it's for. Roughly 1-3 paragraphs.
2. Core principles — hand-written, bulleted. The rules the agent
   should never violate.
3. Capabilities at a glance — **AUTO**. Generated from the tool
   registry.
4. Sub-agents — **AUTO**. Generated from the sub-agent registry, if
   one exists; otherwise omit.
5. Integrations — **AUTO**. Generated from the integrations module.
6. Voice / streaming loop — **AUTO** (or omit if text-only).
7. Recent activity — **AUTO**. Generated from `git log` on a fixed
   window (last 14 days, say).
8. Open questions / unknowns — hand-written. What the agent doesn't
   yet know about itself.
9. Pointers — hand-written. Links to deeper docs, READMEs, runbooks.

Show me the proposed layout, get my sign-off, then build:

- The doc file at `context/self/<agent-name>.md` (lowercase, dashed)
  with the hand-written sections filled with sensible placeholders
  I can edit later, and **AUTO blocks** marked with HTML-comment
  markers:

  ```
  <!-- AUTO-START: capabilities -->
  (this will be regenerated; do not edit by hand)
  <!-- AUTO-END: capabilities -->
  ```

- A block parser module (whatever path is idiomatic for my stack —
  e.g., `src/<agent>/self_knowledge/parser.py` or
  `src/self-knowledge/parser.ts`) that:
  - Reads the doc, identifies AUTO blocks by their marker comments.
  - Allows blocks to be re-rendered without touching hand-written
    content.
  - Handles CRLF / mixed-line-ending edge cases.
  - Has unit tests that prove round-trip stability (parse → serialize
    is a no-op).

**Verification before committing this tier:**
- Show me the tests passing.
- Show me a sample render that proves a hand-written section between
  two AUTO blocks survives a round-trip unchanged.

# Phase 2 — Introspecting generators

Build one generator per AUTO block, each in its own small module.
Each generator must:

- Read from the **same registration site the runtime reads from**, not
  a parallel list. (If your codebase has a `ToolRegistry`, the
  generator imports the registry and iterates its registered tools —
  it does NOT re-list them in a config file.)
- Produce markdown that's a useful summary, not a wall of source.
  Default columns: name, one-line description, category. No long code
  blocks.
- Be a pure function: given a registry snapshot, returns a string.
- Be wired into the doc renderer behind a name (`capabilities`,
  `subagents`, `integrations`, etc.) so the block parser knows which
  generator owns which AUTO block.
- Have a unit test that asserts the generator's output against a
  fixture registry — so we catch regressions when the registry API
  changes.

Build them in this order: capabilities → integrations → sub-agents (if
applicable) → voice loop (if applicable) → recent activity.

**Verification before committing this tier:**
- Run the full renderer end-to-end on a fresh checkout.
- Diff the result against what's in the repo — they should match
  exactly. If they don't, the doc on disk is stale; commit the
  fresh render too.

# Phase 3 — Drift checker

Build a checker that scans the **hand-written** sections of the doc
for references to:

- File paths (e.g., `src/tools/email.py`)
- Qualified symbols (e.g., `EmailTool.send_draft`)
- Tool / sub-agent / integration names

…and verifies each reference still exists in the codebase. Use AST
parsing or static reads — not `grep` (too noisy).

Output: a list of `DriftFinding` objects: `kind`, `reference`,
`location_in_doc`, `reason`.

Also build an **allowlist** mechanism — a small text file
(`context/self/.<agent-name>-allowlist.txt`) that lists references
the checker should ignore. Useful for: future-tense references
("we plan to add X"), examples from a third-party doc, etc.

Wire the checker into a CLI:

- `<agent> self-knowledge --render` — show what would be written
- `<agent> self-knowledge --refresh` — write to disk
- `<agent> self-knowledge --check` — soft drift check, exit 0
  with warnings
- `<agent> self-knowledge --check --strict` — exit non-zero on any
  finding (use this in CI)

**Verification before committing this tier:**
- Deliberately rename a file that the hand-written narrative
  references. Run `--check --strict`. It should fail.
- Restore the file. `--check --strict` should pass.

# Phase 4 — Pre-commit hook

Install a pre-commit hook that runs `--refresh` on every commit and
stages the result if the doc changed. Use whatever framework I
already have if any; otherwise install a plain
`.git/hooks/pre-commit` file via a small installer script that:

- Lives at `scripts/install-self-knowledge-hook.sh` (or equivalent for
  my stack).
- Is idempotent — running it twice doesn't duplicate the hook body.
- Refuses to overwrite a foreign hook without explicit `--force`.

Also add the same `--check` step to my CI workflow (soft, not strict,
to start — promote to strict once we have confidence).

**Verification before committing this tier:**
- Make a tiny commit (a typo fix in a docstring). Confirm the
  self-knowledge doc auto-refreshes and gets included in the commit.
- Make a registry change (add a new tool). Confirm the doc's
  capabilities section reflects it in the same commit.

# Phase 5 — Wire into the agent's system prompt

Modify the system-prompt assembly function we identified in Phase 0
to inject the rendered self-knowledge doc — or a **slimmed-down
summary** of it — into the prompt. Two flavors worth supporting:

- **Slim summary** — the doc's identity + core principles + a short
  capabilities list (just names, no descriptions). Fits in ~500
  tokens. This goes into every turn's system prompt.
- **Full doc** — the entire rendered doc. Used for tool-use turns
  where the agent might need to reason about whether a capability
  exists. Optional.

Add a setting or feature flag to control which flavor is used, with
the slim summary as the default.

**Verification before committing this tier:**
- Ask the agent a question whose answer depends on a recently-added
  tool. The agent should mention the tool by name.
- Remove the tool. Refresh the doc. Ask the same question. The agent
  should no longer mention it.

# After all five tiers

Summarize what shipped:

- Files added / modified, by tier.
- The CLI commands the user can run.
- The CI behavior (soft check today, promote to strict once stable).
- The pre-commit hook behavior.

Then propose 2-3 follow-ups (typically: tighten CI to strict, add more
AUTO blocks for project-specific structure, write a runbook).

# Rules for the whole session

- **Don't combine tiers.** Each tier is its own commit and its own
  approval gate.
- **No placeholder code.** If a step needs code, show the code.
- **Match my stack's conventions** — don't impose a different
  testing framework, formatter, or layout than the rest of the
  repo uses.
- **Read before you write.** Before generating any AUTO block, read
  the actual registration site from my code. If you can't find
  it, ask — don't invent.
- **Failure modes matter.** If a generator can't find its source
  registry (import error, etc.), it should produce a clearly-marked
  "_unavailable, regenerate manually_" placeholder, not crash the
  commit.
- **Don't gate the pre-commit hook on network calls.** The refresh
  should be entirely local and fast (sub-second on a warm import).

Start with Phase 0.
````

---

## Why this works

Most agent codebases drift away from their system prompt within weeks. Tools get renamed, sub-agents get retired, integrations get swapped. The agent's self-description quietly rots, and the agent quietly hallucinates capabilities that no longer exist.

The pattern above flips the direction of truth: the **code** is the source, the **doc** is a rendering, and the **prompt** is a slice of the rendering. Every commit re-renders. Drift becomes a CI failure, not an embarrassing demo moment.

A few things that surprised me when I built this:

- **The slim summary matters more than the full doc.** A 500-token block of "you are X, you can do Y/Z/W, you must never do Q" outperforms a 5000-token full self-knowledge block in every test I ran.
- **The drift checker is the highest-leverage piece.** It catches the moment a doc reference goes stale — usually within a day of the actual rename — and forces the fix before it ever reaches the agent.
- **The hook should be soft by default.** A pre-commit hook that fails on drift becomes a thing developers disable. A hook that auto-refreshes and stages the diff becomes invisible infrastructure.
- **Sub-agents need this even more than top-level agents.** When a sub-agent claims to be able to do something it can't, the failure mode is usually a tool-use loop that exhausts iterations rather than a clean refusal.

---

## What this is NOT

- It's not a memory system. The self-knowledge doc describes **architecture**, not lived experience. (Memory is a different problem — embeddings, retrieval, durable summarizers. This pattern composes with memory but doesn't replace it.)
- It's not a replacement for runtime introspection. If your agent needs to know what tools are available **right now** (e.g., to dispatch correctly), it should still query the live registry. The doc is for grounded self-description in conversation.
- It's not a substitute for tests. The drift checker only catches references in prose; it won't notice a logic bug in a tool's `execute()`.

---

## Calibration

If your codebase is under 5K LOC and has one tool registry, Phase 1-2 takes 30-45 minutes with a capable coding agent. Each subsequent phase is another 30-45 minutes. The full five-phase pass is doable in a single 3-4 hour session.

If your agent has multiple sub-agents and a voice loop, plan on a half-day. The interview matters — answer it carefully and the implementation goes straight; rush it and you'll redo Phase 2.

---

If you build this and the prompt could be tighter or a phase could be clearer, I want to hear about it.
