# Run Your Laptop-Only Sub-Agents From the Cloud

A drop-in prompt for your AI coding agent (Claude Code, Cursor, Aider, or similar) that helps it add **cross-machine agent routing** to your project: the ability to talk to an always-on **cloud** agent (from your phone, a PWA, a chat client) and have it delegate work to a sub-agent that can *only* run on your **local machine** — because that sub-agent writes files into your repo, shells out to a local CLI, or needs local credentials the cloud doesn't have.

The cloud is the brain you talk to anywhere. The laptop is where the laptop-bound work actually happens. This prompt teaches the bridge between them: a durable task queue that doesn't lose work when the laptop is asleep, a way to run the *real* local agent to completion, and an honest round-trip so the human always knows what happened.

It encodes a battle-tested architecture and — critically — instructs the agent to **interview you first**, because not every codebase has a cloud/local split, a task queue, or laptop-only agents at all. Naming, transport, and topology all adapt to your answers.

**How to use it:** copy everything below the horizontal rule into a fresh chat with your AI agent. Answer the interview questions when it asks, then let it build tier by tier.

---

You are helping me add **cross-machine agent routing** to my project. Treat this whole message as your brief. **Do not write any code yet.** Your first job is to interview me, echo my answers back as a short plan, and only then build tier by tier with verification at each step.

## The feature, in one paragraph

I have (or want) an always-on **cloud** instance of my agent that I can talk to from anywhere, and a **local** instance that runs on my laptop. Some of my sub-agents are **laptop-only** — they write to my project filesystem, run a local CLI, or use local credentials — so they physically cannot execute in the cloud. Today, when I ask the cloud agent to run one of those, it either can't see the tool at all or fails. I want the cloud agent to instead **enqueue the request to a durable queue**, have my laptop **claim and run the real agent locally to completion**, light up whatever **local UI** I already have so I can watch it, and **ping me back** (on the phone/cloud surface) when it's done — including when my laptop was asleep at request time and only wakes up later. Think "remote control for the agents that can't leave home."

## Step 1 — Interview me before coding

Ask me the questions in each block below, **wait for my answers**, then summarize back what you heard before moving on. If I'm vague, follow up. If something I describe doesn't exist yet, flag it before going further — you may need to scaffold a thin version first.

### A. Topology — is there actually a cloud/local split?

