"""
web_search — provider-agnostic web search tool, backed by the Brave Search
API.

Registered wherever a real search API key is configured: the main chat
registry (agent/tools/registry.py's build_registry(), conditional on
settings.brave_search_api_key) and the Software Factory's autonomous
scheduler's private opportunity-scout registry
(agent/factory/software/opportunity_scout.py). Client-side by design — the
model calls this tool and Trillion's own code makes the HTTP request — so it
works identically regardless of which LLM provider (Claude/OpenAI/Ollama) is
configured, unlike Anthropic's Claude-only server-side web_search tool.
"""

from __future__ import annotations

import aiohttp

from .base import BaseTool

BRAVE_SEARCH_URL = "https://api.search.brave.com/res/v1/web/search"
DEFAULT_RESULT_COUNT = 8
MAX_RESULT_CHARS = 20_000
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=15, connect=5)


class WebSearchTool(BaseTool):
    name = "web_search"
    description = (
        "Search the web. Returns titles, URLs, and short descriptions for "
        "the top results."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "The search query."},
        },
        "required": ["query"],
    }

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key

    async def _search(self, query: str, count: int = DEFAULT_RESULT_COUNT) -> dict:
        headers = {"X-Subscription-Token": self._api_key, "Accept": "application/json"}
        params = {"q": query, "count": count}
        async with aiohttp.ClientSession(timeout=REQUEST_TIMEOUT) as session:
            async with session.get(BRAVE_SEARCH_URL, params=params, headers=headers) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    raise RuntimeError(f"Brave Search API error {resp.status}: {body[:200]}")
                return await resp.json()

    async def run(self, query: str = "", **_) -> str:
        if not query.strip():
            return "[web_search rejected: empty query]"
        try:
            data = await self._search(query)
        except Exception as e:  # noqa: BLE001
            return f"[web_search error: {type(e).__name__}: {e}]"

        results = data.get("web", {}).get("results", [])
        if not results:
            return f"No results found for {query!r}."

        lines = []
        for i, r in enumerate(results, start=1):
            lines.append(
                f"{i}. {r.get('title', '')} — {r.get('url', '')}\n   {r.get('description', '')}"
            )
        output = "\n".join(lines)
        if len(output) > MAX_RESULT_CHARS:
            output = output[:MAX_RESULT_CHARS] + "\n[...truncated]"
        return output
