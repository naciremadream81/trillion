"""
Tests for the draft_email tool (agent/tools/email.py).

Run from the project root:
    python -m unittest tests.test_email
"""

import asyncio
import unittest

from agent.safety.risk import LOW
from agent.tools.email import DraftEmailTool


def run(coro):
    return asyncio.run(coro)


class TestDraftEmailTool(unittest.TestCase):
    def test_risk_is_low(self):
        self.assertEqual(DraftEmailTool.risk, LOW)

    def test_formats_draft_not_sent(self):
        tool = DraftEmailTool()
        result = run(tool.run(to="sean@example.com", subject="Hello", body="Just checking in."))
        self.assertIn("Draft (not sent):", result)
        self.assertIn("To: sean@example.com", result)
        self.assertIn("Subject: Hello", result)
        self.assertIn("Just checking in.", result)

    def test_rejects_missing_to(self):
        tool = DraftEmailTool()
        result = run(tool.run(to="", subject="Hello", body="Body text"))
        self.assertIn("needs a non-empty", result)

    def test_rejects_missing_subject(self):
        tool = DraftEmailTool()
        result = run(tool.run(to="sean@example.com", subject="  ", body="Body text"))
        self.assertIn("needs a non-empty", result)

    def test_rejects_missing_body(self):
        tool = DraftEmailTool()
        result = run(tool.run(to="sean@example.com", subject="Hello", body=""))
        self.assertIn("needs a non-empty", result)

    def test_never_sends_anything(self):
        # There is no send code path in this tool at all — the closest thing
        # to a regression test for "draft only" is asserting the class has
        # no send-shaped method or attribute.
        tool = DraftEmailTool()
        self.assertFalse(hasattr(tool, "send"))


if __name__ == "__main__":
    unittest.main()