1. Do I run my agent in **two places** today — an always-on remote/cloud process AND a local/laptop process — or just one? If just one, this feature may not apply; help me decide whether I actually need it before we build anything. (If I only have a laptop, there's nothing to route *from*.)
2. What does the cloud process do today (autonomous jobs, a phone/PWA chat surface, both)? What does the laptop process do (serves my main UI, runs interactive sessions)?
3. Do both processes share a **database**? Routing needs a shared, durable store both sides can reach. If they don't share one, that's the first thing to fix.
4. What identifies "this laptop" as the intended worker? (A `worker_role` string like `laptop_primary`, a hostname, a user id.) Single-user or multi-tenant?

### B. Which agents are laptop-only — and why

1. List my sub-agents. For each, tell me: does it run fine in the cloud, or is it **laptop-bound**? What specifically binds it locally — filesystem writes, a local CLI/subprocess, local-only credentials, a desktop app?
2. For each laptop-only agent, **how is it invoked today on the laptop** — a tool exposed to a parent agent (`dispatch_to_<name>`), a direct call, a job? The cloud proxy we build must mirror that tool's *name and input schema* exactly so the cloud LLM calls it without knowing the difference.
3. Does any laptop-only agent **pause for human approval** partway through (e.g. it proposes something and waits for me to click approve)? If so, where does that approval happen — and is that surface on the laptop, the cloud, or both? (This shapes what "done" means for the round-trip.)
4. When one of these agents runs locally today, does it already emit **progress/lifecycle events** to my local UI? Name them. We'll reuse those for in-flight visibility instead of inventing new ones.

### C. The queue / transport

1. Do I already have a **durable task queue** both processes can use (a DB table, Redis, SQS, a job runner)? If yes, show me its enqueue/claim/complete API. If no, we'll build a minimal one on the shared DB.
2. How do workers **wake up** for new work — a push mechanism (Postgres `LISTEN/NOTIFY`, Redis pub/sub, a webhook) or polling? Either is fine; the queue must also **drain on startup** so work queued while the laptop slept gets picked up on wake.
3. How are **results** delivered back to the requester today (a results channel, a callback, polling)? The cloud needs to learn when the laptop finished so it can ping me.

### D. The remote surface (where I talk to the cloud, and get pinged)

1. Where do I talk to the cloud agent — a PWA, SMS, a chat client, voice? That's where the "on it / queued" acknowledgement and the later "it's done" ping land.
2. Do I want the completion ping to be a **full result** or a short **notification** ("Finished — created X, approve it on your desktop")? Most laptop-only agents do their real work on the laptop, so a short ping + "go look at your desktop" is usually right. Tell me your preference.
3. If my laptop is **offline** when I dispatch, what should the cloud say? Options: (a) always queue and say "I'll start it when your computer's online" (recommended — the queue runs it on wake); (b) queue silently and say "on it"; (c) refuse. Pick one.

### E. Failure modes I want to avoid

1. **Double execution.** If the cloud proxy and the real local tool ever both run for one request, I get duplicate work. Confirm: the proxy is registered *only* where the real tool is absent (the cloud), and the real tool runs *only* locally. List which process registers which, so we can prove they never overlap.
2. **Silent failure.** Laptop-only agents that catch their own errors and "finish" normally will report success even when they failed. How do I want failures surfaced in the completion ping? (At minimum: the worker must report a failure *status*, and the ping must distinguish it from success.)
3. **Lost work on a sleeping laptop.** Confirm the queue persists pending work and the worker drains it on startup — not just on a live push that the sleeping laptop missed.
4. **Cross-tenant / cross-user leakage** (if multi-tenant). The result ping and any local UI push must be scoped to the right user. If single-user, say so and we skip the guard (but leave the payload shaped so it can be added later).

After all five blocks, **echo back a short plan** — topology, which agents are laptop-only and how they're invoked, the queue/transport, the remote surface + ping style, offline behavior, and the no-double-fire proof — and ask me to confirm or adjust before you move on.

## The design principles (the "why" behind every tier)

- **The queue is the contract, and it must be durable.** Routing across machines means the requester and the runner are never guaranteed to be online at the same time. The only thing that survives that gap is a persisted row. Push notifications are an optimization on top; the source of truth is the queue, and the worker must drain it on startup.
- **Run the real agent, not a reimplementation.** The laptop already has the working local agent. The worker's job is to invoke *that*, in the laptop process, so it inherits the real behavior, the real local UI events, and the real credentials. Don't fork the agent's logic into the worker.
- **Run to completion so "done" means done.** Many local agents are fire-and-forget locally (they return a "started" ack immediately). For a *remote* round-trip, the worker must **await the real end** of the run, so the completion ping reflects what actually happened — not just that it kicked off.
- **The proxy is a doppelgänger, not a new tool.** The cloud proxy must have the *same name and input schema* as the real local tool, so the cloud LLM picks it up naturally with zero prompt changes. The only difference is the body: enqueue instead of execute.
- **Honesty over optimism.** If the laptop is offline, say so. If the run failed, say so. A cheerful "done!" that hides a failure or a never-started job is worse than a blunt "queued — will run when your computer's online."
- **No double-fire, by construction.** Register the proxy only in the process where the real tool is absent. Then the two can never both run — not by convention, but because only one exists per process.

## Step 2 — Build it tier by tier

Adapt these to my stack; don't follow them literally if my context calls for something different. Each tier is independently shippable with verification. If I already have a tier (e.g. a queue), adapt to it instead of rebuilding.

### Tier 1 — A durable cross-machine task queue

**Goal:** a persisted queue both processes reach, with atomic claim and drain-on-startup. Skip/adapt if I already have one (Block C.1).

**Minimal table:**

```text
remote_tasks
────────────
id              (uuid)
worker_role     (text)        -- which worker should run this, e.g. "laptop_primary"
kind            (text)        -- task type (Tier 2 adds "remote_agent_dispatch")
payload         (json/text)
status          (enum: pending | claimed | completed | failed)
result          (json, nullable)
error_message   (text, nullable)
claimed_by      (text, nullable)
created_at, claimed_at, completed_at
```

**Repo API** (adapt to your DB/ORM):

```text
enqueue(worker_role, kind, payload) -> task_id     # insert pending + notify
claim(worker_role, claimed_by)      -> task | None  # atomically grab oldest pending
complete(task_id, result)                           # status=completed + notify results
fail(task_id, error)                                # status=failed + notify results
```

**The atomic claim is the load-bearing detail.** On Postgres, use `SELECT ... FOR UPDATE SKIP LOCKED LIMIT 1` inside the claim so a notification storm (or two workers) can never grab the same row twice. On other stores, use the equivalent (a conditional update returning the claimed row; Redis `BRPOPLPUSH`; SQS visibility timeout).

**The worker loop** (laptop side): on startup, **drain all pending** for my `worker_role`, *then* subscribe to the wake signal (LISTEN/NOTIFY, pub/sub, or a poll tick). The wake handler just calls drain again. Draining is "claim until none left." This is what makes a request survive the laptop being asleep at enqueue time.

**Verification:**

```bash
# Enqueue a no-op task from a REPL/CLI, kill the worker, restart it.
# Expect: the worker claims and completes the task ON STARTUP, before any
# new notification — proving drain-on-startup works.
$ <repo CLI> enqueue --role laptop_primary --kind noop --payload '{}'
$ <restart the worker>          # it should drain the pending noop immediately
$ <repo CLI> show-task <id>     # → status: completed
```

### Tier 2 — A generic remote-dispatch kind + per-agent runner registry

**Goal:** one task kind, `remote_agent_dispatch`, carrying `{agent, args}`, that the worker routes to the right local agent and **runs to completion**.

**On the worker**, add a branch to the task handler:

```text
if kind == "remote_agent_dispatch":
    payload = parse(task.payload)            # {"agent": "...", "args": {...}}
    runner  = REMOTE_RUNNERS.get(payload.agent)
    if runner is None:        return error_summary("no runner for agent")
    if deps_missing:          return error_summary("not configured here")
    summary = await runner(payload.args, deps)   # AWAIT the real agent to its end
    return summary                                # -> stored as the task result
```

`REMOTE_RUNNERS` is a small `{agent_name -> async runner}` map. Each runner runs the **real local agent** to completion and returns a summary dict `{status, ...}` for the result row. Start with one entry for the agent I named in Block B; others are one line each later.

**The run-to-completion subtlety (read this twice).** If my local agent is normally fire-and-forget — its dispatch returns a "started" ack immediately while the real work runs in the background — then naively calling that dispatch will make the task "complete" instantly, before the agent finishes, and my ping will be a lie. The runner must `await` the *actual* end of the run (the underlying pipeline/coroutine), not the fire-and-forget wrapper. If the local agent ends in a human-approval pause, "completion" means "reached the approval point"; read the agent's persisted state to build the summary.

**Inject the worker's dependencies.** The worker is often a minimal process. To run the real local agent it needs the same dependencies the local app wires up — repos, the local event emitter, settings. Pass a small `deps` bundle into the worker at construction (from the local app's startup, where those already exist). If deps are absent, the runner returns an error summary rather than crashing.

