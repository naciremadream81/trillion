"""
Software Factory opportunity scout: researches real problems within the
autonomous scheduler's configured themes and selects the most promising one
to build next.

Same two-shot-with-retry shape as agent/factory/research.py's
run_research()/agent/factory/software/planning.py's run_planning(): ask for
bare JSON, validate, one corrective retry before giving up.

Unlike those modules, this one gives its Agent a private ToolRegistry
containing only web_search — the model does its own research via ordinary
tool calls (Trillion's existing Tier 2 tool-calling loop in agent/core.py),
then reports back what it found.
"""

from __future__ import annotations

import hashlib
import json
import re

from ...core import Agent
from ...tools.registry import ToolRegistry
from ...tools.web_search import FIRECRAWL_DEFAULT_BASE_URL, WebSearchTool
from .doctrine import DocumentOverrides, Lane, doctrine_prompt

REQUIRED_CANDIDATE_FIELDS = ("problem", "evidence", "source_url")
CANDIDATE_COUNT = 5


class OpportunityScoutError(Exception):
    """Raised when the opportunity scout can't produce a valid report."""


def _scout_system_prompt(
    themes: list[str],
    lane: Lane | None = None,
    overrides: DocumentOverrides | None = None,
    already_seen: list[dict] | None = None,
) -> str:
    """
    The scout's system prompt: the operator's doctrine, then the two things
    only this run knows — the themes it is authorized to hunt in, and which
    hunting lane fired today.

    The doctrine comes first and the run's constraints after, so an operator
    editing DOCTRINE.md is editing the top of the prompt and can see exactly
    what they are changing. The hard boundary (themes) is restated here
    rather than left to the doctrine because it is a safety property of the
    run, not an instruction the operator should be able to edit away.
    """
    parts = [doctrine_prompt(overrides)]
    parts.append(
        "## This run\n\n"
        "Sean has authorized self-initiated builds within these themes only: "
        f"{', '.join(themes)}. Everything you bring back must sit inside one "
        "of them. Use web_search to do the research — search up to 8 times "
        "before answering; a handful of focused searches is usually enough."
    )
    if lane is not None:
        parts.append(
            f"## Today's lane: {lane.title}\n\n"
            f"{lane.brief}\n\n"
            "Hunt this lane specifically. It narrows where you look, not what "
            "counts as evidence — the doctrine above still governs that."
        )
    note = _already_seen_note(already_seen or [])
    if note:
        parts.append(note)
    return "\n\n".join(parts)


def fingerprint(candidate: dict) -> str:
    """
    A stable identity for "the same problem", so a repeat sighting collapses
    onto one row instead of accumulating near-duplicates.

    Built from the source URL when there is one, because two runs describing
    the same forum thread are the same finding however differently they word
    it. Falls back to the normalized problem statement — weaker, since the
    model rarely phrases a problem identically twice, but better than minting
    a fresh identity for every candidate with no link.

    A hash rather than the raw text: it is a primary key, it should be a
    bounded length, and nothing reads it for meaning.
    """
    source = str(candidate.get("source_url", "")).strip().lower()
    if source:
        # Trailing slashes and tracking params are not identity.
        source = re.sub(r"[?#].*$", "", source).rstrip("/")
        basis = f"url:{source}"
    else:
        problem = re.sub(r"\s+", " ", str(candidate.get("problem", "")).strip().lower())
        basis = f"problem:{problem}"
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:32]


def _already_seen_note(seen: list[dict]) -> str:
    """
    The repetition memory — playbook/opportunity-scout.md Tier 6.

    Cheap, and it compounds: telling the scout what it already found stops
    the rotation from re-reporting the same three problems every week, and
    the ones it re-finds anyway are the ones with real staying power.

    Deliberately phrased as "find something new, but say so if you find one
    of these again" rather than a flat ban. A problem sighted five times
    across a month is a stronger signal than a fresh one, and forbidding the
    repeat would throw that away.
    """
    if not seen:
        return ""
    lines = []
    for row in seen:
        label = str(row.get("problem", "")).strip().replace("\n", " ")
        if len(label) > 140:
            label = label[:137] + "..."
        times = int(row.get("times_seen", 1) or 1)
        suffix = f" (seen {times}x)" if times > 1 else ""
        lines.append(f"- {label}{suffix}")
    return (
        "## Already seen\n\n"
        "Previous runs have already surfaced these. Prefer something new. If "
        "you do find one of them again with materially better evidence, "
        "include it and say explicitly that it is a repeat and what changed — "
        "a problem that keeps reappearing is a stronger signal than a fresh "
        "one, not a wasted slot.\n\n" + "\n".join(lines)
    )


