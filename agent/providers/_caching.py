"""
Anthropic prompt-caching helper.

Two cache breakpoints per request:
  1. On the system prompt — caches the stable prefix (tools + system) so the
     personality, schema docs, and tool schemas aren't re-read every turn.
  2. On the newest message — caches the conversation prefix incrementally, so
     a long chat stays about as fast at turn 15 as at turn 2 instead of
     creeping slower as history grows.

The stable prefix must be byte-identical turn to turn for the cache to hit, so
keep anything dynamic (timestamps, etc.) out of the system prompt and at the
end of the message list.

Pure dict manipulation, and crucially **non-mutating**: the caller's history is
reused next turn, so we copy before adding cache_control. Mutating it would
leave stale breakpoints on old messages and blow the 4-breakpoint limit.
"""

from __future__ import annotations

_EPHEMERAL = {"type": "ephemeral"}


def apply_prompt_caching(system: str, messages: list[dict]) -> tuple[list, list]:
    """
    Return (system_blocks, messages) with cache_control breakpoints added.
    Does not mutate the input `messages` or their dicts.
    """
    system_blocks = [
        {"type": "text", "text": system, "cache_control": _EPHEMERAL}
    ]

    cached = list(messages)
    if cached:
        last = dict(cached[-1])  # shallow copy of the last message dict
        content = last.get("content")

        if isinstance(content, str):
            last["content"] = [
                {"type": "text", "text": content, "cache_control": _EPHEMERAL}
            ]
        elif isinstance(content, list) and content:
            blocks = [dict(b) if isinstance(b, dict) else b for b in content]
            if isinstance(blocks[-1], dict):
                blocks[-1] = {**blocks[-1], "cache_control": _EPHEMERAL}
            last["content"] = blocks

        cached[-1] = last

    return system_blocks, cached
