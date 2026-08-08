# Make Your Voice AI Feel Human

A guided prompt for smoothing out a voice assistant: cutting the lag between you finishing a sentence and it starting to talk, making it stream its reply instead of making you wait for the whole thing, keeping it snappy even deep into a long conversation, and — the part everyone forgets — teaching it to recognize when the conversation is over so it stops taking the last word.

This is written to be handed to a coding agent (Claude Code, Cursor, Aider, or similar) that already has access to your project. It does the work; you answer a few questions and approve changes.

---

## How to use this

Paste this whole document into your coding agent inside your voice-agent project and say: *"Read this and start with the interview. Do not change any code until we've finished the interview and agreed on what to tackle first."*

The agent will:

1. **Interview you and your codebase first.** It looks at how your conversation loop actually works today and asks you to fill in the gaps — what listens, what speaks, what thinks, and where it feels slow or awkward. It writes nothing until it understands your setup.
2. **Measure before it touches anything.** Smoothness problems are easy to misdiagnose by feel. The agent instruments one real turn and finds where the time actually goes.
3. **Improve one layer at a time.** Each tier below is independent and shippable on its own. After each, you should feel a specific, nameable improvement — and the agent shows you the numbers.

The golden rule the agent must follow: **understand the existing system before changing it, and never swap out your speech-to-text or text-to-speech provider without asking you first.** Provider changes have cost, latency, quality, and language tradeoffs that are yours to make, not the agent's.

---

## Ground rules for the agent (read these to yourself before you begin)

- **Investigate, don't assume.** Find the real files and the real settings. Quote them back. Do not guess how the loop works from the framework's defaults — read the actual code path from "audio comes in" to "audio goes out."
- **One change at a time.** Bundling five improvements means that when something regresses, nobody can tell which one did it. Ship a tier, measure, move on.
- **Measure before and after, every time.** "Feels faster" is not evidence. Put a number on it.
- **Conservative by default on anything that can misfire.** Especially the "stop talking" logic later — a false trigger (going silent when the person wanted a reply) reads as broken. When unsure, keep replying.
- **Keep a do-not-break list.** Before editing, ask what currently works that must keep working (wake word, barge-in, languages, accessibility, a specific device).
- **Don't reach for a rewrite.** Almost all of this is tuning and reordering existing steps, not new architecture.

---

## Tier 0 — The interview (do this first; write nothing yet)

Work through these out loud with the person. Fill in what you can by reading the code, and ask them the rest. The goal is a shared, written picture of the conversation loop before a single change.

### A. The shape of the conversation

- Where does the conversation happen — a phone app, a browser tab, a desktop app on dedicated hardware, a hardware device, a phone call? More than one?
- Is it hands-free (it keeps listening after it replies) or push-to-talk (the person taps or holds to speak)? Both modes?
- Is there a wake word, or is the mic always hot during a session?

### B. The ears (speech-to-text)

- Which speech-to-text service or model is in use (for example Deepgram, Whisper, AssemblyAI, Google, a local model)? What model tier?
- Is audio **streamed** to it continuously, or recorded and sent as one chunk after the person stops? This is the single biggest factor in end-of-turn lag — confirm it, don't assume.
- How does the system decide the person has **finished speaking**? Look for: a fixed silence timeout, voice-activity detection on audio energy, the recognizer's own "end of utterance" / "endpointing" signal, or a manual tap. What are the actual numbers (how many milliseconds of silence)?
- Roughly how long after the person stops talking does transcription "finalize"? Is there a hard wait or sleep in there?

### C. The mouth (text-to-speech)

- Which text-to-speech service or model is in use (for example ElevenLabs, Cartesia, PlayHT, OpenAI, Azure, Rime, a local model)? Which voice/model setting?
- Is the spoken audio **streamed and played as it's generated**, or is the whole reply synthesized first and then played? Confirm by reading the code — "synthesize the full reply, then play it" is a very common and very fixable source of dead air.
- What audio format and sample rate is requested, and does it match what the playback side wants without a conversion step?
- Is there a low-latency mode or a faster model variant available on the current provider that isn't turned on?

### D. The brain (the language model)

- Which model answers, and is its response **streamed** (text arrives token by token) or returned all at once?
- Is the answer handed to text-to-speech as one finished block, or sentence by sentence as it streams?
- Does the system send a large, growing context every turn (long instructions, history, tool definitions)? Is any of it cached between turns, or is the full thing re-sent and re-read each time?

### E. Turn-taking and endings

