# Voice Agent Latency — AI Agent Prompt

A drop-in prompt for your AI coding agent (Claude Code, Cursor, Aider, or similar) that helps it pipeline LLM streaming with TTS in your existing voice-first AI agent project. It encodes a battle-tested fix for the dead-air gap that almost every voice agent has on day one — and, critically, instructs the agent to **interview you first** so the implementation matches your stack, your transport, and your existing audio playback path.

**Why this prompt exists:** the default voice-agent architecture is sequential. The server waits for the LLM to finish its full response, then ships the whole text to TTS, then waits for the whole MP3 to come back, then sends it to the client, then the client downloads the whole thing, then it starts playing. Each step is "wait for everything, then begin the next." That's where the four-to-eight seconds of dead air comes from. The fix isn't a faster LLM or a faster TTS — it's pipelining. Stop waiting; start streaming as soon as the first sentence is ready. This pattern is re-derived in every long-running voice-agent project. Don't re-derive it.

**How to use it:** copy everything below the horizontal rule into a fresh chat with your AI agent. Answer the interview questions when it asks, then let it build.

---

You are helping me cut the response latency of my AI voice agent. Treat this whole message as your brief. **Do not write any code yet.** Your first job is to interview me, echo my answers back as a short plan, and only then start building.

## What we're solving

After the user stops speaking, my agent has a long pause before its first audible word — typically four to eight seconds. The pause is not the LLM being slow; it's the *architecture* being sequential. We need to break the LLM-must-finish gate and the audio-must-fully-download gate so the user hears the first sentence within roughly one second of finishing their thought.

## The mechanic (so you understand what you're implementing)

After the user stops talking, the latency budget on a typical voice agent looks like this:

```
silence detection → STT finalize → LLM time-to-first-token → LLM full response →
  TTS request → TTS full synthesis → TTS download → audio decode → play
```

**Most of those steps don't have to be sequential.** The LLM streams sentences as it generates them. Most modern TTS APIs stream audio bytes as they synthesize. WebSockets and `StreamingResponse` HTTP exist precisely to let downstream consumers start before upstream producers are done. The default code path almost always throws away these streaming benefits at one or two specific gates — find those gates, remove them, and the latency collapses.

Two specific bottlenecks you'll almost certainly find:

**Bottleneck A — server-side LLM-complete gate.** The server consumes the LLM stream sentence-by-sentence (often forwarding text to the UI as `transcript_delta` events for live captions), but **buffers the audio side** — accumulates all sentences, joins them after the LLM finishes, *then* requests TTS. Audio doesn't begin until the entire response exists.

**Bottleneck B — client-side full-blob wait.** Even when the server streams TTS bytes correctly, the client does something like `await response.arrayBuffer()` or `await response.blob()` before passing the audio to a player. That single `await` discards the entire streaming benefit and reintroduces a blocking download.

Plus a smaller contributor: the **silence-detection (VAD) window** — the duration of silence the server requires before deciding "user is done." Default values are often 0.8–1.0 seconds. Tuning matters in both directions; we'll come back to it.

## Interview me first

Ask me these questions one at a time, in order. After my answers, summarize what you'll change (no code yet) and wait for my "go ahead":

1. **What language and framework is the server?** (Python + FastAPI, Node + Hono, Go + Echo, Elixir + Phoenix, etc.) Where is the WebSocket or streaming-HTTP endpoint that owns the voice loop?
2. **What LLM provider and SDK?** (Anthropic Claude, OpenAI, local Ollama, Gemini, etc.) Show me the call site that processes the response. Is it streaming (`async for chunk` / `for await chunk of stream`)? Does the SDK expose sentence-level boundaries, or am I splitting on punctuation myself?
3. **What TTS provider?** (Eleven Labs, OpenAI TTS, Cartesia, Deepgram TTS, Azure, etc.) Critically: **does the API support streaming output bytes**, or is it one-shot only? Most modern providers stream — confirm yours does and which specific method/endpoint you call.
4. **What transport between the server and the client?** WebSocket only? WebSocket + HTTP for audio? Server-Sent Events? This determines how segments are announced to the client and how audio bytes reach it.
5. **What's the current audio architecture on the client?** Web `<audio>` element with a Blob URL? WebAudio `AudioBufferSource` from raw PCM? Some native mobile audio API? Show me the function that takes audio bytes and produces sound.
6. **Is there a tool-use / function-calling loop?** If yes, the server may yield text from intermediate rounds (e.g. an "okay let me check" before a tool call). Confirm that's desirable behavior to speak live, or that we should suppress it. Most teams want it spoken live — it makes the agent feel responsive — but it's a deliberate choice.
7. **Where, specifically, is the perceived dead air?** Right after I stop talking? Right before audio starts? In the middle of a long response? Paste a recent transcript or describe the worst case so we know what to optimize for.
8. **What's the current silence-detection (VAD) threshold?** A value in milliseconds, where it lives in the code, and whether you've tuned it before. Some stacks have two — a server-side VAD AND a STT-provider endpointing window — that compose. Make sure I know about both.

