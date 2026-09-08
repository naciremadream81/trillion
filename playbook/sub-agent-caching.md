# Give Your Sub-Agent Team Prompt Caching

If you run a team of AI sub-agents on Anthropic, you've probably gotten the email: *"Your prompt cache hit rate is low — caching repeated content like system prompts could save you up to 35%."* And your first thought was: *don't I already cache?*

Maybe you do — partly. Here's the trap almost every agent team falls into: you add `cache_control` to your **main** chat loop, see the hit rate climb, and call it done. But your sub-agents — the support-ticket triager, the research helper, the background poller that fires every two minutes — each re-send a large, **identical** system prompt and a full set of tool schemas on *every single call*, at full price, uncached. Those are the calls quietly dragging your average down.

The frustrating part: **the repeated content is the easiest thing in the world to cache, and the data to prove it's working is already on every response.** Every Anthropic response carries `usage.cache_read_input_tokens` and `usage.cache_creation_input_tokens`. You don't have to guess whether caching helps — you can measure it before and after.

This prompt walks your agent codebase (via Claude Code, Cursor, or Aider) through a **diagnose-then-fix pass on prompt caching across your whole sub-agent team**:

- **Measure the real baseline** — what fraction of your input tokens are full-price today
- **Audit every API call site** — which cache, which don't, and *why*
- **Cache the shared sub-agent loop** — one change that covers every agent that runs through it
- **Cache the main conversation history** — not just the system prompt, the growing turns too
- **Catch the silent invalidators** — a `datetime.now()` in a system prompt, an unstable tool order, a tool set that changes mid-session — that keep a hit rate near zero even *with* `cache_control` set
- **Verify** — re-pull the same usage numbers and watch the full-price share fall

It is deliberately generic. It doesn't care what your sub-agents are named or what they do — only how your code calls the API.

---

## How to use this

1. Open your agent codebase in Claude Code / Cursor / Aider.
2. Paste **the entire prompt block below** as your first message.
3. Answer the interview honestly — the agent maps the build to *your* call structure.
4. Approve each tier as it ships. Each one is committable on its own, and each is verifiable against the usage numbers.

---

## The paste-ready prompt

````
I want you to help me add (or fix) prompt caching across this AI agent
and its sub-agent team, so we stop re-paying full price for the large,
identical system prompts and tool schemas we send on every call. This
codebase uses the Anthropic Messages API. We will DIAGNOSE first —
measure the current cache hit rate and find every place we miss — then
fix it in independently-shippable tiers, re-measuring after each.

Don't write any code yet. Start with an interview.

# Phase 0 — Interview

Ask me, one at a time:

1. **The agent's name and one-line description**, and the primary
   language / framework. Follow the conventions already in the repo.

2. **Confirm the provider is Anthropic.** This prompt assumes the
   Anthropic Messages API and its `usage` cache fields
   (`cache_read_input_tokens`, `cache_creation_input_tokens`). If any
   calls go through a different provider, STOP and tell me — the
   caching mechanics and field names differ.

3. **The call-site map — the most important question.** Find EVERY
   place code calls the API: `messages.create`, `messages.stream`,
   `beta.messages.create`. For each, give me: file:line, which
   agent/feature it serves, and how often it runs (per user turn? per
   sub-agent dispatch? a background poller on a fixed interval?). I
   especially want two things:
   - (a) The **main conversation loop** — the highest-volume,
     largest-prompt path.
   - (b) Whether the sub-agents share ONE tool-use loop helper, or each
     have their own copy of the loop. If there's a shared loop, show it
     to me — fixing it once fixes every agent that runs through it.

4. **Is `cache_control` set ANYWHERE in an actual request today?** Grep
   for `cache_control` / `ephemeral`. Ignore matches that are only
   pricing or usage-accounting code. Tell me exactly which call sites
   set it and which don't.

5. **How is each system prompt built?** For the main loop and each
   sub-agent: is it a static string/constant, or assembled per request?
   CRUCIALLY — does any system prompt interpolate VOLATILE content that
   changes on every call: today's date, the current time, a session id,
   a UUID, per-request user state, a "current context" block, live
   memory contents? Quote the exact lines. (Caching is a prefix match —
   one changing byte in the cached prefix invalidates everything after
   it, so a single `datetime.now()` in a system prompt can drive a hit
   rate to zero.)

6. **How is the tools list built, and is its order STABLE across
   requests?** Is it a fixed list, sorted deterministically, or iterated
   from a dict/set? Does the tool SET vary per request or per user
   (conditional tools, per-user tool allowlists, tools registered at
   runtime)? Tools render BEFORE the system prompt, so any change to the
   tool list busts the entire cache for that request.

7. **Does any agent combine its LLM calls with a context-management /
   history-compaction / "compact" feature?** Note which. We will let
   those opt OUT of caching — combining `cache_control` with aggressive
   context editing has caused token-accounting blow-ups for people, and
   it's not worth the risk on a first pass.

