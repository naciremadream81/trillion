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

    def unregister(self, name: str) -> None:
        """No-op if the name isn't registered — callers (e.g. the Factory's
        RegistryWatcher) diff against reality and shouldn't need to guard."""
        self._tools.pop(name, None)

    def names(self) -> list[str]:
        return list(self._tools)

    def subset(self, names: list[str]) -> "ToolRegistry":
        """
        A new registry containing only the named tools that currently exist
        here. Used by the Agent Factory to build a spawned agent's tool
        registry from its tool_allowlist — silently drops names that don't
        (or no longer) resolve rather than erroring, since an allowlist
        computed at draft time may reference a tool since removed.
        """
        result = ToolRegistry()
        for name in names:
            tool = self._tools.get(name)
            if tool is not None:
                result.register(tool)
        return result

    def schemas(self) -> list[dict]:
        """Tool schemas in the provider's expected shape (Anthropic format)."""
        return [t.definition() for t in self._tools.values()]

    def factory_allowed_names(self) -> set[str]:
        """Names of tools the Agent Factory (agent/factory/) may grant to a
        spawned specialist — those with BaseTool.factory_allowed set."""
        return {name for name, t in self._tools.items() if t.factory_allowed}

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
