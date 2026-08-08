# Personality Persistence · AI Agent Prompt

A drop-in prompt for your AI coding agent (Claude Code, Cursor, Aider, or similar) that helps it add a personality-persistence layer to your existing AI agent project. It encodes a battle-tested fix for tonal drift in long conversations and, critically, instructs the agent to **interview you first** so the implementation matches your stack, your existing personality file, and the way your messages are built.

**Why this prompt exists:** you've written a beautiful personality prompt for your agent: witty, dry, opinionated. The first reply lands. By turn five it's saying *"Great question, let me help you with that."* By turn ten it's a customer-service bot. This isn't a bug in your prompt. It's a structural property of how LLMs attend to context. The fix is positional, not motivational, and it's been re-derived in every long-running agent project. Don't re-derive it.

**How to use it:** copy everything below the horizontal rule into a fresh chat with your AI agent. Answer the interview questions when it asks, then let it build.

---

You are helping me add a personality-persistence layer to my AI agent. Treat this whole message as your brief. **Do not write any code yet.** Your first job is to interview me, echo my answers back as a short plan, and only then start building.

## What we're solving

My agent has a strong personality on turn one and slowly drifts toward generic-assistant mode (`"Great question"`, `"Let me help"`, `"Based on the information"`) over a multi-turn conversation. The personality prompt is fine. The *position* of the personality prompt is the problem. We need to make it win the recency contest, not just live at the top of the system prompt.

## The mechanic (so you understand what you're implementing)

On every turn the model attends to two things:

1. The **system prompt**: your personality file, sitting at the top of the context.
2. The **conversation history**: every prior user and assistant turn.

As the conversation grows, the assistant's *own prior outputs* become the strongest behavioral signal. They're recent, they're labeled "this is what assistant says here", and they progressively outweigh the cached personality block at the top. Once one reply slips toward generic-helpful, that reply becomes the reference point for the next, and the slide compounds.

The fix is to put a personality cue **after** the assistant's recent turns, in a position the model attends to more strongly than the system prompt, without polluting stored history. Plus a small reinforcement back in the system block, so the personality is enforced from both ends.

## Interview me first

Ask me these questions one at a time, in order. After my answers, summarize what you'll change (no code yet) and wait for my "go ahead":

1. **Where does my agent's personality currently live?** A file? An inline string? Multiple places? Show me the file or section.
2. **What language and SDK is the agent built on?** (Python + Anthropic SDK, TypeScript + OpenAI, Go + custom HTTP client, etc.) Is prompt caching turned on, and on which content blocks?
3. **Show me the function that builds the messages array sent to the model.** I need to see exactly where the API payload is assembled so I know where to inject the cue.
4. **Show me the function that builds the system prompt.** Is it a single string or a list of content blocks? Which blocks are cached and which are dynamic-per-turn?
5. **How is conversation history stored?** A `ConversationManager` class? A list in memory? A database? I need to know what I can mutate without polluting persisted history.
6. **Do you have tool use / function calling in the loop?** If yes, the cue logic needs to skip tool-result rounds (their content is a block-list, not a string). Confirm so I don't break the `tool_use` ↔ `tool_result` pairing.
7. **Paste a transcript where the drift happened.** Even three or four turns is enough to see what generic-mode looks like in your agent's voice.
8. **What's the desired voice in one sentence?** ("Dry, slightly snarky co-founder", "Warm and slightly nerdy librarian", etc.) Also, do you have any concrete one-line examples of how it should sound?

## What you'll build (target)

Four cooperating layers:

```
┌───────────────────────────────────────────────────────────┐
│  System prompt (cached)                                    │
│    ├── personality file — rule-heavy, with concrete        │
│    │     contrastive voice examples (sound like X / not Y) │
│    └── tool inventory + core knowledge                     │
├───────────────────────────────────────────────────────────┤
│  System prompt (uncached, per-turn)                        │
│    └── tonal checkpoint  ← fires every turn (threshold 1)  │
├───────────────────────────────────────────────────────────┤
│  Conversation history (user / assistant / user / ...)      │
├───────────────────────────────────────────────────────────┤
│  LAST user message (API payload only — NOT stored)         │
│    └── voice cue  ← sits AFTER all prior assistant turns,  │
│                     embeds 3–5 concrete voice examples,    │
│                     bans the customer-service openers,     │
│                     positive direction + cruelty guardrail │
└───────────────────────────────────────────────────────────┘
```

The voice cue is the load-bearing layer. The other three are reinforcement.

## Build it in four steps

Do these in order. Show me the diff after each step and wait for my "go" before moving to the next.

### Step 1: strengthen the personality file

Audit my existing personality definition. For each section, ask: is this telling the model how to *behave*, or is it just describing a vibe?

Convert vibe sections into rule-and-example sections. Specifically:

