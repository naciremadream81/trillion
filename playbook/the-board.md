# Build a Board of Advisors for my AI agent

I already have an AI agent that knows things about my business — it can read my revenue, it remembers what I've told it, and it can do work for me. What it can't do is **disagree with me from a position I respect.**

I want you to give it a **board of advisors**: a standing council of well-documented operators I can convene on a real decision. I say "ask the board about X" and I get genuinely different reads from people whose actual published thinking has been researched and written down — not one model wearing five hats and nodding along.

This is a description of the design and the engineering approach, not a code listing. Use your own judgment about the implementation and match my stack, but follow the structural decisions below closely. Almost all of the value is in the specific defenses, and a board without them is worse than no board at all.

**Two failure modes make this worthless if you don't defeat them, and you must design against both from the first line of code:**

**Consensus theater.** One model asked to voice five people produces five hats on one head. Everyone agrees, agreement carries no information, and you've built an expensive way to hear your own opinion in someone else's voice. The defense is *structural, not prompted*: each seat gets its own isolated model call containing exactly one dossier. Not one call that role-plays five people. Not one prompt listing five personalities. Five calls, five contexts, none of them aware the others exist.

**Confident fabrication.** A persona will happily invent a position the real person has never taken. I then act on advice I believe came from a named operator. That is not a bad feature, it is an actively harmful one — it laundered my own assumption back to me wearing a real person's name. The defense is a **citation gate**: every dossier holds numbered doctrine entries, a seat may only cite the ids in its own dossier, and any other citation is stripped server-side before I ever see it.

**Understand the limit of that gate, because everything else rests on it.** The gate can prove a citation *exists in the file*. It can never prove the file is *true*. That is why the research process below has two stages and why the second stage is adversarial.

**Work tier by tier.** Each tier ends with something I can actually run. Don't collapse tiers — when the board agrees with everything or cites something that doesn't exist, we need to know exactly which layer to look at.

---

## Tier 0 — Interview me first (don't write code yet)

Before touching any files, ask me these questions and wait for my answers. Group them so I can answer in one pass. If I skip one, use the default in parentheses — and where the default says "go look," go look at my project rather than guessing.

**About my agent and stack**

1. What's my agent's **name**, and what language/framework is it built in? (Default: go look. Everything below is stack-agnostic — the dossiers are markdown, the rest is whatever my agent already speaks.)

2. How does my agent **call tools** today, and how do I add a new one? (Default: find the existing tool registry or function-calling setup and follow its conventions exactly. Do not introduce a second pattern.)

3. Which **model** should the seats and the chair use, and do I have a **per-call cost ceiling** convention already? (Default: use whatever my agent uses for reasoning. A full meeting is one router call plus one call per seat plus one chair call — that is real money, so build a cost ceiling regardless and tell me the number you chose.)

**About what the board can see — this is what makes it more than a chatbot**

4. What **real numbers** can my agent read about my business right now — revenue, subscriptions, churn, active projects, pipeline? Which tools or queries return them? (Default: go find them. **This is the highest-value question in the interview.** A board that argues about my business without seeing my business is a party trick. The chair must read live figures, every time, and never a number written down in a file.)

5. Should the seats see those numbers too, or only the chair? (Default: **both see a short brief, but only the chair sees everything.** Seats get enough context to be specific; the chair is the one who reconciles them against the full picture. Say which you did and why.)

6. How does my agent **surface something to me** — a notification, an alert row, a card in a UI, an email? (Default: use whatever exists. A meeting that finishes into a log file nobody reads is a meeting that didn't happen.)

**About the roster**

7. Which **advisors** do I want seated? Give me your read on each one's fit for my actual business before you research them. (Default: ask me. Don't pick for me — but *do* tell me plainly if someone I named is a poor fit, and why. A dossier that pretends universal applicability makes the whole board less useful.)

8. What should each seat be **for** — what's the domain where it has standing? (Default: propose a seat title per advisor once you've researched them, and check for overlap. Two seats covering the same ground is fine and often good; five seats covering the same ground is a waste of five calls.)

9. Can my agent **schedule recurring work**? (Default: go look for a scheduler or cron mechanism. If one exists, the standing review in Tier 7 is worth building. If not, skip Tier 7 and say so.)

**About my appetite for the hard parts**

