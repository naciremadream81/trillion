"""
Sanitization for user-supplied text before it's inlined into any Factory-
generated LLM prompt (role descriptions, reject feedback, etc.).

Two separate concerns, kept as two functions because they fail differently:
  - clean_for_prompt(): mechanical scrub (control chars, length cap) —
    always safe to apply, never raises.
  - flag_injection_attempt(): a heuristic that flags likely prompt-injection
    phrasing ("ignore previous instructions", "system:", ...) — a hint the
    caller can act on, not a hard block, since it's regex-based and easy to
    both false-positive and evade. Combined with clean_for_prompt() and the
    rule that generated prompts paraphrase rather than quote raw input
    verbatim, this is defense in depth, not a guarantee.
"""

from __future__ import annotations

import re

MAX_LENGTH = 2000

# Control characters except common whitespace (\t \n \r).
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

# Loose patterns for the most common injection phrasing. Case-insensitive.
_INJECTION_PATTERNS = [
    re.compile(r"ignore\s+(all\s+)?(previous|prior|above)\s+instructions", re.I),
    re.compile(r"disregard\s+(all\s+)?(previous|prior|above)\s+instructions", re.I),
    re.compile(r"^\s*system\s*:", re.I | re.M),
    re.compile(r"you\s+are\s+now\s+(a|an)\b", re.I),
    re.compile(r"new\s+instructions\s*:", re.I),
    re.compile(r"forget\s+(everything|all)\s+(you\s+)?(know|were\s+told)", re.I),
]


def clean_for_prompt(text: str) -> str:
    """Strip control characters and cap length. Never raises."""
    if not text:
        return ""
    cleaned = _CONTROL_CHARS.sub("", text)
    cleaned = cleaned.strip()
    return cleaned[:MAX_LENGTH]


def flag_injection_attempt(text: str) -> str | None:
    """Return the matched pattern (as a string) if text looks like a prompt
    injection attempt, else None."""
    for pattern in _INJECTION_PATTERNS:
        match = pattern.search(text)
        if match:
            return match.group(0)
    return None
