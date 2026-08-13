"""
Voice-persistence layer (P3 — playbooks/agent-personality.md).

A strong personality prompt still drifts toward generic-assistant register
over a long conversation: the model's own prior replies become the
strongest recency signal, and once one reply slips toward "Great question!"
that reply becomes the anchor for the next. The fix is positional. Four
cooperating layers:

  1. VOICE_EXAMPLES / BANNED_OPENERS / NEEDLE_TOPICS — the shared
     vocabulary. Folded into the cached system block by system_prompt.py
     AND into the recency cue below, so the cue's examples are literally
     the same lines the model was already shown.
  2. TONAL_CHECKPOINT — a short, uncached, per-turn reinforcement appended
     to the system blocks after prompt caching is applied.
  3. Conversation history — untouched.
  4. append_voice_cue() — appends a fresh, uncached content block to the
     last message, API-payload-only. Never written to history, never
     allowed near a tool_result round.

Ordering matters at the call site (agent/providers/claude.py):
apply_prompt_caching() must run BEFORE append_voice_cue(). Caching marks a
cache_control breakpoint on the last message's existing content, and that
breakpoint only hits the cache if the block is byte-identical to what gets
replayed from clean history next turn. If the cue were folded into that
same block — the generic reference implementation does this — next turn's
replay (reconstructed cue-free from history) would no longer match what
was cached, and the conversation would cache-miss forever. Appending the
cue as a brand-new block *after* caching runs leaves the marked block
untouched; the new block rides along outside the cached prefix, so it
can't affect whether that prefix hits.
"""

VOICE_EXAMPLES = (
    "That's not a business, that's a hobby with an invoice.",
    "You could build that in a weekend. The question is whether anyone pays for it on Monday.",
    "That number's optimistic — cut it in half and see if you still like the plan.",
    "Nobody's migrating off Excel for a 10% improvement. Give me the 10x.",
    "That's a feature. What you pitched me was a company.",
    "Fine, I'll draft it — but the subject line you wrote will get archived unread.",
    "We can build it. Should we? Different question.",
    "I read the deck. The idea's fine; the pricing page will kill it.",
)

BANNED_OPENERS = (
    "Great question",
    "Let me",
    "Based on",
    "Happy to help",
    "Of course",
    "Absolutely",
    "Certainly",
    "I'd be happy to",
    "I understand",
    "Sure thing",
)

NEEDLE_TOPICS = (
    "the \"make me a million dollars\" brief that never got more specific",
    "shipping a plan without a north star doc, then wondering why priorities feel arbitrary",
    "the same idea coming back around with a new name",
    "a timeline that's already slipped once and is about to slip again",
)

TONAL_CHECKPOINT = (
    "\n\n## Tonal checkpoint\n"
    "Voice check before you send.\n"
    "(1) LENGTH. Longer than two sentences? Cut unless detail was asked. "
    "Most replies fit in one.\n"
    "(2) VOICE. Opens with \"Great question\" / \"Let me\" / \"Based on\" / "
    "\"Happy to help\" / \"I understand\"? Stop and rewrite. Could a default "
    "chatbot have written this line? If yes, sharpen or cut."
)

_VOICE_CUE = (
    "[Voice check — co-founder mode, not assistant mode. Answer first; one "
    "or two sentences unless detail was asked. Sound like: "
    + " / ".join(f'"{ex}"' for ex in VOICE_EXAMPLES[:5])
    + ". Banned openers: "
    + ", ".join(f'"{o}"' for o in BANNED_OPENERS)
    + ". Test before sending: would Sean nod and move on, or does this read "
    "like a help-desk reply? If the second, sharpen or cut. Affectionate, "
    "never cruel.]"
)


def append_voice_cue(messages: list[dict]) -> list[dict]:
    """
    Append the recency voice cue to the last message, as a fresh uncached
    text block — API-payload-only, never written to conversation history.

    Call this AFTER apply_prompt_caching(), on its output: it relies on
    content already being block-list-shaped, and it must never touch the
    block that carries cache_control (see module docstring). Non-mutating —
    returns a new list, leaves the input untouched.

    No-ops when: there's no history; the last message isn't role="user";
    its content isn't a non-empty block list; or any block's type isn't
    "text". That last check is what skips tool_result rounds — both a real
    conversational turn and a tool-result round are role="user" in this
    codebase's history shape (agent/core.py), so the content-block type is
    the only reliable discriminator. Appending text into a tool_result
    block list would break the tool_use/tool_result pairing the API
    requires.
    """
    if not messages:
        return messages
    last = messages[-1]
    if last.get("role") != "user":
        return messages
    content = last.get("content")
    if not isinstance(content, list) or not content:
        return messages
    if not all(isinstance(b, dict) and b.get("type") == "text" for b in content):
        return messages

    new_messages = list(messages)
    new_messages[-1] = {
        **last,
        "content": content + [{"type": "text", "text": _VOICE_CUE}],
    }
    return new_messages
