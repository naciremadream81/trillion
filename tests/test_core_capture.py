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
    TokenUsage,
)


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


if __name__ == "__main__":
    unittest.main()
