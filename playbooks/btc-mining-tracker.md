# Bitcoin Mining Tracker — AI Agent Prompt

A drop-in prompt for your AI coding agent (Claude Code, Cursor, Aider, or similar) that helps it add a Bitcoin mining tracking feature to your project. It encodes a battle-tested architecture and — critically — instructs the agent to **interview you first** so the implementation matches your pool, your hosting setup, and your dashboard.

**How to use it:** copy everything below the horizontal rule into a fresh chat with your AI agent. Answer the interview questions when it asks, then let it build.

---

You are helping me add a Bitcoin mining tracking feature to my project. Treat this whole message as your brief. **Do not write any code yet.** Your first job is to interview me, echo my answers back as a short plan, and only then start building.

## The feature, in one paragraph

I want a small widget in my app that shows the live state of my Bitcoin mining operation: how much hashrate I'm producing right now, how many of my workers are online, what I've earned but not yet been paid, what my last payout was, and roughly what I'm netting per day. It should poll my mining pool's public API on a schedule, store enough history to draw simple trend lines, and surface alerts when something is off (a worker drops, hashrate falls off a cliff, a payout lands). Think "the Bitcoin mining version of a Stripe revenue widget."

## Step 1 — Interview me before coding

Ask me the questions in each block below, **wait for my answers**, then summarize back what you heard before moving on. If I'm vague, follow up. If a pool I name doesn't have a public API, flag it before going further.

### A. Mining setup

1. Which mining pool am I on? (e.g. one of the major public pools, a regional pool, solo via a public-pool relay, or something else)
2. Does the pool publish a public API? If I have a docs URL, I'll share it. If I don't, ask me to find one before you proceed.
3. Is the API keyless (looked up by wallet address) or does it require an API key, OAuth, or a logged-in session?
4. Roughly how big is my operation? Total hashrate range, number of workers, single-rig vs multi-rig vs farm.

### B. Hosting & economics

1. Where is the gear physically hosted? (home, colocated in a data center, a hosted-mining facility, off-grid, solar + battery, hybrid)
2. What's my power source and approximate cost per kWh? Or, if I'm at a hosted facility, what's the all-in fee per TH/month or per kWh?
3. Any unusual constraints worth knowing about — winter-only operation, immersion cooling, hydro power, heat reuse, curtailment contracts, time-of-use rates?
4. Do I want the tracker to compute **net** profit (revenue minus power/hosting), or just gross revenue from the pool?

### C. KPIs & alerts

1. Which metrics matter most to me? (hashrate, worker uptime, unpaid balance, payouts, $/day, efficiency in J/TH, something else)
2. Which conditions should trigger an alert? (a worker going offline, hashrate dropping below a baseline, a payout arriving, data going stale, pool fee changes)
3. How do I want to be notified? (in-app banner, browser push, email, voice agent, Slack/Discord webhook, a combination)

### D. App context

1. Am I adding this to an **existing** app, or building a **standalone** tracker? Route the rest of your questions accordingly.
2. **If existing:** what's the stack — frontend framework, backend language, database? Where on the dashboard should the widget live, and is there an existing pattern (a card, a sidebar tile, a status pill) I should match?
3. **If standalone:** do I have a stack preference, or should you recommend one? Any deployment target (local, VPS, serverless, Raspberry Pi)?
4. Dark mode? Mobile responsive? Single-user or multi-user with auth?

After all four blocks, **echo back a short plan** — pool, scale, hosting model, KPIs, alerts, app target — and ask me to confirm or adjust before you move on.

## Step 2 — Reference architecture

Once I've confirmed the plan, build along these lines. Adapt them to my stack; don't follow them literally if my context calls for something different.

### Data sources

- **The pool API** is the source of truth for hashrate, workers, unpaid balance, and payouts. Read its docs (or ask me for a sample response) before you write any parsing code — do **not** invent field names or endpoint shapes.
- **A public BTC/USD price feed** (any reputable, keyless one will do) for converting BTC amounts to fiat. Treat the price feed as cacheable with a short TTL (around 60 seconds is plenty) and an in-memory lock to avoid stampedes.

### Two polling cadences

- **Fast tick (~60 seconds)** for live state: hashrate windows, worker presence, unpaid balance, last share timestamp.
- **Slow tick (~5 minutes)** for payouts. Payouts arrive on a much slower cadence and the relevant endpoints tend to be the flakiest of the bunch — isolating them keeps live data snappy when the payout endpoint hiccups.

Run the two ticks independently. A failure on one must not block the other.

### Idempotent payout ingestion

Many pools don't give you a "give me payouts since timestamp X" cursor. Don't fight it — every slow tick, re-poll a sliding window (the last 7 days is a good default) and dedupe at the storage layer using a uniqueness key like `(wallet, paid_at, amount)`. Overlap is free; missed payouts are not.

### Storage shape

Four conceptual tables. Field lists are illustrative; pick names that match your stack's conventions.

- **wallets** — the payout addresses you're tracking. Fields: id, wallet_address (unique), label, optional monthly_cost_usd for net-profit math, created_at.
- **wallet_snapshots** — one row per wallet per fast tick. Fields: wallet_id, multiple hashrate windows (e.g. 60s, 10m, 3h, 24h), unpaid_balance_btc, estimated_next_block_btc, last_share_at, active_worker_count, btc_usd_at_snapshot, captured_at.
- **worker_snapshots** — one row per worker per fast tick. Fields: wallet_id, worker_name, short-window hashrate, long-window hashrate, status (online/degraded/offline), captured_at.
- **payouts** — append-only, idempotent. Fields: wallet_id, paid_at, amount_btc, amount_usd_at_payout, on_chain_txid, lightning_txid (if applicable), with the uniqueness key above.

