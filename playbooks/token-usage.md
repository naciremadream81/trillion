# Give Your AI Agent a Cost Dashboard

If you've built an AI agent, you've probably had this moment: you open your LLM provider's billing page and think *"...where did that come from?"* You have no idea which part of your agent spent the money, whether prompt caching is actually helping, or what a normal day even costs.

The frustrating part: **the data was in your hands the whole time and you threw it away.**

Every response your LLM returns carries a `usage` object — input tokens, output tokens, and (if your provider supports prompt caching) cache-read and cache-write tokens. Almost every agent codebase reads `response.content` and discards `response.usage` without a second glance. You're paying for that data twice: once to the provider, and again in not knowing.

This prompt walks your agent codebase (via Claude Code, Cursor, or Aider) through building a **live cost dashboard**:

- A small **pricing table** — per-model rates — and a cost function
- A **usage table** — one row per LLM call, with token counts and computed cost
- A **best-effort recorder** wired into your central LLM client — captures every call's usage *without ever being able to break a conversation turn*
- A **month-to-date aggregation**: total cost, a per-model breakdown, **cache savings**, and a **day-by-day** view
- A **cost indicator** in your UI — at-a-glance month-to-date spend, click to expand the full breakdown

Once it's in, you glance at your own UI and know exactly what your agent costs — this month, today, per model — and you can *see* prompt caching earning its keep.

---

## How to use this

1. Open your agent codebase in Claude Code / Cursor / Aider.
2. Paste **the entire prompt block below** as your first message.
3. Answer the interview honestly — the agent adapts the build to your stack and your LLM provider.
4. Approve each tier as it ships. Each one is committable on its own.

---

## The paste-ready prompt

````
I want you to help me build a cost dashboard into this AI agent — live
tracking of the agent's own LLM API token usage and cost, surfaced as
an indicator in the UI. The data we need is already coming back on
every LLM response (the `usage` field) and is currently being thrown
away; we're going to stop discarding it.

Don't write any code yet. Start with an interview.

# Phase 0 — Interview

Ask me, one at a time:

1. **The agent's name and one-line description.**

2. **Primary language and framework** — Python + FastAPI, TypeScript +
   Next.js, Go, etc. Follow the conventions already in the repo.

3. **The central LLM client.** This is the most important question.
   Find the chokepoint where the agent's main calls to the LLM API go
   through — one module, one or two functions. Show me the file. I need
   to see where the response object is received.
   - If there is NO single chokepoint — if `messages.create` /
     `chat.completions.create` calls are scattered across many files —
     STOP and tell me. The first thing we do is introduce a thin
     wrapper so every call funnels through one place. Cost tracking
     bolted onto a chokepoint is reliable; sprinkled everywhere it
     rots.
   - Note whether the client has BOTH a streaming and a non-streaming
     path — both need capture.

4. **LLM provider(s)** — Anthropic, OpenAI, Google, etc. This decides
   the shape of the `usage` object:
   - Anthropic: `input_tokens`, `output_tokens`,
     `cache_creation_input_tokens`, `cache_read_input_tokens`
   - OpenAI: `prompt_tokens`, `completion_tokens`, and cached tokens
     under `prompt_tokens_details.cached_tokens`
   - Confirm by reading the SDK's response type, don't assume.

5. **Does the agent use prompt caching?** If yes, we'll price cache
   reads and writes separately and surface the savings — it's the most
   satisfying number on the whole dashboard.

6. **Storage** — what database does the agent use, and how are
   migrations done? Show me one recent migration so I match the
   pattern.

7. **Is there a UI?** What stack? Is there an **existing indicator,
   pill, badge, or widget** in it I can mirror — something small and
   always-visible? If so, show it to me. Mirroring an existing element
   is how the new one looks native instead of bolted-on.

8. **How is configuration wired** — a settings module, env vars? And
   how does the LLM client get its dependencies (constructor
   injection, a registry, globals)?

9. **Do sub-agents or background jobs make their own LLM calls** with
   their own models? We'll scope the MVP to the main client, but I
   want to know what we're deliberately leaving out.

After each answer, summarize what you've learned in 2-3 lines so I can
correct you.

# Phase 1 — Pricing

