"""
ToolRegistry — holds tools, exposes their schemas, dispatches calls.

The conversation core (agent/core.py) talks only to this: `schemas()` feeds
the provider, `run(tool_call)` executes. Adding a capability = write a tool
and register it here; the core never changes.

`run()` is also where Tier 6's untrusted-content pass happens (P2): every
tool result gets scrubbed via agent/safety/untrusted.py before it reaches
the model, unless the tool declares `trusted_output = True`. Doing it here
rather than in agent/core.py means Factory-spawned specialists — which run
through a `subset()` of this same registry — inherit the scrub automatically,
since the logic lives in the class method itself.

Same reasoning applies to the §3.2 per-tool anomaly gate: it's checked
before `tool.run()` is even called, so a runaway loop is stopped here,
process-wide, rather than per spawned agent.
"""

from __future__ import annotations

from typing import Callable

from ..providers.base import ToolCall
from ..safety.untrusted import TOOL_RESULT_MAX_LENGTH, clean_for_prompt, flag_injection_attempt
from ..security.anomaly import AnomalyGate
from .base import BaseTool

AuditSink = Callable[..., None]


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, BaseTool] = {}
        self._audit_sink: AuditSink | None = None
        self._anomaly_gate = AnomalyGate()

    def set_audit_sink(self, sink: AuditSink | None) -> None:
        """Wire a callable matching SafetyRepo.log()'s signature
        (event, *, tool_name=None, action_id=None, detail=None) -> None.
        Best-effort: if unset, sanitization/flagging still runs, just without
        an audit trail."""
        self._audit_sink = sink

    def register(self, tool: BaseTool) -> None:
        self._tools[tool.name] = tool

    def unregister(self, name: str) -> None:
        """No-op if the name isn't registered — callers (e.g. the Factory's
        RegistryWatcher) diff against reality and shouldn't need to guard."""
        self._tools.pop(name, None)

    def names(self) -> list[str]:
        return list(self._tools)

    def get(self, name: str) -> BaseTool | None:
        """The registered tool, or None. Tier 6's gate reads `risk` off it."""
        return self._tools.get(name)

    def subset(self, names: list[str]) -> "ToolRegistry":
        """
        A new registry containing only the named tools that currently exist
        here. Used by the Agent Factory to build a spawned agent's tool
        registry from its tool_allowlist — silently drops names that don't
        (or no longer) resolve rather than erroring, since an allowlist
        computed at draft time may reference a tool since removed.
        """
        result = ToolRegistry()
        result._audit_sink = self._audit_sink
        result._anomaly_gate = self._anomaly_gate
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

        blocked = self._anomaly_gate.check(tool_call.name)
        if blocked is not None:
            if self._audit_sink is not None:
                self._audit_sink(
                    "anomaly_gate_blocked",
                    tool_name=tool.name,
                    detail=blocked,
                )
            return (
                f"[anomaly_gate_blocked: {tool.name} has hit its rate cap "
                f"({blocked['count']}/{blocked['limit']} calls in "
                f"{blocked['window_seconds']:.0f}s). Try again later.]"
            )

        result = await tool.run(**(tool_call.arguments or {}))
        if tool.trusted_output:
            return result

        flagged = flag_injection_attempt(result)
        if flagged and self._audit_sink is not None:
            self._audit_sink(
                "injection_flagged",
                tool_name=tool.name,
                detail={"pattern": flagged},
            )
        return clean_for_prompt(result, max_length=TOOL_RESULT_MAX_LENGTH)


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

    from .web_search import WebSearchTool, resolve_search_provider

    resolved = resolve_search_provider(settings)
    if resolved is not None:
        provider, api_key = resolved
        registry.register(
            WebSearchTool(provider, api_key, firecrawl_base_url=settings.firecrawl_base_url)
        )

    # Tier 2's first three capabilities — unconditional, unlike the
    # third-party integrations above: these are core to what Trillion is,
    # not optional connections that vanish when unconfigured.
    from .email import DraftEmailTool
    from .notes import SearchNotesTool

    # Mining tracker — registered only when a wallet is configured, same
    # posture as the analytics and search tools above.
    if getattr(settings, "mining_wallet", ""):
        from ..mining.storage import MiningRepo
        from .mining import QueryMiningTool

        registry.register(QueryMiningTool(MiningRepo(), settings.mining_wallet))

    # Design agent — off by default and additionally gated on the claude CLI
    # actually being installed, since generate_mockup is useless without it
    # and a registered-but-broken tool is worse than an absent one.
    if getattr(settings, "design_agent_enabled", False):
        from ..design.budget import DesignBudget
        from ..design.claude_code_runner import claude_binary
        from .design import GenerateMockupTool, ListDesignProjectsTool

        if claude_binary():
            registry.register(
                GenerateMockupTool(
                    settings,
                    budget=DesignBudget(
                        per_dispatch_usd=settings.design_per_dispatch_usd,
                        daily_usd=settings.design_daily_usd,
                    ),
                )
            )
            registry.register(ListDesignProjectsTool(settings))

            # Tier 5 — only when it can actually run. Same posture as the
            # design agent itself: absent beats registered-and-broken.
            from ..design.image_gen import is_available as image_gen_available

            if image_gen_available():
                from .design import GenerateImageTool

                registry.register(GenerateImageTool(settings))
        else:
            print(
                "Design agent enabled but the `claude` CLI is not on PATH; "
                "design tools not registered."
            )

    registry.register(SearchNotesTool(settings.notes_index_path))
    registry.register(DraftEmailTool())

    return registry
