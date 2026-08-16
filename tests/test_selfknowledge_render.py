"""
Tests for agent/selfknowledge/render.py.

Run from the project root:
    python -m unittest tests.test_selfknowledge_render
"""

import os
import tempfile
import unittest

from agent.config import Settings
from agent.selfknowledge import parser, render


class TestRenderBlocks(unittest.TestCase):
    def test_returns_all_three_block_keys(self):
        blocks = render.render_blocks(Settings())
        self.assertEqual(set(blocks), {"capabilities", "config-gating", "slim"})

    def test_capabilities_reflects_passed_in_settings(self):
        # default Settings() has no analytics DB — query_analytics absent.
        blocks = render.render_blocks(Settings())
        self.assertNotIn("query_analytics", blocks["capabilities"])
        # with the DSN set, it must appear.
        blocks_with_db = render.render_blocks(
            Settings(supabase_analytics_url="postgresql://user@host/db")
        )
        self.assertIn("query_analytics", blocks_with_db["capabilities"])


class TestRefreshText(unittest.TestCase):
    def test_fills_in_blank_scaffold(self):
        text = render.refresh_text(render.INITIAL_DOCUMENT, Settings())
        self.assertNotEqual(
            parser.extract_auto_block(text, "capabilities").strip(), ""
        )
        self.assertNotEqual(parser.extract_slim_block(text).strip(), "")

    def test_preserves_hand_written_prose_outside_blocks(self):
        doc = render.INITIAL_DOCUMENT.replace(
            "# What Trillion knows about itself",
            "# What Trillion knows about itself\n\nHAND-WRITTEN NOTE.",
        )
        text = render.refresh_text(doc, Settings())
        self.assertIn("HAND-WRITTEN NOTE.", text)

    def test_is_idempotent(self):
        once = render.refresh_text(render.INITIAL_DOCUMENT, Settings())
        twice = render.refresh_text(once, Settings())
        self.assertEqual(once, twice)

    def test_raises_when_a_required_block_is_missing(self):
        with self.assertRaises(parser.BlockNotFoundError):
            render.refresh_text("no markers at all", Settings())


class TestRefreshFile(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "self", "trillion.md")

    def test_creates_file_from_scaffold_when_missing(self):
        changed = render.refresh_file(self.path, Settings())
        self.assertTrue(changed)
        self.assertTrue(os.path.isfile(self.path))

    def test_second_refresh_with_unchanged_settings_reports_no_change(self):
        render.refresh_file(self.path, Settings())
        changed_again = render.refresh_file(self.path, Settings())
        self.assertFalse(changed_again)

    def test_refresh_with_different_settings_reports_change(self):
        render.refresh_file(self.path, Settings())
        changed = render.refresh_file(
            self.path, Settings(supabase_analytics_url="postgresql://user@host/db")
        )
        self.assertTrue(changed)
        with open(self.path, encoding="utf-8") as f:
            self.assertIn("query_analytics", f.read())

    def test_hand_edit_between_markers_survives_a_no_op_refresh(self):
        render.refresh_file(self.path, Settings())
        with open(self.path, encoding="utf-8") as f:
            text = f.read()
        text = text.replace(
            "## Summary (injected into every system prompt)",
            "## Summary (injected into every system prompt)\n\nA HUMAN WROTE THIS.",
        )
        with open(self.path, "w", encoding="utf-8") as f:
            f.write(text)

        render.refresh_file(self.path, Settings())

        with open(self.path, encoding="utf-8") as f:
            self.assertIn("A HUMAN WROTE THIS.", f.read())


if __name__ == "__main__":
    unittest.main()