8. **Do you record `usage` per call anywhere** — a usage table, a
   metrics sink, logs? If yes, can we query the month-to-date split of
   cache-read vs cache-write vs uncached input tokens? If no, we'll read
   `response.usage` directly off a couple of live calls to get a
   baseline. Either way I want a number before we touch anything.

After each answer, summarize what you've learned in 2-3 lines so I can
correct you.

# Phase 1 — Measure the baseline (no fixes yet)

Establish the current state in numbers, because "the hit rate feels low"
is not a baseline.

- If a usage table / metrics exist: sum this month's `input_tokens`
  (full price), `cache_creation_input_tokens` (writes, ~1.25x), and
  `cache_read_input_tokens` (reads, ~0.1x). Report each as a share of
  total input-side tokens. The number that matters is the **full-price
  uncached share** — that's the headroom.
- If no usage data exists: pick the two highest-frequency call sites,
  log `response.usage` for a handful of real calls, and report the same
  split from those samples. Note that a single synthetic call can't show
  caching — you need at least two requests with an identical prefix
  within the cache TTL.
- Write the baseline down. We compare against it at the end.

# Phase 2 — The audit (still no fixes)

Produce a single prioritized findings table. One row per API call site,
columns:
- **Call site** (file:function) and **frequency**
- **Caches today?** (yes/no, and which blocks — tools, system, messages)
- **Cacheable prefix size** — roughly how big is the stable
  system+tools prefix? (Anthropic has a per-model minimum cacheable
  prefix, ~1k–4k tokens; below it, caching silently no-ops.)
- **Silent invalidators** — any volatile content in the cached prefix,
  unstable tool order, per-request tool set, or model switching
  mid-session
- **Opt-out?** — does it combine caching with context-management
  (Phase 0, Q7)
- **Recommended fix + priority** — biggest win = highest-frequency call
  site with the largest uncached stable prefix

Rank by impact. A background poller that fires every minute with a 5k-token
uncached system prompt outranks a once-a-week digest. Show me the table
and your proposed fix order. I approve the order before you touch code.

# Phase 3 — Cache the shared sub-agent loop

If the sub-agents share one tool-use loop (Phase 0, Q3b), this is the
highest-leverage fix — one change caches tools+system for every agent
that runs through it.

- Where the loop builds the request, pass the system prompt as a single
  cached content block instead of a bare string:
  a `[{"type": "text", "text": <system>, "cache_control": {"type": "ephemeral"}}]`.
  A breakpoint on the system block caches **tools + system together**,
  because tools render before system in the request. That stable prefix
  is what every sub-agent re-sends identically.
- Add a per-call **opt-out flag** (e.g. `cache_prompt: bool = True`).
  Any agent that combines caching with context-management (Phase 0, Q7)
  passes `cache_prompt=False` and is left byte-for-byte unchanged. This
  lets you turn caching on for the whole team without ripping it back
  out when one agent misbehaves.
- If the loop flips `tool_choice` (e.g. forcing a terminal tool on the
  last iteration), that's fine — a `tool_choice` change only invalidates
  the messages-tier cache, never the tools+system prefix.

**Verification:** a unit test that the loop passes a list-form cached
`system` block when caching is on, and the bare string when it's off.
Then confirm the loop's existing tests still pass — behavior is
unchanged except for the cache marker.

# Phase 4 — Cache the main conversation history

The main loop may already cache its system prompt. The leak that's left
is usually the **conversation history** — the growing list of prior
turns, re-sent in full at full price on every turn.

- Add a rolling cache breakpoint on the **last message block** of the
  request (or use the SDK's top-level `cache_control` on the create
  call, which auto-places a breakpoint on the last cacheable block).
  This caches the conversation prefix so each new turn reads the prior
  turns from cache instead of re-paying for them.
- This coexists with the existing system-prompt breakpoint — Anthropic
  allows up to 4 breakpoints per request; you're using 2.
- Keep any volatile per-turn content (the live date/time, the current
  user message) AFTER the last breakpoint, never inside the cached
  prefix.

**Verification:** inspect that `cache_control` is set on both the
streaming and non-streaming create calls. Then have a real multi-turn
conversation and confirm `cache_read_input_tokens` climbs turn over turn
(it can only be measured with real traffic).

# Phase 5 — Per-agent one-shots and volatile prompts

Mop up the remaining call sites.

- **Static-system one-shots** (a classifier, a summarizer, a planner
  with a constant system prompt): wrap the system in a cached block, same
  as Phase 3. Cheap, and they add up.
- **Prompts with a volatile token in the system text** (e.g. a per-run
  domain or topic injected into the system prompt): don't try to cache
  as-is — instead MOVE the volatile token OUT of the system prompt and
  into the first user message (or anywhere after the breakpoint). That
  turns a never-caching prompt into a static, cacheable one. If moving it
  isn't worth it, just leave that call uncached and note it.
- **Don't cache below the minimum.** If a call's stable prefix is under
  the model's minimum cacheable size, adding `cache_control` does
  nothing (no error, just no cache). Skip it and say so.