def _final_ask() -> str:
    return (
        "Based on your research, reply with ONLY a single JSON object, no "
        "prose before or after, matching exactly this shape:\n"
        '{"candidates": [{"problem": "...", "evidence": "...", '
        '"source_url": "..."}, ...], "selected_index": 0, '
        '"selection_reasoning": "..."}\n'
        f"candidates must have exactly {CANDIDATE_COUNT} entries, each a "
        "real problem you found evidence for online. selected_index "
        f"(0-{CANDIDATE_COUNT - 1}) is the one you think is most likely to "
        "succeed as a small software project. selection_reasoning explains "
        "why, in 1-3 sentences."
    )


def _extract_json(text: str) -> dict:
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        raise OpportunityScoutError("no JSON object found in the model's reply")
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError as e:
        raise OpportunityScoutError(f"invalid JSON: {e}") from e


def _validate_report(data: dict) -> dict:
    candidates = data.get("candidates")
    if not isinstance(candidates, list) or len(candidates) != CANDIDATE_COUNT:
        raise OpportunityScoutError(f"'candidates' must be a list of exactly {CANDIDATE_COUNT} items")

    validated_candidates = []
    for i, c in enumerate(candidates):
        if not isinstance(c, dict):
            raise OpportunityScoutError(f"candidate {i} must be an object")
        missing = [f for f in REQUIRED_CANDIDATE_FIELDS if f not in c]
        if missing:
            raise OpportunityScoutError(f"candidate {i} missing fields: {', '.join(missing)}")
        validated_candidates.append({f: str(c[f]) for f in REQUIRED_CANDIDATE_FIELDS})

    selected_index = data.get("selected_index")
    if (
        not isinstance(selected_index, int)
        or isinstance(selected_index, bool)
        or not (0 <= selected_index < CANDIDATE_COUNT)
    ):
        raise OpportunityScoutError(
            f"'selected_index' must be an integer between 0 and {CANDIDATE_COUNT - 1}"
        )

    reasoning = str(data.get("selection_reasoning", "")).strip()
    if not reasoning:
        raise OpportunityScoutError("'selection_reasoning' must not be empty")

    return {
        "candidates": validated_candidates,
        "selected_index": selected_index,
        "selection_reasoning": reasoning,
    }


def _has_search_evidence(history: list[dict]) -> bool:
    """True if any assistant turn in the conversation actually made a
    web_search tool call. Only Claude's provider drives tool calls today
    (see agent/providers/ — only the Claude provider yields ToolCall), so on
    other providers this guards against the model fabricating a plausible
    but entirely unresearched report."""
    for message in history:
        if message.get("role") != "assistant":
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("name") == "web_search":
                return True
    return False


async def run_opportunity_scout(
    themes: list[str],
    provider,
    api_key: str,
    search_provider: str = "brave",
    firecrawl_base_url: str = FIRECRAWL_DEFAULT_BASE_URL,
    lane: Lane | None = None,
    overrides: DocumentOverrides | None = None,
    already_seen: list[dict] | None = None,
) -> dict:
    """
    Run the opportunity scout and return a validated report:
    {"candidates": [...5 items...], "selected_index": int, "selection_reasoning": str,
     "lane": str | None}.
    Raises OpportunityScoutError if the model can't produce a valid report
    after one corrective retry.

    `lane` is the hunting strategy that fired for this run (see
    doctrine.py's lane_for()); its label rides back on the report so the
    record of which lane found what survives the run. `overrides` is the
    operator's document cache — omitted, the shipped DOCTRINE.md and
    LANES.md are used, which is what the tests and a bare call want.
    """
    registry = ToolRegistry()
    registry.register(WebSearchTool(search_provider, api_key, firecrawl_base_url=firecrawl_base_url))

    agent = Agent(provider=provider, tool_registry=registry)
    agent.system = _scout_system_prompt(themes, lane, overrides, already_seen)
    prompt = _final_ask()

    last_error: Exception | None = None
    for attempt in range(2):  # one shot + one corrective retry
        if attempt == 1:
            prompt = (
                f"That reply wasn't valid JSON matching the required shape "
                f"({last_error}). Reply again with ONLY the corrected JSON object."
            )
        reply = ""
        async for chunk in agent.turn(prompt):
            reply += chunk
        try:
            data = _extract_json(reply)
            report = _validate_report(data)
        except OpportunityScoutError as e:
            last_error = e
            continue
        if not _has_search_evidence(agent.history):
            last_error = OpportunityScoutError(
                "no evidence web_search was ever called — refusing an unresearched report"
            )
            continue
        report["lane"] = lane.label if lane is not None else None
        return report

    raise OpportunityScoutError(f"failed after retry: {last_error}")
