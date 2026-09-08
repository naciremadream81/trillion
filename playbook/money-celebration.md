# Build a Revenue Celebration Into Your Stripe-Connected Agent

Copy everything below the line into your coding agent (Claude Code, Cursor, Aider, or similar) while it's pointed at the codebase for the agent or app you want to add this to. It will interview you about your setup first, then build the feature in independent tiers, verifying each one before moving on.

The goal: when real money comes in through Stripe, your assistant *notices and reacts* — a celebration animation sized to the payment, a sound, and a visible "the assistant is reacting" moment — and it never misses one, even if you weren't looking at the screen when it happened.

---

You are going to help me add a **revenue celebration** to my assistant. When a customer successfully pays through Stripe, my interface should come alive: a celebratory animation scaled to the size of the payment, a sound, and — if my assistant has any kind of avatar, orb, character, or audio visualizer — that element should react to the sound as if the assistant is celebrating the win with me. The feeling I'm after is delight and momentum: a small, genuine "cha-ching" moment that makes revenue feel real.

Do **not** start writing code yet. Work through this in phases. Treat each phase as something you build, verify, and confirm with me before starting the next. If you discover my architecture makes a suggested approach a poor fit, say so and propose the better fit rather than forcing it.

## Phase 0 — Interview me first

Before designing anything, ask me about my setup so your plan fits reality. At minimum, find out:

- **Where payments are detected today.** Do I already receive Stripe webhooks, or do I poll the Stripe API? Is there a backend service that's always running, or does my backend only run when the app is open? This matters enormously — see the "nobody was watching" problem below.

- **How my interface gets real-time updates.** Do I have a live channel between backend and front end (WebSocket, server-sent events, a subscription, polling)? Is the part of my system that detects the payment the *same* process that's connected to the user's screen, or a different one? If they're different, how does a message get from one to the other?

- **What my front end is.** A web app, a desktop app, a mobile app, a terminal UI? What can it realistically render — a full animation, or something simpler?

- **Whether my assistant has a visual "presence."** An avatar, an orb, a character, a waveform, an audio visualizer — anything that could visibly react to sound. If it has a "speaking" or "talking" animation already, how is that triggered, and is it driven by real audio amplitude or just toggled on and off?

- **What I want the celebration to feel like.** Subtle or loud? A sound I provide, or something generated? Should the assistant's voice/presence react, or just play a sound and an animation?

- **Where I store state.** Is there a database or persistent store the backend writes to, and can the front end read recent history from it?

Summarize my answers back to me and propose a tiered plan before building. Confirm the plan with me.

## Phase 1 — Reliable payment detection

Build the part that knows a real payment happened. Principles:

- **Prefer webhooks if I have them; fall back to short-window polling if I don't.** If polling, only look back a few minutes each cycle — this is a celebratory "just happened" signal, not a historical reconciliation job. Make the lookback comfortably longer than the poll interval so nothing slips through a gap.

- **Filter aggressively to "real money in."** Only successful, captured charges. Exclude zero-amount charges, refunds, and failed or pending attempts. Be aware that some Stripe list endpoints don't honor a status filter the way you'd expect, so re-check the status on each record yourself rather than trusting the query.

- **Give every payment a stable identity.** Use the Stripe charge (or payment-intent) ID as a unique key. You'll lean on it for deduplication in several places.

- **Deduplicate at the source.** The same payment should produce exactly one celebration event, even across restarts, retries, or overlapping polls. Persist a record of which payment IDs you've already handled, with a time window, so a restart doesn't re-fire old ones.

- Capture the bits the celebration will want: amount, currency, customer label, and the timestamp.

**Verify:** confirm a real (or test-mode) successful charge produces exactly one detected event with the right amount, and that failures, refunds, and repeats produce none.

## Phase 2 — Get the signal to the screen, and solve the "nobody was watching" problem

This is the phase everyone underestimates. The naive version — "when a payment is detected, push a message to the connected client" — works only if a client is connected *at that exact instant*. In the real world the screen is often closed, asleep, or on another tab. If your only trigger is the live push, those payments are **silently never celebrated**, which feels random and broken. That single gap is the most common reason these features feel "unstable."

Build it in two halves:

- **Live delivery.** When a payment is detected, deliver it to any currently-connected interface over my real-time channel so it can celebrate immediately. If the detecting process is *different* from the one holding the user's connection, make sure there's a reliable hand-off between them (a shared store plus a notification, a message bus, whatever fits my stack) — and recognize that the hand-off still only reaches a client that's connected right now.

