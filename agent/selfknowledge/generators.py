"""
Generators for context/self/trillion.md's AUTO/SLIM blocks.

Both generators are DERIVED, not hand-maintained:

- generate_capabilities() reads straight off a live ToolRegistry
  (agent/tools/registry.py's build_registry()), so it lists exactly the
  tools actually wired up for the settings passed in — not a hypothetical
  full list.
- probe_config_gating() differentially toggles every agent.config.Settings
  field and diffs which tool names build_registry() produces, so "what
  config turns on what" can never drift from the wiring in registry.py: it's
  reading the same code that runs in production, not a parallel list of
  claims about it.

Numeric fields (int, float, float | None) aren't probed — none of them gate
tool *registration* today (they gate caps, cadences, budgets), and there's
no single "toggled" value that's meaningfully different for a float without
domain knowledge the prober doesn't have.
"""

from __future__ import annotations

import dataclasses

from ..config import Settings
from ..tools.registry import ToolRegistry, build_registry

SLIM_CHAR_BUDGET = 1800

# One representative "on" value per probeable field type. Compared against
# whatever the baseline Settings() already holds for that field, skipping
# probes where baseline already equals the toggle value.
_TOGGLE_VALUES: dict[str, object] = {
    "str": "probe-value",
    "bool": True,
    "list[str]": ["probe-item"],
}


@dataclasses.dataclass(frozen=True)
class ConfigGate:
    field: str
    gained: tuple[str, ...]
    lost: tuple[str, ...]


def generate_capabilities(registry: ToolRegistry) -> str:
    """Markdown table: Tool / Risk tier / Description, sourced from a live
    registry rather than a hand-maintained list."""
    names = sorted(registry.names())
    lines = ["| Tool | Risk tier | Description |", "| --- | --- | --- |"]
    for name in names:
        tool = registry.get(name)
        description = tool.description.strip().splitlines()[0] if tool.description.strip() else ""
        lines.append(f"| `{tool.name}` | {tool.risk} | {description} |")
    return "\n".join(lines)


def probe_config_gating(settings_factory=Settings, build_registry_fn=build_registry) -> list[ConfigGate]:
    """
    Which Settings fields gate tool *registration*, discovered by differential
    probing rather than declared by hand.

    For each probeable field, build a copy of a baseline Settings with just
    that field toggled and diff build_registry_fn(...).names() against the
    baseline. Only fields with an observed effect are reported.

    build_registry_fn defaults to the real agent.tools.registry.build_registry
    but is swappable so the probing algorithm itself can be unit-tested
    against a fake settings/registry pair, independent of whatever tools
    happen to be wired up today.
    """
    baseline = settings_factory()
    baseline_names = set(build_registry_fn(baseline).names())

    findings: list[ConfigGate] = []
    for f in dataclasses.fields(baseline):
        toggle = _TOGGLE_VALUES.get(f.type)
        if toggle is None:
            continue
        if getattr(baseline, f.name) == toggle:
            continue
        probed = dataclasses.replace(baseline, **{f.name: toggle})
        probed_names = set(build_registry_fn(probed).names())
        if probed_names == baseline_names:
            continue
        findings.append(
            ConfigGate(
                field=f.name,
                gained=tuple(sorted(probed_names - baseline_names)),
                lost=tuple(sorted(baseline_names - probed_names)),
            )
        )
    return findings


def generate_config_gating(gates: list[ConfigGate]) -> str:
    """Markdown bullet list: which env-backed setting turns on which tool."""
    if not gates:
        return "No Settings field currently gates tool registration."
    lines = []
    for gate in gates:
        bits = []
        if gate.gained:
            bits.append(f"enables {', '.join(f'`{n}`' for n in gate.gained)}")
        if gate.lost:
            bits.append(f"disables {', '.join(f'`{n}`' for n in gate.lost)}")
        lines.append(f"- `{gate.field}` — {'; '.join(bits)}")
    return "\n".join(lines)


def generate_slim_summary(registry: ToolRegistry, gates: list[ConfigGate]) -> str:
    """The ≤SLIM_CHAR_BUDGET-char block that rides on every turn."""
    names = sorted(registry.names())
    tool_list = ", ".join(f"`{n}`" for n in names) if names else "none right now"
    lines = [f"Tools currently available: {tool_list}."]

    gate_bits = [f"`{g.field}`→{', '.join(g.gained)}" for g in gates if g.gained]
    if gate_bits:
        lines.append("Unset config that would add more: " + "; ".join(gate_bits) + ".")

    lines.append("Full detail: context/self/trillion.md.")
    summary = "\n".join(lines)

    if len(summary) > SLIM_CHAR_BUDGET:
        summary = summary[: SLIM_CHAR_BUDGET - 1].rstrip() + "…"
    return summary
