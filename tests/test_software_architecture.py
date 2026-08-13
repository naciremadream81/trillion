"""
Tests for the Software Factory architecture subagent
(agent/factory/software/architecture.py).

Run from the project root:
    python -m unittest tests.test_software_architecture
"""

import asyncio
import unittest

from agent.factory.software.architecture import run_architecture
from agent.providers.base import BaseProvider, ProviderResponse, TextChunk, TokenUsage


def run(coro):
    return asyncio.run(coro)


class FakeProvider(BaseProvider):
    def __init__(self, replies):
        self._replies = list(replies)

    @property
    def model_name(self):
        return "fake-model"

    async def stream(self, messages, system, tools=None):
        text = self._replies.pop(0) if self._replies else ""
        yield TextChunk(text=text)
        yield ProviderResponse(text=text, tool_calls=[], usage=TokenUsage(), model=self.model_name)


class TestRunArchitecture(unittest.TestCase):
    def test_returns_the_providers_markdown_reply(self):
        reply = "# Architecture\n\nA single main.py handles parsing and CSV output."
        provider = FakeProvider([reply])
        plan = {
            "tech_stack": "python",
            "files": ["main.py"],
            "tasks": [{"title": "Implement CLI", "description": "Write main.py.", "acceptance_criteria": "works"}],
        }
        result = run(run_architecture("a CLI that converts markdown tables to CSV", plan, provider))
        self.assertEqual(result, reply)

    def test_no_tools_are_offered_to_the_architecture_agent(self):
        # The architecture stage runs before SCAFFOLDING, so there's nothing
        # on disk yet to read — tool_registry=None, same as planning.py.
        seen_tools = []

        class RecordingProvider(BaseProvider):
            @property
            def model_name(self):
                return "fake-model"

            async def stream(self, messages, system, tools=None):
                seen_tools.append(tools)
                yield TextChunk(text="# Architecture")
                yield ProviderResponse(text="# Architecture", tool_calls=[], usage=TokenUsage(), model=self.model_name)

        plan = {"tech_stack": "python", "files": [], "tasks": []}
        run(run_architecture("project", plan, RecordingProvider()))
        self.assertEqual(seen_tools, [None])


if __name__ == "__main__":
    unittest.main()
