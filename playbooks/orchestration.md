# Give Your AI Agents an Orchestration Layer

If you've built more than one AI agent, you already know the dirty
secret: **the hard part isn't the agents. It's the conductor that
runs them.**

Spinning up a "coding agent" or a "research agent" is a weekend
project. Getting five of them to work together without stepping on
each other, running up a surprise bill, silently chaining one
mistake into the next, or taking down your whole process when one of
them hits a wall — that's the part nobody posts about.

That conductor is the **orchestration layer**. It's the thin but
opinionated layer that sits *above* your agents and decides who does
what, with which tools, under what limits, and what happens when
something goes wrong. Most multi-agent setups don't have one. They
have a router that calls functions and hopes for the best.

This prompt walks your agent codebase (via Claude Code, Cursor, or
Aider) through building a real orchestration layer with six
properties:

1. **Smart routing** — the orchestrator picks the right agent,
   decomposes multi-step requests, and asks one clarifying question
   instead of guessing.
2. **Least-privilege tool scoping + bounded execution** — every
   agent gets a filtered subset of tools and hard limits, so none of
   them can run away.
3. **Failure isolation** — errors are contained at every boundary;
   one agent failing never crashes the orchestrator or the other
   agents.
4. **Human-in-the-loop confirmation gates** — risky/irreversible
   actions stop and ask before they execute.
5. **A handoff system** — when one agent finishes, it *proposes* the
   next step and target agent; the human approves. No agent silently
   dispatches another.
6. **Live hot-reload** — new agents register at runtime from config,
   with no restart or redeploy.

It is **framework-agnostic**. It doesn't matter whether you're in
Python, TypeScript, Go, or Rust, or which LLM provider you use. The
patterns below are about *control flow and boundaries*, not any
specific SDK.

Read the whole thing once. Then start at Phase 0.

---

## Phase 0 — Interview first (do NOT write code yet)

> **AI assistant reading this:** before you implement anything, your
> first job is to understand the system you're working in. Ask the
> human the questions below, **wait for their answers**, and only
> then map the tiers onto their actual stack. Do not scaffold,
> install, or refactor anything until you've done this. If an answer
> reveals a tier they already have, skip it and say so.

Ask these, grouped. Keep it conversational — you don't need all of
them answered perfectly, you need enough to make good calls.

**About the stack**
1. What language/runtime and framework is your agent built in?
2. Which LLM provider and SDK are you calling? (This determines how
   you read token usage, stop reasons, and stream responses.)
3. Is your agent loop synchronous or async/concurrent? Can two
   agents run at once today?

**About how agents are invoked today**
4. How does your main agent call a sub-agent right now — a tool/
   function call, a subprocess, an HTTP call, something else?
5. Do you have a single shared **tool/function registry**, or are
   tools wired in ad hoc per agent?
6. Do you have one agent or several? If several, how does the main
   one decide which to call?

**About state and signalling**
7. Where does durable state live — a database, flat files, nothing
   yet? Do you have any pub/sub or notify mechanism (e.g. Postgres
   `LISTEN/NOTIFY`, Redis, a message bus)?
8. Is there a UI, log stream, or WebSocket that watches agent
   activity, or is it all headless?

**About risk and control**
9. Which actions in your system are **destructive or irreversible**
   (send email, deploy, charge a card, write to prod, delete files)?
10. What failure modes have actually bitten you — runaway loops,
    cascading errors, cost blowups, one agent's crash killing the
    run?
11. What's your human-in-the-loop expectation: should agents ever
    act fully autonomously, or do you want a human in the path for
    anything consequential?

> Once you have answers, summarize the system back to the human in
> 3–4 sentences, tell them which of the six tiers they already have
> (even partially), and propose the **smallest possible first PR**.
> Then build tier by tier. Each tier below is independently
> shippable — do not bundle them.

---

## The design principles (the "why" behind every tier)

Keep these in front of you the whole build. Every decision below
traces back to one of them.

- **The orchestrator is a router, not a worker.** It decides *who*
  and *whether*, then gets out of the way. Keep its own logic thin.