## Architecture (target)

```
                                         ┌────────────────────────────┐
   user finishes speaking                 │  fix this gate             │
            │                             │                            │
            ▼                             ▼
   [STT finalize] → [LLM stream] ─sentence 1─→ [TTS sentence 1] → bytes
                                │                                   │
                                ├─sentence 2─→ [TTS sentence 2] ─→ bytes
                                │                                   │
                                └─sentence N─→ [TTS sentence N] ─→ bytes
                                                                    │
                                  ┌─────────────────────────────────┘
                                  ▼
                           ─→  client audio queue → sequential playback
                                  ▲
                                  │
                                  └── fix this gate too:
                                      stream playback / small per-segment
                                      fetches, NOT a full-response blob wait
```

Two cooperating layers:

1. **Per-sentence segment events on the server.** As each sentence is produced by the LLM, the server emits a `speak_segment` (or whatever name fits your protocol) event over the existing transport, carrying the segment text or a per-segment id the client can use to fetch its TTS bytes.
2. **Audio queue on the client.** The client maintains an ordered queue of pending segments. While segment N is playing, segment N+1 may already be downloading or in queue. Playback chains via `<audio>.onended` or its native equivalent. Final segment carries an `is_final` flag so the client knows when to fire turn-completion logic.

The result: the user hears segment 1 within roughly the time it takes to detect silence + LLM time-to-first-token + first-sentence completion + TTS time-to-first-byte. The rest of the response is still being generated, but the user is already hearing the answer.

## Build it in five steps

Do these in order. Show me the diff after each step and wait for my "go" before moving to the next.

### Step 1 — confirm the LLM is streaming sentences

In your existing LLM call site, confirm the iteration is yielding sentence-bounded pieces of text. If your provider's SDK yields token-by-token deltas, you'll need a tiny sentence splitter — buffer characters until you hit a `.`, `?`, or `!` followed by whitespace (with reasonable handling for `Mr.`, `e.g.`, decimals, etc.) and yield the complete sentence. Many codebases already have this; check before writing one.

If the LLM call site isn't streaming at all, this step is the prerequisite for everything else — you can't pipeline a non-streaming call. Switch to the streaming variant of your provider's SDK first.

### Step 2 — extract a per-sentence emit helper