Build a small pricing module:

- A `MODEL_PRICING` table: per-model rates, in cost-per-million-tokens,
  for each token class the provider bills separately — input, output,
  and (if caching is used) cache-write and cache-read.
- A `compute_cost(model, ...token counts...)` function.
- **Longest-prefix model matching.** Model strings carry version
  suffixes (`claude-sonnet-4-6-20260514`, `gpt-5-turbo-2026-01`).
  Match `claude-sonnet-4-6` against the `claude-sonnet-4` family entry
  by longest matching prefix, so a dated string still resolves.
- **Unknown model → return 0.0 and log a warning** naming the model.
  Never throw. A row should still be recorded with its token counts so
  cost can be backfilled once the rate is added.
- Fill the table with the provider's current published rates. Add a
  comment that pricing changes over time and the table is the thing to
  update.

**Verification:** unit tests — exact-model match; version-suffixed
prefix match; unknown model → 0 + warning; a call with cache tokens
priced correctly across all classes.

# Phase 2 — Storage

Add a `usage` table (or your stack's equivalent): one row per LLM call.
Columns: id, model, the token counts (one column per class),
`cost_usd`, a `source` label (free-text — `"conversation"` now, room
for `"summarizer"` etc. later), and a timestamp. Index the timestamp —
every dashboard query is a time-range scan.

One row per call is fine. Even a heavy month is a few thousand rows.
Don't pre-optimize with rollup tables; revisit only if it ever matters.

**Verification:** a migration test that the table exists with the
expected columns, and a repo round-trip test.

# Phase 3 — Capture (the part that must never break a turn)

Build a `record_usage` helper and wire it into the central client.

- A repo class with `record(...)` (insert one row) and a
  `usage_since(start, end)` aggregation that returns summed token
  counts, summed cost, and a per-model breakdown.
- A `record_usage(model, usage, source)` helper that pulls the token
  counts off the provider's `usage` object (tolerating missing/None
  cache fields), computes cost via Phase 1, and inserts a row.
- **`record_usage` is wrapped, in its entirety, in a catch-all.** Any
  failure — DB down, malformed usage object, anything — is logged and
  swallowed. Recording cost must NEVER slow or break a conversation
  turn. This is non-negotiable: a metrics feature that can take down
  the agent is worse than no feature.
- **Wire the repo in with a module-level setter** — a
  `set_usage_repo(repo)` called once at startup — rather than threading
  a repo argument through the LLM client's constructor and every call
  site. Less churn, and it mirrors how other optional integrations are
  usually wired.
- Call `record_usage` in the client immediately after each response is
  received — in BOTH the streaming and non-streaming paths. For
  streaming, the usage totals arrive on the final message of the
  stream; grab them there. Record the model the API actually
  *returned*, not just the one you requested.

**Verification:** unit tests — `record_usage` computes the right cost
and calls the repo; a repo that raises does NOT propagate; a usage
object with None cache fields is handled. Confirm existing client
tests still pass and conversation behavior is unchanged.

# Phase 4 — Aggregation + endpoint

Build the month-to-date payload:

- A function that returns: this month's total cost and token counts; a
  per-model breakdown (cost + call count); **cache savings** — what
  cache-read tokens cost versus what those same tokens would have cost
  at the full input rate; today's spend; a month-over-month delta; and
  a **day-by-day** breakdown (group rows by calendar day — the data
  supports it for free because every row is timestamped).
- Wrap it behind a **short in-memory cache** (~60s) so a UI that polls
  every minute is effectively free.
- Expose it as one endpoint (`GET /…/usage` or your equivalent).

**Verification:** unit tests for the aggregation — totals, cache-saving
math, the delta, the day grouping, and the empty-table case (must
return a clean zeroed payload, never an error). One test that the
endpoint returns the payload over HTTP.

# Phase 5 — The cost indicator UI

Add a small always-visible cost indicator, with a click-to-expand
panel.

- The indicator face: month-to-date cost, big and glanceable. If you
  have a month-over-month delta, show it — but **invert the color
  sense from revenue/positive metrics**: for spend, *rising* is the
  bad direction. Up = red, down = green.