### Worker status inference

Most pools don't expose a per-worker "last share" timestamp. Derive status from hashrate windows:

- **Online** — short-window hashrate > 0.
- **Degraded** — short-window is 0 but a longer window is still > 0 (the rig was hashing recently, just stopped).
- **Offline** — both windows are 0.

Document this rule wherever the inference happens so future-you (and I) don't get confused when a worker shows "degraded" five minutes after I unplug it.

### Unit handling

Pool APIs commonly return raw H/s. Convert to TH/s (or PH/s for big farms) at the API boundary, and **bake the unit into your column names** (e.g. `hashrate_60s_ths`). This makes unit drift impossible later — a column named `hashrate_ths` cannot quietly become H/s.

### Error tolerance

A per-wallet failure must not crash the whole poller. Catch and log per wallet, return successful wallets, retry the failed ones on the next tick. Surface staleness to the UI honestly — never paper over old data with a stale number that looks fresh.

## Step 3 — UI pattern: pill + expansion panel

This pattern works well and has nice ergonomics. Describe it to me and I'll either approve it or tell you what I want instead.

- **A compact pill** lives in a corner of the app and shows the headline metric (current hashrate) plus a small row of color-coded dots — one per worker (green = online, amber = degraded, red = offline). At rest it's tiny and ignorable.
- **Click the pill → an expansion panel** slides in above it with the full breakdown: per-worker rows, unpaid balance in BTC and USD, today's net and gross, last payout (amount + how long ago), and a freshness indicator ("live", "3m ago", etc.).
- **Freshness cue** — when data is older than a threshold (5 minutes is a reasonable default), turn the pill's border red. Never silently show stale numbers.
- **Auto-open hook** — if my app has an AI assistant or any agent that reads mining state on my behalf, fire an event that auto-opens the panel when those tools run, then auto-close it after a few seconds. The visual should match the spoken/written answer.
- **Why this pattern** — minimal footprint at rest, rich detail on demand, and the same shape generalizes to other "metric pills" (revenue, alerts, signups) so my dashboard stays consistent.

## Step 4 — Voice / agent integration (optional)

Skip this section if my app has no AI assistant. If it does:

- Expose three read-only tools to the assistant: **current status**, **recent payouts in a window** (default 7 days, max ~90), and **bucketed history** (24h / 7d / 30d) for trend questions.
- Tools should return short, voice-friendly summaries — not raw JSON dumps. ("You're at 734 TH/s with 3 of 4 workers online. Unpaid balance is about $103.") The dashboard widget already shows the numbers; the assistant doesn't need to repeat them.
- Wire the auto-open UI hook described above to these tool calls so the visual confirms what the assistant just said.

## Step 5 — Alerting

Four small checkers, each a pure function over the latest snapshot plus recent history. Each needs a dedup key so the same condition doesn't fire repeatedly.

- **Worker offline** — short-window hashrate transitions to 0. Dedup by (worker_name, "offline").
- **Worker recovered** — offline → online transition. Dedup by (worker_name, "recovered", offline_started_at).
- **Hashrate drop** — current 3h window falls below ~75% of the 24h rolling average. Dedup once per hour at most.
- **Payout received** — a new payout row appears in storage. Dedup by the payout's uniqueness key.

Keep the checkers nonblocking; one slow notification channel must not stall the next tick.

## Step 6 — Privacy & safety guardrails

These aren't optional, even if I don't ask:

- The **wallet address is public information but personal data**. Don't log it to third-party services without my consent, don't commit it to a public repo as a default, and prefer reading it from an environment variable or local config file. Use a placeholder in any example code.
- **Don't fabricate the pool's API.** If you don't have docs or a sample response, ask me for one. Made-up field names will silently break in production.
- **Never put API keys in client-side code.** If the pool requires auth, the credential lives on my server and the browser talks to my server, not the pool.
- **Public price feeds are rate-limited.** Cache aggressively, share the cached value across requests, and degrade gracefully when the feed is unreachable (return the last known price with a "stale" flag rather than throwing).
- Don't store secrets in source. Use the project's existing secret-management pattern; ask me what it is if it's not obvious.

## Step 7 — Suggested build order

Once the interview is done and the plan is confirmed, build in this order. Ship a working vertical slice as early as possible, then expand.

1. **Confirm the API contract.** Hit the pool API once with my wallet address and paste the actual response shapes into the plan. Don't proceed if anything is unclear.
2. **Data model + storage migrations.** The four tables above (or your stack's equivalent).
3. **The fast-tick poller** for hashrate, workers, and unpaid balance — single wallet, write to storage, no UI yet.
4. **The slow-tick poller** for payouts, with the idempotent sliding window.
5. **A backend endpoint** that aggregates a snapshot for the widget (current hashrate, workers, unpaid, today's totals, last payout, freshness).
6. **The pill + panel UI**, polling that endpoint every ~30 seconds.
7. **Alerting** — wire the four checkers into whichever notification channel I picked.
8. **Voice/agent tools** (only if applicable to my app).

## Step 8 — Final reminder

Do not start coding until the interview is complete and you've echoed my answers back as a plan I've confirmed. Then build the smallest possible end-to-end slice first — one wallet, one metric, hardcoded refresh — and expand from there. If you hit something the pool's API doesn't expose (per-worker timestamps, payout cursors, fee history), tell me before working around it; the workaround often shapes the architecture.

Ready when you are. Start with the interview.
