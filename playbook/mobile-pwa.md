# Voice-First Mobile PWA · AI Agent Prompt

A drop-in prompt for your AI coding agent (Claude Code, Cursor, Aider, or similar) that helps it add a voice-first mobile companion to your existing AI agent project. It encodes a battle-tested architecture for iOS PWAs and, critically, instructs the agent to **interview you first** so the implementation matches your stack, your transport, and your existing tools.

**Why this prompt exists:** voice on a mobile browser is full of subtle iOS Safari traps that cost real time to discover. The MP3 + dual-path audio architecture below is the answer that worked after several broken arrangements. Don't relearn it the hard way.

**How to use it:** copy everything below the horizontal rule into a fresh chat with your AI agent. Answer the interview questions when it asks, then let it build.

---

You are helping me add a voice-first mobile PWA companion to my AI agent. Treat this whole message as your brief. **Do not write any code yet.** Your first job is to interview me, echo my answers back as a short plan, and only then start building.

## What we're building

A Progressive Web App that the user installs to their phone's home screen. They tap an animated orb, talk, the agent thinks, and audio plays back, fully hands-free. The PWA shell is dark and minimal: an animated orb fills the viewport, a small status pill in the corner, a transcript subtitle that streams the agent's response, and a tap hint at the bottom. Tools that the agent calls surface as a small floating chip near the top of the screen.

The phone PWA does NOT replace any desktop / laptop UI you may already have for the same agent. It sits alongside it, talking to the same backend over a different network path.

## Interview me first

Ask me these questions one at a time, in order. After my answers, summarize what you'll build (no code yet) and wait for my "go ahead":

1. **What's the existing agent backend?** Language and framework (e.g. Python + FastAPI, Node + Hono, Go + Echo). Do you already have a WebSocket route for voice-style streaming, or do I need to add one?
2. **What model + provider runs the conversation?** (Anthropic Claude, OpenAI, local Ollama, etc.) Are there existing tools / function calls registered? Where do they live in the code?
3. **STT and TTS providers?** (Deepgram, Whisper, Eleven Labs, OpenAI TTS, Cartesia, etc.) Streaming or one-shot? Do I need to wire new ones, or are they already in the backend?
4. **Where will this be deployed?** (Cloud VM, container, edge, behind Caddy / Nginx / Cloudflare Tunnel?) The PWA must be served over HTTPS. `getUserMedia` and PWA install both require it.
5. **Auth model?** Single user, multi-user, none? If single-user (typical for personal-agent setups), a bearer-token model is simplest. If multi-user, what's the existing session mechanism?
6. **Should risky actions (send email, schedule things, delete) be voice-confirmed?** Strong recommend yes. If so, we'll add a tiny `await_confirmation` signal tool and a prompt rule that gates destructive tools behind a verbal yes/no.
7. **Any visual style anchors?** Orb color palette, brand accent, dark/light. Default is dark cosmic with teal accents.
8. **Is there existing conversation persistence?** (Postgres / SQLite / in-memory.) The phone needs to read and write the same conversation table the desktop / laptop side does, or share a session somehow.

## Architecture (target)

```
iPhone PWA (HTML+JS+WebGL)
  │  HTTPS + WSS over a single TLS proxy (Caddy / Nginx / CF Tunnel)
  ▼
Phone server (FastAPI / Node / etc.)
  │   ├─ WebSocket /ws            ← bearer-token gated, accepts mic frames
  │   ├─ GET  /                   ← public shell HTML (Cache-Control: no-store)
  │   ├─ GET  /scene.js           ← animated orb (Three.js)
  │   ├─ GET  /sw.js              ← minimal pass-through service worker
  │   ├─ GET  /manifest.webmanifest
  │   ├─ GET  /api/tts/{turn_id}  ← MP3 stream, token-gated, NON-evicting
  │   └─ healthz
  ▼
Existing agent brain (LLM + tools + conversation manager)
```

The phone server is a small adapter. It owns the voice loop and audio routing, then delegates the actual thinking to your existing brain layer.

## The interaction loop