- Does the assistant **always** produce a reply to every utterance — including when the person clearly signs off with "okay, thanks" or "cool, I'll do that"? Does it tend to "take the last word"?
- Can the person interrupt it mid-sentence (barge-in), or do they have to wait for it to finish?
- Has anyone complained it talks over them, cuts off the first word, or pauses awkwardly before answering?

### F. Where it actually hurts, and the limits

- Where does it feel slow: the gap before it starts talking, choppiness mid-reply, or it being slow to realize you're done? Or does it feel like it gets slower the longer you talk?
- Are there budget, latency, language, or hardware constraints to respect?
- What must absolutely not break?

**Output of Tier 0:** a short written summary of the loop — ears, mouth, brain, transport — plus the one or two things that bother the person most. Confirm it with them before continuing, and let them pick where to start. The tiers below are ordered by typical impact, but follow the pain.

---

## Tier 1 — Measure first (find where the time really goes)

You cannot smooth what you cannot see. Before tuning anything, instrument **one real turn** and break the wait into its parts:

- the moment the person stops speaking,
- to the transcript being final,
- to the first token of the model's reply,
- to the first byte of audio coming back,
- to the first sound actually playing.

Lay those five numbers out for a typical turn. The biggest gap is your first target — and it's often not where people guess. Common culprits, in rough order of how often they're the real problem:

1. **End-of-turn detection waiting too long** before it even starts thinking.
2. **Text-to-speech synthesizing the entire reply** before any of it plays.
3. **The model answer not being streamed**, so nothing starts until the whole thing is written.
4. **A growing prompt** making each turn a little slower than the last.

Don't fix anything yet. Just show the person the breakdown and name the dominant cost. Every later tier should move one of these numbers, and you'll prove it did.

---

## Tier 2 — Stop waiting so long to start (end-of-turn detection)

The most jarring lag is the silence after you finish talking, before it reacts. Usually that's a single conservative "wait this many milliseconds of silence to be safe" timeout doing double duty for every situation.

The smoother pattern is **layered**: be quick to respond when you're confident the person is done, and fall back to patient only when you're not.

- If your speech-to-text gives an explicit "end of utterance" signal, lean on it — it's smarter than raw silence. Once it fires, you only need a short quiet window to confirm, not a long one.
- Keep a **fast path and a slow path.** When the recognizer has just confirmed a finalized phrase, a brief pause (a few hundred milliseconds) is enough to take the turn. When it hasn't — noisy room, trailing off, mid-thought — fall back to the longer, safer silence window so you don't cut people off.
- Replace any fixed "sleep for X then proceed" with a **check-and-go**: poll for the recognizer's final result and move the instant it's ready, with a ceiling so you never wait longer than the old fixed delay. Most turns finish well under that ceiling.
- Be honest about the tradeoff: shave too aggressively and it clips the ends of sentences or interrupts a pause. Tune toward "snappy but never rude." Test with slow talkers and mid-sentence pauses, not just clean one-liners.

While you're here, confirm **barge-in** works: if the person starts talking while it's speaking, it should stop and listen. Nothing feels more robotic than an assistant that won't yield.

**Verify:** the "stop speaking → start thinking" number from Tier 1 should drop, and you should not have introduced clipped or interrupted turns. Re-measure with both quick and trailing-off endings.

---

## Tier 3 — Stream the thinking (make the first word fast)

If the system waits for the model to finish writing the whole answer before doing anything, the person stares at silence for the length of the entire reply.

- **Stream the model's response** and hand it onward in **sentence-sized pieces** as they arrive, rather than waiting for the full answer. The first sentence can be on its way to the voice while the model is still writing the second.
- Favor short replies by default for a voice interface — long answers are the enemy of conversation. A model that leads with the answer and offers detail only if asked feels dramatically snappier than one that preambles.
- **Keep a long conversation from getting slower over time.** If you send a big block of stable instructions plus a growing history every turn, make sure the stable part is cached between turns and the parts that change every turn (the current time, anything dynamic) sit at the very end — not wedged in the middle, where they quietly break the cache for everything after them. The classic bug: caching is "on," but a small changing detail sits early in the prompt, so the whole history gets re-read from scratch each turn and responses creep slower the deeper you go. Confirm the stable prefix actually stays identical turn to turn.

**Verify:** the "to first model token" and "to first audio" numbers should drop, and a long, multi-turn conversation should stay roughly as fast at turn fifteen as at turn two.

---

## Tier 4 — Stream the voice (pipeline the speech)

Even with a fast model, if you synthesize the entire spoken reply before playing any of it, you've put the silence back.

