"""
Tests for the Software Factory opportunity scout
(agent/factory/software/opportunity_scout.py).

Run from the project root:
    python -m unittest tests.test_opportunity_scout
"""

import asyncio
import json
import unittest
from unittest.mock import AsyncMock, patch

from agent.factory.software.opportunity_scout import OpportunityScoutError, run_opportunity_scout
from agent.providers.base import BaseProvider, ProviderResponse, TextChunk, TokenUsage, ToolCall
from agent.tools.web_search import WebSearchTool


def run(coro):
    return asyncio.run(coro)


def _valid_report_json(selected_index=2):
    return json.dumps({
        "candidates": [
            {
                "problem": f"Problem {i}",
                "evidence": f"Evidence {i}",
                "source_url": f"https://example.com/{i}",
            }
            for i in range(5)
        ],
        "selected_index": selected_index,
        "selection_reasoning": "This one has the clearest evidence and smallest scope.",
    })


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


class ToolCallingFakeProvider(BaseProvider):
    """First call requests the web_search tool; second call returns the final
    JSON reply — proves the scout can drive a real tool round-trip through
    its private registry, not just parse text."""

    def __init__(self, final_reply: str):
        self._final_reply = final_reply
        self.call_count = 0

    @property
    def model_name(self):
        return "fake-model"

    async def stream(self, messages, system, tools=None):
        self.call_count += 1
        if self.call_count == 1:
            tc = ToolCall(id="tc_1", name="web_search", arguments={"query": "example problem"})
            yield tc
            yield ProviderResponse(text="", tool_calls=[tc], usage=TokenUsage(), model=self.model_name)
        else:
            yield TextChunk(text=self._final_reply)
            yield ProviderResponse(
                text=self._final_reply, tool_calls=[], usage=TokenUsage(), model=self.model_name
            )


class TestRunOpportunityScout(unittest.TestCase):
    def test_valid_reply_returns_report(self):
        provider = FakeProvider([_valid_report_json(selected_index=3)])
        report = run(run_opportunity_scout(["cli productivity tools"], provider, "fake-brave-key"))
        self.assertEqual(len(report["candidates"]), 5)
        self.assertEqual(report["selected_index"], 3)
        self.assertIn("clearest evidence", report["selection_reasoning"])

    def test_invalid_json_then_valid_recovers_on_retry(self):
        provider = FakeProvider(["not json at all", _valid_report_json()])
        report = run(run_opportunity_scout(["cli productivity tools"], provider, "fake-brave-key"))
        self.assertEqual(len(report["candidates"]), 5)

    def test_still_invalid_after_retry_raises(self):
        provider = FakeProvider(["not json", "still not json"])
        with self.assertRaises(OpportunityScoutError):
            run(run_opportunity_scout(["cli productivity tools"], provider, "fake-brave-key"))

    def test_wrong_candidate_count_raises_after_retry(self):
        bad_reply = json.dumps({
            "candidates": [{"problem": "p", "evidence": "e", "source_url": "https://example.com"}],
            "selected_index": 0,
            "selection_reasoning": "only one candidate",
        })
        provider = FakeProvider([bad_reply, bad_reply])
        with self.assertRaises(OpportunityScoutError):
            run(run_opportunity_scout(["cli productivity tools"], provider, "fake-brave-key"))

    def test_out_of_range_selected_index_raises_after_retry(self):
        bad_reply = json.dumps({
            "candidates": [
                {"problem": f"p{i}", "evidence": f"e{i}", "source_url": f"https://example.com/{i}"}
                for i in range(5)
            ],
            "selected_index": 9,
            "selection_reasoning": "reasoning",
        })
        provider = FakeProvider([bad_reply, bad_reply])
        with self.assertRaises(OpportunityScoutError):
            run(run_opportunity_scout(["cli productivity tools"], provider, "fake-brave-key"))

    def test_scout_drives_a_real_web_search_tool_call_through_its_registry(self):
        provider = ToolCallingFakeProvider(_valid_report_json(selected_index=1))
        with patch.object(
            WebSearchTool, "_search", new=AsyncMock(return_value={"web": {"results": []}})
        ) as mock_search:
            report = run(run_opportunity_scout(["cli productivity tools"], provider, "fake-brave-key"))
        mock_search.assert_called_once()
        self.assertEqual(report["selected_index"], 1)


if __name__ == "__main__":
    unittest.main()
