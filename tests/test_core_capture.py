"""
Phase 3 verification — capture is wired into the conversation loop without
changing conversation behavior, and a failing repo never breaks a turn.

Drives a real Agent.turn() with a stub provider (no network).

Run from the project root:
    python -m unittest tests.test_core_capture
"""

import unittest
from typing import AsyncIterator

from agent.core import Agent
from agent.cost.recorder import set_usage_repo
from agent.providers.base import (
    BaseProvider,
    ProviderResponse,
    TextChunk,
    ToolCall,
    TokenUsage,
)
from agent.safety.risk import READ_ONLY
from agent.tools.base import BaseTool
from agent.tools.registry import ToolRegistry


class StubProvider(BaseProvider):
    """Yields two text chunks then a ProviderResponse carrying usage."""

    def __init__(self, usage: TokenUsage | None, model: str = "claude-sonnet-4-6"):
        self._usage = usage
        self._model = model

    @property
    def model_name(self) -> str:
        return self._model

    async def stream(self, messages, system, tools=None) -> AsyncIterator:
        yield TextChunk(text="hello")
        yield TextChunk(text=" world")
        yield ProviderResponse(
            text="hello world",
            usage=self._usage,
            model=self._model,
        )


class FakeRepo:
    def __init__(self, raise_on_record: bool = False):
        self.calls: list[dict] = []
        self.raise_on_record = raise_on_record

    def record(self, **kwargs):
        if self.raise_on_record:
            raise RuntimeError("simulated DB failure")
        self.calls.append(kwargs)
        return len(self.calls)


class TestCoreCapture(unittest.IsolatedAsyncioTestCase):
    def tearDown(self):
        set_usage_repo(None)

    async def _run_turn(self, agent: Agent, text: str) -> str:
        out = ""
        async for chunk in agent.turn(text):
            out += chunk
        return out

    async def test_turn_streams_text_and_records_usage(self):
        repo = FakeRepo()
        set_usage_repo(repo)
        agent = Agent(provider=StubProvider(TokenUsage(input_tokens=120, output_tokens=30)))

        reply = await self._run_turn(agent, "hi there")

        # Conversation behavior unchanged: text streams through intact...
        self.assertEqual(reply, "hello world")
        # ...history holds the user + assistant turn...
        self.assertEqual(agent.history[0], {"role": "user", "content": "hi there"})
        self.assertEqual(agent.history[-1]["role"], "assistant")
        self.assertEqual(agent.history[-1]["content"], "hello world")
        # ...and exactly one usage row was recorded, for the returned model.
        self.assertEqual(len(repo.calls), 1)
        self.assertEqual(repo.calls[0]["model"], "claude-sonnet-4-6")
        self.assertEqual(repo.calls[0]["input_tokens"], 120)
        self.assertEqual(repo.calls[0]["output_tokens"], 30)

    async def test_failing_repo_does_not_break_the_turn(self):
        set_usage_repo(FakeRepo(raise_on_record=True))
        agent = Agent(provider=StubProvider(TokenUsage(input_tokens=1, output_tokens=1)))

        reply = await self._run_turn(agent, "hi")

        # The turn completes fully despite the recorder blowing up.
        self.assertEqual(reply, "hello world")
        self.assertEqual(agent.history[-1]["content"], "hello world")

    async def test_no_usage_still_completes_turn(self):
        repo = FakeRepo()
        set_usage_repo(repo)
        agent = Agent(provider=StubProvider(usage=None))

        reply = await self._run_turn(agent, "hi")

        self.assertEqual(reply, "hello world")
        # No usage on the response → nothing recorded, no error.
        self.assertEqual(len(repo.calls), 0)


class _SlowProvider(BaseProvider):
    """Yields one text chunk, then hangs on a second yield forever — long
    enough for a caller to cancel the turn() generator mid-stream via
    aclose() before the round (and the turn) ever completes."""

    @property
    def model_name(self) -> str:
        return "fake"

    async def stream(self, messages, system, tools=None) -> AsyncIterator:
        yield TextChunk(text="partial")
        # Suspend here indefinitely; aclose() on the consuming generator
        # throws GeneratorExit at this await, unwinding the turn() loop
        # without ever reaching the assistant append.
        import asyncio

        await asyncio.Event().wait()
        yield ProviderResponse(text="partial", usage=None, model="fake")  # pragma: no cover