**Verification:**

```bash
# Enqueue a remote_agent_dispatch for your laptop-only agent with real args.
$ <repo CLI> enqueue --role laptop_primary --kind remote_agent_dispatch \
    --payload '{"agent":"<your-agent>","args":{...}}'
# Expect: the REAL local agent runs (watch its local logs/UI), the task
# reaches `completed`, and result holds a status summary.
$ <repo CLI> show-task <id>     # → status: completed, result: {status: ...}
# Unknown agent → completes with {status:"error", ...}, not a crash.
```

### Tier 3 — The cloud proxy tool (doppelgänger of the local tool)

**Goal:** on the cloud, expose a tool with the **same name + input schema** as the real local tool, but whose body **enqueues** a `remote_agent_dispatch` task instead of executing.

```text
def make_remote_dispatch_tool(agent, definition, queue, presence, worker_role):
    class Proxy(Tool):
        def definition(self):  return definition   # SAME name + schema as the local tool
        async def execute(self, args):
            online  = await presence.is_online(worker_role)   # Tier 4
            task_id = await queue.enqueue(worker_role, "remote_agent_dispatch",
                                          {"agent": agent, "args": args})
            spoken  = ("On it — starting on your computer." if online
                       else "Queued — it'll start when your computer's online.")
            return json({"spoken": spoken, "task_id": task_id})
    return Proxy()
```

