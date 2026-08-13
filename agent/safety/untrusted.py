"""
Sanitization for text originating outside Trillion's own reasoning — tool
results, and (promoted from `agent/factory/sanitize.py`) user-supplied text
inlined into a Factory-generated prompt (role descriptions, reject feedback).

Two separate concerns, kept as two functions because they fail differently:
  - clean_for_prompt(): mechanical scrub (control chars, length cap) —
    always safe to apply, never raises.
  - flag_injection_attempt(): a heuristic that flags likely prompt-injection
    phrasing ("ignore previous instructions", "system:", ...) — a hint the
    caller can act on, not a hard block, since it's regex-based and easy to
    both false-positive and evade. Combined with clean_for_prompt() and the
    rule that generated prompts paraphrase rather than quote raw input
    verbatim, this is defense in depth, not a guarantee.

Applied at `ToolRegistry.run()` (agent/tools/registry.py) so every tool
result is scrubbed before it reaches the model — including results seen by
Factory-spawned specialists, since they run through the same registry via
`subset()`. `MAX_LENGTH` (2000) is sized for short user-supplied fields like
Factory role descriptions; tool results get the much larger
`TOOL_RESULT_MAX_LENGTH` since tools like web_search already self-truncate
at their own, more generous limit — this is a backstop, not the primary cap.
"""

from __future__ import annotations

import re

MAX_LENGTH = 2000
TOOL_RESULT_MAX_LENGTH = 50_000

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


def clean_for_prompt(text: str, max_length: int = MAX_LENGTH) -> str:
    """Strip control characters and cap length. Never raises."""
    if not text:
        return ""
    cleaned = _CONTROL_CHARS.sub("", text)
    cleaned = cleaned.strip()
    return cleaned[:max_length]


def flag_injection_attempt(text: str) -> str | None:
    """Return the matched pattern (as a string) if text looks like a prompt
    injection attempt, else None."""
    for pattern in _INJECTION_PATTERNS:
        match = pattern.search(text)
        if match:
            return match.group(0)
    return None
