"""
Sandboxed filesystem tools for the Software Factory's internal build agent
(agent/factory/software/pipeline.py) — never registered on Trillion's main
chat registry or handed to Agent-Factory-spawned specialists. `factory_allowed
= False` on all three keeps them out of Agent Factory's tool_allowlist
intersection; in practice they're also constructed fresh per build and
injected only into the CODING/TESTING step's own private ToolRegistry, never
the shared one build_registry() returns.

The path-jail guard here (resolve_in_sandbox) is the one piece of security
logic this feature needs that nothing else in the codebase provides: every
relative_path is resolved against the build's own project directory, and
anything that would land outside it — via an absolute path, `..` traversal,
or a symlink — is refused rather than silently clamped.
"""

from __future__ import annotations

import asyncio
import os

from ..safety.risk import CONSEQUENTIAL, HARDLINE, READ_ONLY
from ..security.subprocess_env import shell_minimal
from .base import BaseTool

MAX_READ_CHARS = 20_000
TEST_TIMEOUT_SECONDS = 60


class PathEscape(ValueError):
    """Raised when a relative_path would resolve outside the project sandbox."""


def resolve_in_sandbox(base_dir: str, relative_path: str) -> str:
    """
    Resolve relative_path against base_dir, refusing any result that escapes
    it. Raises PathEscape rather than silently clamping, so callers see the
    failure instead of writing to (or reading from) the wrong place.
    """
    if os.path.isabs(relative_path):
        raise PathEscape(f"absolute paths are not allowed: {relative_path!r}")
    base = os.path.realpath(base_dir)
    target = os.path.realpath(os.path.join(base, relative_path))
    if target != base and not target.startswith(base + os.sep):
        raise PathEscape(f"path escapes the project sandbox: {relative_path!r}")
    return target


class WriteProjectFileTool(BaseTool):
    name = "write_project_file"
    description = (
        "Write (or overwrite) a file inside the project being built. "
        "relative_path must be relative to the project root — absolute "
        "paths and '..' traversal are refused."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "relative_path": {"type": "string", "description": "Path relative to the project root."},
            "content": {"type": "string", "description": "Full file content to write."},
        },
        "required": ["relative_path", "content"],
    }
    factory_allowed = False
    # Tier 6: writes to disk, and overwrites without asking. Path-jailed to the
    # build's own directory, which bounds the damage without removing it.
    risk = CONSEQUENTIAL

    def __init__(self, project_dir: str) -> None:
        self.project_dir = project_dir

    async def run(self, relative_path: str = "", content: str = "", **_) -> str:
        if not relative_path.strip():
            return "[write_project_file rejected: empty relative_path]"
        try:
            target = resolve_in_sandbox(self.project_dir, relative_path)
        except PathEscape as e:
            return f"[write_project_file rejected: {e}]"
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w", encoding="utf-8") as f:
            f.write(content)
        return f"Wrote {len(content)} bytes to {relative_path}"


class ReadProjectFileTool(BaseTool):
    name = "read_project_file"
    description = (
        "Read a file inside the project being built. relative_path must be "
        "relative to the project root — absolute paths and '..' traversal "
        "are refused."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "relative_path": {"type": "string", "description": "Path relative to the project root."},
        },
        "required": ["relative_path"],
    }
    factory_allowed = False
    risk = READ_ONLY

    def __init__(self, project_dir: str) -> None:
        self.project_dir = project_dir

    async def run(self, relative_path: str = "", **_) -> str:
        if not relative_path.strip():
            return "[read_project_file rejected: empty relative_path]"
        try:
            target = resolve_in_sandbox(self.project_dir, relative_path)
        except PathEscape as e:
            return f"[read_project_file rejected: {e}]"
        if not os.path.isfile(target):
            return f"[read_project_file: no such file: {relative_path}]"
        with open(target, "r", encoding="utf-8", errors="replace") as f:
            data = f.read()
        if len(data) > MAX_READ_CHARS:
            return data[:MAX_READ_CHARS] + f"\n[...truncated, {len(data)} chars total]"
        return data


class RunProjectTestsTool(BaseTool):
    name = "run_project_tests"
    description = (
        "Run the project's test command inside its own project directory. "
        f"Hard wall-clock timeout of {TEST_TIMEOUT_SECONDS}s — a hung test "
        "suite is killed and reported as a timeout, not left running."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "The shell command to run, e.g. 'pytest'."},
        },
        "required": ["command"],
    }
    factory_allowed = False
    # Tier 6: runs a shell command that Trillion itself wrote, on Sean's
    # machine. Bubblewrap and the timeout narrow it; nothing makes it routine.
    # Also named in risk.py's HARDLINE_TOOLS, so no mode can clear it.
    risk = HARDLINE

    def __init__(self, project_dir: str) -> None:
        self.project_dir = project_dir

    async def run(self, command: str = "", **_) -> str:
        if not command.strip():
            return "[run_project_tests rejected: empty command]"
        proc = await asyncio.create_subprocess_shell(
            command,
            cwd=self.project_dir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=shell_minimal(),
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=TEST_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return f"[run_project_tests timed out after {TEST_TIMEOUT_SECONDS}s]"
        output = stdout.decode("utf-8", errors="replace")
        if len(output) > MAX_READ_CHARS:
            output = output[:MAX_READ_CHARS] + "\n[...truncated]"
        return f"exit_code={proc.returncode}\n{output}"
