"""
Spawning Claude Code as a subprocess — playbooks/design-subagent.md, Tier 3.

The split this tier rests on: the *planning* agent stays cheap, and the
*composition* step shells out to Claude Code, which runs in the project root
with narrow Bash permissions and can actually install packages, write TSX,
and run a build. Composition is the expensive step and the one that needs a
real toolchain; planning does not.

This is the most dangerous thing in the design agent, so the constraints are
explicit rather than implied:

**Env is stripped.** `with_keys("ANTHROPIC_API_KEY")` from
agent/security/subprocess_env.py — the OS baseline plus exactly the one
secret the child legitimately needs. Every other key Trillion holds
(Deepgram, GitHub, the mining wallet, the web auth token) stays out of the
child process.

**Tools are an allowlist, and Bash is scoped by prefix.** The playbook is
specific: "Avoid `Bash(*)` — too permissive. Avoid no-Bash — CC can't run the
build." So Bash is permitted only for the commands a build actually needs.

**It is bounded.** max_turns, a wall-clock timeout, and a cost ceiling
enforced by the caller. A subprocess that can spend money needs all three:
turns bound the loop, the timeout bounds a hang, and the ceiling bounds the
bill.

**Success is verified on the filesystem, not reported.** The caller checks
that out/<feature>/<screen>/index.html exists. Claude Code saying it built
something is not evidence that it did.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from dataclasses import dataclass, field

from ..security.subprocess_env import with_keys

# Narrow but functional, straight from the playbook. Bash is prefix-scoped to
# the commands a Next build needs and nothing else.
DEFAULT_ALLOWED_TOOLS = (
    "Read", "Write", "Edit", "Glob", "Grep",
    "Bash(npm install:*)",
    "Bash(npm run:*)",
    "Bash(npx shadcn:*)",
    "Bash(npx shadcn@latest:*)",
    "Bash(npx magicui-cli:*)",
    "Bash(ls:*)",
    "Bash(mkdir:*)",
    "Bash(cat:*)",
)

DEFAULT_MAX_TURNS = 40
# A first dispatch runs npm install for Next 15 on a Raspberry Pi. That is
# slow — minutes, not seconds — so the timeout is generous. It exists to
# bound a hang, not to police a slow build.
DEFAULT_TIMEOUT_SECONDS = 1800.0


class ClaudeCodeError(RuntimeError):
    pass


@dataclass
class ClaudeCodeResult:
    ok: bool = False
    exit_code: int | None = None
    result_text: str = ""
    total_cost_usd: float = 0.0
    num_turns: int = 0
    duration_seconds: float = 0.0
    error: str = ""
    events: list = field(default_factory=list)


def claude_binary() -> str | None:
    """Where the `claude` CLI lives, or None if it isn't installed."""
    return shutil.which("claude")


def build_command(
    prompt: str,
    *,
    model: str | None = None,
    max_turns: int = DEFAULT_MAX_TURNS,
    allowed_tools=DEFAULT_ALLOWED_TOOLS,
) -> list:
    """
    The argv for one composition run.

    Pure and separately testable — the arguments are the security boundary
    (an over-broad --allowedTools is the whole ballgame), so they deserve
    assertions that don't require spawning anything.
    """
    binary = claude_binary()
    if not binary:
        raise ClaudeCodeError(
            "The `claude` CLI is not on PATH. The design agent composes mockups "
            "by spawning it; install it or unset the design tools."
        )
    argv = [
        binary,
        "-p", prompt,
        "--output-format", "stream-json",
        "--verbose",
        "--max-turns", str(int(max_turns)),
        "--allowedTools", ",".join(allowed_tools),
    ]
    if model:
        argv += ["--model", model]
    return argv