1. User taps the orb. Browser fires a click handler. **Everything that needs user-activation must happen synchronously inside this handler** (see iOS quirks below).
2. PWA opens the WebSocket if not already open, sends `{type: "start_listening"}`, optimistically flips its UI state to "listening" (orb shifts to a warm color, mic icon visible).
3. Server starts STT, sends back `{type: "start_mic"}`. PWA opens `getUserMedia`, captures 16 kHz PCM at 256-sample frames, sends each frame as a base64-encoded `audio_frame` message over the WS.
4. PWA also runs voice-activity detection client-side; on detected silence, sends `{type: "stop_listening"}`.
5. Server finalizes the transcript, sends `{type: "transcript", role: "user", text: "..."}`, then `{type: "status", state: "processing"}`.
6. Server streams the agent's response sentence by sentence as `{type: "transcript_delta", text: "..."}` so the PWA can show the text scrolling in.
7. After the response stream completes, the server stores the full text in a TTL'd in-memory dict keyed by a fresh `turn_id`, then sends `{type: "speak", turn_id: "..."}`.
8. PWA fetches `GET /api/tts/{turn_id}?token=...`, which returns an MP3 stream from your TTS provider.
9. PWA plays the MP3 through an `<audio>` element AND in parallel decodes a clone of the bytes into an AudioBuffer for an analyser node (so the orb can be voice-reactive).
10. When `<audio>.onended` fires, PWA sends another `start_listening` if continuous mode is on, else returns to idle.

## Critical iOS audio quirks (the lessons that cost us real time)

These are not theoretical. Each of these came from a broken state we hit and had to debug. Bake all of them in from the start.

### 1. MP3 over the wire, not raw PCM

iOS Safari's WebAudio + raw PCM is fragile (sample-rate quirks, silent failures). Have your TTS provider stream MP3 (e.g. `mp3_44100_128` for Eleven Labs). The `<audio>` element decodes MP3 natively without any WebAudio gymnastics.

### 2. Dual-path audio: `<audio>` for sound, AudioBufferSource for analyser data

iOS Safari has a long-standing bug where `MediaElementSource` audio plays correctly but the connected analyser node returns zeros. So you cannot get a voice-reactive orb by piping `<audio>` through an AnalyserNode.

The workaround:

```js
// Audible path: <audio> element with the MP3 blob URL
const blob = new Blob([mp3Bytes], { type: 'audio/mpeg' });
audio.src = URL.createObjectURL(blob);
audio.play();

// Analysis path (parallel): decode a CLONE of the bytes (decodeAudioData
// transfers ownership of its input buffer, so .slice(0) it first)
const buffer = await audioCtx.decodeAudioData(mp3Bytes.slice(0));
const src = audioCtx.createBufferSource();
src.buffer = buffer;
src.connect(analyser);  // side-branch only — NO destination connection
src.start();             // produces no audible output, just analyser data
```

The `<audio>` element is audible. The buffer source produces no sound (not connected to destination) but feeds real analyser samples to the orb shader.

### 3. Silent switch on iPhone respects AudioContext output but NOT `<audio>` element output

If you route audible audio through `AudioContext.destination`, flipping the silent switch on the side of the iPhone mutes everything. Users will assume the app is broken. Routing through `<audio>` ignores the silent switch, so voice plays regardless. This is THE reason the dual-path arrangement above is correct: audible on `<audio>`, visualization on WebAudio.

### 4. `<audio>.play()` after `await` rejects with NotAllowedError

iOS user-activation only persists across synchronous code in a click handler. If you `await fetch(...)` then call `audio.play()`, iOS has already revoked the gesture window. The fix: prime the `<audio>` element with a real (silent) MP3 / WAV `data:` URI as its initial `src` and call `play()` synchronously in the click handler.

A 46-byte silent WAV that works:

```html
<audio id="tts" playsinline preload="auto"
       src="data:audio/wav;base64,UklGRiYAAABXQVZFZm10IBAAAAABAAEAQB8AAIA+AAACABAAZGF0YQIAAAAAAA=="></audio>
```

Then in the click handler:

```js
audio.play().then(() => audio.pause()).catch(() => {});
```

A successful `play()` inside a gesture grants the element permanent user-activation for the page lifetime. Subsequent `play()` calls after awaits will succeed.

### 5. The TTS endpoint must be NON-evicting on read

iOS Safari's `<audio>` element makes **two** GET requests to the audio source: one to probe metadata/length, one to actually play. If your `/api/tts/{turn_id}` evicts the cached text on first read, the second request 404s and audio fails silently.

