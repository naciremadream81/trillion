"""
Tests for the Software Factory README renderer (agent/factory/software/readme_md.py).

Run from the project root:
    python -m unittest tests.test_software_readme
"""

import unittest

from agent.factory.software.readme_md import readme_markdown

PLAN = {
    "project_name": "md-to-csv",
    "summary": "Converts markdown tables to CSV.",
    "tech_stack": "python",
    "files": ["main.py"],
    "entry_point": "main.py",
    "test_command": "pytest",
}


class TestReadmeMarkdown(unittest.TestCase):
    def test_renders_without_new_sections_when_omitted(self):
        content = readme_markdown("brief", PLAN, True, "1 passed")
        self.assertIn("# md-to-csv", content)
        self.assertNotIn("## Architecture", content)
        self.assertNotIn("## Tasks", content)
        self.assertNotIn("## Integration review", content)

    def test_renders_architecture_section_when_present(self):
        content = readme_markdown(
            "brief", PLAN, True, "1 passed",
            architecture_doc="# Architecture\n\nOne module, main.py.",
        )
        self.assertIn("## Architecture", content)
        self.assertIn("One module, main.py.", content)

    def test_renders_tasks_table_when_present(self):
        task_results = [
            {"task_id": 1, "title": "Implement CLI", "status": "PASSED", "attempts": 1, "last_feedback": ""},
            {"task_id": 2, "title": "Write docs", "status": "BLOCKED", "attempts": 3, "last_feedback": "still failing"},
        ]
        content = readme_markdown("brief", PLAN, True, "1 passed", task_results=task_results)
        self.assertIn("## Tasks", content)
        self.assertIn("Implement CLI", content)
        self.assertIn("PASSED", content)
        self.assertIn("Write docs", content)
        self.assertIn("BLOCKED", content)

    def test_renders_integration_verdict_when_present(self):
        verdict = {"verdict": "NEEDS_WORK", "notes": "one task blocked"}
        content = readme_markdown("brief", PLAN, True, "1 passed", verdict=verdict)
        self.assertIn("## Integration review", content)
        self.assertIn("NEEDS_WORK", content)
        self.assertIn("one task blocked", content)


if __name__ == "__main__":
    unittest.main()