def parse_event(line: str) -> dict | None:
    """
    One NDJSON line from the stream into a small shape for the UI.

    Claude Code's stream carries far more than a progress display needs, so
    this narrows rather than forwarding raw events: the playbook wants Sean
    to see "[CC] Read design.md", "[CC] npm run build" as it happens, not a
    firehose. Unparseable lines return None rather than raising — a single
    malformed line must not kill a twenty-minute build.
    """
    line = (line or "").strip()
    if not line:
        return None
    try:
        data = json.loads(line)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None

    kind = data.get("type")
    if kind == "assistant":
        for block in ((data.get("message") or {}).get("content") or []):
            if isinstance(block, dict) and block.get("type") == "tool_use":
                name = block.get("name", "tool")
                target = ""
                args = block.get("input") or {}
                if isinstance(args, dict):
                    target = str(
                        args.get("file_path") or args.get("path")
                        or args.get("command") or args.get("pattern") or ""
                    )[:120]
                return {"type": "tool", "name": name, "target": target}
        return None
    if kind == "result":
        return {
            "type": "result",
            "is_error": bool(data.get("is_error")),
            "result": str(data.get("result") or "")[:2000],
            "total_cost_usd": float(data.get("total_cost_usd") or 0.0),
            "num_turns": int(data.get("num_turns") or 0),
            "duration_ms": int(data.get("duration_ms") or 0),
        }
    if kind == "system" and data.get("subtype") == "init":
        return {"type": "start", "model": str(data.get("model") or "")}
    return None


async def spawn_claude_code(
    prompt: str,
    cwd: str,
    *,
    model: str | None = None,
    max_turns: int = DEFAULT_MAX_TURNS,
    allowed_tools=DEFAULT_ALLOWED_TOOLS,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    on_event=None,
) -> ClaudeCodeResult:
    """
    Run one composition. Never raises for a failed build — a failure comes
    back as a result with ok=False and an error string, because the caller is
    a tool whose job is to hand the model something it can reason about.
    """
    result = ClaudeCodeResult()
    try:
        argv = build_command(prompt, model=model, max_turns=max_turns, allowed_tools=allowed_tools)
    except ClaudeCodeError as e:
        result.error = str(e)
        return result

    if not os.path.isdir(cwd):
        result.error = f"working directory does not exist: {cwd}"
        return result

    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            # Exactly one secret crosses this boundary. Everything else
            # Trillion holds stays on this side of it.
            env=with_keys("ANTHROPIC_API_KEY"),
        )
    except (OSError, ValueError) as e:
        result.error = f"could not start claude: {e}"
        return result

    async def drain() -> None:
        assert process.stdout is not None
        async for raw in process.stdout:
            event = parse_event(raw.decode("utf-8", "replace"))
            if event is None:
                continue
            result.events.append(event)
            if event["type"] == "result":
                result.result_text = event["result"]
                result.total_cost_usd = event["total_cost_usd"]
                result.num_turns = event["num_turns"]
                result.duration_seconds = event["duration_ms"] / 1000.0
                if event["is_error"]:
                    result.error = event["result"] or "claude reported an error"
            if on_event is not None:
                try:
                    on_event(event)
                except Exception:
                    # An observer that throws must never take down the run —
                    # orchestration.md Tier 3, fire-and-forget side effects.
                    pass

    try:
        await asyncio.wait_for(drain(), timeout=timeout_seconds)
        await asyncio.wait_for(process.wait(), timeout=30)
    except asyncio.TimeoutError:
        result.error = f"claude did not finish within {timeout_seconds:.0f}s; killed"
        try:
            process.kill()
            await process.wait()
        except ProcessLookupError:
            pass
        return result
    except Exception as e:  # noqa: BLE001
        result.error = f"claude stream failed: {type(e).__name__}: {e}"
        try:
            process.kill()
        except ProcessLookupError:
            pass
        return result

    result.exit_code = process.returncode
    if process.returncode != 0 and not result.error:
        stderr = b""
        if process.stderr is not None:
            try:
                stderr = await process.stderr.read()
            except Exception:
                stderr = b""
            result.error = (
                stderr.decode("utf-8", "replace").strip()[:500]
                or f"claude exited {process.returncode}"
            )
    result.ok = not result.error and process.returncode == 0
    return result
