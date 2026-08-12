# Software Factory: research-driven autonomous build proposals

*Design doc — 2026-08-12*

## Motivation

The Software Factory's autonomous scheduler (`agent/factory/software/scheduler.py`) currently proposes each self-initiated build with a single LLM call (`propose_brief()`) that just invents a project idea from the configured themes — no grounding in what people actually need. Sean asked for a real research step: search online for problems people are having, narrow a shortlist down to the most promising one, and use that as the brief for one of the day's (up to `factory_daily_build_cap`, default 3) autonomous builds.

Trillion has no web-search capability today — `agent/factory/research.py`'s system prompt already says "Use web_search if you need current information," but no such tool has ever existed anywhere in the codebase (a stale aspiration, confirmed via full-codebase grep). This is new capability, not a fix.

## Scope

In scope: a new `web_search` tool (`agent/tools/web_search.py`), a new config setting, a new research subagent (`agent/factory/software/opportunity_scout.py`), and `scheduler.py`'s `propose_brief()` replacement.

Out of scope: the on-demand `/build <description>` command (unchanged — you still describe exactly what to build), the per-task Dev↔QA pipeline from the prior redesign (unaffected — the scout only changes *what brief* reaches `start_build()`, not what happens after), and `agent/core.py`/`agent/providers/*.py` (no changes needed — see Decision 3).

## Decisions (resolved during brainstorming)

1. **Scoped to the autonomous scheduler only, not on-demand `/build`.** "One of the 3 that it builds" reads as the scheduler's daily build cap. `/build <description>` keeps working exactly as today.

2. **Brave Search API, called from a first-class Trillion tool — not Anthropic's server-side `web_search` tool.** Anthropic's Claude API has a native server-side web-search tool, but it's Claude-specific (no equivalent on the OpenAI/Ollama providers Trillion also supports) and would need special-casing in `agent/core.py`'s tool-call loop (server-side tool-use blocks aren't client-dispatched the way Trillion's `ToolRegistry` tools are). A client-side `web_search` tool — the model calls it, Trillion's own code makes the HTTP request — works identically regardless of `TRILLION_PROVIDER`, using the *existing* Tier 2 tool-calling loop unchanged. This is also literally AGENT.md's original Tier 2 capability #1 ("web search for market intel... revenue ideas"), planned at Tier 0 and never built. Brave over SerpAPI: genuine free tier (2,000 queries/month) vs. paid-only, and a clean REST API with no scraping/ToS gray area.

3. **No changes needed to `agent/core.py` or `agent/providers/*.py`.** Because `web_search` is a normal client-side `BaseTool`, the research subagent is just an `Agent(provider, tool_registry=<registry with only web_search>)` — the same shape every other subagent in this codebase already uses (`planning.py`, `architecture.py`, etc.), running through Trillion's existing tool-call loop. No provider-specific plumbing, no new `Agent` parameters.

4. **`web_search` is registered in both places**: the scheduler's private research-subagent registry, and the main chat registry (`build_registry()`), conditional on `settings.brave_search_api_key` being set — same conditional-registration pattern `query_analytics` already uses for `supabase_analytics_url`. This closes AGENT.md's original gap for regular chat too, not just the scheduler.