- **Agents propose, humans dispose.** The default posture for
  anything consequential is "suggest and wait," not "act."
- **Least privilege by default.** An agent should hold exactly the
  tools its job requires and not one more.
- **Bound everything.** Every loop has a max iteration count, every
  call has a token ceiling, every agent has a model it's allowed to
  use. No unbounded anything.
- **Fail in a box.** An error should be the smallest blast radius
  possible — caught at its own boundary, reported as data, never
  propagated up as an exception that kills the run.
- **Pass references, not payloads.** When agents hand work to each
  other, pass paths/IDs/URLs, not giant inline blobs. Keeps results
  small and serializable.

---

## Tier 1 — Smart routing (dispatch intelligence)

**Goal:** the orchestrator chooses the right agent on purpose,
instead of pattern-matching a keyword and guessing.

**The problem this solves:** naive routing keys off surface words.
The user says "build," so it calls the engineering agent — even when
what they actually needed was a design/spec step first. The result
is technically-correct, generically-bad output, and a wasted
dispatch.

**What to build:**

- A short, explicit **routing policy** the orchestrator reads on
  every turn. Not a vibes-based "pick an agent" — concrete rules:
  - *Which* agent owns which kind of work, in one line each.
  - *Ordering rules* between agents that have a natural sequence
    ("a design/spec step should precede an implementation step").
  - A *decomposition rule*: a multi-step request becomes multiple
    sequential dispatches, not one mega-dispatch.
  - A *clarify-don't-guess rule*: if the request is genuinely
    ambiguous between two agents, ask **one** short question first.
- If you use a system prompt for the orchestrator, this policy lives
  there. If you route in code, encode it as explicit branching with
  comments explaining the *why*, not just the *what*.

**Key design decision:** routing intelligence belongs to the
orchestrator alone. Individual agents should not know about each
other's existence at dispatch time — that knowledge centralizes in
the conductor.

**Verification:**
- Give it three requests: one obviously for agent A, one obviously
  for agent B, and one ambiguous between them. It should route the
  first two correctly and *ask a question* on the third.
- Give it a multi-step request and confirm it produces an ordered
  plan of separate dispatches, not a single blurred one.

---

## Tier 2 — Least-privilege tool scoping + bounded execution

**Goal:** each agent runs with a filtered subset of your tools and
hard execution limits, so no single agent can do more than its job
or loop forever.

**The problem this solves:** if every agent gets the full toolbox
and an unbounded loop, a confused agent can call tools it has no
business calling and burn tokens in circles. Scope and bounds turn
"runaway" into "stops cleanly."

**What to build:**

- A per-agent **tool allowlist**. Define each agent's allowed tool
  names (in config or a manifest), and at dispatch time filter the
  global registry down to just those. The agent only ever *sees* the
  tools it's allowed to use.
- A **bounded agent loop** for every agent:
  - A `MAX_ITERATIONS` cap on the tool-use loop (e.g. 6–10). On
    exhaustion, return a clear "didn't converge" result rather than
    spinning.
  - A `max_tokens` ceiling per call.
  - A declared **model per agent** — cheaper models for cheap work,
    stronger models only where they earn it.
- If you don't already have one, introduce a **single shared tool
  registry** so allowlists are just "names from the registry." This
  is the backbone the rest of the tiers lean on.

**Key design decision:** filter tools by *reference to a shared
registry*, don't re-implement tools per agent. One source of truth
for "what a tool is," many views of "which tools this agent gets."

**Verification:**
- Configure an agent with a 2-tool allowlist and confirm, at
  runtime, it's offered exactly those 2 — not the whole set.
- Force a loop (a tool that always asks for more) and confirm it
  stops at `MAX_ITERATIONS` with a graceful message, not a hang.

---

## Tier 3 — Failure isolation at every boundary

**Goal:** any single agent, tool, or observer can fail and the
orchestrator keeps running. One blast never takes out the system.

**The problem this solves:** in a naive setup, an exception inside a
tool or a sub-agent bubbles up and kills the whole turn — or worse,
the whole process. Users see a stack trace instead of "that agent
hit a snag."

