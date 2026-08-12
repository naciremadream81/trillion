"""
Tests for the web_search tool (agent/tools/web_search.py).

Run from the project root:
    python -m unittest tests.test_web_search_tool
"""

import asyncio
import unittest

from agent.tools.web_search import MAX_RESULT_CHARS, WebSearchTool


def run(coro):
    return asyncio.run(coro)


class FakeWebSearchTool(WebSearchTool):
    """Overrides _search so run() can be tested without a network call."""

    def __init__(self, response=None, error=None):
        super().__init__(api_key="fake-key")
        self._response = response
        self._error = error

    async def _search(self, query: str, count: int = 8) -> dict:
        if self._error is not None:
            raise self._error
        return self._response


class TestWebSearchTool(unittest.TestCase):
    def test_returns_formatted_results(self):
        tool = FakeWebSearchTool(response={
            "web": {"results": [
                {"title": "Example", "url": "https://example.com", "description": "An example result."},
            ]}
        })
        result = run(tool.run(query="example problem"))
        self.assertIn("Example", result)
        self.assertIn("https://example.com", result)
        self.assertIn("An example result.", result)

    def test_empty_results_reports_cleanly(self):
        tool = FakeWebSearchTool(response={"web": {"results": []}})
        result = run(tool.run(query="a query with no hits"))
        self.assertIn("No results found", result)

    def test_empty_query_rejected(self):
        tool = FakeWebSearchTool(response={"web": {"results": []}})
        result = run(tool.run(query="   "))
        self.assertIn("rejected", result)

    def test_api_error_status_reported_not_raised(self):
        tool = FakeWebSearchTool(error=RuntimeError("Brave Search API error 401: unauthorized"))
        result = run(tool.run(query="example"))
        self.assertIn("web_search error", result)
        self.assertIn("401", result)

    def test_network_error_reported_not_raised(self):
        tool = FakeWebSearchTool(error=ConnectionError("network unreachable"))
        result = run(tool.run(query="example"))
        self.assertIn("web_search error", result)

    def test_long_output_is_truncated(self):
        many_results = [
            {"title": f"Result {i}", "url": f"https://example.com/{i}", "description": "x" * 500}
            for i in range(100)
        ]
        tool = FakeWebSearchTool(response={"web": {"results": many_results}})
        result = run(tool.run(query="example"))
        self.assertIn("truncated", result)
        self.assertLess(len(result), MAX_RESULT_CHARS + 500)


if __name__ == "__main__":
    unittest.main()