- **Catch-up on (re)connect.** This is the fix. Whenever an interface connects or reconnects, have it ask for recent payments (say, the last 24 hours) and replay celebrations for any it hasn't already shown. Back this with a **durable record of which payments have already been celebrated** (stored per-client is fine) so:

  - a payment is never celebrated twice,

  - one the user already saw live is never replayed,

  - and a payment that arrived while they were away finally gets its moment the next time they open the screen.

  Live celebrations should also write into that "already celebrated" record, so the live path and the catch-up path stay in agreement.

Decide with me how a burst of missed payments should replay — for example, replay each one but cap how many, and play the *sound* only once for the burst so you don't stack overlapping audio. Note out loud any cap you impose, so "we replayed the 6 most recent and the rest are only in the history" is explicit rather than a silent truncation.

**Verify:** detect a payment with the screen closed, then open the screen and confirm the celebration fires on connect. Confirm a payment seen live is not re-celebrated on the next reload. Confirm repeats never double-fire.

## Phase 3 — The celebration itself

Now make it delightful. Components:

- **Tier the celebration by amount.** Define a few brackets (for example small / medium / large) and scale the intensity — particle count, size, duration, brightness — so a big sale visibly feels bigger than a small one. Keep the thresholds easy to find and adjust.

- **The animation.** Render it as an overlay that doesn't disrupt whatever the user is doing — non-interactive, on top, self-clearing when it finishes. Show the amount ("+$1,300") as part of it so the number itself is the hero.

- **The sound.** Play a sound when it fires. Let me supply my own audio file rather than assuming a generated tone — I may want a specific jingle or effect. Load and decode it once and reuse it. Respect browser/OS autoplay rules: audio often can't play until the user has interacted with the page at least once, so make the *visual* the primary signal and treat sound as an enhancement that may be silent on a cold load until the first interaction. Don't let a blocked or missing sound break the animation.

- **The assistant reacting (if I have a visual presence).** If my assistant has an avatar, orb, character, or visualizer with a "speaking"/active animation, drive that same animation from the celebration sound so it looks like the assistant is reacting to the win. The most convincing way is to feed the sound's real-time amplitude into whatever already animates during speech. **Do this cosmetically** — drive only the visual reaction, and do **not** flip the app into its real "assistant is speaking" state, because that can clear UI, interrupt a real conversation, or confuse other logic. It should look like a reaction without actually hijacking the assistant.

- **Accessibility and control.** Honor a reduced-motion preference by toning the animation way down (or to a quiet minimal version). Provide a user toggle to mute the sound, and ideally one to disable celebrations entirely. Default them on.

**Verify:** trigger celebrations at each tier and confirm the animation scales, the sound plays after an interaction, the assistant's presence reacts in sync, reduced-motion is respected, and the mute toggle works. A manual "fire a test celebration at amount X" trigger is invaluable here — build one for yourself.

## Phase 4 — Polish and the rough edges

- **Don't interrupt or clash.** If the assistant is actively listening or speaking when a payment lands, let the visual celebration play but skip the parts that would fight active audio or input. Decide the right behavior with me.

- **Quiet hours / focus.** If I have any notion of "quiet hours" or do-not-disturb, decide whether a payment celebration should be suppressed, deferred, or always allowed — and make sure that whatever path a deferred payment takes when it's finally released *still* triggers the celebration. (A common bug: held notifications get released through a different code path that forgets to celebrate.)

- **Bursts.** Make sure several payments close together don't stack into overlapping noise or an endless animation queue. One sound per burst is a good default.

- **Never double-celebrate, never miss.** Re-confirm the dedup story end to end: source detection, live delivery, and catch-up all agree on what's been celebrated.

**Verify:** run through the edge cases — payment during active use, a burst of several at once, a reload right after one fires, and (if applicable) a payment during quiet hours — and confirm each behaves sensibly.

## Working principles for the whole build

- **Ship tier by tier.** Each phase should be independently working and verified before the next. Don't build all four and test at the end.

- **Verify with real signals, not assumptions.** Use Stripe test mode and your own manual trigger. Show me proof each tier works rather than telling me it should.

- **The "nobody was watching" gap is the heart of this.** If you build only the live push, it will feel broken in exactly the way that's hardest to debug. The catch-up-on-connect mechanism is what makes it feel reliable. Don't skip it.

- **Keep the reaction cosmetic.** Making the assistant *look* like it's reacting is great; actually putting it into its real speaking/processing state will cause subtle breakage. Keep them separate.

- **Make everything tunable and surfaced.** Tier thresholds, the sound file, the toggles, any caps — keep them obvious and adjustable, and never silently drop or truncate without saying so.

Start by interviewing me about my setup (Phase 0), then propose the tiered plan.
