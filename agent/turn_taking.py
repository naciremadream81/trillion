"""
Sign-off detection — knowing when NOT to reply.

Runs on the transcript BEFORE any model call. If the user is just wrapping up
("thanks", "got it", "sounds good"), the assistant stays silent instead of
manufacturing one more reply and grabbing the last word. A goodbye then costs
nothing: no model call, no tokens.

It is deliberately **conservative**, because the failure modes aren't symmetric:
going silent when the person wanted a reply looks broken, while occasionally
replying to a borderline goodbye is just the mild old behavior. So it only
returns True when confident, and everything ambiguous falls through to a reply.

Tuning is a one-line change: move a phrase into the right set below. Capture the
misses (a real goodbye that slipped through, or a false silence) as test cases.
"""

from __future__ import annotations

import re

# A strong sign-off signal present anywhere (substring match, lowercased).
_STRONG_PHRASES = (
    "thank", "sounds good", "will do", "got it", "good night", "goodnight",
    "see you", "see ya", "right on", "that's all", "thats all", "all good",
    "no thanks", "appreciate", "cheers", "ttyl", "take care",
    "talk later", "talk to you later", "catch you later", "have a good",
)

# Strong sign-off words (exact word match).
_STRONG_WORDS = {"bye", "goodbye", "cya", "peace", "later"}

# Bare positives — only a sign-off in a very short utterance (else they lead
# into new information: "great, the meeting went well").
_BARE_POSITIVES = {
    "great", "cool", "nice", "perfect", "awesome", "ok", "okay", "sure",
    "yep", "yeah", "yup", "alright", "sweet", "gotcha", "fine", "good",
    "excellent", "lovely", "wonderful",
}

# Self-commitment ("great, I'll send that") = a sign-off, and it lifts the
# command veto. NOTE: apostrophe forms only, to avoid the "ill"/"well"
# look-alikes for "I'll"/"we'll".
_SELF_COMMIT = (
    "i'll", "we'll", "i will", "we will", "i'm going to", "i am going to",
    "let me", "i got this", "i'll take", "i'll handle",
)

# Any of these means the person wants something back → always reply.
_QUESTION_MARKERS = (
    "can you", "could you", "would you", "will you", "how about", "what about",
    "one more thing", "how do", "how can", "how does", "how would", "do you",
    "are you", "is there", "any chance", "what if", "what's", "whats", "how's",
    "hows", "tell me", "show me", "give me", "help me", "i need", "could i",
    "can i", "let's", "wait", "actually", "remind", "one more",
)

# Imperative commands → an instruction, not a farewell (unless self-commit).
_COMMAND_VERBS = {
    "send", "email", "call", "write", "draft", "schedule", "add", "create",
    "make", "find", "search", "set", "update", "delete", "remove", "book",
    "open", "run", "check", "tell", "show", "build", "fix", "look", "get",
    "pull", "give", "remind", "move", "change", "start",
}

_MAX_WORDS = 6  # real goodbyes are brief
_WORD = re.compile(r"[a-z']+")


def is_signoff(text: str, has_assistant_spoken: bool) -> bool:
    """
    True only when `text` is confidently just a sign-off and the assistant has
    already been part of the conversation. Biased hard toward False.
    """
    # Never swallow the very first thing someone says.
    if not has_assistant_spoken:
        return False

    raw = (text or "").strip()
    if not raw:
        return False

    # A question mark means they want an answer.
    if "?" in raw:
        return False

    t = raw.lower()
    words = _WORD.findall(t)
    if not words or len(words) > _MAX_WORDS:
        return False

    # Requests / questions veto.
    if any(m in t for m in _QUESTION_MARKERS):
        return False

    has_self_commit = any(sc in t for sc in _SELF_COMMIT)

    # Commands veto — unless the person is committing to do it themselves.
    if not has_self_commit and any(w in _COMMAND_VERBS for w in words):
        return False

    # A clear sign-off phrase or word.
    if any(p in t for p in _STRONG_PHRASES):
        return True
    if any(w in _STRONG_WORDS for w in words):
        return True

    # Wrap-up by self-commitment led by a positive: "great, I'll send that."
    if has_self_commit and words[0] in _BARE_POSITIVES:
        return True

    # A short, purely positive acknowledgement: "great", "cool cool", "okay".
    if len(words) <= 3 and all(w in _BARE_POSITIVES for w in words):
        return True

    return False
