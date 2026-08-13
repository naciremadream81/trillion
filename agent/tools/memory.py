"""
remember_fact / forget_fact — Tier 4's write path onto the memory store.

Both close over an Agent instance's update_memory() bound method rather than
being registered through build_registry(), the same pattern
agent/tools/confirm.py uses for confirm_action: the tool needs to reach back
into *this* agent's running system prompt, so it's constructed inside
Agent.__init__ where `self` exists.

remember_fact is LOW risk (cheap, reversible — forgetting a wrong fact is one
more tool call away). forget_fact deletes data and is named in
agent/safety/risk.py's HARDLINE_TOOLS, so it is gated in every confirmation
mode regardless of what it declares here; risk = HARDLINE on the class is
belt-and-braces documentation, not what actually enforces the gate.
"""

from __future__ import annotations

from typing import Callable

from ..memory import DEFAULT_MEMORY_PATH, append_fact, remove_fact
from ..safety.risk import HARDLINE, LOW
from .base import BaseTool


class RememberFactTool(BaseTool):
    name = "remember_fact"
    description = (
        "Save a durable fact about Sean or the project to memory, so future "
        "conversations start already knowing it. One plain statement per "
        "call, e.g. 'Sean prefers dry, direct answers over hedging.' "
        "Calling this again with a fact that's already saved is a no-op."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "fact": {
                "type": "string",
                "description": "The fact to remember, as a single plain statement.",
            }
        },
        "required": ["fact"],
    }

    risk = LOW

    def __init__(self, path: str, on_change: Callable[[list[str]], None]) -> None:
        self.path = path
        self.on_change = on_change

    async def run(self, **kwargs) -> str:
        fact = str(kwargs.get("fact", "")).strip()
        if not fact:
            return "[remember_fact needs a non-empty fact to save.]"
        facts = append_fact(self.path or DEFAULT_MEMORY_PATH, fact)
        self.on_change(facts)
        return f"Remembered: {fact}"


class ForgetFactTool(BaseTool):
    name = "forget_fact"
    description = (
        "Remove a previously remembered fact from memory. Pass the fact "
        "exactly as it's stored — this is destructive and requires an exact "
        "match, no partial or fuzzy matching."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "fact": {
                "type": "string",
                "description": "The exact fact text to remove.",
            }
        },
        "required": ["fact"],
    }

    # Belt-and-braces: HARDLINE_TOOLS already gates "forget_fact" by name
    # regardless of this declaration.
    risk = HARDLINE

    def __init__(self, path: str, on_change: Callable[[list[str]], None]) -> None:
        self.path = path
        self.on_change = on_change

    async def run(self, **kwargs) -> str:
        fact = str(kwargs.get("fact", "")).strip()
        if not fact:
            return "[forget_fact needs a non-empty fact to remove.]"
        removed, facts = remove_fact(self.path or DEFAULT_MEMORY_PATH, fact)
        if not removed:
            return f"[No exact match for that fact — nothing removed: {fact!r}]"
        self.on_change(facts)
        return f"Forgot: {fact}"