**Register it ONLY where the real tool is absent.** On the cloud, the real laptop-only tool isn't registered (it can't run there). So: after building the cloud tool registry, `if "<tool_name>" not in registry: register(proxy)`. On the laptop, the real tool is present and the proxy is never registered. This is the no-double-fire guarantee from Principle 6 — enforced by *which process has which tool*, not by a flag. Confirm both registrations against my Block E.1 answer.

**Why same name + schema matters:** the cloud LLM already knows how to call `dispatch_to_<agent>` (or whatever yours is named). If the proxy matches, you change *zero* prompts — the model can't tell it's talking to a proxy. Drift the name or schema and you'll be editing system prompts forever.

**Verification:**

```bash
# In the cloud process, confirm the proxy registered and the real tool didn't.
# Then call it and confirm it ENQUEUES rather than runs.
$ <cloud REPL> call dispatch_to_<agent> {...}
# → returns {spoken, task_id}; a row appears in remote_tasks (pending);
#   NOTHING ran in the cloud process.
```

### Tier 4 — Presence heartbeat + honest acknowledgements

**Goal:** let the cloud know whether the laptop is online *right now*, so it words the ack honestly (Block D.3). Advisory only — work always queues regardless.

**Tiny table + repo:**

```text
worker_presence(worker_role PK, claimed_by, last_seen)
heartbeat(worker_role, claimed_by)  -> upsert last_seen = now
is_online(worker_role, max_age_s)   -> now - last_seen < max_age_s
```

The laptop process runs a small loop that upserts a heartbeat every ~30s. The cloud proxy calls `is_online(worker_role, max_age_s≈90)` to choose wording. **Never gate work on this** — if the presence check errors or is stale, default to "offline" wording and still enqueue. A stale read costs you a slightly-off sentence, never lost work.

**Verification:**

```bash
$ <start the laptop process>; sleep 35
$ <cloud REPL> is_online laptop_primary   # → true
$ <stop the laptop process>; sleep 120
$ <cloud REPL> is_online laptop_primary   # → false
# Dispatch while "false": proxy still enqueues; ack uses offline wording.
```

### Tier 5 — In-flight visibility on the local UI

**Goal:** while the laptop-only agent runs (via the worker), my local UI shows it — ideally with **zero new UI code**.

**The clean path:** because the worker runs the real agent *in the same process as my local UI server* (or with the same local event emitter injected — Tier 2 deps), the agent's **existing** progress/lifecycle events (Block B.4) fire exactly as they do for a locally-initiated run. The local UI lights up natively: the same "started" indicator, the same progress, the same approval card, the same "new thing created" tab. Confirm the worker's injected event emitter is the **local** one (pushes to the local UI), not a remote/phone one.

**If the worker is a separate process** from the local UI: bridge its lifecycle events to the UI over your existing local push channel (e.g. the worker writes a row + notifies; the UI process listens and pushes to its socket). Keep these notifications **best-effort** — a UI that isn't open must never break or delay the agent run.

**Verification:**

```bash
# With the local UI open, dispatch from the cloud surface.
# Expect: the local UI shows the agent working through its normal stages,
# including any approval step — identical to a locally-initiated run.
```

### Tier 6 — Completion ping back to the remote surface

**Goal:** when the worker finishes (or fails), tell the cloud, and the cloud pings my remote surface (Block D.2).

When `complete()`/`fail()` fire the results signal, a cloud-side handler reads the finished task and — if `kind == "remote_agent_dispatch"` — pushes a short message to my remote surface (phone/PWA/chat). Derive the line from the result summary:

```text
if status in ("error","failed") or error_message:  "X ran into trouble on your computer."
elif status == "awaiting_approval":                "X finished — created <name>. Approve it on your desktop."
else:                                              "X finished on your computer."
```

