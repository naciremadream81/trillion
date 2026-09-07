"""
Tests for agent/selfknowledge/drift.py.

Includes the real-repo guardrail: context/self/trillion.md as checked in
must match what render.py generates from the BASELINE — the dataclass
defaults, with no environment read. That is what a commit changing
agent/tools/registry.py or agent/config.py without running
`python -m agent.selfknowledge --refresh` trips (see .pre-commit-config.yaml).

The baseline matters and is the point of TestGuardrailIsPortable below. The
default used to be live settings, which made the committed document a
per-machine artifact: whoever refreshed it last stamped their own `.env` into
it, and the check then failed for everyone else — CI included, since CI has
no `.env` at all. A guardrail that fails on a clean checkout is one people
learn to skip.

Run from the project root:
    python -m unittest tests.test_selfknowledge_drift
"""

import os
import tempfile
import unittest
from unittest import mock

from agent.config import Settings
from agent.selfknowledge import drift, render


class TestCheckNoDrift(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "trillion.md")

    def test_missing_file_is_drift(self):
        # A deleted context/self/trillion.md must fail the check, not pass
        # it silently — _load_self_knowledge() falls back to reading this
        # exact file, so a missing file silently drops the capability
        # summary from every prompt built without a live tool_registry.
        with self.assertRaises(drift.DriftError) as ctx:
            drift.check_no_drift(self.path)
        self.assertIn("missing", str(ctx.exception))
        self.assertIn("--refresh", str(ctx.exception))

    def test_freshly_refreshed_file_has_no_drift(self):
        render.refresh_file(self.path, Settings())
        drift.check_no_drift(self.path, Settings())  # must not raise

    def test_the_default_settings_are_the_baseline_not_the_environment(self):
        # refresh and check must agree without either being told which
        # configuration to use — that agreement is what makes the committed
        # file portable.
        render.refresh_file(self.path)
        drift.check_no_drift(self.path)  # must not raise

    def test_stale_auto_block_is_detected(self):
        render.refresh_file(self.path, Settings())
        with open(self.path, encoding="utf-8") as f:
            text = f.read()
        text = text.replace("| Tool | Risk tier | Description |", "STALE CONTENT")
        with open(self.path, "w", encoding="utf-8") as f:
            f.write(text)

        with self.assertRaises(drift.DriftError) as ctx:
            drift.check_no_drift(self.path, Settings())
        self.assertIn("capabilities", str(ctx.exception))

    def test_stale_slim_block_is_detected(self):
        render.refresh_file(self.path, Settings())
        with open(self.path, encoding="utf-8") as f:
            text = f.read()
        text = text.replace("Tools currently available", "STALE SLIM")
        with open(self.path, "w", encoding="utf-8") as f:
            f.write(text)

        with self.assertRaises(drift.DriftError) as ctx:
            drift.check_no_drift(self.path, Settings())
        self.assertIn("SLIM", str(ctx.exception))

    def test_missing_block_is_detected(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("# no marker blocks here at all\n")

        with self.assertRaises(drift.DriftError):
            drift.check_no_drift(self.path, Settings())

    def test_error_message_points_to_the_fix(self):
        render.refresh_file(self.path, Settings())
        with open(self.path, encoding="utf-8") as f:
            text = f.read()
        text = text.replace("Tools currently available", "STALE")
        with open(self.path, "w", encoding="utf-8") as f:
            f.write(text)

        with self.assertRaises(drift.DriftError) as ctx:
            drift.check_no_drift(self.path, Settings())
        self.assertIn("--refresh", str(ctx.exception))


class TestRealRepoDocIsCurrent(unittest.TestCase):
    """The guardrail that actually matters: the checked-in
    context/self/trillion.md must not have drifted from what the SOURCE
    offers. Run `python -m agent.selfknowledge --refresh` and commit the
    result if this fails."""

    def test_checked_in_doc_matches_baseline_generation(self):
        drift.check_no_drift()  # default path — the real repo file


class TestGuardrailIsPortable(unittest.TestCase):
    """
    The check must give the same answer on every machine.

    This is the regression that made the gate useless: with a live-settings
    default, the committed file matched only the environment it was generated
    in, so the test failed for every other developer and in CI. These set
    real environment variables and assert the answer does not move.
    """

    def _with_env(self, **env):
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_configured_environment_does_not_break_the_checked_in_doc(self):
        # Every gate switched on. Before the fix this failed loudly: the
        # capabilities table would have gained four tools the committed file
        # does not list.
        self._with_env(
            SUPABASE_ANALYTICS_URL="postgresql://u:p@h:6543/postgres",
            BRAVE_SEARCH_API_KEY="brave-key",
            FIRECRAWL_API_KEY="firecrawl-key",
            TRILLION_MINING_WALLET="bc1qexampleexampleexample",
            TRILLION_DESIGN_AGENT="1",
        )
        drift.check_no_drift()  # must not raise

    def test_an_empty_environment_does_not_break_it_either(self):
        # The CI case, and the one that was failing before.
        for key in ("SUPABASE_ANALYTICS_URL", "BRAVE_SEARCH_API_KEY",
                    "FIRECRAWL_API_KEY", "TRILLION_MINING_WALLET",
                    "TRILLION_DESIGN_AGENT"):
            self._with_env(**{key: ""})
        drift.check_no_drift()  # must not raise

    def test_baseline_settings_reads_no_environment(self):
        self._with_env(SUPABASE_ANALYTICS_URL="postgresql://u:p@h:6543/postgres")
        self.assertEqual(render.baseline_settings().supabase_analytics_url, "")

    def test_the_baseline_documents_every_gate_rather_than_one_machines(self):
        # With nothing set, every probeable field is actually probed, so the
        # config-gating block is the COMPLETE list. A configured baseline
        # would silently omit the gates already switched on.
        gating = render.render_blocks()["config-gating"]
        for field in ("supabase_analytics_url", "firecrawl_api_key",
                      "design_agent_enabled", "mining_wallet"):
            with self.subTest(field=field):
                self.assertIn(field, gating)


if __name__ == "__main__":
    unittest.main()