5. **Missing `BRAVE_SEARCH_API_KEY`, or a failed research call, skips the tick — no fallback to the old blind-guess `propose_brief()`.** Matches the existing "fail fast, don't silently degrade" posture used elsewhere in this codebase (e.g. `planning.py`'s task-count cap) and the scheduler's own existing pattern of skipping (not crashing) a tick when a cap is hit.

6. **Observability via the existing `description` field — no new storage.** The scout's chosen candidate plus its selection reasoning becomes the build's `description` (passed to `start_build()`), so it shows up in `/builds` and the README's "Original brief" section exactly like any other build — reusing what's already there instead of adding new schema.

## Data flow

```
AutonomousScheduler.tick_once()
  -> (kill switch / themes-unset / build-cap checks, unchanged)
  -> settings.brave_search_api_key not set? -> log + skip tick
  -> run_opportunity_scout(themes, provider, brave_search_api_key)
       -> Agent(provider, tool_registry={web_search})
       -> up to 8 web_search tool calls (Trillion's existing Tier 2 loop)
       -> structured JSON: 5 candidates + selected_index + selection_reasoning
       -> validated with one corrective retry (planning.py's established shape)
  -> scout failed after retry? -> log + skip tick
  -> brief = render(selected candidate + reasoning)
  -> start_build(brief, ..., created_by="factory-auto")   # unchanged from here
```

## Module changes

### `agent/tools/web_search.py` (new)

```python
class WebSearchTool(BaseTool):
    name = "web_search"
    description = "Search the web. Returns titles, URLs, and snippets for the top results."
    input_schema = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "The search query."},
        },
        "required": ["query"],
    }

    def __init__(self, api_key: str) -> None: ...
    async def run(self, query: str = "", **_) -> str: ...
```

`run()` does a `GET https://api.search.brave.com/res/v1/web/search` with header
`X-Subscription-Token: <api_key>` and query params `q=<query>&count=8`, via
`aiohttp` (already a project dependency — no new package). Parses
`response["web"]["results"]` (list of `{title, url, description}`), formats
as a numbered list of `title — url\n  description`, capped at
`MAX_READ_CHARS`-style truncation matching `project_fs.py`'s existing
truncation convention. Non-2xx responses and network errors are caught and
returned as a `[web_search error: ...]` string (never raised) — same
"errors become tool output, not exceptions" convention `query_analytics`
already follows.

### `agent/config.py`

- `Settings` gains `brave_search_api_key: str = ""`.
- `get_settings()` reads it from `BRAVE_SEARCH_API_KEY` (default `""`, same
  pattern as `supabase_analytics_url`).

### `agent/tools/registry.py`

`build_registry()` gains:

```python
if settings.brave_search_api_key:
    from .web_search import WebSearchTool
    registry.register(WebSearchTool(settings.brave_search_api_key))
```

right alongside the existing `query_analytics` conditional registration.

### `agent/factory/software/opportunity_scout.py` (new)

- System prompt (distilled from `agency-agents/product/product-trend-researcher.md`'s
  identity, trimmed to what a single research subagent call actually needs —
  not the source file's full enterprise-consultant feature list): a market-
  intelligence researcher whose job is finding real, evidenced problems
  people are having that a small software project could plausibly solve,
  constrained to the caller's themes.
- `async def run_opportunity_scout(themes: list[str], provider, api_key: str) -> dict`:
  builds a private `ToolRegistry` containing only `WebSearchTool(api_key)`,
  runs an `Agent` through Trillion's existing tool-calling loop (searches
  happen as ordinary `ToolCall`/`tool_result` round-trips — no new loop
  logic), capped implicitly by the existing per-turn tool-call handling;
  explicitly caps total searches at 8 by instructing the model in the
  system prompt and validating the returned candidate count, not by adding
  new iteration-counting machinery.
- Ends by asking for a single JSON object (same "ask for bare JSON, validate,
  one corrective retry" shape as `planning.py`/`agent/factory/research.py`):
  ```json
  {
    "candidates": [
      {"problem": "...", "evidence": "...", "source_url": "..."}
    ],
    "selected_index": 0,
    "selection_reasoning": "..."
  }
  ```
  Validation requires exactly 5 `candidates` (each with all three string
  fields), `selected_index` in range, non-empty `selection_reasoning`.
- Raises `OpportunityScoutError` (mirrors `PlanningError`/`ResearchError`'s
  shape) if the model can't produce a valid report after the retry.

### `agent/factory/software/scheduler.py`

- `propose_brief()` removed; replaced by a call to `run_opportunity_scout()`.
- `tick_once()` gains an early check: if `not self.settings.brave_search_api_key`,
  log and return (skip the tick) — same style as the existing
  `factory_paused`/`factory_autonomous_themes`/build-cap early returns.
- On a successful scout result, render the brief text from the selected
  candidate + its reasoning (e.g. `f"{problem}\n\nWhy this one: {reasoning}\n\n(Source: {source_url})"`)
  and pass that as `description` to `start_build()`, unchanged from there.
- `OpportunityScoutError` is caught alongside the existing
  `FactoryPaused`/`BuildCapExceeded`/`BudgetCapExceeded` skip-and-log
  handling in `tick_once()`.

## Cost containment

- Search calls capped at 8 per scheduler tick (prompted + validated, not a
  hard iteration-counter — matches the lightweight style of this module;
  a genuinely runaway model would still be bounded by the existing
  `MAX_TOOL_ROUNDS = 8` safety valve already in `agent/core.py`'s `Agent.turn()`).
- Brave's free tier (2,000 queries/month ≈ 66/day) comfortably covers up to
  8 searches × up to 3 autonomous builds/day.
- No new LLM-call cost beyond what `propose_brief()` already spent per tick,
  plus the (now-necessary) search-tool round-trips — search results are
  cheap relative to generation tokens.

## Testing strategy

- `tests/test_web_search_tool.py` (new): `WebSearchTool.run()` against a
  fake `aiohttp` response — success path (parses results), empty-results
  path, non-2xx path, network-error path. No live Brave API calls.
- `tests/test_opportunity_scout.py` (new): `run_opportunity_scout()` against
  a `FakeProvider` that returns canned tool-call sequences and a canned
  final JSON reply — valid-candidates success path, invalid-JSON-then-valid
  retry-recovery path, still-invalid-after-retry raises
  `OpportunityScoutError`, wrong `candidates` count raises. No live LLM or
  Brave calls.
- `tests/test_software_scheduler.py` (extend): the existing
  `test_tick_proposes_and_starts_a_build_when_clear` test's `FakeProvider`
  reply sequence needs updating for the new scout-based flow (propose →
  search round-trips → final JSON, instead of one bare-text propose call);
  new tests for the `BRAVE_SEARCH_API_KEY`-unset skip path and the
  scout-failure skip path.
- `tests/test_config.py` or equivalent (extend/create if none exists —
  confirm during planning): `brave_search_api_key` reads from
  `BRAVE_SEARCH_API_KEY` with the same pattern as `supabase_analytics_url`.

## Non-goals

- No change to on-demand `/build`.
- No change to `agent/core.py`, `agent/providers/*.py`, or the per-task
  Dev↔QA pipeline.
- No Anthropic server-side `web_search` tool usage — client-side only, for
  provider-agnosticism.
- No new persistent storage for the top-5 candidates — folded into the
  existing `description` field.
