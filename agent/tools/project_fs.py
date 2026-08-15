"""
Sandboxed filesystem tools for the Software Factory's internal build agent
(agent/factory/software/pipeline.py) — never registered on Trillion's main
chat registry or handed to Agent-Factory-spawned specialists. `factory_allowed
= False` on all three keeps them out of Agent Factory's tool_allowlist
intersection; in practice they're also constructed fresh per build and
injected only into the CODING/TESTING step's own private ToolRegistry, never
the shared one build_registry() returns.

Two independent guards do the actual sandboxing:
- resolve_in_sandbox() for the file tools: every relative_path is resolved
  against the build's own project directory, and anything that would land
  outside it — via an absolute path, `..` traversal, or a symlink — is
  refused rather than silently clamped.
- a bubblewrap (bwrap) jail for run_project_tests: an allowlisted executable
  name alone doesn't stop a general-purpose interpreter from reading/writing
  anywhere the host process can, so the test command actually runs inside an
  unprivileged OS-level sandbox with no network, a cleared environment, and a
  filesystem view where only the project's own directory is writable/visible
  as anything other than the toolchain's own read-only binaries/libs.
"""

from __future__ import annotations

import asyncio
import os
import shlex
import shutil

from ..safety.risk import CONSEQUENTIAL, HARDLINE, READ_ONLY
from .base import BaseTool

MAX_READ_CHARS = 20_000
TEST_TIMEOUT_SECONDS = 60

# Test commands run with no shell (no &&, |, `` ` ``, redirection, or env-var
# expansion — argv only) and only these executables, so a planned
# test_command that goes off the rails can't turn "run the tests" into
# arbitrary shell access. Broad enough to cover the common single-language
# toolchains a planned tech_stack is likely to name.
ALLOWED_TEST_EXECUTABLES = {
    "pytest", "python", "python3",
    "npm", "npx", "yarn", "pnpm", "node",
    "go", "cargo", "make",
    "mvn", "gradle", "rspec", "ruby",
}

# Only what a normal build toolchain needs to find its interpreter and write
# its cache/config files — deliberately excludes everything else the parent
# process has (API keys, tokens, etc.) so a test command can't read or leak
# secrets it was never given. This is belt-and-suspenders: the bubblewrap
# jail below (--clearenv) is what actually enforces the empty environment
# inside the sandbox; this just keeps bwrap's own process minimal too.
_ENV_PASSTHROUGH = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "TERM")


def _scrubbed_env() -> dict:
    env = {k: os.environ[k] for k in _ENV_PASSTHROUGH if k in os.environ}
    env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
    return env


# An allowlisted executable name is not a sandbox — python3/node/etc. are
# general-purpose interpreters that, given cwd + a relative path, can read or
# write anywhere the host process can (e.g. `python3 -c "open('../../.env')"`
# from inside generated-projects/<slug>). Only an OS-level jail actually
# bounds that. bubblewrap (unprivileged, no root needed) gives us: no
# network, no environment except what we explicitly set, and a filesystem
# view where the only writable, escapable-from location is the project's own
# directory — everything else the interpreter needs (its own binary, libs)
# is mounted read-only at its real path so toolchains "just work" without
# being able to touch anything outside the project.
_BWRAP = shutil.which("bwrap")

# Host directories a language toolchain typically needs read access to in
# order to run at all (its own interpreter/binaries and shared libraries).
# Bound read-only; anything not listed here (most importantly the repo root
# and its .env) is simply invisible inside the sandbox, not just off-limits.
_SANDBOX_RO_BIND_CANDIDATES = ("/usr", "/bin", "/lib", "/lib64", "/lib32", "/libx32", "/sbin", "/etc/alternatives")


def _sandbox_argv(argv: list, project_dir: str) -> list:
    project_dir = os.path.realpath(project_dir)
    cmd = [
        _BWRAP,
        "--unshare-all",
        "--die-with-parent",
        "--clearenv",
        "--setenv", "PATH", "/usr/local/bin:/usr/bin:/bin",
        "--setenv", "HOME", "/home/sandbox",
        "--setenv", "LANG", "C.UTF-8",
        "--proc", "/proc",
        "--dev", "/dev",
        "--tmpfs", "/tmp",
        "--dir", "/home/sandbox",
    ]
    for host_dir in _SANDBOX_RO_BIND_CANDIDATES:
        if os.path.exists(host_dir):
            cmd += ["--ro-bind", host_dir, host_dir]
    cmd += ["--bind", project_dir, project_dir, "--chdir", project_dir]
    cmd += ["--"] + argv
    return cmd


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
            "command": {
                "type": "string",
                "description": (
                    "The test runner command to run, e.g. 'pytest'. Runs as "
                    "argv (no shell) and must start with one of: "
                    + ", ".join(sorted(ALLOWED_TEST_EXECUTABLES))
                ),
            },
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
        try:
            argv = shlex.split(command)
        except ValueError as e:
            return f"[run_project_tests rejected: could not parse command: {e}]"
        if not argv:
            return "[run_project_tests rejected: empty command]"
        executable = os.path.basename(argv[0])
        if executable not in ALLOWED_TEST_EXECUTABLES:
            return (
                f"[run_project_tests rejected: {executable!r} is not an allowed "
                f"test runner — allowed: {', '.join(sorted(ALLOWED_TEST_EXECUTABLES))}]"
            )
        if not _BWRAP:
            return (
                "[run_project_tests rejected: no sandbox available (bubblewrap/bwrap "
                "not installed) — refusing to run untrusted test commands unsandboxed]"
            )
        try:
            proc = await asyncio.create_subprocess_exec(
                *_sandbox_argv(argv, self.project_dir),
                env=_scrubbed_env(),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except FileNotFoundError as e:
            # The runner itself missing inside the sandbox surfaces as a
            # nonzero bwrap exit (a normal test failure, see below) — this
            # only catches bwrap itself vanishing, so it's still a reported
            # failure rather than an uncaught exception that fails the build.
            return f"[run_project_tests failed to start: {e}]"
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
