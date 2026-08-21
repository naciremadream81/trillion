"""
Tests for the composer — playbooks/design-subagent.md Tier 3, plus Tier 6's
prompt sections and the cost ceiling from the Tier 0 interview.

This is the most dangerous part of the design agent: it spawns a subprocess
that installs packages, writes files, and spends money. The tests below are
weighted accordingly — the argv is the security boundary, so it is asserted
directly rather than through a spawn.

Run from the project root:
    python -m unittest tests.test_design_composer
"""

import asyncio
import os
import shutil
import tempfile
import unittest

from agent.design.budget import BudgetExceeded, DesignBudget
from agent.design.claude_code_runner import (
    DEFAULT_ALLOWED_TOOLS,
    ClaudeCodeError,
    build_command,
    claude_binary,
    parse_event,
)
from agent.design.composer import build_composition_prompt
from agent.design.design_tokens import default_tokens_yaml, parse_tokens
from agent.design.docs import ProjectScan, render_design_doc
from agent.safety.risk import CONSEQUENTIAL, READ_ONLY
from agent.tools.design import GenerateMockupTool, ListDesignProjectsTool


def make_tokens():
    return parse_tokens(render_design_doc(ProjectScan(name="demo"), default_tokens_yaml()))


@unittest.skipIf(claude_binary() is None, "claude CLI not installed")
class TestCommandIsTheSecurityBoundary(unittest.TestCase):
    def setUp(self):
        self.argv = build_command("compose a thing", model="claude-sonnet-4-6", max_turns=12)
        self.tools = self.argv[self.argv.index("--allowedTools") + 1]

    def test_bash_is_never_unrestricted(self):
        # The playbook: "Avoid Bash(*) — too permissive."
        self.assertNotIn("Bash(*)", self.tools)
        self.assertNotIn("Bash", self.tools.split(",")[:5])

    def test_bash_is_prefix_scoped_to_build_commands(self):
        bash_entries = [t for t in self.tools.split(",") if t.startswith("Bash(")]
        self.assertTrue(bash_entries)
        for entry in bash_entries:
            with self.subTest(entry=entry):
                self.assertTrue(entry.endswith(":*)"), entry)

    def test_dangerous_commands_are_not_permitted(self):
        for forbidden in ("rm", "curl", "git", "sudo", "ssh", "chmod"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(f"Bash({forbidden}", self.tools)

    def test_turns_are_bounded(self):
        self.assertIn("--max-turns", self.argv)
        self.assertEqual(self.argv[self.argv.index("--max-turns") + 1], "12")

    def test_the_stream_format_is_requested(self):
        self.assertIn("--output-format", self.argv)
        self.assertEqual(self.argv[self.argv.index("--output-format") + 1], "stream-json")

    def test_the_model_is_passed_through_when_given(self):
        self.assertEqual(self.argv[self.argv.index("--model") + 1], "claude-sonnet-4-6")

    def test_no_model_flag_when_none_given(self):
        self.assertNotIn("--model", build_command("x"))


class TestEnvIsStripped(unittest.TestCase):
    def test_only_the_anthropic_key_crosses_the_boundary(self):
        # Every other secret Trillion holds stays out of the child.
        from agent.security.subprocess_env import with_keys

        original = dict(os.environ)
        os.environ.update({
            "ANTHROPIC_API_KEY": "keep-me",
            "DEEPGRAM_API_KEY": "secret",
            "GITHUB_TOKEN": "secret",
            "TRILLION_WEB_AUTH_TOKEN": "secret",
            "TRILLION_MINING_WALLET": "bc1qsecret",
        })
        try:
            env = with_keys("ANTHROPIC_API_KEY")
            self.assertEqual(env.get("ANTHROPIC_API_KEY"), "keep-me")
            for leaked in ("DEEPGRAM_API_KEY", "GITHUB_TOKEN",
                           "TRILLION_WEB_AUTH_TOKEN", "TRILLION_MINING_WALLET"):
                self.assertNotIn(leaked, env)
        finally:
            os.environ.clear()
            os.environ.update(original)


class TestStreamParsing(unittest.TestCase):
    def test_a_tool_use_becomes_a_progress_event(self):
        event = parse_event(
            '{"type":"assistant","message":{"content":['
            '{"type":"tool_use","name":"Read","input":{"file_path":"design.md"}}]}}'
        )
        self.assertEqual(event, {"type": "tool", "name": "Read", "target": "design.md"})

    def test_a_result_carries_cost_and_turns(self):
        event = parse_event(
            '{"type":"result","is_error":false,"result":"done",'
            '"total_cost_usd":1.23,"num_turns":7,"duration_ms":4200}'
        )
        self.assertEqual(event["total_cost_usd"], 1.23)
        self.assertEqual(event["num_turns"], 7)

    def test_an_error_result_is_flagged(self):
        self.assertTrue(parse_event('{"type":"result","is_error":true,"result":"boom"}')["is_error"])

    def test_malformed_lines_never_raise(self):
        # A single bad line must not kill a twenty-minute build.
        for line in ("", "   ", "garbage", "{", "[]", "null", '{"type":"unknown"}'):
            with self.subTest(line=line):
                self.assertIsNone(parse_event(line))

    def test_an_oversized_result_is_bounded(self):
        event = parse_event('{"type":"result","result":"' + "x" * 9000 + '"}')
        self.assertLessEqual(len(event["result"]), 2000)


class TestCompositionPrompt(unittest.TestCase):
    def setUp(self):
        self.prompt = build_composition_prompt(
            project_slug="demo-project", feature_slug="landing", screen_name="hero",
            description="A landing hero.", tokens=make_tokens(),
            visual_direction="grid-pattern at 0.5 opacity", quality="premium",
            components_hint=["card"], first_dispatch=True,
        )

    def test_it_lands_in_the_playbooks_size_band(self):
        # "The CC prompt typically runs 4-6KB. Worth every byte."
        self.assertGreater(len(self.prompt), 3500)
        self.assertLess(len(self.prompt), 9000)

    def test_the_brief_is_law_section_is_present(self):
        self.assertIn("THE BRIEF IS LAW", self.prompt)
        self.assertIn("outrank the wording of this task", self.prompt)

    def test_the_visual_requirements_are_countable_not_vague(self):
        for requirement in ("Opacity >= 0.4", "product surface", "at least two",
                            "at least three", "14-16px"):
            with self.subTest(requirement=requirement):
                self.assertIn(requirement, self.prompt)

    def test_it_names_the_reading_order(self):
        self.assertIn("design.md", self.prompt)
        self.assertIn(".prism/brief.md", self.prompt)

    def test_it_names_the_exact_deliverable_path(self):
        # The "did it build" check has to agree with what the prompt asked for.
        self.assertIn("out/landing/hero/index.html", self.prompt)

    def test_first_dispatch_asks_for_npm_install(self):
        self.assertIn("npm install", self.prompt)

    def test_a_later_dispatch_forbids_reinstalling(self):
        prompt = build_composition_prompt(
            project_slug="p", feature_slug="f", screen_name="s", description="d",
            tokens=make_tokens(), first_dispatch=False,
        )
        self.assertIn("do NOT run `npm install` again", prompt)

    def test_image_urls_must_be_used_verbatim(self):
        # Plain <img> is not basePath-prefixed, so a shortened URL 404s.
        prompt = build_composition_prompt(
            project_slug="p", feature_slug="f", screen_name="s", description="d",
            tokens=make_tokens(),
            image_urls=["/api/design/p/preview/assets/f/backdrop.png"],
        )
        self.assertIn("verbatim", prompt)
        self.assertIn("/api/design/p/preview/assets/f/backdrop.png", prompt)

    def test_it_forbids_installing_uncatalogued_libraries(self):
        self.assertIn("not installable", self.prompt)

    def test_the_premium_bar_differs_from_standard(self):
        standard = build_composition_prompt(
            project_slug="p", feature_slug="f", screen_name="s", description="d",
            tokens=make_tokens(), quality="standard",
        )
        self.assertIn("PREMIUM", self.prompt)
        self.assertIn("STANDARD", standard)
        self.assertNotIn("PREMIUM", standard)


class TestBudget(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.budget = DesignBudget(
            os.path.join(self.tmp, "d.db"), per_dispatch_usd=5.0, daily_usd=15.0
        )

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_fresh_day_allows_a_dispatch(self):
        self.budget.check_before_dispatch()

    def test_it_refuses_when_a_whole_dispatch_would_not_fit(self):
        # Not "when there are no cents left" — starting a dispatch that can
        # only afford a third of itself produces the half-composed screen the
        # ceiling exists to prevent.
        self.budget.record("p", cost_usd=11.0)
        with self.assertRaises(BudgetExceeded):
            self.budget.check_before_dispatch()

    def test_spend_accumulates(self):
        self.budget.record("p", cost_usd=2.5)
        self.budget.record("p", cost_usd=1.25)
        self.assertAlmostEqual(self.budget.spent_today(), 3.75)

    def test_a_failed_dispatch_still_costs(self):
        # Money spent on a build that failed is money spent.
        self.budget.record("p", cost_usd=3.0, succeeded=False)
        self.assertAlmostEqual(self.budget.spent_today(), 3.0)


class TestToolSafetyPosture(unittest.TestCase):
    def test_generate_mockup_is_gated(self):
        # It spawns a subprocess that installs packages, writes files, and
        # spends money — exactly what the confirmation gate is for.
        self.assertEqual(GenerateMockupTool.risk, CONSEQUENTIAL)

    def test_generate_mockup_is_unreachable_by_a_spawned_specialist(self):
        # Specialists run without a gate by construction, which is only safe
        # because every tool they can reach is read-only.
        self.assertFalse(GenerateMockupTool.factory_allowed)

    def test_listing_projects_is_read_only(self):
        self.assertEqual(ListDesignProjectsTool.risk, READ_ONLY)
        self.assertTrue(ListDesignProjectsTool.factory_allowed)


class TestToolRefusals(unittest.IsolatedAsyncioTestCase):
    """Every refusal path must return a string, never raise, and never spawn."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.tmp, "demo-project"))

        class Settings:
            software_factory_root = self.tmp
            design_compose_model = ""

        self.settings = Settings()
        self.budget = DesignBudget(os.path.join(self.tmp, "d.db"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _tool(self):
        return GenerateMockupTool(self.settings, budget=self.budget)

    async def test_a_traversal_slug_is_refused(self):
        result = await self._tool().run(
            project_slug="../etc", feature_slug="f", screen_name="s", description="d"
        )
        self.assertIn("kebab-case", result)

    async def test_a_missing_project_is_refused(self):
        result = await self._tool().run(
            project_slug="no-such", feature_slug="f", screen_name="s", description="d"
        )
        self.assertIn("no such project", result)

    async def test_a_missing_description_is_refused(self):
        result = await self._tool().run(
            project_slug="demo-project", feature_slug="f", screen_name="s", description="  "
        )
        self.assertIn("description", result)

    async def test_an_exhausted_budget_refuses_before_spawning(self):
        self.budget.record("demo-project", cost_usd=20.0)
        result = await self._tool().run(
            project_slug="demo-project", feature_slug="f", screen_name="s", description="d"
        )
        self.assertIn("refused", result)
        self.assertIn("budget", result.lower())

    async def test_an_unusable_design_doc_bails_loudly(self):
        # "Bail loudly if the YAML block is missing or invalid — don't try to
        # compose against a half-shaped system."
        from agent.design.docs import bootstrap_project, write_project_file

        bootstrap_project("demo-project", self.settings)
        root = os.path.join(self.tmp, "demo-project")
        write_project_file(root, "design.md", "# design\n\nno tokens block here\n")
        result = await self._tool().run(
            project_slug="demo-project", feature_slug="f", screen_name="s", description="d"
        )
        self.assertIn("not usable", result)
        self.assertIn("design.md", result)


class TestCostCeilingIsEnforcedNotJustReserved(unittest.TestCase):
    """
    Codex review, P1: per_dispatch_usd was only added to historical spend to
    decide whether a run could START. Nothing constrained the run itself, so a
    single dispatch could spend past both it and the daily cap before any cost
    was recorded.
    """

    def test_assistant_events_expose_usage_for_mid_flight_tracking(self):
        # Confirmed against the real CLI. This is what makes a mid-flight
        # ceiling possible — total_cost_usd only arrives on the final result
        # event, far too late to stop a run already over budget.
        event = parse_event(
            '{"type":"assistant","message":{"model":"claude-sonnet-4-6","usage":'
            '{"input_tokens":1200,"output_tokens":300,"cache_read_input_tokens":900,'
            '"cache_creation_input_tokens":50}}}'
        )
        self.assertEqual(event["type"], "usage")
        self.assertEqual(event["input_tokens"], 1200)
        self.assertEqual(event["output_tokens"], 300)
        self.assertEqual(event["cache_read_tokens"], 900)
        self.assertEqual(event["cache_write_tokens"], 50)

    def test_a_usage_event_does_not_shadow_tool_progress(self):
        # An assistant event carrying content rather than usage must still
        # produce the progress line the UI shows.
        event = parse_event(
            '{"type":"assistant","message":{"content":[{"type":"tool_use",'
            '"name":"Write","input":{"file_path":"page.tsx"}}]}}'
        )
        self.assertEqual(event["type"], "tool")

    def test_the_runner_accepts_a_ceiling(self):
        import inspect

        from agent.design.claude_code_runner import spawn_claude_code

        self.assertIn("max_cost_usd", inspect.signature(spawn_claude_code).parameters)

    def test_the_tool_passes_its_configured_ceiling_through(self):
        import inspect

        source = inspect.getsource(GenerateMockupTool.run)
        self.assertIn("max_cost_usd=self.budget.per_dispatch_usd", source)

    def test_a_killed_run_still_charges_the_budget(self):
        # A run killed for going over never emits a result event, so
        # total_cost_usd stays 0. Recording that would hand the budget back
        # the very money that triggered the kill.
        import inspect

        source = inspect.getsource(GenerateMockupTool.run)
        self.assertIn("max(result.total_cost_usd, result.estimated_cost_usd)", source)


class TestStaleOutputIsNotSuccess(unittest.IsolatedAsyncioTestCase):
    """
    Codex review, P2: regenerating an existing screen leaves the previous
    index.html in place, so an existence-only check reported success for a
    failed rebuild and handed back a URL serving the old design.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.project = os.path.join(self.tmp, "demo-project")
        os.makedirs(self.project)

        class Settings:
            software_factory_root = self.tmp
            design_compose_model = ""

        self.settings = Settings()
        self.budget = DesignBudget(os.path.join(self.tmp, "d.db"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    async def _run_with_fake_claude(self, fake):
        import agent.tools.design as design_module

        original = design_module.spawn_claude_code
        design_module.spawn_claude_code = fake
        try:
            tool = GenerateMockupTool(self.settings, budget=self.budget)
            return await tool.run(
                project_slug="demo-project", feature_slug="landing",
                screen_name="hero", description="A hero.",
            )
        finally:
            design_module.spawn_claude_code = original

    def _plant_stale_output(self):
        from agent.design.claude_code_runner import ClaudeCodeResult  # noqa: F401

        out = os.path.join(self.project, ".prism", "preview", "out", "landing", "hero")
        os.makedirs(out, exist_ok=True)
        path = os.path.join(out, "index.html")
        with open(path, "w") as f:
            f.write("<html>the OLD design</html>")
        old_time = 1_600_000_000
        os.utime(path, (old_time, old_time))
        return path

    async def test_a_failed_rebuild_over_stale_output_is_not_success(self):
        from agent.design.claude_code_runner import ClaudeCodeResult

        self._plant_stale_output()

        async def failing(*a, **kw):
            return ClaudeCodeResult(ok=False, error="build failed", total_cost_usd=1.5)

        result = await self._run_with_fake_claude(failing)
        self.assertIn("did not produce a screen", result)
        self.assertNotIn("Built landing/hero", result)

    async def test_a_successful_run_that_touched_nothing_is_not_success(self):
        # Claude Code reporting ok while leaving the previous file untouched.
        from agent.design.claude_code_runner import ClaudeCodeResult

        self._plant_stale_output()

        async def no_op(*a, **kw):
            return ClaudeCodeResult(ok=True, total_cost_usd=1.0)

        result = await self._run_with_fake_claude(no_op)
        self.assertIn("untouched", result)

    async def test_a_genuine_rebuild_is_success(self):
        from agent.design.claude_code_runner import ClaudeCodeResult

        path = self._plant_stale_output()

        async def rebuilds(*a, **kw):
            with open(path, "w") as f:
                f.write("<html>the NEW design</html>")
            return ClaudeCodeResult(ok=True, total_cost_usd=2.0, num_turns=9)

        result = await self._run_with_fake_claude(rebuilds)
        self.assertIn("Built landing/hero", result)

    async def test_a_failed_dispatch_is_still_charged(self):
        from agent.design.claude_code_runner import ClaudeCodeResult

        async def failing(*a, **kw):
            return ClaudeCodeResult(ok=False, error="boom", total_cost_usd=1.25)

        await self._run_with_fake_claude(failing)
        self.assertAlmostEqual(self.budget.spent_today(), 1.25)


if __name__ == "__main__":
    unittest.main()
