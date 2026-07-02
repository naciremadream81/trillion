"""
ToolRegistry — holds tools, exposes their schemas, dispatches calls.

The conversation core (agent/core.py) talks only to this: `schemas()` feeds
the provider, `run(tool_call)` executes. Adding a capability = write a tool
and register it here; the core never changes.
"""

from __future__ import annotations

from ..providers.base import ToolCall
from .base import BaseTool


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, BaseTool] = {}

    def register(self, tool: BaseTool) -> None:
        self._tools[tool.name] = tool

    def names(self) -> list[str]:
        return list(self._tools)

    def schemas(self) -> list[dict]:
        """Tool schemas in the provider's expected shape (Anthropic format)."""
        return [t.definition() for t in self._tools.values()]

    async def run(self, tool_call: ToolCall) -> str:
        tool = self._tools.get(tool_call.name)
        if tool is None:
            return f"[unknown tool: {tool_call.name}]"
        return await tool.run(**(tool_call.arguments or {}))


def build_registry(settings) -> ToolRegistry:
    """
    Construct the registry from settings. Each tool is registered only when its
    dependency (a connection string) is configured, so an unset DB simply means
    the tool isn't offered.
    """
    registry = ToolRegistry()

    if settings.supabase_analytics_url:
        from .analytics_tool import QueryAnalyticsTool

        registry.register(QueryAnalyticsTool(settings.supabase_analytics_url))

    return registry