Make eviction TTL-driven, not on-read:

```python
# pseudo-Python
def get_turn_text(turn_id):
    entry = store.get(turn_id)
    if not entry or entry.expires < now:
        return None
    return entry.text   # do NOT delete on read

def record_turn_text(text):
    turn_id = uuid()
    # Lazy prune on write — drops anything older than TTL
    for k, v in list(store.items()):
        if v.expires < now: del store[k]
    store[turn_id] = (text, now + TTL)
    return turn_id
```

### 6. Tokens via header AND query param

Browsers cannot set custom headers on `<audio src>` requests or on the WebSocket upgrade handshake. Your auth has to accept the token via:

- `Authorization: Bearer <token>` (HTTP, when JS does the fetch)
- `X-Auth-Token: <token>` (custom header, alternate)
- `?token=<token>` query param (the only path the `<audio>` element and WebSocket can use)

All three should be valid for `/api/tts/{turn_id}` and for the WebSocket. Use a constant-time compare (`secrets.compare_digest` in Python, similar in your stack). The token is a long-lived shared secret.

### 7. PWA cache-busting: `Cache-Control: no-store` on the shell + versioned imports

iOS PWA cache is sticky. Without explicit no-cache:

- Shell HTML serves with `Cache-Control: no-store` so a deploy is picked up on next open.
- Static JS modules (`scene.js`, etc.) imported via versioned URL: `import {...} from '/scene.js?v=YYYYMMDD-feature'`. Bump the version on every shipped change to force a fresh module fetch.
- Service worker is a true pass-through (don't cache the shell; iOS PWA will strand users on broken shells for days).

### 8. Viewport: `viewport-fit=cover` + `100dvh` for canvases

```html
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover, user-scalable=no">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="theme-color" content="#000000">
```

Full-screen canvases must use `100dvh` (dynamic viewport), not `100vh`:

```css
#orbCanvas {
  position: fixed; top: 0; left: 0;
  width: 100vw; height: 100vh;
  height: 100dvh;  /* extends under the home-indicator zone on iOS PWA */
}
```

WebGL renderers should size to the canvas's `clientHeight`, not `window.innerHeight`. Pass `false` as the third argument to Three.js `setSize(w, h, false)` so it doesn't write inline `style.height` and override your CSS.

### 9. AudioContext re-suspension

iOS suspends AudioContext on backgrounding, lock screen, audio-route changes. Call `audioCtx.resume()` on every user gesture (the click handler) to keep it warm.

```js
function ensureAudioPrepared() {
  if (audioCtx && audioCtx.state === 'suspended') audioCtx.resume();
  // ... rest of audio prep
}
```

## The listening-state trap

The phone server's voice loop typically sends `start_mic` to begin capture but does NOT send a `status: listening` message. So the PWA must flip its visual to "listening" itself when it sends `start_listening` to the server (optimistic, instant feedback) AND when it receives `start_mic` back (covers auto-continue paths).

```js
// Click handler:
send({ type: 'start_listening' });
setStatus('listening');  // optimistic — orb shifts color immediately

// WS handler:
case 'start_mic':
  setStatus('listening');  // idempotent confirm + auto-continue path
  startMicCapture();
  break;
```

Without the optimistic flip, the orb only changes color after the WS round-trip. Users will read this as "no visual change at all" and tap repeatedly.

## Confirmation pattern for risky actions (recommended)

For any tool that does something destructive or irreversible (send email, create / modify calendar event, delete data), don't let the LLM call the tool directly. Add a no-op signal tool like:

```python
class AwaitConfirmationTool:
    def definition(self):
        return ToolDefinition(
            name="await_confirmation",
            description=(
                "Signal that you're about to take a destructive or risky "
                "action. Call this BEFORE the actual tool. After calling "
                "this, voice the proposed action in the same turn and "
                "wait for the user's verbal yes/no in the next turn."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "summary": {"type": "string"},
                    "target_tool": {"type": "string"},
                },
                "required": ["summary", "target_tool"],
            },
        )

    async def execute(self, tool_input):
        # Pure signal — no work. The phone server detects the call name
        # in the tool_use stream and emits a confirm_request WS message.
        return json.dumps({"status": "user_will_confirm"})
```

Add a hard rule to your system prompt:

> Before calling `send_email`, `create_calendar_event`, `modify_calendar_event`, `memory_forget`, or any destructive tool, you MUST call `await_confirmation` first in the same turn. Voice the proposal ("Sending Sarah '...'. Confirm?") and wait for the user's next message. Only call the actual tool after they say yes.

When the server sees `await_confirmation` fire, emit a special WS message:

```json
{"type": "confirm_request", "summary": "send email", "target_tool": "send_email"}
```

The PWA renders this as an amber chip near the top of the screen. The user can tap the orb to send a `cancel_confirmation` message, which the server handles by feeding a synthetic `[cancel]` transcript through the LLM (the system-prompt rule tells the agent to drop the proposal on cancel).

## Anti-hallucination rule for numeric answers

Without an explicit rule, LLMs will fabricate numbers when asked about real-world data ("hashrate," "revenue today," "user count"). Add to your system prompt:

> Numbers about the user's businesses (revenue, customer counts, percentages, balances, anything quantitative) MUST come from a tool you called THIS turn. Quote the tool's number. Don't round, soften ("about", "roughly"), or estimate from training data. If the tool returns no data or fails, say so plainly: "I don't have current X right now. The Y may be down." Never paper over a missing number with a fabricated one.

This pairs well with logging tool results at INFO level on the server so a journal grep reveals what the model actually saw on each turn.

## Action chip UI (top-right slide-in)

When a tool fires, show a small floating capsule near the top-right of the screen that slides in from the edge. Four states:

- `calling`: neutral teal, animated dot spinner, label like "Pulling weather…"
- `success`: soft green, single emoji + label like "✉ Sent to alex"
- `error`: soft red, "⚠ Stripe timed out"
- `confirm`: warm amber, persistent (no auto-fade), "⚠ Confirm: send email"

CSS sketch:

```css
#actionChip {
  position: fixed;
  top: calc(env(safe-area-inset-top, 0px) + 56px);
  right: 12px;
  padding: 7px 12px;
  border-radius: 999px;
  background: rgba(20, 30, 40, 0.72);
  backdrop-filter: blur(18px) saturate(140%);
  font-size: 12px;
  transform: translateX(calc(100% + 16px));
  transition: transform 0.34s cubic-bezier(0.22, 1, 0.36, 1), opacity 0.28s ease;
  opacity: 0;
}
#actionChip.visible { opacity: 1; transform: translateX(0); }
```

Server emits `tool_start` with the tool name and `tool_end` with a chip-friendly result summary (≤22 chars). Client maps tool name → emoji icon client-side; server formats the result text per tool (e.g. for a mining-status tool, return `"938 TH/s"`).

## Conversation persistence

If your existing agent persists conversation history (typical), the phone server should write to the same store. Don't build a parallel persistence layer for the phone. If your conversation manager has a `start_session` step (creating a new conversation row in the DB), call it on the first WebSocket connect. Otherwise your phone messages will be in-memory only and disappear on every restart.

## What you (the AI agent) should do now

1. Ask me the eight interview questions above, **one at a time**.
2. Echo my answers back in a short plan: "I'll add a phone server at `<path>` that talks to your existing brain in `<path>`, using `<STT>` for capture and `<TTS>` for playback. The PWA will live at `<routes>`. Risky tools will use the `await_confirmation` pattern. Caching gets `no-store` on the shell. Conversation persists via your existing `<repo>`. Ready?"
3. Wait for my "go ahead."
4. Build incrementally: server scaffold first, then PWA shell, then voice loop, then chip UI, then confirmation flow. Verify each layer works before moving to the next.
5. Apply ALL the iOS quirks above without me having to remind you. They are non-negotiable; we learned each one from real user-visible breakage.
6. Write tests for the server (auth gating, TTS endpoint shape, WebSocket message handling, tool result formatters). The PWA shell is harder to unit-test; manual verification on a real iPhone is the source of truth.
7. Document a deploy path that includes: HTTPS reverse proxy config, environment variables (token, model API keys), and how to install the PWA on iOS (visit URL → share sheet → "Add to Home Screen").

If anything in your interview answers conflicts with the architecture above, raise it before building. The patterns here are battle-tested. Divergence should be deliberate and justified.