- **Replace adjective lists** ("witty, dry, snarky, sharp") with **8–15 concrete one-line voice examples** the model should sound like. One-liners are gold. They're short enough to imitate and concrete enough to anchor on. Pull examples from real transcripts if I have them.
- **Add 4–6 "not like" examples**: the customer-service phrasings the model must never produce ("Great question!", "I'd be happy to...", "Based on the information available...", "Here are three things to consider:", etc.). Contrastive examples teach voice better than abstract rules.
- **Add a "needle topics" list**: specific, harmless things the agent is allowed to gently call out about the user. Concrete topics > "be playful".
- **Keep counterweights.** If the file has a "warm when it counts" section or a "never cruel" rule, leave them alone. Cranking snark without those guardrails produces a roast bot, which is worse than a generic bot.

### Step 2: verify caching on the personality block

The personality file should sit inside a system content block with prompt caching turned on (`cache_control: { type: "ephemeral" }` for Anthropic, equivalent provider-specific config for OpenAI). The dynamic per-turn additions (current time, alerts, the tonal checkpoint from Step 4) stay uncached.

If caching isn't on the personality block yet, turn it on. The point: keeping the entire personality resident every turn is what enables this whole approach without ballooning cost.

### Step 3: add the recency voice cue (the load-bearing change)

In the function that builds the API payload, **after** the messages array is built, append a short voice-rule string to the LAST user message's content, and **do not** store this in conversation history. The cue lives in the API-bound copy only.

Properties of the cue:

- **3–5 concrete voice examples**, lifted directly from the personality file (the same one-liners). Concrete examples next to generation are the strongest priming signal.
- **6–10 banned openers** the model must never produce ("Great question", "Let me", "Based on", "Happy to help", "Of course", "Absolutely", "Certainly", "I'd be happy to", "I understand", "Sure thing").
- **Positive direction**: what TO do, not just what to avoid. ("Earn the smirk: dry observation, needle, undercut, deadpan flag.") A pure ban-list produces blandly compliant replies; a positive direction gives the model something to aim at.
- **A "test before sending" line**: ("Would the user smirk or think 'fair point'? If no, sharpen or cut.") This turns the cue into an explicit pre-send check.
- **A guardrail**: ("Affectionate, never cruel.") The dial-up needs a cruelty floor or it tips into roast-bot.
- **Skips block-list content.** If the last user message's `content` is a list (tool_result rounds), no-op. Appending text to a block list breaks the `tool_use` ↔ `tool_result` pairing.
- **Does not mutate stored history.** The function operates on the fresh dicts returned for the API call, never on the in-memory `Message` objects.

### Step 4: add a per-turn tonal checkpoint in the system prompt

In the function that builds the system prompt, append a short reinforcement block to the **dynamic (uncached) portion** of the system on every turn (threshold 1, not "after N turns").

This block lives in a different position than the cue (early-context, system-role) and reinforces the same rules from a second angle. Together with the cue, the personality is enforced from both ends of the context window.

The checkpoint should be tighter than the cue (about 100–150 tokens), covering:

- Length discipline ("longer than two sentences? cut unless detail was asked")
- Voice discipline (banned openers, "could a default chatbot have written this?")

Don't duplicate the personality file's full voice spec here. The cue carries the examples; the checkpoint carries the reminder.

### Step 5: write tests

After all four steps, add unit tests that assert:

- The cue appears in the last user message of the API payload.
- The cue does NOT appear in stored conversation history.
- The cue is skipped when the last user content is a block-list (tool_result rounds).
- The cue contains BOTH banned openers AND positive direction.
- The personality file's voice examples appear inside the cue.
- The cue preserves the cruelty guardrail ("never cruel" or "affectionate").

These are structural tests. They prove the wiring is correct. Whether the agent is *actually* funnier is something only the human can judge in a live conversation.

## Reference implementation (Python, Anthropic SDK)

