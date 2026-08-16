"""
Tests for agent/selfknowledge/parser.py's AUTO/SLIM marker-block parsing.

Run from the project root:
    python -m unittest tests.test_selfknowledge_parser
"""

import unittest

from agent.selfknowledge import parser

DOC = """\
# Heading

Hand-written prose before the block.

<!-- AUTO-START: capabilities -->
old capabilities content
<!-- AUTO-END: capabilities -->

More hand-written prose between blocks.

<!-- AUTO-START: config-gating -->
old gating content
<!-- AUTO-END: config-gating -->

<!-- SLIM-START -->
old slim content
<!-- SLIM-END -->

Trailing hand-written prose.
"""


class TestExtract(unittest.TestCase):
    def test_extract_auto_block_returns_inner_content(self):
        self.assertEqual(
            parser.extract_auto_block(DOC, "capabilities"), "old capabilities content"
        )

    def test_extract_auto_block_raises_when_missing(self):
        with self.assertRaises(parser.BlockNotFoundError):
            parser.extract_auto_block(DOC, "nonexistent")

    def test_extract_slim_block_returns_inner_content(self):
        self.assertEqual(parser.extract_slim_block(DOC), "old slim content")

    def test_extract_slim_block_raises_when_missing(self):
        with self.assertRaises(parser.BlockNotFoundError):
            parser.extract_slim_block("no slim markers here")

    def test_auto_block_names_lists_in_document_order(self):
        self.assertEqual(parser.auto_block_names(DOC), ["capabilities", "config-gating"])


class TestReplace(unittest.TestCase):
    def test_replace_auto_block_updates_only_named_block(self):
        result = parser.replace_auto_block(DOC, "capabilities", "NEW CAPS")
        self.assertIn("NEW CAPS", result)
        self.assertNotIn("old capabilities content", result)
        # the other AUTO block and the SLIM block are untouched
        self.assertIn("old gating content", result)
        self.assertIn("old slim content", result)

    def test_replace_auto_block_raises_when_missing(self):
        with self.assertRaises(parser.BlockNotFoundError):
            parser.replace_auto_block(DOC, "nonexistent", "X")

    def test_replace_slim_block_updates_only_slim(self):
        result = parser.replace_slim_block(DOC, "NEW SLIM")
        self.assertIn("NEW SLIM", result)
        self.assertNotIn("old slim content", result)
        self.assertIn("old capabilities content", result)

    def test_hand_written_prose_outside_blocks_is_untouched(self):
        result = parser.replace_auto_block(DOC, "capabilities", "X")
        result = parser.replace_slim_block(result, "Y")
        self.assertIn("Hand-written prose before the block.", result)
        self.assertIn("More hand-written prose between blocks.", result)
        self.assertIn("Trailing hand-written prose.", result)

    def test_round_trip_is_stable(self):
        once = parser.replace_auto_block(DOC, "capabilities", "STABLE")
        twice = parser.replace_auto_block(once, "capabilities", "STABLE")
        self.assertEqual(once, twice)

    def test_replace_strips_new_content_whitespace(self):
        result = parser.replace_auto_block(DOC, "capabilities", "\n\nPADDED\n\n")
        self.assertEqual(parser.extract_auto_block(result, "capabilities"), "PADDED")


if __name__ == "__main__":
    unittest.main()
