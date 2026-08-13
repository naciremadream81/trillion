"""
Tests for the Software Factory's sandboxed filesystem tools
(agent/tools/project_fs.py).

Run from the project root:
    python -m unittest tests.test_project_fs
"""

import asyncio
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

from agent.tools.project_fs import (
    PathEscape,
    ReadProjectFileTool,
    RunProjectTestsTool,
    WriteProjectFileTool,
    resolve_in_sandbox,
)


def run(coro):
    return asyncio.run(coro)


class TestResolveInSandbox(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_normal_relative_path_resolves_inside(self):
        target = resolve_in_sandbox(self.tmp, "src/main.py")
        self.assertTrue(target.startswith(os.path.realpath(self.tmp)))

    def test_absolute_path_is_refused(self):
        with self.assertRaises(PathEscape):
            resolve_in_sandbox(self.tmp, "/etc/passwd")

    def test_dotdot_escape_is_refused(self):
        with self.assertRaises(PathEscape):
            resolve_in_sandbox(self.tmp, "../../etc/passwd")

    def test_dotdot_within_sandbox_is_allowed(self):
        # a/../b.py normalizes to b.py, which is still inside the sandbox.
        target = resolve_in_sandbox(self.tmp, "a/../b.py")
        self.assertEqual(target, os.path.join(os.path.realpath(self.tmp), "b.py"))


class TestWriteAndReadProjectFile(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.write_tool = WriteProjectFileTool(self.tmp)
        self.read_tool = ReadProjectFileTool(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_write_then_read_round_trip(self):
        result = run(self.write_tool.run(relative_path="main.py", content="print('hi')"))
        self.assertIn("Wrote", result)
        content = run(self.read_tool.run(relative_path="main.py"))
        self.assertEqual(content, "print('hi')")

    def test_write_creates_nested_directories(self):
        run(self.write_tool.run(relative_path="src/pkg/mod.py", content="x = 1"))
        self.assertTrue(os.path.isfile(os.path.join(self.tmp, "src", "pkg", "mod.py")))

    def test_write_refuses_path_escape(self):
        result = run(self.write_tool.run(relative_path="../outside.py", content="evil"))
        self.assertIn("rejected", result)
        self.assertFalse(os.path.exists(os.path.join(os.path.dirname(self.tmp), "outside.py")))

    def test_write_refuses_absolute_path(self):
        result = run(self.write_tool.run(relative_path="/tmp/evil.py", content="evil"))
        self.assertIn("rejected", result)

    def test_read_missing_file_reports_cleanly(self):
        result = run(self.read_tool.run(relative_path="nope.py"))
        self.assertIn("no such file", result)

    def test_read_refuses_path_escape(self):
        result = run(self.read_tool.run(relative_path="../../etc/passwd"))
        self.assertIn("rejected", result)

    def test_read_truncates_long_files(self):
        from agent.tools.project_fs import MAX_READ_CHARS

        run(self.write_tool.run(relative_path="big.txt", content="a" * (MAX_READ_CHARS + 500)))
        content = run(self.read_tool.run(relative_path="big.txt"))
        self.assertIn("truncated", content)
        self.assertLess(len(content), MAX_READ_CHARS + 500)


class TestRunProjectTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.tool = RunProjectTestsTool(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_successful_command_reports_exit_code_zero(self):
        result = run(self.tool.run(command="echo hello"))
        self.assertIn("exit_code=0", result)
        self.assertIn("hello", result)

    def test_failing_command_reports_nonzero_exit_code(self):
        result = run(self.tool.run(command="exit 1"))
        self.assertIn("exit_code=1", result)

    def test_runs_from_project_directory(self):
        with open(os.path.join(self.tmp, "marker.txt"), "w") as f:
            f.write("here")
        result = run(self.tool.run(command="cat marker.txt"))
        self.assertIn("here", result)

    def test_timeout_is_enforced(self):
        import agent.tools.project_fs as project_fs

        original = project_fs.TEST_TIMEOUT_SECONDS
        project_fs.TEST_TIMEOUT_SECONDS = 0.2
        try:
            result = run(self.tool.run(command="sleep 5"))
            self.assertIn("timed out", result)
        finally:
            project_fs.TEST_TIMEOUT_SECONDS = original

    def test_empty_command_rejected(self):
        result = run(self.tool.run(command="   "))
        self.assertIn("rejected", result)

    def test_secrets_are_not_visible_to_the_spawned_shell(self):
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-should-not-leak"}):
            result = run(self.tool.run(command="echo key=$ANTHROPIC_API_KEY"))
        self.assertNotIn("sk-ant-should-not-leak", result)


if __name__ == "__main__":
    unittest.main()