```python
# personality.py
from pathlib import Path

PERSONALITY_FILE = Path(__file__).parent / "AGENT.md"


_VOICE_CUE = (
    "[Voice check — agent mode, not assistant mode. Answer first; one "
    "or two sentences unless detail was asked. EARN THE SMIRK: dry "
    "observation, needle, undercut, deadpan flag. Funny is the goal, "
    "not a bonus. Sound like \"<your one-liner 1>\" / \"<your "
    "one-liner 2>\" / \"<your one-liner 3>\" / \"<your one-liner 4>\". "
    "Banned openers: \"Great question\", \"Let me\", \"Based on\", "
    "\"Happy to help\", \"Of course\", \"Absolutely\", \"Certainly\", "
    "\"I'd be happy to\", \"I understand\". Test before sending: "
    "would the user smirk, snort, or think \"huh, fair point\"? "
    "If the line has zero edge it's a miss, even when the facts are "
    "right — rewrite or cut shorter. Bland-and-correct is still "
    "bland. Affectionate, never cruel.]"
)


def append_voice_cue(messages: list[dict]) -> list[dict]:
    """Append the voice cue to the last user-text message in the API
    payload. No-op for empty history, assistant-last messages, or
    block-list content (tool_result rounds). Mutates and returns
    ``messages``.

    IMPORTANT: only call this on the API-bound copy of messages — not
    on stored history. The cue must NOT compound across the transcript
    or the model will start treating it as text to imitate.
    """
    if not messages:
        return messages
    last = messages[-1]
    if last.get("role") != "user":
        return messages
    content = last.get("content")
    if not isinstance(content, str):
        return messages
    messages[-1] = {**last, "content": f"{content}\n\n{_VOICE_CUE}"}
    return messages


_TONAL_CHECKPOINT = (
    "\n## Tonal checkpoint\n"
    "Voice check before you send.\n"
    "(1) LENGTH. Longer than two sentences? Cut unless detail was "
    "asked. Most replies fit in one sentence.\n"
    "(2) VOICE. Opens with \"Great question\" / \"Let me\" / \"Based "
    "on\" / \"Happy to help\" / \"I understand\"? Stop and rewrite. "
    "Could a default chatbot have written this line? If yes, sharpen "
    "or cut."
)


def build_system_prompt(personality_text: str) -> list[dict]:
    """Two-block system prompt:
       - cached: personality + tools + core knowledge (changes rarely)
       - uncached: per-turn tonal checkpoint (and any other dynamic
         context like current time, alerts, etc.)
    """
    return [
        {
            "type": "text",
            "text": personality_text,
            "cache_control": {"type": "ephemeral"},
        },
        {
            "type": "text",
            "text": _TONAL_CHECKPOINT,
        },
    ]


# At your API call site:
def respond(client, conversation, personality_text: str) -> str:
    messages = conversation.get_messages_for_api()  # fresh list of dicts
    append_voice_cue(messages)                       # API-only mutation
    system = build_system_prompt(personality_text)

    response = client.messages.create(
        model="claude-opus-4-7",
        system=system,
        messages=messages,
        max_tokens=2048,
    )
    text = "".join(b.text for b in response.content if b.type == "text")
    conversation.add_assistant_message(text)         # stored cleanly
    return text
```

## Tuning knobs

If the agent still drifts toward generic mode:

- **Add more examples to the cue.** Two extra concrete voice patterns next to generation moves the needle more than rewriting the personality file. Recency wins.
- **Drop your checkpoint threshold to 1.** If you're using a "fire after N turns" gate on the system-block checkpoint, set it to 1. The first reply sets the trajectory the rest of the session anchors on; you want reinforcement from turn 1.
- **Raise temperature slightly.** A flat-but-correct reply at temp 0.3 reads more "professional" (read: blander) than the same content at temp 0.8. Try 0.7+.
- **Audit the personality file for hedge words.** "Slightly", "lightly", "occasionally", "aim for": every one of those gives the model permission to dial down. Replace hedged directives with imperative ones.

If the agent goes too far (mean instead of dry):

- The "affectionate, never cruel" line in the cue is the guardrail. Make sure it's literally present in the cue string.
- Don't crank "be funny" without preserving the counterweights ("warm when it counts", "deadpan, never try hard"). Forced humor reads as desperate, which is the opposite of dry.

## What NOT to do

- **Don't write the cue into stored conversation history.** Future turns will see the cue 30+ times and the model will start parroting the cue's format (brackets, lists, meta-commentary). The cue must be invisible to history. API-bound only.
- **Don't append the cue to tool_result messages.** Their `content` is a block-list, not a string. Skip those rounds. The cue on the original user message is enough; the model carries the personality through the tool loop on its own.
- **Don't try to fix drift by re-injecting the personality file mid-conversation as a user or system reminder.** That works once and then becomes noise. The recency cue solves the same problem in a way that scales.
- **Don't crank temperature without fixing position first.** High temp without the right cue gives you chaotic-bland, not dry-witty.
- **Don't make the cue paragraph-long.** It rides on every user message. Keep it dense: concrete examples, banned phrases, one-line guardrails. If it grows to multiple paragraphs you've turned it into a second personality file.

## Why this works

The recency cue beats the system prompt's position. The cached personality block keeps token cost flat across long sessions. The per-turn tonal checkpoint reinforces from a second position so the model is hemmed in from both ends. The concrete examples next to generation give the model something to imitate, not just abstractions to interpret. And the cruelty guardrail keeps the dial-up from tipping into roast-bot territory.

You are not making the agent more disciplined. You're moving the rules to where the model actually attends.

## Verify before declaring victory

Before you tell me the work is done:

1. Run the unit tests. All six structural assertions must pass.
2. Print the full API payload for a sample turn (system blocks + messages array) and read it yourself. Confirm: cue appears once, on the last user message, with concrete examples and the cruelty guardrail; checkpoint appears in the dynamic system block; personality file appears in the cached system block.
3. Ask me to run a 10-turn conversation and read me the transcript. Drift, if any, will show up as openers from the banned list. Flag them.

Then tell me what's left, what you decided, and what knob to turn next if it's still not landing.
