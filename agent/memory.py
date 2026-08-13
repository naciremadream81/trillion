"""
Persistent memory store (Tier 4).

Markdown is the source of truth, not SQLite — Tier 4 explicitly wants facts
Sean can read and edit by hand, and that means zero migration burden if the
storage seam ever changes (see playbooks/hybrid-model.md). One fact per
line, as a plain markdown bullet:

    - Sean's co-founder for Trillion, works from a Pi 5 and a laptop.
    - Prefers dry, direct answers over hedging.

load_facts() is what agent/core.py's memory_facts= constructor argument
gets fed at startup; append_fact()/remove_fact() are what the memory tools
(agent/tools/memory.py) call, then hand the returned list to
Agent.update_memory() so the running system prompt picks up the change
without a restart.
"""

from __future__ import annotations

import os

DEFAULT_MEMORY_PATH = "memory/facts.md"


def load_facts(path: str = DEFAULT_MEMORY_PATH) -> list[str]:
    """
    Read facts from the store. Missing file = no facts yet, not an error —
    the store is created lazily on the first remembered fact.
    """
    if not os.path.isfile(path):
        return []
    facts = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line.startswith("- "):
                facts.append(line[2:].strip())
    return facts


def _write_facts(path: str, facts: list[str]) -> None:
    dirname = os.path.dirname(path)
    if dirname:
        os.makedirs(dirname, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for fact in facts:
            f.write(f"- {fact}\n")


def append_fact(path: str, fact: str) -> list[str]:
    """
    Add a fact and return the full, updated list.

    A no-op (not an error) when the fact is already present verbatim — the
    model calling this tool repeatedly with the same fact shouldn't grow
    the file.
    """
    fact = fact.strip()
    facts = load_facts(path)
    if fact and fact not in facts:
        facts.append(fact)
        _write_facts(path, facts)
    return facts


def remove_fact(path: str, fact: str) -> tuple[bool, list[str]]:
    """
    Remove a fact by exact match and return (removed, updated_list).

    Exact stripped-string match only, deliberately — no fuzzy matching.
    forget_fact is a data-destroying, hardline-gated action; matching the
    wrong fact because it "looked similar" is not an acceptable failure mode.
    """
    fact = fact.strip()
    facts = load_facts(path)
    if fact not in facts:
        return False, facts
    facts = [f for f in facts if f != fact]
    _write_facts(path, facts)
    return True, facts
