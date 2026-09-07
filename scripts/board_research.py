#!/usr/bin/env python
"""
Seat an advisor — playbook/the-board.md Tier 2.

    python scripts/board_research.py "Advisor Name" --seat "Pricing and packaging"

Runs both stages: research, then an INDEPENDENT adversarial pass that assumes
the file is wrong and tries to refute every entry. Only entries that survive
are written, marked `verification: sourced`. Everything else is dropped and
the reason printed.

**Read the rejections.** The playbook is emphatic that what got thrown out is
as informative as what survived — a run that rejects four of nine is the
machinery working, not failing. What it caught against a real roster included
a podcast episode that does not exist as described, a framework the advisor
had explicitly credited to someone else in the very source cited, and a ratio
its subject has publicly disowned. Every one of those would otherwise have
been delivered as sourced advice in a real person's name.

Nothing is written unless at least MIN_ENTRIES survive: a thin dossier of six
verified entries is far better than nine with one invented, and a dossier of
two is not a seat.

Needs a search key (BRAVE_SEARCH_API_KEY or FIRECRAWL_API_KEY) and network
access. Costs real money — two model calls per advisor, each doing its own
web searches.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv  # noqa: E402

from agent.board.convene import SEATS_DIR  # noqa: E402
from agent.board.research import ResearchError, build_search_registry, research_seat  # noqa: E402
from agent.config import get_settings  # noqa: E402
from agent.core import Agent  # noqa: E402
from agent.providers import get_provider  # noqa: E402
from agent.tools.web_search import resolve_search_provider  # noqa: E402


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")


async def run(advisor: str, seat_title: str, provider_name: str) -> int:
    load_dotenv()
    settings = get_settings()

    resolved = resolve_search_provider(settings)
    if resolved is None:
        print("No search provider configured. Set BRAVE_SEARCH_API_KEY or "
              "FIRECRAWL_API_KEY — research without search is research from "
              "memory, which is where the fabrications come from.")
        return 1
    search_provider, search_api_key = resolved
    provider = get_provider(provider_name)
    registry = build_search_registry(search_api_key, search_provider, settings.firecrawl_base_url)

    def make_agent(system: str):
        agent = Agent(provider=provider, tool_registry=registry)
        if system:
            agent.system = system
        return agent

    seat_id = slugify(advisor)
    path = os.path.join(SEATS_DIR, f"{seat_id}.md")
    if os.path.exists(path):
        print(f"{path} already exists. Delete it first if you mean to re-research.")
        return 1

    print(f"Researching {advisor}… (two model calls, each searching)")
    try:
        markdown, report = await research_seat(
            advisor, seat_id, seat_title, make_agent, search_api_key,
            search_provider=search_provider,
            firecrawl_base_url=settings.firecrawl_base_url,
        )
    except ResearchError as e:
        print(f"\nNo dossier written: {e}")
        return 1

    print("\nFact-check report — read the rejections:\n")
    for row in report:
        marker = {"confirmed": "  OK ", "corrected": " FIX ", "rejected": "DROP "}.get(
            row["verdict"], "  ?  "
        )
        print(f"{marker} {row['title']}")
        if row["reason"]:
            print(f"       {row['reason']}")

    kept = sum(1 for r in report if r["verdict"] in ("confirmed", "corrected"))
    print(f"\n{kept} of {len(report)} entries survived.\n")

    os.makedirs(SEATS_DIR, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(markdown)
    print(f"Wrote {path}")
    print("Read it before you trust it. The blind spots section is what the "
          "chair uses to discount this seat — if it reads as evasive, fix it "
          "by hand.")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Research and fact-check one board seat.")
    parser.add_argument("advisor", help="The advisor's name, as published.")
    parser.add_argument("--seat", default="", help="What this seat is for.")
    parser.add_argument("--provider", default=os.getenv("TRILLION_PROVIDER", "claude"),
                        choices=["claude", "openai", "ollama"])
    args = parser.parse_args()
    sys.exit(asyncio.run(run(args.advisor, args.seat, args.provider)))


if __name__ == "__main__":
    main()
