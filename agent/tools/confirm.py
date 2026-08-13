"""
confirm_action — the tool the model calls once Sean has said yes.

The one piece of Tier 6 the model can reach directly. It carries no arguments
beyond the action id, because the action's arguments were frozen when the gate
parked it: there is deliberately nothing here the model can use to change what
gets run.

It reads the conversation history through a callable rather than holding a
reference to the Agent, so the tool can be constructed before the Agent exists
(both adapters build the registry first) and so the tool never gains the
ability to *modify* history — only to look at how long it is and what kind of
turns are in it.
"""

from __future__ import annotations

from typing import Callable

from ..safety.risk import READ_ONLY
from .base import BaseTool


class ConfirmActionTool(BaseTool):
    name = "confirm_action"
    description = (
        "Execute an action that was parked for Sean's confirmation, after he "
        "has explicitly agreed to it in his own message. Pass the action_id "
        "from the [CONFIRMATION REQUIRED] notice. The action runs with exactly "
        "the arguments that were shown to Sean — you cannot change them here. "
        "Only call this once he has actually said yes; calling it without his "
        "agreement is refused and logged."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "action_id": {
                "type": "integer",
                "description": "The id from the [CONFIRMATION REQUIRED] notice.",
            }
        },
        "required": ["action_id"],
    }

    # A spawned specialist must never be able to approve the main agent's
    # parked actions — its history is not Sean's conversation.
    factory_allowed = False

    # Approving is not itself an action; the thing being approved is. Gating
    # this would make every pending action permanently unapprovable, which is
    # why risk.py also lists it in NEVER_GATED — belt and braces.
    risk = READ_ONLY
    requires_confirmation = False

    def __init__(self, gate, history_provider: Callable[[], list[dict]]) -> None:
        self.gate = gate
        self.history_provider = history_provider

    async def run(self, **kwargs) -> str:
        raw = kwargs.get("action_id")
        try:
            action_id = int(raw)
        except (TypeError, ValueError):
            return (
                f"[confirm_action needs a numeric action_id; got {raw!r}. "
                "Use the id from the [CONFIRMATION REQUIRED] notice.]"
            )
        return await self.gate.confirm(action_id, self.history_provider())
