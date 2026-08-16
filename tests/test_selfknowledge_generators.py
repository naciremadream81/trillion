"""
Tests for agent/selfknowledge/generators.py.

Run from the project root:
    python -m unittest tests.test_selfknowledge_generators

_FakeSettings below uses `from __future__ import annotations`, matching
agent/config.py: probe_config_gating() dispatches on dataclasses.fields()
`.type`, which is a *string* ('str', 'bool', 'list[str]') only because of
that import. Without it here too, this fixture wouldn't exercise the real
code path production actually hits.
"""

from __future__ import annotations

import dataclasses
import unittest

from agent.config import Settings
from agent.safety.risk import CONSEQUENTIAL, LOW, READ_ONLY
from agent.selfknowledge import generators
from agent.tools.base import BaseTool
from agent.tools.registry import ToolRegistry, build_registry


class _FakeTool(BaseTool):
    def __init__(self, name, description, risk):
        self.name = name
        self.description = description
        self.risk = risk

    async def run(self, **kwargs) -> str:
        return ""


class TestGenerateCapabilities(unittest.TestCase):
    def test_empty_registry_produces_header_only(self):
        table = generators.generate_capabilities(ToolRegistry())
        self.assertEqual(table, "| Tool | Risk tier | Description |\n| --- | --- | --- |")

    def test_lists_each_tool_sorted_by_name(self):
        registry = ToolRegistry()
        registry.register(_FakeTool("zeta_tool", "Does zeta things.", LOW))
        registry.register(_FakeTool("alpha_tool", "Does alpha things.", READ_ONLY))
        table = generators.generate_capabilities(registry)
        self.assertLess(table.index("alpha_tool"), table.index("zeta_tool"))

    def test_includes_risk_tier_and_description(self):
        registry = ToolRegistry()
        registry.register(_FakeTool("send_thing", "Sends a thing.", CONSEQUENTIAL))
        table = generators.generate_capabilities(registry)
        self.assertIn("`send_thing`", table)
        self.assertIn("consequential", table)
        self.assertIn("Sends a thing.", table)

    def test_only_first_description_line_is_used(self):
        registry = ToolRegistry()
        registry.register(_FakeTool("multi", "First line.\nSecond line.", LOW))
        table = generators.generate_capabilities(registry)
        self.assertIn("First line.", table)
        self.assertNotIn("Second line.", table)


@dataclasses.dataclass
class _FakeSettings:
    api_key: str = ""
    enabled: bool = False
    tags: list = dataclasses.field(default_factory=list)
    daily_cap: int = 3  # unprobeable type — must be skipped, not crash


def _fake_build_registry(settings) -> ToolRegistry:
    registry = ToolRegistry()
    if settings.api_key:
        registry.register(_FakeTool("gated_by_key", "x", READ_ONLY))
    if settings.enabled:
        registry.register(_FakeTool("gated_by_flag", "x", READ_ONLY))
    # tags and daily_cap intentionally gate nothing
    registry.register(_FakeTool("always_on", "x", READ_ONLY))
    return registry