10. Do I want the board to be able to **decline a question**? (Default: **yes, and it should decline often.** A board that answers everything is a board that answers nothing well. But read the warning in Tier 3 first — an over-eager decline gate is the single most annoying failure mode in this entire build.)

11. Am I willing to have seats **abstain**? (Default: **yes.** An advisor saying "I have no doctrine on this" is a real answer and far more valuable than a plausible invention. Abstention must be a first-class outcome, rendered differently from a failure.)

Once I've answered, restate the plan in four or five lines — which seats, what the chair reads, how a meeting reaches me — then read my project **read-only** and report what you actually found before starting Tier 1.

---

## The big picture (read once, then build incrementally)

The mental model: **a dossier is a knowledge base, not a personality.**

The instinct is to write a system prompt that says "you are Alex Hormozi, be direct and talk about offers." That produces an impression of a person. What you want is a **file of numbered, sourced principles** that a model reads and reasons *from*, in that person's voice. The voice is the smallest part; the doctrine is the whole thing.

Each seat is one markdown file:

| Section | What it holds | Why it exists |

|---|---|---|

| **Frontmatter** | id, name, seat title, domains, status | `domains` is how the router decides who has standing |

| **Doctrine** | Numbered entries `D1`, `D2`…), each with a source and a paragraph | The ids are what get cited; the source is what makes it checkable |

| **Characteristic objection** | What this person reliably pushes back on, and their opening questions | This is the anti-sycophancy section — it makes the seat argue rather than agree |

| **Blind spots** | Where this doctrine does *not* transfer, stated plainly | The chair uses this to discount the seat. It must be honest or it's worse than absent |

| **Voice** | Tone only — how they talk | Kept separate from doctrine on purpose, so tone can never substitute for substance |

**Doctrine ids are explicit in the file, not derived from ordering.** This matters more than it looks: reordering entries must never silently invalidate the citations on meetings you've already stored.

**Three architectural rules, decided up front:**

**One — isolation is structural.** One model call per seat, each system prompt containing exactly one dossier. Never one call that plays several parts. If you find yourself writing "you are playing the following five advisors," stop — you have just rebuilt the failure mode this whole design exists to prevent.

**Two — the parser must never take the board down.** A malformed dossier loses *its own seat* and logs why. A meeting must not fail because one file was hand-edited badly. Equally: a dossier that fails to parse must never silently vanish without a warning, because a quorum that quietly shrinks is far worse than a stale one.

**Three — treat every field the model returns as hostile input.** Not because the model is adversarial, but because a schema-declared boolean can come back as the string `"false"`, and in most languages that is truthy. A seat "abstaining" when it didn't means I'm told a named person declined to answer when they did not. Check identity, not truthiness. Coerce every scalar. Drop anything structural where a sentence belonged.

Build the dossier format first (Tier 1), then the research process that fills it (Tier 2), then routing (Tier 3), then the meeting itself (Tier 4), then the chair (Tier 5), then surfacing (Tier 6), then the standing review (Tier 7).

---

## Tier 1 — The dossier format and the citation gate

Write the parser and the gate before you write a single dossier. They define what a valid seat is.

**The parser** reads one markdown file and returns a structured seat, or nothing. It must reject, with a logged reason: a file with no domains (it could never be routed to), a file with no doctrine (there is nothing to cite), and **a file with duplicate doctrine ids** — because a citation to `D3` when two entries claim `D3` is ambiguous, and anti-fabrication machinery that fails open is not machinery.

Read files as UTF-8 tolerantly. A file saved in the wrong encoding should degrade to a logged warning, not an exception that takes out the roster.

**The citation gate** is a pure function: given whatever the model returned as citations and the set of ids that seat was actually shown, return only the valid ones. Uppercase-normalize, de-duplicate, preserve order, discard everything else.

Two details that are easy to get wrong and expensive to miss:

- The valid set is **what the seat was shown**, not everything in the file. When you add retirement in Tier 6 these diverge, and the gap is a seat citing something withheld from it.

- A seat that cites nothing and didn't abstain is **unsourced**. Track that as its own flag and show it to me. It's not an error, but it's the seat talking without support and I should be able to see that at a glance.

**Verify this tier:** hand-write one dossier, parse it, and confirm a fabricated citation gets stripped. Also confirm a dossier with duplicate ids is rejected rather than silently collapsed.

---

## Tier 2 — Research the seats (two stages, and the second one is adversarial)