- The expand panel: per-model rows, the cache-savings line, today vs
  month-to-date, and the day-by-day list (newest day first, in a
  scrollable region so a full month fits).
- If you're mirroring an existing UI element (Phase 0, Q7): copy it
  faithfully — **and mirror the element TYPE, not just the CSS.** A
  hard-won lesson: a `<button>` and a `<div>` with byte-identical CSS
  render differently — the button carries user-agent chrome and its
  own font. If the element you're mirroring is a button, yours is a
  button.
- Label the indicator so it can't be confused with any neighbor. If a
  revenue figure and a cost figure sit side by side, both are just
  dollar amounts — give the cost one a small "API" / "TOKENS" tag.
- Poll the endpoint on the same cadence as the cache TTL (~60s), only
  while the tab/window is visible.

**Verification:** no automated test for the markup, so verify by hand —
have the agent make a couple of LLM calls, confirm rows land, confirm
the indicator shows a non-zero figure, expand the panel, check every
section renders, and confirm it visually matches the element you
mirrored.

# After all five tiers

Summarize: files added/changed by tier, the endpoint, the polling
cadence, and what's deliberately out of scope (sub-agent calls, etc.).
Then propose 2-3 follow-ups — typically: fold in sub-agent/background
usage, a sparkline for the daily trend, or a soft budget alert.

# Rules for the whole session

- **Don't combine tiers.** Each tier is its own commit and its own
  approval gate.
- **No placeholder code.** If a step needs code, write the code.
- **Match my stack's conventions** — testing framework, migration
  style, file layout. Don't import new patterns.
- **Read before you write.** Before touching the LLM client, read it
  completely — both paths, how the response is consumed. A capture
  call inserted in the wrong place can change conversation behavior.
- **Best-effort is the law for recording.** If you ever find yourself
  writing a `record_usage` that can raise into the conversation path,
  stop and re-do it.
- **The capture insert is tiny and local.** Don't make the conversation
  turn wait on anything slow; don't add network calls to the hot path.

Start with Phase 0.
````

---

## Why this works

The whole feature rests on one observation: **you are already being handed the data.** Every modern LLM SDK returns a `usage` object on the response. You don't have to instrument anything, sample anything, or call a billing API. You have to stop calling `response.content` and ignoring `response.usage` two lines later.

A few things that mattered more than expected when building this:

- **The chokepoint is everything.** If every LLM call in your agent already funnels through one client, this feature is a half-day. If they're scattered, the real work is consolidating them first — and that's worth doing regardless, because the next observability feature will need the same chokepoint.
- **Best-effort recording is not a nicety.** The first instinct is to `await` the DB write and move on. But a metrics path that can throw is a metrics path that can drop a user's conversation. Wrap the entire recorder in a catch-all and mean it.
- **Cache savings is the number people love.** Total spend is useful but anxious. "Prompt caching saved you $14 this month" is the line that makes the dashboard feel like a win instead of a bill.
- **Mirror the element type, not just the CSS.** Two UI elements with identical styling rendered visibly differently — one was a `<button>`, one a `<div>`. Browsers give buttons their own chrome and font. If you're copying an existing widget, copy the tag.
- **Timestamp every row and the time-series is free.** Because each call is its own dated row, "this month, day by day" is a `GROUP BY date`. You don't design for the historical view — you just don't throw the timestamp away.

---

## What this is NOT

- It's not a replacement for your provider's billing. Your local rate table can drift from published pricing; treat the dashboard as a high-fidelity *estimate* and update the table when prices change.
- It's not full distributed tracing. It answers "what did my agent spend and where" — not "why was this one request slow."
- It doesn't cap spend. A budget alert or hard limit is a natural follow-up, but it's a separate feature with its own failure modes — don't smuggle it into the MVP.

---

## Calibration

If your agent already has a single LLM client, Phases 1-3 take about an hour with a capable coding agent — they're mechanical once the chokepoint is identified. Phase 4 is another 30-45 minutes. Phase 5 depends entirely on whether you have an existing UI element to mirror: with one, an hour; without one, budget more for the design.

The whole thing is a comfortable single afternoon. The interview is what keeps it on the rails — answer Q3 (the central client) carefully and the rest goes straight.

---

If you build this and a phase could be clearer, I want to hear about it.
