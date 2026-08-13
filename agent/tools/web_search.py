"""
web_search — provider-agnostic web search tool, backed by either the Brave
Search API or Firecrawl's search endpoint.

Registered wherever a real search API key is configured: the main chat
registry (agent/tools/registry.py's build_registry(), via
resolve_search_provider()) and the Software Factory's autonomous scheduler's
private opportunity-scout registry
(agent/factory/software/opportunity_scout.py). Client-side by design — the
model calls this tool and Trillion's own code makes the HTTP request — so it
works identically regardless of which LLM provider (Claude/OpenAI/Ollama) is
configured, unlike Anthropic's Claude-only server-side web_search tool.

Backend selection: TRILLION_SEARCH_PROVIDER ("brave" | "firecrawl") wins when
set and its matching key is present. Otherwise auto-pick from whichever key
is configured, preferring Firecrawl if both are set. See
resolve_search_provider().
"""

from __future__ import annotations

import aiohttp

from ..safety.risk import READ_ONLY
from .base import BaseTool

BRAVE_SEARCH_URL = "https://api.search.brave.com/res/v1/web/search"
FIRECRAWL_DEFAULT_BASE_URL = "https://api.firecrawl.dev"
VALID_PROVIDERS = {"brave", "firecrawl"}
DEFAULT_RESULT_COUNT = 8
MAX_RESULT_CHARS = 20_000
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=15, connect=5)


def resolve_search_provider(settings) -> tuple[str, str] | None:
    """Decide which search backend (name, api_key) to use, or None if
    neither is configured.

    Explicit settings.search_provider wins when set and its key is present.
    An explicit value that's unrecognized or whose key is missing fails
    closed (None) rather than silently falling back to a different provider
    than the one requested. When unset entirely, auto-pick whichever key is
    configured, preferring Firecrawl when both are set.
    """
    explicit = (settings.search_provider or "").strip().lower()
    if explicit:
        if explicit == "firecrawl" and settings.firecrawl_api_key:
            return ("firecrawl", settings.firecrawl_api_key)
        if explicit == "brave" and settings.brave_search_api_key:
            return ("brave", settings.brave_search_api_key)
        return None

    if settings.firecrawl_api_key:
        return ("firecrawl", settings.firecrawl_api_key)
    if settings.brave_search_api_key:
        return ("brave", settings.brave_search_api_key)
    return None


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
    # Tier 6: observes the outside world, changes nothing in it. (What comes
    # *back* is untrusted content, which is a separate concern from the gate.)
    risk = READ_ONLY

    def __init__(
        self,
        provider: str,
        api_key: str,
        firecrawl_base_url: str = FIRECRAWL_DEFAULT_BASE_URL,
    ) -> None:
        if provider not in VALID_PROVIDERS:
            raise ValueError(f"Unknown search provider: {provider!r}")
        self._provider = provider
        self._api_key = api_key
        self._firecrawl_base_url = firecrawl_base_url.rstrip("/")

    async def _search(self, query: str, count: int = DEFAULT_RESULT_COUNT) -> dict:
        if self._provider == "firecrawl":
            return await self._search_firecrawl(query, count)
        return await self._search_brave(query, count)

    async def _search_brave(self, query: str, count: int) -> dict:
        headers = {"X-Subscription-Token": self._api_key, "Accept": "application/json"}
        params = {"q": query, "count": count}
        async with aiohttp.ClientSession(timeout=REQUEST_TIMEOUT) as session:
            async with session.get(BRAVE_SEARCH_URL, params=params, headers=headers) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    raise RuntimeError(f"Brave Search API error {resp.status}: {body[:200]}")
                return await resp.json()

    async def _search_firecrawl(self, query: str, count: int) -> dict:
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        payload = {"query": query, "limit": count}
        url = f"{self._firecrawl_base_url}/v1/search"
        async with aiohttp.ClientSession(timeout=REQUEST_TIMEOUT) as session:
            async with session.post(url, json=payload, headers=headers) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    raise RuntimeError(f"Firecrawl Search API error {resp.status}: {body[:200]}")
                return await resp.json()

    def _parse_results(self, data: dict) -> list[dict]:
        if self._provider == "firecrawl":
            return data.get("data", []) or []
        return data.get("web", {}).get("results", []) or []

    async def run(self, query: str = "", **_) -> str:
        if not query.strip():
            return "[web_search rejected: empty query]"
        try:
            data = await self._search(query)
        except Exception as e:  # noqa: BLE001
            return f"[web_search error: {type(e).__name__}: {e}]"

        results = self._parse_results(data)
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