This is where the board earns trust or fails to. **Do not let the same agent that wrote a dossier be the one that checks it.**

**Stage one — research.** For each advisor, find their actual published thinking: books with chapters, dated talks and posts, named interviews. Every doctrine entry needs a real, checkable source. Prefer primary sources over summaries. **A thin dossier of six verified entries is far better than nine with one invented.**

**Stage two — an independent fact-check that assumes the file is wrong.** A separate pass, without access to the researcher's reasoning, instructed to try to *refute* each entry. For every claim: does the cited source exist, does it actually say this, is it *this person's* idea or one they credited to someone else, and is the framing honest — a narrow finding presented as a general law is a defect even when every word is true.

I am not being theoretical about this. Run against a real roster, that second pass caught:

- a citation to a podcast episode that does not exist as described

- a statistic that was true but presented as a general law when it was a much narrower comparison

- a seat stating a ratio its subject has **publicly disowned**

- a famous framework attributed to the advisor that they had explicitly **credited to someone else** in the very source cited

- a number attributed to an interview that contains no such number — traceable only to content-marketing blogs quoting each other

Every one of those would have been delivered to me as sourced advice in a real person's name.

**Mark each entry's verification state** — `sourced` when it survived the adversarial pass, `user` when I typed it in myself later. Seats may cite my own entries; my knowledge is legitimate input. But the chair must be told, so it can say "this rests on something you wrote yourself" instead of presenting it as documented. Otherwise my own half-remembered impression comes back in three weeks wearing an advisor's name and reads as outside corroboration of a belief I already held.

**Verify this tier:** show me, per entry, what the fact-check confirmed, what it changed, and what it threw out. **What was rejected is as informative as what survived** — I want to see the discipline.

---

## Tier 3 — Routing: who has standing, and should this be a board question at all

Before spending money on a fan-out, one cheap call decides two things: is this a question for the board, and which seats have standing.

Match seats by name and by domain. Two things will bite you:

**Normalize apostrophes and unicode before matching names.** If my agent is voice-driven, a dictated "O'Leary" arrives with a curly apostrophe (U+2019) and a naive comparison against `O'Leary` fails silently — every spoken request for that advisor falls through to a paid router call that then has to guess. Handle the curly apostrophe, the fullwidth variant, non-breaking spaces, and soft hyphens.

**Watch for names that collide with mine.** If an advisor shares a first name with me, a bare mention of that name is ambiguous and must not route. Require the surname.

**Now the warning I promised in the interview.** Getting the decline gate right is harder than it sounds, and erring toward "decline" is *actively bad*.

A real example: a gate written to refuse "personal, medical, legal" questions declined **"Should I cut this product loose?"** as *"a personal business decision about your own company."* That is precisely the question a board exists for. The word "personal" was in the decline list meaning health and family; the model read it as "about you."

Write the decline criteria as concrete examples of what to refuse — code, medical, legal, health, family — and state explicitly that **business decisions about my own company are the core use case**, no matter how personal they feel. Then test it against ten real questions I'd actually ask, and show me the results before moving on.

Cap the number of seats per meeting. Four is a good default: enough for genuine disagreement, few enough to stay affordable and readable.

**Verify this tier:** run ten realistic questions through the router. I want to see which seats each picked and why, and I want at least one legitimate decline and zero illegitimate ones.

---

## Tier 4 — The meeting: fan out, in isolation

For each selected seat, one call. Its system prompt contains that seat's dossier and nothing else. It doesn't know who else is in the room.

Give each seat a structured output: a position, its reasoning, its citations, a confidence, what would change its mind, and whether it abstains.

**Things that will cost you real money if you skip them:**

**Set a generous output limit.** A truncated response means you paid for a meeting and got nothing. If a seat gets cut off mid-JSON, the whole call is wasted — and the chair's limit needs to be *larger* than the seats', because it consumes all of them.

**Budget per call, chair included**, so no single seat can eat the whole ceiling. And make a ceiling of zero actually mean zero — if the cost check only happens *after* a call returns, "spend nothing" spends a full fan-out before anything notices.

**De-duplicate seats before calling.** A router naming one advisor twice must not buy two calls to the same dossier — and must never produce two identical opinions, because the unanimity guard in Tier 5 counts voices and one advisor duplicated would clear the bar by agreeing with himself.

