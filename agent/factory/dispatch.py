"""
Hot-reload dispatch layer: turns spawned_agents rows into live, callable
sub-agents without restarting the process.

ConfigDrivenAgent wraps one spawned_agents row as a scratch Agent (its own
history, a restricted ToolRegistry built via subset(tool_allowlist), and the
row's system_prompt in place of Trillion's own). DispatchTool exposes that as
a `dispatch_to_<slug>` tool so Trillion's main Agent can call it like any
other tool.

RegistryWatcher keeps the live ToolRegistry in sync with spawned_agents by
polling — main.py and serve.py are separate processes with no shared memory
and sqlite has no LISTEN/NOTIFY, so periodic diff-and-reconcile is the
simplest correct option. sync_once() is also called directly right after a
CLI /approve for immediate UX feedback, rather than waiting a full poll
interval.
"""

from __future__ import annotations

import asyncio
import logging

from ..core import Agent
from .storage import FactoryRepo

logger = logging.getLogger(__name__)

DISPATCH_PREFIX = "dispatch_to_"


def dispatch_tool_name(slug: str) -> str:
    return DISPATCH_PREFIX + slug.replace("-", "_")


class ConfigDrivenAgent:
    """A spawned specialist, fully specified by a spawned_agents row."""

    def __init__(self, row: dict, provider, base_registry) -> None:
        self.slug = row["slug"]
        self.name = row["name"]
        restricted_registry = (
            base_registry.subset(row["tool_allowlist"]) if base_registry else None
        )
        self._agent = Agent(provider=provider, tool_registry=restricted_registry)
        self._agent.system = row["system_prompt"]

    async def run(self, message: str) -> str:
        reply = ""
        async for chunk in self._agent.turn(message):
            reply += chunk
        return reply


class DispatchTool:
    """
    Exposes a ConfigDrivenAgent as a tool Trillion's main Agent can call.
    Not a BaseTool subclass at import time to avoid a hard dependency loop —
    it satisfies the same duck-typed interface ToolRegistry expects
    (name, description, input_schema, definition(), run()).
    """

    factory_allowed = False  # a spawned agent must never dispatch to another

    def __init__(self, row: dict, provider, base_registry) -> None:
        self.name = dispatch_tool_name(row["slug"])
        self.description = (
            f"Delegate to the '{row['name']}' specialist agent. {row['system_prompt'][:200]}"
        )
        self.input_schema = {
            "type": "object",
            "properties": {"message": {"type": "string", "description": "What to ask the specialist."}},
            "required": ["message"],
        }
        self._sub_agent = ConfigDrivenAgent(row, provider, base_registry)

    def definition(self) -> dict:
        return {"name": self.name, "description": self.description, "input_schema": self.input_schema}

    async def run(self, **kwargs) -> str:
        message = kwargs.get("message", "")
        try:
            return await self._sub_agent.run(message)
        except Exception as e:  # noqa: BLE001 — never crash the caller's tool round-trip
            return f"[dispatch to '{self._sub_agent.slug}' failed: {e}]"


class RegistryWatcher:
    """
    Polls FactoryRepo.list_active_agents() and reconciles dispatch_to_<slug>
    tools into the live registry — registers new/changed agents, unregisters
    ones that were disabled or removed.
    """

    def __init__(self, repo: FactoryRepo, provider, registry, base_registry=None) -> None:
        self.repo = repo
        self.provider = provider
        self.registry = registry
        # The registry a spawned agent's own tools are drawn from — defaults
        # to the live registry, but callers may pass a frozen snapshot so a
        # spawned agent can't be granted dispatch tools to other specialists.
        self.base_registry = base_registry if base_registry is not None else registry
        self._known: dict[str, str] = {}  # slug -> system_prompt, to detect changes

    def sync_once(self) -> None:
        active = self.repo.list_active_agents()
        active_by_slug = {row["slug"]: row for row in active}

        # Unregister anything we previously registered that's no longer active.
        for slug in list(self._known):
            if slug not in active_by_slug:
                self.registry.unregister(dispatch_tool_name(slug))
                del self._known[slug]

        # Register new agents, or re-register ones whose prompt/tools changed.
        for slug, row in active_by_slug.items():
            fingerprint = row["system_prompt"] + "|" + ",".join(row["tool_allowlist"])
            if self._known.get(slug) == fingerprint:
                continue
            tool = DispatchTool(row, self.provider, self.base_registry)
            self.registry.register(tool)
            self._known[slug] = fingerprint

    async def run_forever(self, poll_interval: float = 30.0) -> None:
        while True:
            try:
                self.sync_once()
            except Exception:  # noqa: BLE001 — a broken poll must never kill the watcher
                logger.exception("RegistryWatcher.sync_once failed")
            await asyncio.sleep(poll_interval)
