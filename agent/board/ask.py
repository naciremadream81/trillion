"""
The `ask_model` adapter the board's calls go through.

Every model call in agent/board/ takes an `ask_model(system, prompt,
max_tokens) -> str` rather than a provider, for one reason: the guards in
meeting.py — the unanimity floor, the prose/verdict check, the hostile-input
coercion — are the parts most worth testing, and they must be testable
without a network, an API key, or a model. A provider threaded through those
modules would have made every one of those tests an integration test.

This is the one place that knows about Agent and a provider.
"""

from __future__ import annotations

from ..core import Agent
from ..tools.registry import ToolRegistry


def make_ask_model(provider):
    """
    Build an ask_model bound to `provider`.

    Each call is a FRESH Agent with an empty tool registry and no history.
    That is the isolation Tier 4 requires made concrete: a shared Agent would
    accumulate one seat's answer into the next seat's context, and the seats
    would converge on whoever spoke first — which is exactly the failure a
    fan-out costs money to avoid. No tools, because an advisor reasoning from
    a dossier has nothing to look up; giving a seat web_search would let it
    answer from the open web instead of from its doctrine.
    """

    async def ask_model(system: str = "", prompt: str = "", max_tokens: int = 0) -> str:
        agent = Agent(provider=provider, tool_registry=ToolRegistry())
        if system:
            agent.system = system
        reply = ""
        async for chunk in agent.turn(prompt):
            reply += chunk
        return reply

    return ask_model