**What to build — wrap each of these boundaries in its own
try/catch (or equivalent) that returns an error *as data*:**

- **Tool execution.** A failing tool returns a structured error
  result (`{"error": "..."}`) that the model can read and react to —
  it never throws past the router.
- **Sub-agent dispatch.** Wrap the agent's run; on failure return a
  human-friendly "that agent ran into trouble" result plus a short
  error string for logs. The orchestrator stays alive.
- **Observer/side-effect hooks.** Any hook that emits events to a UI,
  logs, or analytics must be fire-and-forget: if it throws, swallow
  it. A broken dashboard must never block real work.

Add **logging at each boundary** so a contained failure is still
visible to you (log the tool name + a truncated result), even though
it didn't crash anything.

**Key design decision:** errors cross boundaries as **values, not
exceptions.** Inside an agent you can throw freely; at the boundary
where it hands back to the orchestrator, convert to a result object.

**Verification:**
- Make a tool throw on purpose. Confirm the turn completes, the
  model gets an error result, and the process is still up.
- Make an observer hook throw. Confirm the dispatch still succeeds.

---

## Tier 4 — Human-in-the-loop confirmation gates

**Goal:** destructive or irreversible actions stop and ask for
explicit confirmation before they execute.

**The problem this solves:** an agent that can send email, deploy,
move money, or delete files is one hallucinated tool call away from
a very bad day. A gate makes "almost did something dumb" into "asked
first."

**What to build:**

- Mark certain tools as **`requires_confirmation`** (a flag on the
  tool definition is the cleanest place).
- In your tool router, when a flagged tool is called, **don't
  execute it.** Return a structured `confirmation_required` payload
  describing the tool and its inputs.
- Surface that to the human (voice, chat, a UI banner — whatever your
  surface is) and **wait**. Only on explicit human approval do you
  call a separate "execute-confirmed" path that bypasses the gate
  and runs the action.
- Decide your default: for anything irreversible, the gate should be
  **on** unless the human has explicitly opted that tool out.

**Key design decision:** the gate lives in the **router**, not
inside each tool. Tools stay simple; the orchestration layer owns
the "should this even run yet" decision.

**Verification:**
- Call a confirmation-gated tool and confirm it returns the
  confirmation prompt and does **not** perform the action.
- Approve, run the confirmed path, and confirm the action now
  executes exactly once.

---

## Tier 5 — The handoff system (propose, don't chain)

**Goal:** when one agent finishes, it can recommend the *next* step
and *which agent* should take it — but the human is always the one
who pulls the trigger. No agent silently dispatches another.

**The problem this solves:** the most tempting multi-agent pattern is
also the most dangerous one: agent A finishes and auto-calls agent
B, which auto-calls agent C. Errors compound invisibly and you get a
confident, wrong result three hops deep with no human checkpoint.
Structured handoffs keep the chain *visible and interruptible.*

**What to build:**

- A small, typed **handoff recommendation** object an agent can
  optionally return alongside its result. Fields that earn their
  keep:
  - `target_agent` — which agent should take the next step.
  - `reason` — one human-readable sentence on *why* (so the
    orchestrator can voice it naturally, not as a robotic flag).
  - `task` — the natural-language task to pass the next agent
    verbatim on acceptance.
  - `artifacts` — a map of **paths/IDs/URLs** the next agent should
    read. **Not inline blobs** — references keep the handoff small
    and serializable into your event stream.
  - `preconditions` — things the human should verify before
    accepting (surface these in the approval UI).
  - `confidence` (0–1) — how strongly the agent vouches for the
    handoff. The orchestrator uses this to *phrase* the offer
    ("definitely worth handing off" vs "you might want to…").
- Orchestrator behavior: read the recommendation, present it to the
  human as a **conversational offer**, and **wait for approval**. On
  acceptance, call the target agent's normal dispatch path with the
  `task`. On rejection, drop it.

**Key design decision:** **the human is the circuit-breaker.** Agents
*propose* the graph of work; the human *approves* each edge. This
single rule is what stops errors from compounding across a chain —
do not let agents call each other directly.