**Two integration traps:**

- **One handler per channel.** If your results channel only supports a single subscriber and something already listens there, *merge* — wrap both into one handler that calls each, each filtering by `kind`. Registering a second subscriber may silently clobber the first. Check your pub/sub's semantics before assuming you can just add a listener.
- **Surface the failure honestly.** If the local agent persists a `failed` status and returns normally (common!), the completion handler must treat that status as failure — not just an exception or an `error` string. Map *all* terminal-failure shapes to the "trouble" line, or you'll cheerfully report failures as successes (Block E.2).

**Verification:**

```bash
# Happy path: dispatch from the cloud surface; on completion the surface
# receives a "finished" ping with the right name.
# Failure path: force the local agent to fail; confirm the ping says
# "ran into trouble", not "finished".
# Offline path: dispatch with the laptop asleep; wake it; confirm it runs
# on startup-drain and the ping still arrives.
```

## How the tiers fit together

```
  Phone / PWA / chat  ──talk──▶  CLOUD agent
                                    │ calls dispatch_to_<agent> (PROXY, Tier 3)
                                    ▼
                          enqueue {agent, args}  (Tier 1 queue)
                          ack worded by presence (Tier 4)
                                    │  (durable row; survives sleep)
                                    ▼
        LAPTOP worker ── claim + drain-on-startup (Tier 1) ──▶ remote_agent_dispatch (Tier 2)
                                    │ runs the REAL local agent to completion
                                    ├─▶ local UI lights up natively (Tier 5)
                                    ▼
                          complete(result)  ──▶ results signal
                                    │
                                    ▼
                          CLOUD handler ──ping──▶ Phone / PWA / chat  (Tier 6)
```

Ship Tier 1 first and prove drain-on-startup — everything else rides on the durable queue. Tiers 2–3 give you the end-to-end happy path (dispatch from cloud → runs locally). Tier 4 makes the ack honest, Tier 5 gives you eyes on it, Tier 6 closes the loop.

## Stumbling blocks to avoid

- **"Started" is not "done."** The single most common bug: the runner awaits a fire-and-forget wrapper that returns immediately, the task completes instantly, and the ping fires before the agent has done anything. Await the *real* end of the run.
- **The proxy drifting from the real tool.** Same name, same input schema, always. The moment they diverge, the cloud LLM either can't call it or calls it wrong, and you start patching prompts. Derive the proxy's definition from the same source as the real tool's definition so they can't drift.
- **Registering the proxy everywhere.** If the proxy registers on the laptop too (where the real tool lives), you get double execution. Gate it on "real tool absent."
- **Trusting the push.** A push that fires while the laptop is asleep is gone. Drain-on-startup is not optional; it's the whole reason the queue is durable.
- **Reporting failure as success.** Local agents that swallow their own errors and return normally will lie to your completion handler unless it inspects the persisted *status*. Map every terminal-failure shape to a failure ping.
- **Clobbering an existing results subscriber.** If your channel is one-handler-per-channel, merge handlers instead of adding a second one. Verify the semantics.
- **Gating work on presence.** Presence is for *wording only*. If you refuse to enqueue when the laptop looks offline, you lose the best property of the whole design — queue now, run on wake.
- **Letting one agent spawn an unbounded chain.** If a routed agent can itself dispatch more remote agents, bound it (a depth/origin guard) so a single human request can't fan out across machines without a human in the loop.

## Final reminder

Do not start coding until the interview is complete and you've echoed my answers back as a plan I've confirmed — especially the topology (Block A) and which agents are genuinely laptop-only (Block B). If I turn out to have no cloud/local split, or no laptop-only agents, tell me this feature may not be worth building rather than constructing a bridge to nowhere.

Then build the smallest end-to-end slice first: Tier 1's durable queue (prove drain-on-startup), then one runner (Tier 2) and one proxy (Tier 3) for a single laptop-only agent — dispatch from the cloud, watch it run locally, see it complete. Expand to honesty (Tier 4), visibility (Tier 5), and the ping (Tier 6) from there.

Ready when you are. Start with the interview.
