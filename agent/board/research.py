"""
Seat research, in two stages — playbook/the-board.md Tier 2.

This is where the board earns trust or fails to, and the rule that makes it
work is structural: **the agent that wrote a dossier is never the agent that
checks it.** Two separate calls, and the second one does not see the first
one's reasoning — only the finished claims, with instructions to try to
refute them.

The playbook is not being theoretical about why. Run against a real roster,
that second pass caught a citation to a podcast episode that does not exist
as described; a statistic that was true but framed as a general law when it
was a much narrower comparison; a seat stating a ratio its subject has
publicly disowned; a famous framework attributed to an advisor who had
explicitly credited it to someone else in the very source cited; and a number
traceable only to content-marketing blogs quoting each other.

Every one of those would otherwise have been delivered as sourced advice in a
real person's name.

**A thin dossier of six verified entries is far better than nine with one
invented**, so the fact-check drops rather than repairs by default, and what
it rejected is reported as prominently as what survived.

Entries that survive are marked `sourced`. Anything else is `user` — see
storage.py for why that state is server-owned.
"""

from __future__ import annotations

import json
import logging
import re

from ..tools.registry import ToolRegistry
from ..tools.web_search import FIRECRAWL_DEFAULT_BASE_URL, WebSearchTool
from .dossier import SOURCED, USER

logger = logging.getLogger(__name__)

MIN_ENTRIES = 5
MAX_ENTRIES = 12


class ResearchError(RuntimeError):
    """Raised when a stage can't produce a usable result."""


def _extract_json(text: str) -> dict:
    match = re.search(r"\{.*\}", text or "", re.S)
    if match is None:
        raise ResearchError("no JSON object in the reply")
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError as e:
        raise ResearchError(f"invalid JSON: {e}") from e


def research_prompt(advisor: str, seat_title: str) -> str:
    return (
        f"Research the published thinking of {advisor}, for a board seat "
        f"covering {seat_title or 'their area of expertise'}.\n\n"
        "Find their ACTUAL published positions: books with chapter or section "
        "references, dated talks, named interviews, dated posts. Use "
        "web_search — do not answer from memory. Memory is where the "
        "fabrications come from.\n\n"
        "Rules that decide whether this is useful or actively harmful:\n"
        "- Every entry needs a real, checkable source. A source you cannot "
        "point at is not a source.\n"
        "- Prefer primary sources over summaries. A blog post about what "
        "someone said is not what they said.\n"
        "- If they credited an idea to someone else, it is NOT their "
        "doctrine. Leave it out.\n"
        "- A narrow finding is not a general law. State the scope the source "
        "actually claimed.\n"
        f"- {MIN_ENTRIES} well-sourced entries beat {MAX_ENTRIES} where one "
        "is invented. Stop when you run out of things you can verify.\n\n"
        "Also capture, honestly: what this person reliably pushes back on, "
        "and where their doctrine does NOT transfer. The blind spots section "
        "is used to discount this seat later, so an evasive one makes the "
        "whole board worse than leaving the seat empty.\n\n"
        "Reply with ONLY a JSON object:\n"
        '{"seat": "proposed seat title", "domains": ["pricing", ...], '
        '"entries": [{"title": "...", "source": "...", "body": "..."}], '
        '"objection": "...", "blind_spots": "...", "voice": "..."}'
    )


def factcheck_prompt(advisor: str) -> str:
    """
    The adversarial pass. Deliberately does NOT receive the researcher's
    reasoning — only the finished claims, and an instruction to assume they
    are wrong.
    """
    return (
        f"You are fact-checking a dossier of claims about {advisor}'s "
        "published thinking. Assume the file is wrong and try to refute each "
        "entry. You did not write it and you owe it nothing.\n\n"
        "For every entry, use web_search and answer four questions:\n"
        "1. Does the cited source exist, as described?\n"
        "2. Does that source actually say this?\n"
        "3. Is this THIS person's idea, or one they credited to someone "
        "else? A framework they attributed to another person is not their "
        "doctrine, even if they made it famous.\n"
        "4. Is the framing honest? A narrow finding presented as a general "
        "law is a defect even when every word is true. So is a number or "
        "ratio the person has since publicly disowned.\n\n"
        "Verdict per entry:\n"
        '- "confirmed" — survives all four. Only then.\n'
        '- "corrected" — the claim is real but the framing or source was '
        "wrong; give the corrected text.\n"
        '- "rejected" — cannot be verified, is misattributed, or is framed '
        "dishonestly. Reject freely. A thin dossier is fine; a fabricated "
        "one is not.\n\n"
        "Reply with ONLY a JSON object:\n"
        '{"results": [{"index": 0, "verdict": "confirmed|corrected|rejected", '
        '"reason": "...", "title": "...", "source": "...", "body": "..."}]}\n'
        "For a correction, include the corrected title/source/body. For a "
        "rejection, the reason is what matters."
    )


def _registry(api_key: str, search_provider: str, firecrawl_base_url: str) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(WebSearchTool(search_provider, api_key, firecrawl_base_url=firecrawl_base_url))
    return registry


async def _run(make_agent, system: str, prompt: str) -> dict:
    agent = make_agent(system)
    reply = ""
    async for chunk in agent.turn(prompt):
        reply += chunk
    return _extract_json(reply)