**Let a seat fail without failing the meeting.** Gather results tolerantly. Three good opinions and one timeout is a partial meeting, not a dead one.

**Verify this tier:** ask a question where you'd expect real disagreement and confirm you got some. **If every seat agrees on your first real question, something is wrong with your isolation** — check that each call really received only its own dossier.

---

## Tier 5 — The chair: your agent, and the only one who read the numbers

Your own agent synthesizes. This is the design's other half: the seats are well-read but blind, and the chair is the only participant who has read your actual revenue.

The chair receives every opinion with its citations, the live business brief, and each seat's **blind spots**. Instruct it to:

- **Name the split before the agreement.** Where the board divided is the information; where it agreed is often just the obvious.

- **Discount seats using their documented blind spots.** When a position falls inside one, say so plainly.

- **Treat abstention as abstention**, never as assent.

- **Flag positions resting on unverified entries** as my own assumption rather than that person's documented view.

**Two guards worth building deterministically rather than trusting to a prompt:**

**A unanimity guard.** Fewer than two non-abstaining seats can never be reported as unanimous. One voice is not a consensus, and a model will cheerfully call it one.

**Check that the spoken summary matches the computed verdict.** If your guard sets `unanimous: false`, the sentence read aloud must not say "the board is unanimous." Ask me how I know: because it did, once, and the guard was working perfectly the whole time — the prose just ignored it.

Keep the spoken part short. One or two sentences, leading with the split. The detail belongs on screen.

**Verify this tier:** confirm the chair actually used a blind spot to discount a seat, and confirm the unanimity guard fires when only one seat speaks.

---

## Tier 6 — Surface it, store it, and let me edit the seats

**Store every meeting** — question, seats, opinions, synthesis, cost. When you surface the meeting card, **snapshot each cited entry's title and source into it**. Then a stored meeting renders its own citations without re-reading the dossier, and stays readable after the file changes.

**Surface it the way my agent already surfaces things.** Don't invent a new channel.

**Then make the seats editable**, because a dossier I can't correct is a dossier I'll stop trusting. Two rules, both learned expensively:

**Retire, never delete.** Deleting a doctrine entry frees its id for reuse, and a later entry inheriting `D3` silently re-points every stored citation at *different content*. That is a wrong attribution, not a broken link — far worse. Retiring keeps the id spoken for forever.

**Never let me edit a verification state.** The server decides it. If I change an entry's substance, it drops to `user` automatically, because the fact-check no longer covers what it now says. Otherwise the editor becomes a way to stamp an unchecked claim as confirmed — the exact laundering this design exists to prevent.

If you store edits in a database while the files stay as shipped defaults, **check that every surface reads the edited version.** If your agent runs in more than one place — a laptop and a server, say — an edit that reaches one and not the other means your board quietly disagrees with itself, and nothing errors.

---

## Tier 7 — The standing review (only if my agent can schedule work)

Everything so far waits for me to ask. The most valuable thing a real board does is show up **when you didn't call the meeting**.

Once a month, convene with no question on the table but the business itself. The prompt that makes this work:

> *You did not call this meeting, so there is no question but the business. Read the numbers. What would you put on the agenda this month that they are not already looking at? Name the specific number that moves you, say what it implies, and give one concrete thing to do in the next 30 days. If the numbers genuinely warrant nothing, say so plainly rather than manufacturing a concern.* *Do not ask what they want to discuss — this is your agenda, not theirs.***

That last line is load-bearing. Without it the likeliest failure is a board that politely hands the question back, which defeats the entire routine.

Route it into whatever channel actually interrupts me, and mark it as unprompted so I can tell it apart from an answer I asked for.

**If my agent runs in more than one place, be careful which one owns this.** A scheduled job needs the always-on surface, not the one that's asleep at 8am. And verify it end to end rather than reasoning about it — this is the single easiest thing in the build to get "working by accident," where it runs only because of a coincidence that the next cleanup silently removes.

---

## What to hand me at the end

- A one-line command or phrase that convenes the board

- The dossiers, so I can read what my advisors actually believe

- The fact-check report per seat: confirmed, corrected, rejected

- The cost of one full meeting, measured not estimated

- An honest list of what you deferred and why

**And tell me plainly where you think the board is weak.** Which seat is thinnest, which one's blind spots most limit it for my business, and which of my questions it will handle badly. A board that claims to be good at everything is the first version of the failure this whole design exists to prevent.