**Verification:**
- Have an agent return a handoff recommendation and confirm the
  orchestrator surfaces it as an offer and pauses.
- Confirm that *nothing* dispatches to the target agent until the
  human explicitly accepts.
- Confirm artifacts ride as references and the receiving agent reads
  them from the path/ID, not from an inlined payload.

---

## Tier 6 — Live hot-reload (config-driven agent runtime)

**Goal:** add, change, or retire an agent at runtime from
config/data, with no restart and no redeploy.

**The problem this solves:** if every new agent means a code change
and a restart, your roster is frozen between deploys and you can't
let the system extend itself. A config-driven runtime makes agents
*data*, so the set of available agents can change while you're
running.

**What to build:**

- A **config-driven agent runtime**: one generic agent class that
  takes a manifest (system prompt, allowed model, tool allowlist)
  and runs the standard bounded tool-use loop from Tier 2. No
  bespoke class per agent — the manifest *is* the agent.
- A **manifest store** — rows in a database or files on disk — that
  is the source of truth for "which agents exist right now."
- A **registry watcher** that, on startup and on a change signal
  (a DB `NOTIFY`, a file-watch event, a webhook — whatever your
  stack offers from your Phase 0 answers):
  - loads the current active manifests,
  - **registers** a `dispatch_to_<name>` tool for any new agent,
  - **unregisters** the dispatch tool for any agent that's gone,
  - refreshes the in-memory roster.
- A **dispatch-tool factory** that, given a manifest name, returns a
  fresh dispatch tool wired to the generic runtime. This is what the
  watcher calls to register new agents on the fly.

**Key design decision:** the *capability* (a `dispatch_to_<name>`
tool) and the *agent definition* (a manifest) are decoupled. The
watcher's only job is to keep the live set of dispatch tools in sync
with the manifest store. Adding an agent = inserting a manifest +
firing the signal. Nothing restarts.

**Verification:**
- With the system running, add a new agent manifest and fire your
  change signal. Confirm a `dispatch_to_<name>` tool appears and is
  immediately callable — no restart.
- Retire (deactivate) that manifest, fire the signal, and confirm the
  dispatch tool disappears.

---

## How the six tiers fit together

Built in order, each tier leans on the one before it:

- **Tier 2's shared registry** is what **Tier 1** routes over, what
  **Tier 5** filters into allowlists, and what **Tier 6** registers
  into at runtime.
- **Tier 3's failure isolation** is what makes **Tier 5's** handoffs
  and **Tier 6's** dynamic agents safe to run — a bad agent fails in
  its box.
- **Tier 4's confirmation gates** and **Tier 5's handoff approvals**
  are the same idea applied at two levels: *nothing consequential
  happens without a human yes.*

The through-line across all six: the orchestration layer optimizes
for **safe orchestration** — smart routing in, least-privilege and
bounded execution in the middle, human-in-the-loop at every
consequential edge, and failure contained so one agent's problem
never becomes the whole system's problem.

You don't need all six to get value. Ship Tier 1 and you route
smarter tomorrow. Add Tier 3 and you stop getting paged at 2am. The
order above is the recommended path, but each tier stands on its
own.

---

## Stumbling blocks to avoid

- **Don't let agents call each other directly.** The moment agent A
  can dispatch agent B without a human in the path, you've built an
  error amplifier. Route everything through the orchestrator and the
  handoff system.
- **Don't put the confirmation gate inside the tools.** It belongs
  in the router so every tool benefits and the logic lives in one
  place.
- **Don't pass big payloads between agents.** References (paths/IDs/
  URLs) keep handoffs small, serializable, and easy to log.
- **Don't give every agent the full toolbox** "to be safe." That's
  the opposite of safe. Scope down.
- **Don't skip the bounds** because "it usually converges." The one
  time it doesn't is the run that costs you.
- **Don't make the orchestrator do the work.** If you find domain
  logic creeping into the conductor, push it down into an agent. The
  conductor decides *who*; the agents decide *how*.