def apply_factcheck(entries: list[dict], results) -> tuple[list[dict], list[dict]]:
    """
    Merge the adversarial pass into the researched entries.

    Returns (surviving, report). Pure so the merge is testable without a
    model — and it is the step where a bug would silently mark an unchecked
    claim `sourced`.

    An entry the fact-check did not mention is NOT confirmed. Silence is not
    a verdict, and defaulting to `sourced` on missing input is exactly the
    fail-open behaviour this tier exists to prevent.
    """
    verdicts: dict[int, dict] = {}
    if isinstance(results, list):
        for item in results:
            if not isinstance(item, dict):
                continue
            try:
                index = int(item.get("index"))
            except (TypeError, ValueError):
                continue
            verdicts[index] = item

    surviving: list[dict] = []
    report: list[dict] = []
    next_number = 1
    for i, entry in enumerate(entries):
        verdict_row = verdicts.get(i)
        verdict = str((verdict_row or {}).get("verdict", "")).strip().lower()
        reason = str((verdict_row or {}).get("reason", "")).strip()

        if verdict_row is None:
            report.append({"title": entry.get("title", ""), "verdict": "rejected",
                           "reason": "the fact-check did not return a verdict for this entry"})
            continue
        if verdict == "rejected":
            report.append({"title": entry.get("title", ""), "verdict": "rejected", "reason": reason})
            continue
        if verdict not in ("confirmed", "corrected"):
            report.append({"title": entry.get("title", ""), "verdict": "rejected",
                           "reason": f"unrecognized verdict {verdict!r}"})
            continue

        merged = dict(entry)
        if verdict == "corrected":
            for key in ("title", "source", "body"):
                value = str(verdict_row.get(key, "")).strip()
                if value:
                    merged[key] = value
        merged["id"] = f"D{next_number}"
        merged["verification"] = SOURCED
        next_number += 1
        surviving.append(merged)
        report.append({"title": merged.get("title", ""), "verdict": verdict, "reason": reason})

    return surviving, report


def render_dossier(seat_id: str, advisor: str, researched: dict, entries: list[dict]) -> str:
    """The markdown a verified seat becomes."""
    lines = [
        "---",
        f"id: {seat_id}",
        f"name: {advisor}",
        f"seat: {researched.get('seat', '')}",
        f"domains: {', '.join(researched.get('domains', []) or [])}",
        "status: active",
        "---",
        "",
        "## Doctrine",
        "",
    ]
    for entry in entries:
        lines += [
            f"### {entry['id']} — {entry.get('title', '')}",
            f"source: {entry.get('source', '')}",
            f"verification: {entry.get('verification', USER)}",
            "",
            entry.get("body", "").strip(),
            "",
        ]
    for heading, key in (
        ("Characteristic objection", "objection"),
        ("Blind spots", "blind_spots"),
        ("Voice", "voice"),
    ):
        lines += [f"## {heading}", "", str(researched.get(key, "")).strip(), ""]
    return "\n".join(lines)


async def research_seat(
    advisor: str,
    seat_id: str,
    seat_title: str,
    make_agent,
    api_key: str,
    *,
    search_provider: str = "brave",
    firecrawl_base_url: str = FIRECRAWL_DEFAULT_BASE_URL,
) -> tuple[str, list[dict]]:
    """
    Research one advisor and return (dossier markdown, fact-check report).

    `make_agent(system)` builds an Agent with the web_search registry — the
    caller supplies it so this module doesn't decide which provider or which
    model does the work.

    Raises ResearchError rather than shipping a thin or unchecked dossier: a
    seat that couldn't be verified must not exist, because an empty roster
    declines questions and a fabricated one answers them wrongly in a real
    person's name.
    """
    researched = await _run(make_agent, "", research_prompt(advisor, seat_title))
    entries = researched.get("entries")
    if not isinstance(entries, list) or not entries:
        raise ResearchError(f"no entries researched for {advisor}")

    # The adversarial pass sees ONLY the claims — never how they were reached.
    claims = [
        {"index": i, "title": str(e.get("title", "")), "source": str(e.get("source", "")),
         "body": str(e.get("body", ""))}
        for i, e in enumerate(entries) if isinstance(e, dict)
    ]
    checked = await _run(
        make_agent,
        "",
        factcheck_prompt(advisor) + "\n\n## The claims\n\n" + json.dumps(claims, indent=2),
    )
    surviving, report = apply_factcheck(entries, checked.get("results"))

    if len(surviving) < MIN_ENTRIES:
        raise ResearchError(
            f"only {len(surviving)} of {len(entries)} entries survived the fact-check for "
            f"{advisor} (need {MIN_ENTRIES}). Rejections: "
            + "; ".join(r["reason"] for r in report if r["verdict"] == "rejected")
        )
    return render_dossier(seat_id, advisor, researched, surviving[:MAX_ENTRIES]), report


def build_search_registry(api_key: str, search_provider: str = "brave",
                          firecrawl_base_url: str = FIRECRAWL_DEFAULT_BASE_URL) -> ToolRegistry:
    """A private registry holding only web_search, for the two research calls."""
    return _registry(api_key, search_provider, firecrawl_base_url)
