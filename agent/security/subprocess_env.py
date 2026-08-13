"""
Env presets for subprocess spawn sites — agent-security.md §1.3. There is
exactly one spawn site in this codebase today
(`RunProjectTestsTool.run()` in agent/tools/project_fs.py, via
`asyncio.create_subprocess_shell`), and it was inheriting the *full*
process environment — including ANTHROPIC_API_KEY, GITHUB_TOKEN, and
anything else Trillion holds — to run a Software-Factory-built project's
own test command. That project's test suite has no legitimate use for
Trillion's provider or GitHub credentials.

Three presets, narrowest first:
  shell_minimal() — OS baseline only, no secrets. The default for any
                    subprocess that doesn't itself need a Trillion secret.
  with_keys(*keys) — shell_minimal() plus named keys, for the rare
                     subprocess that legitimately needs one (e.g. spawning
                     an LLM CLI that reads its key from the environment).
  full(reason)     — full inherited env, gated behind a required reason
                     string so a diff reviewer sees the justification at
                     the callsite rather than a bare os.environ.copy().
"""

from __future__ import annotations

import os

# OS baseline a shell command needs to run at all — no application secrets.
_BASELINE_KEYS = (
    "HOME",
    "PATH",
    "USER",
    "LOGNAME",
    "LANG",
    "LC_ALL",
    "TMPDIR",
    "TEMP",
    "TMP",
    "SHELL",
    "PWD",
    "DISPLAY",
)


def shell_minimal() -> dict[str, str]:
    """OS baseline env only — no secrets. Default for subprocess spawns."""
    return {key: os.environ[key] for key in _BASELINE_KEYS if key in os.environ}


def with_keys(*keys: str) -> dict[str, str]:
    """shell_minimal() plus the named keys, for subprocesses that legitimately
    need a specific Trillion secret (e.g. spawning an LLM CLI)."""
    env = shell_minimal()
    for key in keys:
        if key in os.environ:
            env[key] = os.environ[key]
    return env


def full(reason: str) -> dict[str, str]:
    """Full inherited environment. Requires a non-empty justification so the
    diff reviewer sees why this callsite needs everything Trillion holds."""
    if not reason or not reason.strip():
        raise ValueError("full() requires a non-empty reason")
    return dict(os.environ)