**Verification:** unit tests for the system-block shape where you changed
it; a note listing any call sites deliberately left uncached and why.

# Phase 6 — Verify the win

Re-run Phase 1's measurement after some real traffic has flowed through
the changed paths.

- Re-pull the same cache-read / cache-write / uncached split.
- The **full-price uncached share should fall** below the baseline, and
  the **cache-read share should rise**.
- If a call site you "fixed" still shows zero cache reads across repeated
  identical-prefix requests, a silent invalidator slipped through — diff
  the exact bytes of the prefix between two requests to find it (it's
  almost always a timestamp, an unsorted `json.dumps`, or a tool-order
  change).
- Note that month-to-date totals blend pre- and post-change calls, so
  the clean signal is the trend over the next day or two, not an instant
  jump.

# Rules for the whole session

- **Diagnose before you fix.** Phases 0-2 produce numbers and a ranked
  plan. Don't write a `cache_control` until I've approved the audit.
- **One tier per commit, one approval gate per tier.**
- **Never put volatile content in a cached prefix.** A date, a UUID, a
  session id, live state — these belong after the last breakpoint, in the
  messages, not in the system prompt.
- **Tool order and tool set must be stable** for a cache to survive.
  Sort tools deterministically; if tools are registered at runtime, know
  that adding one mid-session busts that request's cache.
- **Give every cached call an opt-out** so one problematic agent doesn't
  block caching for the whole team.
- **Match my stack's conventions** — testing framework, file layout,
  how the API client is constructed. Don't import new patterns.
- **Read the call site fully before editing it** — both the streaming
  and non-streaming paths. A marker in the wrong place changes nothing or
  busts everything.

Start with Phase 0.
````

---

## Why this works

Prompt caching rests on one rule: **it's a prefix match.** Anthropic hashes the request prefix up to each `cache_control` breakpoint; on the next request, the longest identical prefix is served from cache at ~10% of the input price. The render order is fixed — `tools` → `system` → `messages` — so a single breakpoint on the **last system block caches your tools and system prompt together**, which for a sub-agent team is the big, stable, endlessly-repeated part.

That single fact explains both the win and every way it goes wrong:

- **The win for agent teams is structural.** A specialist sub-agent has a fixed persona and a fixed tool set. Every time it runs, it re-sends the same multi-thousand-token prefix. That is the *ideal* caching target — and it's why a background poller that never cached is often the single biggest line in your bill.
- **One changing byte ruins it.** Because the match is exact, a `datetime.now()` in the system prompt, an unsorted `json.dumps()`, or a tool list that iterates a Python `set` in nondeterministic order means the prefix differs every call and *nothing* caches — even though `cache_control` is right there. This is the usual reason a hit rate is "mysteriously" low.
- **Tools come first, so they're the most fragile.** Adding or reordering a tool mid-session invalidates the entire cache, because tools render at position zero. Teams that register tools dynamically (a new specialist comes online) pay for this without realizing it.
- **You can prove it, for free.** `usage.cache_read_input_tokens` vs `usage.input_tokens` on the response tells you, per call, whether the cache hit. If reads stay at zero across two identical-prefix requests, you have an invalidator — no guessing required.

A few things that mattered more than expected:

- **The shared loop is the lever.** If your sub-agents run through one tool-use helper, you cache the whole team in one change. If each agent has its own copy of the loop, the real first task is consolidating them — worth doing regardless.
- **An opt-out flag is not over-engineering.** The day one agent's caching interacts badly with a context-compaction feature, you want to flip *that* agent off — not delete caching for everyone. A one-line `cache_prompt=False` is cheap insurance.
- **Caching the system prompt but not the history is half a fix.** On a long conversation, the re-sent turns can dwarf the system prompt. The second breakpoint on the message tail is where the rest of the savings lives.

---

## What this is NOT

- It's not a model swap or a quality change. Caching is purely a billing/latency optimization — identical outputs, cheaper inputs.
- It's not a fix for *sparse* traffic. The default cache lives ~5 minutes; if your calls are minutes apart, each write can expire before it's read (you'll see a high cache-*write* share). That's a separate lever — a longer TTL or a pre-warm — and a fair thing to leave for a follow-up.
- It's not provider-portable. The field names, the breakpoint model, and the minimum cacheable size here are Anthropic's. Another provider's caching works differently; re-audit, don't assume.

---

## Calibration

If your sub-agents already share one tool-use loop, the highest-value fix (Phase 3) is a 20-minute change plus a test, and it moves the most money. The audit (Phases 0-2) is where the time goes — an hour to map every call site and read each system prompt for volatile content — and it's the part that keeps you from "fixing" a call that was never going to cache.

Budget an afternoon for the whole pass on a mid-sized agent team. The single most important answer is Phase 0, Q5: which system prompts carry a volatile byte. Get that right and the fixes are mechanical.

---

If you run this on your own agent team and a phase could be clearer, I want to hear about it.
