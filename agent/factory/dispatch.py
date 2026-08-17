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
from ..safety.risk import LOW
from .storage import FactoryRepo

logger = logging.getLogger(__name__)

DISPATCH_PREFIX = "dispatch_to_"


def dispatch_tool_name(slug: str) -> str:
    return DISPATCH_PREFIX + slug.replace("-", "_")


class DispatchActivity:
    """
    Tracks which spawned specialists are currently mid-dispatch, and how
    many dispatches each has ever started.

    Read by serve.py's GET /api/agents so the browser's sub-agent
    constellation (cosmic-orb-ui Tier 5) can show a real "working" pulse
    instead of a fabricated one. In-memory and per-process by design, same
    posture as everything else here: a specialist mid-run when the process
    restarts just reverts to idle, which is correct — the run itself didn't
    survive either.

    Two things a plain "currently active" set got wrong (both caught by
    review on the PR that introduced this):

    - Two overlapping dispatches to the same slug (two browser tabs, or a
      specialist Trillion calls twice in one turn) collapsed into one set
      entry; the first call to finish discarded it while the second was
      still running, so /api/agents reported idle mid-dispatch. Fixed by
      counting active dispatches per slug instead of a boolean membership.
    - A dispatch short enough to start and finish between two ~5s browser
      polls (a short model response is routine) was invisible: `working`
      sampled at poll time never observed `True`, and the browser's
      dispatch-beam animation never fired for it at all. A plain "is it
      active right now" signal can't fix that — it needs something that
      stays observable until acknowledged. total_dispatches is that: a
      monotonically increasing per-slug counter the browser diffs against
      what it last saw, so even a dispatch it never caught mid-flight still
      shows up as "the count went up" on the next poll.
    """

    def __init__(self) -> None:
        self._active_count: dict[str, int] = {}
        self._total_dispatches: dict[str, int] = {}

    def mark_started(self, slug: str) -> None:
        self._active_count[slug] = self._active_count.get(slug, 0) + 1
        self._total_dispatches[slug] = self._total_dispatches.get(slug, 0) + 1

    def mark_finished(self, slug: str) -> None:
        remaining = self._active_count.get(slug, 0) - 1
        if remaining > 0:
            self._active_count[slug] = remaining
        else:
            self._active_count.pop(slug, None)

    def snapshot(self) -> set[str]:
        return set(self._active_count)

    def total_dispatches(self, slug: str) -> int:
        return self._total_dispatches.get(slug, 0)


# Module-level singleton, not per-DispatchTool: serve.py's endpoint has no
# reference to any one DispatchTool instance, only to "which slugs are
# active right now" — the same shape FactoryRepo.list_active_agents() and
# RegistryWatcher already use for this process.
_activity = DispatchActivity()


def get_dispatch_activity() -> DispatchActivity:
    return _activity


class ConfigDrivenAgent:
    """A spawned specialist, fully specified by a spawned_agents row."""

    def __init__(self, row: dict, provider, base_registry) -> None:
        self.slug = row["slug"]
        self.name = row["name"]
        restricted_registry = (
            base_registry.subset(row["tool_allowlist"]) if base_registry else None
        )
        # No gate on a spawned specialist, by construction rather than by
        # oversight: its allowlist is intersected with factory_allowed at mint
        # time, and every tool that isn't read-only is factory_allowed = False.
        # A specialist therefore cannot reach a gated tool to begin with — and
        # it must not be able to approve one either, since its history isn't
        # Sean's conversation. Tier 6's untrusted-content pass belongs at the
        # registry for exactly this reason: that one *does* need to reach here.
        self._agent = Agent(
            provider=provider,
            tool_registry=restricted_registry,
            system_prompt_override=row["system_prompt"],
        )

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

    # Tier 6: LOW, not CONSEQUENTIAL — deliberately. This agent's approval gate
    # is at mint time: Sean already said yes to /approve before the specialist
    # existed, and its own tool_allowlist is an intersection of what he allowed.
    # Re-confirming every message to an agent he approved would be noise, and
    # noisy gates get switched off. What the specialist *does* is still gated,
    # because its tools carry their own tiers.
    risk = LOW
    requires_confirmation = None

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
        activity = get_dispatch_activity()
        activity.mark_started(self._sub_agent.slug)
        try:
            return await self._sub_agent.run(message)
        except Exception as e:  # noqa: BLE001 — never crash the caller's tool round-trip
            return f"[dispatch to '{self._sub_agent.slug}' failed: {e}]"
        finally:
            activity.mark_finished(self._sub_agent.slug)


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