class TestProbeConfigGating(unittest.TestCase):
    def test_finds_str_and_bool_gates_only(self):
        gates = generators.probe_config_gating(
            baseline=_FakeSettings(), build_registry_fn=_fake_build_registry
        )
        fields = {g.field for g in gates}
        self.assertEqual(fields, {"api_key", "enabled"})

    def test_reports_which_tool_each_field_gains(self):
        gates = generators.probe_config_gating(
            baseline=_FakeSettings(), build_registry_fn=_fake_build_registry
        )
        by_field = {g.field: g for g in gates}
        self.assertEqual(by_field["api_key"].gained, ("gated_by_key",))
        self.assertEqual(by_field["enabled"].gained, ("gated_by_flag",))

    def test_field_with_no_registration_effect_is_not_reported(self):
        gates = generators.probe_config_gating(
            baseline=_FakeSettings(), build_registry_fn=_fake_build_registry
        )
        fields = {g.field for g in gates}
        self.assertNotIn("tags", fields)

    def test_unprobeable_type_is_skipped_without_crashing(self):
        # daily_cap: int isn't in _TOGGLE_VALUES — this must not raise.
        gates = generators.probe_config_gating(
            baseline=_FakeSettings(), build_registry_fn=_fake_build_registry
        )
        fields = {g.field for g in gates}
        self.assertNotIn("daily_cap", fields)

    def test_no_baseline_given_defaults_to_blank_settings(self):
        # Matches prior behavior for callers with no real settings on hand
        # (e.g. a from-scratch doc render).
        gates = generators.probe_config_gating(build_registry_fn=build_registry)
        fields = {g.field for g in gates}
        self.assertEqual(fields, {"supabase_analytics_url", "brave_search_api_key", "firecrawl_api_key"})

    def test_against_real_settings_and_registry_matches_known_wiring(self):
        # agent.tools.registry.build_registry() only gates tool *registration*
        # via these three fields today (confirmed by reading resolve_search_
        # provider() and build_registry() directly) — this is the guardrail
        # that catches it silently changing.
        gates = generators.probe_config_gating(baseline=Settings(), build_registry_fn=build_registry)
        fields = {g.field for g in gates}
        self.assertEqual(fields, {"supabase_analytics_url", "brave_search_api_key", "firecrawl_api_key"})

    def test_already_satisfied_gate_is_excluded_from_real_baseline(self):
        # Codex review finding: probing from a blank Settings() reports
        # brave_search_api_key as "unset config that would add web_search"
        # even when the real baseline already has it set (and web_search is
        # therefore already in the baseline registry) — self-contradictory.
        # Probing from the real baseline must not report it.
        baseline = Settings(brave_search_api_key="real-key-already-set")
        gates = generators.probe_config_gating(baseline=baseline, build_registry_fn=build_registry)
        fields = {g.field for g in gates}
        self.assertNotIn("brave_search_api_key", fields)

    def test_explicit_provider_selection_blocks_the_other_providers_key_as_a_gate(self):
        # Codex review finding: resolve_search_provider() fails closed when
        # search_provider is explicitly set to one backend — setting the
        # *other* backend's key does not enable web_search. Probing from a
        # blank Settings() (where search_provider is unset) can't see this;
        # probing from this real baseline must correctly report no gate.
        baseline = Settings(search_provider="brave", firecrawl_api_key="fc-key")
        gates = generators.probe_config_gating(baseline=baseline, build_registry_fn=build_registry)
        fields = {g.field for g in gates}
        self.assertNotIn("firecrawl_api_key", fields)

    def test_matching_provider_key_is_still_reported_as_a_gate(self):
        # Sanity check for the fix above: the key that *does* match the
        # explicit provider selection must still show up as a real gate.
        baseline = Settings(search_provider="brave")
        gates = generators.probe_config_gating(baseline=baseline, build_registry_fn=build_registry)
        fields = {g.field for g in gates}
        self.assertIn("brave_search_api_key", fields)


class TestGenerateConfigGating(unittest.TestCase):
    def test_no_gates_produces_explanatory_sentence(self):
        self.assertIn("No Settings field", generators.generate_config_gating([]))

    def test_lists_field_and_gained_tools(self):
        gate = generators.ConfigGate(field="my_key", gained=("my_tool",), lost=())
        text = generators.generate_config_gating([gate])
        self.assertIn("`my_key`", text)
        self.assertIn("`my_tool`", text)


class TestGenerateSlimSummary(unittest.TestCase):
    def test_lists_available_tools(self):
        registry = ToolRegistry()
        registry.register(_FakeTool("only_tool", "x", READ_ONLY))
        summary = generators.generate_slim_summary(registry, [])
        self.assertIn("`only_tool`", summary)

    def test_empty_registry_says_none(self):
        summary = generators.generate_slim_summary(ToolRegistry(), [])
        self.assertIn("none right now", summary)

    def test_stays_within_char_budget_with_many_gates(self):
        registry = ToolRegistry()
        gates = [
            generators.ConfigGate(field=f"field_{i}", gained=(f"tool_{i}",), lost=())
            for i in range(200)
        ]
        summary = generators.generate_slim_summary(registry, gates)
        self.assertLessEqual(len(summary), generators.SLIM_CHAR_BUDGET)

    def test_real_deployment_state_stays_within_budget(self):
        # The actual guardrail: today's real settings/registry must fit.
        from agent.config import get_settings

        settings = get_settings()
        registry = build_registry(settings)
        gates = generators.probe_config_gating(baseline=settings, build_registry_fn=build_registry)
        summary = generators.generate_slim_summary(registry, gates)
        self.assertLessEqual(len(summary), generators.SLIM_CHAR_BUDGET)


if __name__ == "__main__":
    unittest.main()
