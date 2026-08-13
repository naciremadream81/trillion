"""
Tests for the web_search tool (agent/tools/web_search.py).

Run from the project root:
    python -m unittest tests.test_web_search_tool
"""

import asyncio
import unittest

import aiohttp

from agent.config import Settings
from agent.tools.web_search import (
    FIRECRAWL_DEFAULT_BASE_URL,
    MAX_RESULT_CHARS,
    REQUEST_TIMEOUT,
    WebSearchTool,
    resolve_search_provider,
)


def run(coro):
    return asyncio.run(coro)


class FakeWebSearchTool(WebSearchTool):
    """Overrides _search so run() can be tested without a network call."""

    def __init__(self, response=None, error=None, provider="brave", firecrawl_base_url=FIRECRAWL_DEFAULT_BASE_URL):
        super().__init__(provider=provider, api_key="fake-key", firecrawl_base_url=firecrawl_base_url)
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

    def test_timeout_is_configured(self):
        self.assertIsInstance(REQUEST_TIMEOUT, aiohttp.ClientTimeout)
        self.assertEqual(REQUEST_TIMEOUT.total, 15)
        self.assertEqual(REQUEST_TIMEOUT.connect, 5)

    def test_unknown_provider_rejected(self):
        with self.assertRaises(ValueError):
            WebSearchTool(provider="bing", api_key="fake-key")

    def test_firecrawl_returns_formatted_results(self):
        tool = FakeWebSearchTool(
            provider="firecrawl",
            response={"data": [
                {"title": "Example", "url": "https://example.com", "description": "An example result."},
            ]},
        )
        result = run(tool.run(query="example problem"))
        self.assertIn("Example", result)
        self.assertIn("https://example.com", result)
        self.assertIn("An example result.", result)

    def test_firecrawl_empty_results_reports_cleanly(self):
        tool = FakeWebSearchTool(provider="firecrawl", response={"data": []})
        result = run(tool.run(query="a query with no hits"))
        self.assertIn("No results found", result)

    def test_firecrawl_base_url_defaults_to_cloud_api(self):
        tool = FakeWebSearchTool(provider="firecrawl")
        self.assertEqual(tool._firecrawl_base_url, FIRECRAWL_DEFAULT_BASE_URL)

    def test_firecrawl_base_url_accepts_self_hosted_override(self):
        tool = FakeWebSearchTool(provider="firecrawl", firecrawl_base_url="http://localhost:3002/")
        self.assertEqual(tool._firecrawl_base_url, "http://localhost:3002")


class TestResolveSearchProvider(unittest.TestCase):
    def test_none_when_no_keys_configured(self):
        settings = Settings()
        self.assertIsNone(resolve_search_provider(settings))

    def test_brave_only(self):
        settings = Settings(brave_search_api_key="brave-key")
        self.assertEqual(resolve_search_provider(settings), ("brave", "brave-key"))

    def test_firecrawl_only(self):
        settings = Settings(firecrawl_api_key="fc-key")
        self.assertEqual(resolve_search_provider(settings), ("firecrawl", "fc-key"))

    def test_prefers_firecrawl_when_both_set_and_no_explicit_choice(self):
        settings = Settings(brave_search_api_key="brave-key", firecrawl_api_key="fc-key")
        self.assertEqual(resolve_search_provider(settings), ("firecrawl", "fc-key"))

    def test_explicit_provider_wins(self):
        settings = Settings(
            brave_search_api_key="brave-key",
            firecrawl_api_key="fc-key",
            search_provider="brave",
        )
        self.assertEqual(resolve_search_provider(settings), ("brave", "brave-key"))

    def test_explicit_provider_without_its_key_returns_none(self):
        settings = Settings(brave_search_api_key="brave-key", search_provider="firecrawl")
        self.assertIsNone(resolve_search_provider(settings))

    def test_unknown_explicit_provider_falls_back_to_none(self):
        settings = Settings(brave_search_api_key="brave-key", search_provider="bing")
        self.assertIsNone(resolve_search_provider(settings))


if __name__ == "__main__":
    unittest.main()