Add a function `emit_segment(text, seq, is_final)` (or your language's equivalent) that:
- Records the segment text under a stable id the client can use for TTS lookup. The cleanest pattern is a TTL'd in-memory dict keyed by `f"{base_turn_id}::{seq}"`. The TTS endpoint already exists; this just adds new keys.
- Sends a `speak_segment` event to the client over the existing transport with `{turn_id, base_turn_id, seq, is_final, ...}`.
- Logs a per-segment line that includes a `time_since_user_stopped_talking` metric — this becomes your headline number for measuring the win.

### Step 3 — rewrite the streaming consumer with the hold-one-ahead pattern

Wrap your existing LLM-stream loop:

- Keep the existing per-sentence behaviors (e.g. `transcript_delta` for the on-screen subtitle).
- Add: when a sentence is yielded, **don't emit it as a segment immediately**. Instead, hold it. When the *next* sentence arrives, that proves the held one isn't last — emit it as `is_final=false`, and replace the held sentence with the new one.
- After the loop exits, flush whatever's held as `is_final=true`.

This is the cleanest way to flag the final segment without an extra round-trip event. The cost is "one sentence's worth" of latency on the *final* segment only — first-segment latency is unchanged, which is what determines the user's perception of responsiveness.

If your stack also produces tool-use events interleaved with text (Anthropic, OpenAI tool-calling, etc.), keep the existing tool-event forwarding intact. The held-sentence buffer only holds *text* sentences, not tool events.

### Step 4 — client-side audio queue and sequential playback

On the client:

1. Add module-scoped state: `audioQueue`, `currentlyPlaying`, `activeBaseTurnId`, `lastSegmentWasFinal`, `currentFetchAbort`.
2. Replace the existing single-shot `speak` event handler with `speak_segment`. On the first segment of a new turn (queue empty, nothing playing), set `activeBaseTurnId` and any UI state (e.g. flip the orb to "speaking"). Drop messages whose `base_turn_id !== activeBaseTurnId` — those are stale leftovers from a turn the user just interrupted.
3. Push the segment onto `audioQueue`, then call `pumpQueue()`.
4. `pumpQueue` shifts the next segment off the queue, sets `currentlyPlaying = true`, and calls your `playSegmentAudio(turnId)`. If nothing's queued, no-op.
5. `playSegmentAudio` fetches the segment's TTS bytes (wrap the fetch in an `AbortController` so an interrupt can cancel an in-flight download), plays via your existing audio path, and on `<audio>.onended` calls `onSegmentEnded`.
6. `onSegmentEnded` clears `currentlyPlaying`. If the queue still has segments, call `pumpQueue` again. If the queue is empty AND the just-played segment had `is_final: true`, fire your existing "turn complete" routine.
7. Update your interrupt handler (e.g. user taps to stop): clear the queue, abort the in-flight fetch, reset `currentlyPlaying` / `activeBaseTurnId` / `lastSegmentWasFinal`, then run your existing stop logic.

If you have a dual-path audio architecture (e.g. `<audio>` element for sound + `AudioBuffer` for an analyser node feeding a visualizer), keep both paths per segment so the visualizer stays voice-reactive on every sentence.

### Step 5 — tune the silence-detection (VAD) window

This is independent and one line, but it's a real lever:

- A *too-tight* window (e.g. 0.5s) clips the user mid-sentence on natural mid-thought pauses.
- A *too-loose* window (e.g. 1.5s+) feels sluggish — it adds dead air to every turn.
- Sweet spot is usually **0.8–1.0 seconds** for English conversational speech.

Don't tune this in isolation. The streaming work in Steps 1–4 is the much bigger win; tune VAD *after* you've measured the new floor. Many teams find that with proper streaming, they can afford a slightly *longer* VAD window because the response feels snappy enough to absorb it.

If your stack also has STT-provider endpointing (e.g. Deepgram's 300ms endpointing), be aware the two windows compose, not duplicate. Document both.

### Step 6 — write tests

Add unit tests on the server side that assert:

- Single-sentence response → exactly one segment with `is_final=true`.
- Multi-sentence response → N segments, with `is_final` only on the last and `seq` numbered 0..N-1.
- All segments share the same `base_turn_id`.
- Any per-turn flag that should ride only on the last segment (e.g. `auto_continue`) does.
- Text-mode / non-voice paths emit zero `speak_segment` events.

Client-side tests are usually low-signal here; the higher-value verification is a manual conversation on a real device. The structural unit tests catch regressions in the server protocol; the live test catches everything else.

## Reference implementation (Python, illustrative)

```python
# segment_stream.py
import time
import uuid
from typing import AsyncIterator


# In your app state: TTL'd dict keyed by segment id.
# turn_texts: dict[str, tuple[str, float]]
TTL_S = 120.0


def record_segment_text(turn_texts: dict, segment_id: str, text: str) -> None:
    now = time.monotonic()
    # Lazy prune.
    expired = [k for k, (_t, exp) in turn_texts.items() if exp < now]
    for k in expired:
        turn_texts.pop(k, None)
    turn_texts[segment_id] = (text, now + TTL_S)


async def stream_with_segments(
    *,
    llm_stream: AsyncIterator[str],
    ws,
    turn_texts: dict,
    auto_continue: bool = False,
) -> str:
    """Consume an LLM stream of sentences, emit speak_segment events
    with hold-one-ahead so the final segment can be flagged is_final=True
    without an extra round-trip. Returns the full concatenated response.
    """
    base_turn_id = uuid.uuid4().hex
    seq = 0
    held_text: str | None = None
    held_seq: int | None = None
    sentences: list[str] = []
    t0 = time.monotonic()

    async def emit_segment(text: str, segment_seq: int, is_final: bool) -> None:
        segment_id = f"{base_turn_id}::{segment_seq}"
        record_segment_text(turn_texts, segment_id, text)
        await ws.send_json({
            "type": "speak_segment",
            "turn_id": segment_id,
            "base_turn_id": base_turn_id,
            "seq": segment_seq,
            "is_final": is_final,
            # Per-turn flags ride on the final segment only.
            "auto_continue": auto_continue if is_final else False,
        })
        # Headline metric — graph this in your dashboards.
        log(
            f"speak_segment base={base_turn_id} seq={segment_seq} "
            f"chars={len(text)} t_since_user={time.monotonic() - t0:.2f}s "
            f"final={is_final}"
        )

    async for sentence in llm_stream:
        sentences.append(sentence)
        # Existing on-screen subtitle behavior: forward sentence as
        # `transcript_delta`. Voice-mode TTS adds the held-segment emit:
        await ws.send_json({"type": "transcript_delta", "text": sentence})
        if held_text is not None:
            await emit_segment(held_text, held_seq, is_final=False)
        held_text = sentence
        held_seq = seq
        seq += 1

    full_text = " ".join(s.strip() for s in sentences if s.strip())
    if held_text is not None:
        await emit_segment(held_text, held_seq, is_final=True)
    return full_text
```

```js
// audio-queue.js (client)
let activeBaseTurnId = null;
let audioQueue = [];
let currentlyPlaying = false;
let lastSegmentWasFinal = false;
let currentFetchAbort = null;

// In your WS message handler:
function onSpeakSegment(msg) {
  if (activeBaseTurnId === null && audioQueue.length === 0 && !currentlyPlaying) {
    activeBaseTurnId = msg.base_turn_id;
    flipUiToSpeaking();              // your existing 'speaking' state
  } else if (msg.base_turn_id !== activeBaseTurnId) {
    console.warn('dropping stale segment', msg.base_turn_id);
    return;
  }
  audioQueue.push(msg);
  pumpQueue();
}

function pumpQueue() {
  if (currentlyPlaying || audioQueue.length === 0) return;
  const seg = audioQueue.shift();
  lastSegmentWasFinal = !!seg.is_final;
  currentlyPlaying = true;
  playSegmentAudio(seg.turn_id).catch((err) => {
    if (err && err.name === 'AbortError') return; // interrupt path
    console.error('segment playback failed', err);
    onSegmentEnded();
  });
}

async function playSegmentAudio(turnId) {
  currentFetchAbort = new AbortController();
  const url = `/api/tts/${encodeURIComponent(turnId)}`;
  const resp = await fetch(url, { signal: currentFetchAbort.signal });
  if (!resp.ok) throw new Error(`tts ${resp.status}`);
  const arr = await resp.arrayBuffer();
  // ... your existing audio-element + analyser dual-path goes here.
  // The KEY change is just that the unit of audio is one SENTENCE
  // (small, fast to download), not the whole response.
  audioElement.onended = onSegmentEnded;
  audioElement.src = URL.createObjectURL(new Blob([arr], { type: 'audio/mpeg' }));
  await audioElement.play();
}

function onSegmentEnded() {
  currentlyPlaying = false;
  currentFetchAbort = null;
  if (audioQueue.length > 0) {
    pumpQueue();
    return;
  }
  if (lastSegmentWasFinal) {
    activeBaseTurnId = null;
    lastSegmentWasFinal = false;
    onTurnComplete();                 // your existing turn-complete handler
  }
  // else: queue empty but more segments expected — idle until next msg.
}

// Interrupt handler (e.g. user taps to stop):
function onUserInterrupt() {
  audioQueue.length = 0;
  if (currentFetchAbort) {
    try { currentFetchAbort.abort(); } catch (_) {}
    currentFetchAbort = null;
  }
  currentlyPlaying = false;
  activeBaseTurnId = null;
  lastSegmentWasFinal = false;
  try { audioElement.pause(); audioElement.currentTime = 0; } catch (_) {}
  // ... your existing end-session signaling
}
```

This is illustrative — your transport, your audio path, your interrupt protocol will all differ. The *shape* of the change is what matters: per-sentence segments on the wire, sequential queue on the client, hold-one-ahead for clean turn endings.

## Tuning knobs

If perceived first-audio latency is still too high after Steps 1–4:

- **Confirm your TTS provider is actually streaming.** Some SDKs have a streaming method that *appears* to stream but secretly buffers internally. Check the wire — if you can `tcpdump` or watch DevTools Network, the response should arrive in chunks, not as one big payload at the end.
- **Confirm your client isn't accidentally re-buffering.** Even small mistakes like `await response.arrayBuffer()` on the streamed segment fetch reintroduce the blocking wait. For tiny per-segment files (<50KB) this only costs ~50–200ms, but it adds up. If you can switch to direct streaming playback (e.g. `audio.src = url` directly) without breaking your visualizer, it's worth another ~100–300ms.
- **Drop the LLM time-to-first-token.** Most providers offer a "smaller, faster" model variant that's faster at producing the first sentence even if total tokens/sec is similar. For voice agents, TTFT matters far more than throughput.
- **Pre-warm the TTS connection.** If your TTS provider supports a persistent WebSocket (Eleven Labs, Deepgram Aura), maintaining a long-lived connection saves ~100–200ms of TLS handshake on every turn.

If the agent occasionally clips you mid-sentence on natural pauses:

- That's the VAD window being too tight. Bump it 100–200ms at a time. The streaming work you just did made the response feel so much snappier that you have headroom to spend on robustness.

## What NOT to do

- **Don't spawn parallel TTS tasks server-side for multiple sentences at once.** It seems faster but introduces concurrency complexity, can hit TTS-provider rate limits, and produces ordering bugs that are hard to debug. Client-driven sequential playback (one TTS call active at a time per session) is simpler and almost as fast.
- **Don't try to stream the LLM's partial-sentence tokens to TTS.** "Hello, " then "I just" then "checked your" — the TTS will produce mid-thought audio that ends weirdly when the LLM pauses to think. Sentence boundaries are the right granularity. Word-level streaming exists (Eleven Labs has it), but the complexity-to-win ratio is poor unless you're already at sub-second floor.
- **Don't store the per-segment TTS audio bytes long-term.** Use a TTL'd in-memory cache. Persisting it bloats your storage and the bytes are useless after the user has heard them.
- **Don't drop the `is_final` flag and use a separate "turn_end" event instead.** It works, but it's more states to manage on the client. The flag-on-the-final-segment approach is cleaner.
- **Don't tune VAD before you ship the streaming change.** Premature tuning. Streaming is the much bigger lever; VAD is a refinement on top.

## Why this works

The default architecture serializes operations that can run in parallel. Each "wait for everything to finish" step is a discrete latency tax on the user. Pipelining LLM → TTS → playback removes the largest of those taxes — the LLM tax — by letting audio start while the LLM is still generating. The hold-one-ahead pattern is a tiny bit of state that lets you flag the last segment without an extra protocol event. The client audio queue keeps playback continuous so the user perceives one smooth response, not a series of disconnected sentences. The VAD tuning is a small additional refinement on top.

The whole change is fundamentally about *positioning operations in time* so they overlap. You're not making the LLM faster, the TTS faster, or the network faster. You're making them happen at the same time instead of one after the other.

## Verify before declaring victory

Before you tell me the work is done:

1. **Run the unit tests.** All structural assertions on segment shape must pass.
2. **Print the wire log for one sample turn.** I want to see the per-segment log line with `t_since_user` for `seq=0 final=False` — that's the headline metric. Target: under one second for short replies.
3. **Run a 5-turn live conversation on the actual target device** (not localhost in your dev browser — the device that real users will use). Count the seconds between "you stop talking" and "agent's first audible word." Target: ~1.0–1.5 seconds.
4. **Test interruption.** Mid-response, trigger your stop signal. Verify the queue empties, the in-flight fetch aborts, and no audio leaks past the stop.
5. **Test a very short reply** ("Yes." / "Got it.") — this is one sentence so it should still work end-to-end with `is_final` on the only segment.
6. **Test a long reply with tool calls.** If the LLM produces text both before and after a tool call (e.g. "Let me check that. ... Found three items."), the pre-tool text should be spoken live, then the tool runs, then the post-tool text plays. This is the side-benefit of streaming — explicitly verify it doesn't regress.

Then tell me: what's the new headline `t_since_user` number, what's the realistic floor of further wins available, and whether any of the smaller knobs (TTS streaming confirmation, direct-streaming playback, model swap, persistent TTS connection) are worth picking up next.