- **Synthesize and play sentence by sentence.** As each sentence streams out of the model, send it to text-to-speech and start playing it while the next sentence is still being written and synthesized. The person hears the first sentence almost immediately.
- A clean way to handle this is **"hold one ahead"**: keep the most recent sentence buffered just long enough to know whether another one is coming, so you can mark the final sentence correctly without an awkward extra round trip — then let the audio flow continuously.
- Make sure you're using a **low-latency voice model and a playback-friendly audio format** so there's no conversion step between synthesis and the speaker. Many providers have a "turbo"/"flash"/low-latency model and a streaming endpoint that aren't on by default.
- If, and only if, the current provider simply can't hit the latency the person wants, **raise the option of a faster provider with them** — with the cost, voice-quality, and language tradeoffs spelled out. Don't switch silently.

**Verify:** time-to-first-audio should be a small fraction of the full reply length. A three-sentence answer should start speaking almost at once, not after all three are rendered.

---

## Tier 5 — Know when to stop (natural endings, no last word)

This is the part most voice agents miss, and it's what makes them feel needy. When the person wraps up — "okay, thanks," "great, I'll do that," "right on," "sounds good" — a natural human lets the conversation end. Most assistants instead manufacture one more reply ("You're welcome!"), always grabbing the last word.

Build a small, deterministic check that runs the instant the transcript comes back — **before** any model call — and decides whether this turn is just a sign-off. If it is, the assistant stays silent, the session winds down gracefully, and no reply is generated. Doing it this way means a goodbye costs nothing: no model call, no tokens, no round trip.

Design it to be **conservative**, because the failure modes are not symmetric. If it stays silent when the person actually wanted a reply, the assistant looks broken. If it occasionally replies to a borderline goodbye, that's just the mild old behavior. So bias hard toward replying when unsure, with layers of safety:

- **A sign-off phrase must actually be present** — "thanks," "got it," "sounds good," "will do," "perfect," "cool," "bye," "right on," and the like.
- **Questions and requests veto it.** A question mark, "can you," "how about," "what about," "one more thing" — any sign the person wants something back means reply normally.
- **Continuations veto it.** "Okay, so the revenue is up" or "great, the meeting went well" are not goodbyes — they're the person still talking. A leading "okay" or "great" followed by new information should get engagement, not silence.
- **Commands veto it.** "Great, send that email" is an instruction, not a farewell — unless the person is committing to do it themselves ("great, I'll send that"), which is a sign-off.
- **Keep it short.** Real goodbyes are brief. Treat a bare positive like "great" or "cool" as an ending only in a very short utterance; in a longer sentence it's almost always leading into something.
- **Only end a conversation the assistant has actually been part of.** Never let the very first thing someone says get swallowed as a goodbye.
- **Watch for look-alikes.** Tiny details bite here — make sure "well" isn't mistaken for "we'll," or "ill" for "I'll." Test the embarrassing edge cases on purpose.

Expect to **tune this with real usage.** It's a set of word lists and thresholds, not a model — so when a real goodbye slips through, or it goes quiet when it shouldn't have, the fix is to move a phrase into the right bucket and adjust. Build it so that tuning is a one-line change, and capture the misses as you find them.

**Verify:** clear sign-offs end in silence; questions, commands, and "leading into new info" turns still get a normal reply. Write down the exact phrases you tested so the behavior is documented and easy to extend.

---

## Tier 6 — Polish and protect

- **Interruptions:** confirm barge-in is solid and that interrupting mid-reply leaves the conversation in a clean state, not a half-spoken limbo.
- **Graceful overlaps:** if there's background audio (music, a media player), make sure ducking and restoring don't add their own little stutters around each turn.
- **Don't regress the do-not-break list.** Re-test wake word, languages, the specific device, accessibility — whatever the person flagged in Tier 0.
- **Lock in the behavior with tests** where it matters most — especially the "stop talking" word lists, so a future change can't silently re-break it.
- **Measure the whole turn again** and put the before-and-after side by side. The point of all of this is a number the person can feel: from "stop talking" to "first word back," and how steady it stays across a long conversation.

---

## The mindset to leave them with

Smooth conversation isn't one big feature — it's the removal of a dozen small waits and one bit of social grace. The wins come from **overlapping work that used to happen in sequence** (listen, think, and speak should bleed into each other, not queue up) and from **knowing when not to speak at all.** Interview first, measure honestly, improve one layer at a time, and let the person feel each step. Done right, the assistant stops feeling like a system you're operating and starts feeling like someone you're talking to.
