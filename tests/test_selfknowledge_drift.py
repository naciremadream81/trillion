"""
Tests for agent/selfknowledge/drift.py.

Includes the real-repo guardrail: context/self/trillion.md as checked in
must match what render.py would generate right now. This is what a commit
that changes agent/tools/registry.py or agent/config.py without running
`python -m agent.selfknowledge --refresh` trips (see .pre-commit-config.yaml).

Run from the project root:
    python -m unittest tests.test_selfknowledge_drift
"""

import os
import tempfile
import unittest

from agent.config import Settings
from agent.selfknowledge import drift, render


class TestCheckNoDrift(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "trillion.md")

    def test_missing_file_is_not_drift(self):
        drift.check_no_drift(self.path)  # must not raise

    def test_freshly_refreshed_file_has_no_drift(self):
        render.refresh_file(self.path, Settings())
        drift.check_no_drift(self.path)  # must not raise

    def test_stale_auto_block_is_detected(self):
        render.refresh_file(self.path, Settings())
        with open(self.path, encoding="utf-8") as f:
            text = f.read()
        text = text.replace("| Tool | Risk tier | Description |", "STALE CONTENT")
        with open(self.path, "w", encoding="utf-8") as f:
            f.write(text)

        with self.assertRaises(drift.DriftError) as ctx:
            drift.check_no_drift(self.path)
        self.assertIn("capabilities", str(ctx.exception))

    def test_stale_slim_block_is_detected(self):
        render.refresh_file(self.path, Settings())
        with open(self.path, encoding="utf-8") as f:
            text = f.read()
        text = text.replace("Tools currently available", "STALE SLIM")
        with open(self.path, "w", encoding="utf-8") as f:
            f.write(text)

        with self.assertRaises(drift.DriftError) as ctx:
            drift.check_no_drift(self.path)
        self.assertIn("SLIM", str(ctx.exception))

    def test_missing_block_is_detected(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("# no marker blocks here at all\n")

        with self.assertRaises(drift.DriftError):
            drift.check_no_drift(self.path)

    def test_error_message_points_to_the_fix(self):
        render.refresh_file(self.path, Settings())
        with open(self.path, encoding="utf-8") as f:
            text = f.read()
        text = text.replace("Tools currently available", "STALE")
        with open(self.path, "w", encoding="utf-8") as f:
            f.write(text)

        with self.assertRaises(drift.DriftError) as ctx:
            drift.check_no_drift(self.path)
        self.assertIn("--refresh", str(ctx.exception))


class TestRealRepoDocIsCurrent(unittest.TestCase):
    """The guardrail that actually matters: the checked-in
    context/self/trillion.md must not have drifted from the live
    registry/config. Run `python -m agent.selfknowledge --refresh` and
    commit the result if this fails."""

    def test_checked_in_doc_matches_live_generation(self):
        drift.check_no_drift()  # default path — the real repo file


if __name__ == "__main__":
    unittest.main()
