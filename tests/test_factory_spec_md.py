"""
Tests for the Agent Factory spec-markdown writer (agent/factory/spec_md.py).

Run from the project root:
    python -m unittest tests.test_factory_spec_md
"""

import os
import tempfile
import unittest

from agent.factory.spec_md import spec_markdown, write_spec_markdown

REPORT = {
    "domain": "SQL migration review",
    "competencies": ["schema review", "index impact analysis"],
    "tools_available": ["web_search"],
    "tools_wishlist": ["query_analytics", "schema_diff"],
    "design_patterns": ["idempotent migrations"],
    "sources": ["https://example.com/migrations-guide"],
}
SPEC = {
    "slug": "sql-migration-review",
    "system_prompt": "You review database schema changes before deployment.",
    "tool_allowlist": ["web_search"],
}


class TestSpecMarkdown(unittest.TestCase):
    def test_content_includes_expected_sections(self):
        content = spec_markdown("a specialist that reviews SQL migrations", REPORT, SPEC)
        self.assertIn("# sql-migration-review", content)
        self.assertIn("SQL migration review", content)
        self.assertIn("schema review", content)
        self.assertIn(SPEC["system_prompt"], content)
        self.assertIn("web_search", content)
        self.assertIn("query_analytics", content)
        self.assertIn("idempotent migrations", content)
        self.assertIn("https://example.com/migrations-guide", content)
        self.assertIn("a specialist that reviews SQL migrations", content)

    def test_empty_lists_render_as_none_not_crash(self):
        empty_report = {"domain": "misc"}
        content = spec_markdown("role", empty_report, SPEC)
        self.assertIn("(none)", content)

    def test_role_description_is_sanitized(self):
        dirty_role = "reviews migrations\x00 with control chars"
        content = spec_markdown(dirty_role, REPORT, SPEC)
        self.assertNotIn("\x00", content)


class TestWriteSpecMarkdown(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_writes_file_under_specs_dir(self):
        specs_dir = os.path.join(self.tmp, "agent-specs")
        path = write_spec_markdown("a role", REPORT, SPEC, specs_dir=specs_dir)
        self.assertEqual(path, os.path.join(specs_dir, "sql-migration-review.md"))
        self.assertTrue(os.path.exists(path))
        with open(path) as f:
            content = f.read()
        self.assertIn("sql-migration-review", content)

    def test_creates_specs_dir_if_missing(self):
        specs_dir = os.path.join(self.tmp, "nested", "agent-specs")
        self.assertFalse(os.path.exists(specs_dir))
        write_spec_markdown("a role", REPORT, SPEC, specs_dir=specs_dir)
        self.assertTrue(os.path.isdir(specs_dir))

    def test_env_override_used_when_specs_dir_not_passed(self):
        specs_dir = os.path.join(self.tmp, "env-agent-specs")
        prev = os.environ.get("TRILLION_AGENT_SPECS_DIR")
        os.environ["TRILLION_AGENT_SPECS_DIR"] = specs_dir
        try:
            path = write_spec_markdown("a role", REPORT, SPEC)
            self.assertEqual(path, os.path.join(specs_dir, "sql-migration-review.md"))
        finally:
            if prev is None:
                os.environ.pop("TRILLION_AGENT_SPECS_DIR", None)
            else:
                os.environ["TRILLION_AGENT_SPECS_DIR"] = prev


if __name__ == "__main__":
    unittest.main()