class TestTurnCancellation(unittest.IsolatedAsyncioTestCase):
    """
    Regression test: serve.py's /api/chat handler cancels turn() via
    `await turn.aclose()` on client disconnect (barge-in). Before the fix,
    the pre-loop `self.history.append({"role": "user", ...})` at the top of
    turn() survived a cancellation, so the next turn() call on the same
    Agent (serve.py reuses one per session) appended a second consecutive
    "user" entry — breaking every provider's alternating-role requirement.
    """

    async def test_cancelled_turn_leaves_history_unchanged(self):
        agent = Agent(provider=_SlowProvider())
        pre_turn_history = list(agent.history)

        turn = agent.turn("hello")
        chunk = await turn.__anext__()
        self.assertEqual(chunk, "partial")

        await turn.aclose()

        self.assertEqual(agent.history, pre_turn_history)
        self.assertEqual(len(agent.history), len(pre_turn_history))

    async def test_turn_after_cancellation_does_not_double_up_user_roles(self):
        # A second, normal turn on the same (reused) Agent after a
        # cancelled one must not start with two consecutive "user" entries.
        agent = Agent(provider=StubProvider(usage=None))

        cancelled = agent.turn("first, cancelled")
        # StubProvider finishes fast, but we cancel before consuming it —
        # still exercises the same cleanup path regardless of timing.
        await cancelled.aclose()
        self.assertEqual(agent.history, [])

        reply = ""
        async for c in agent.turn("second, completes"):
            reply += c

        self.assertEqual(reply, "hello world")
        roles = [m["role"] for m in agent.history]
        # No two consecutive "user" entries anywhere in history.
        for a, b in zip(roles, roles[1:]):
            self.assertFalse(a == "user" == b, f"consecutive user roles: {roles}")


class _ToolThenTextProvider(BaseProvider):
    """Round 1: calls a tool. Round 2: finishes with plain text. Records the
    `system` prompt it was handed on each round, for asserting freshness."""

    def __init__(self):
        self.systems_seen: list[str] = []

    @property
    def model_name(self) -> str:
        return "fake"

    async def stream(self, messages, system, tools=None) -> AsyncIterator:
        self.systems_seen.append(system)
        if len(self.systems_seen) == 1:
            yield ToolCall(id="1", name="probe_tool", arguments={})
            yield ProviderResponse(text="", usage=None, model="fake")
        else:
            yield TextChunk(text="done")
            yield ProviderResponse(text="done", usage=None, model="fake")


class _RegistryMutatingTool(BaseTool):
    """Simulates RegistryWatcher/a CLI /approve mutating the shared
    tool_registry mid-turn — registers a second tool as a side effect of
    running, so the round-2 system prompt should see it."""

    name = "probe_tool"
    description = "x"
    risk = READ_ONLY

    def __init__(self, registry: ToolRegistry, new_tool: BaseTool):
        self._registry = registry
        self._new_tool = new_tool

    async def run(self, **kwargs) -> str:
        self._registry.register(self._new_tool)
        return "ok"


class _LateArrivalTool(BaseTool):
    name = "late_arrival_tool"
    description = "x"
    risk = READ_ONLY

    async def run(self, **kwargs) -> str:
        return ""


class TestSystemPromptFreshness(unittest.IsolatedAsyncioTestCase):
    """
    Codex review on PR #13: the system prompt's capability summary was only
    ever built at construction (or after a memory change), so a tool
    registered mid-conversation by RegistryWatcher's background poll or a
    CLI /approve stayed invisible to the prose the model reads, even though
    tools_schema (what the model can actually call) was already being read
    fresh every round.
    """

    async def test_system_prompt_reflects_a_tool_registered_mid_turn(self):
        registry = ToolRegistry()
        new_tool = _LateArrivalTool()
        registry.register(_RegistryMutatingTool(registry, new_tool))
        provider = _ToolThenTextProvider()
        agent = Agent(provider=provider, tool_registry=registry)

        out = ""
        async for chunk in agent.turn("do the thing"):
            out += chunk

        self.assertEqual(len(provider.systems_seen), 2)
        self.assertNotIn("late_arrival_tool", provider.systems_seen[0])
        self.assertIn("late_arrival_tool", provider.systems_seen[1])

    async def test_system_prompt_override_never_gets_rebuilt(self):
        # A spawned Factory specialist's prompt (agent/factory/dispatch.py's
        # ConfigDrivenAgent) must survive untouched across rounds — it is
        # not Trillion's own personality/capability summary.
        registry = ToolRegistry()
        new_tool = _LateArrivalTool()
        registry.register(_RegistryMutatingTool(registry, new_tool))
        provider = _ToolThenTextProvider()
        agent = Agent(
            provider=provider,
            tool_registry=registry,
            system_prompt_override="SPECIALIST PROMPT, FIXED FOREVER",
        )

        async for _ in agent.turn("do the thing"):
            pass

        self.assertTrue(
            all(s == "SPECIALIST PROMPT, FIXED FOREVER" for s in provider.systems_seen)
        )


if __name__ == "__main__":
    unittest.main()
