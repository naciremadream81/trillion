"""
Tests for the Software Factory's sandboxed filesystem tools
(agent/tools/project_fs.py).

Run from the project root:
    python -m unittest tests.test_project_fs
"""

import asyncio
import os
import shutil
import subprocess
import tempfile
import unittest

import agent.tools.project_fs as project_fs
from agent.tools.project_fs import (
    PathEscape,
    ReadProjectFileTool,
    RunProjectTestsTool,
    WriteProjectFileTool,
    resolve_in_sandbox,
)

def _has_working_bwrap() -> bool:
    if not project_fs._BWRAP:
        return False
    tmp = tempfile.mkdtemp()
    try:
        result = subprocess.run(
            project_fs._sandbox_argv(["python3", "-c", "print('ok')"], tmp),
            env=project_fs._scrubbed_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=5,
            check=False,
            text=True,
        )
        return result.returncode == 0 and "ok" in result.stdout
    except Exception:
        return False
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


_HAS_WORKING_BWRAP = _has_working_bwrap()


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


@unittest.skipUnless(
    _HAS_WORKING_BWRAP,
    "bubblewrap (bwrap) is unavailable or blocked — sandboxed execution unavailable",
)
class TestRunProjectTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.tool = RunProjectTestsTool(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_successful_command_reports_exit_code_zero(self):
        result = run(self.tool.run(command="python3 -c \"print('hello')\""))
        self.assertIn("exit_code=0", result)
        self.assertIn("hello", result)

    def test_failing_command_reports_nonzero_exit_code(self):
        result = run(self.tool.run(command="python3 -c \"import sys; sys.exit(1)\""))
        self.assertIn("exit_code=1", result)

    def test_runs_from_project_directory(self):
        with open(os.path.join(self.tmp, "marker.txt"), "w") as f:
            f.write("here")
        result = run(self.tool.run(command="python3 -c \"print(open('marker.txt').read())\""))
        self.assertIn("here", result)

    def test_timeout_is_enforced(self):
        original = project_fs.TEST_TIMEOUT_SECONDS
        project_fs.TEST_TIMEOUT_SECONDS = 0.2
        try:
            result = run(self.tool.run(command="python3 -c \"import time; time.sleep(5)\""))
            self.assertIn("timed out", result)
        finally:
            project_fs.TEST_TIMEOUT_SECONDS = original

    def test_empty_command_rejected(self):
        result = run(self.tool.run(command="   "))
        self.assertIn("rejected", result)

    def test_disallowed_executable_is_rejected_without_running(self):
        result = run(self.tool.run(command="rm -rf /"))
        self.assertIn("rejected", result)
        self.assertIn("not an allowed test runner", result)

    def test_shell_metacharacters_are_not_interpreted(self):
        # No shell is involved, so "&&", "|", etc. are just inert argv to
        # python3's -c script, never a second command.
        result = run(self.tool.run(command="python3 -c \"print(1)\" && rm -rf /"))
        self.assertIn("exit_code=0", result)
        self.assertTrue(os.path.exists(self.tmp))  # "rm -rf /" never ran

    def test_env_is_scrubbed_of_secrets(self):
        os.environ["_TEST_PROJECT_FS_SECRET"] = "super-secret-value"
        try:
            result = run(self.tool.run(
                command="python3 -c \"import os; print(os.environ.get('_TEST_PROJECT_FS_SECRET', 'MISSING'))\""
            ))
        finally:
            del os.environ["_TEST_PROJECT_FS_SECRET"]
        self.assertIn("MISSING", result)
        self.assertNotIn("super-secret-value", result)

    def test_sandbox_blocks_reading_files_outside_project_dir(self):
        # A general-purpose interpreter can always try a relative-path escape
        # — an allowlisted executable name alone can't stop that. Only the
        # bwrap jail (no bind for anything above project_dir) does.
        parent = os.path.dirname(self.tmp)
        secret_path = os.path.join(parent, "outside-secret.txt")
        with open(secret_path, "w") as f:
            f.write("super-secret-value")
        try:
            result = run(self.tool.run(
                command="python3 -c \"print(open('../outside-secret.txt').read())\""
            ))
        finally:
            os.remove(secret_path)
        self.assertNotIn("super-secret-value", result)
        self.assertNotEqual("exit_code=0", result.splitlines()[0])

    def test_missing_runner_is_reported_as_ordinary_failure_not_raised(self):
        # rspec is allowlisted but not installed in this environment — this
        # must come back as a normal nonzero-exit result, not an uncaught
        # exception that the build pipeline would treat as a hard FAILED.
        result = run(self.tool.run(command="rspec spec/"))
        self.assertNotIn("rejected", result)
        self.assertNotIn("exit_code=0", result)


class TestRunProjectTestsWithoutSandbox(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.tool = RunProjectTestsTool(self.tmp)
        self._original_bwrap = project_fs._BWRAP
        project_fs._BWRAP = None

    def tearDown(self):
        project_fs._BWRAP = self._original_bwrap
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_refuses_to_run_unsandboxed(self):
        result = run(self.tool.run(command="python3 -c \"print('hello')\""))
        self.assertIn("rejected", result)
        self.assertIn("no sandbox available", result)


if __name__ == "__main__":
    unittest.main()
