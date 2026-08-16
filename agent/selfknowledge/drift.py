"""
Drift check for context/self/trillion.md.

The doc is generated (render.py), and generated docs rot the moment someone
hand-edits AUTO/SLIM block content directly instead of running the refresh,
or agent/tools/registry.py / agent/config.py changes without a refresh
following it. check_no_drift() catches both: it recomputes every block from
the live registry/config and compares against what's checked into the file.

Wired into two places:
  - tests/test_selfknowledge_drift.py, so `python -m unittest discover`
    catches it in every normal test run.
  - .pre-commit-config.yaml's local hook, so a commit touching
    agent/tools/registry.py or agent/config.py without a refresh is blocked
    before it lands.
"""

from __future__ import annotations

import os

from . import parser, render


class DriftError(Exception):
    """context/self/trillion.md doesn't match what render.py would generate
    from the current registry/config right now."""


def check_no_drift(path: str = render.DOC_PATH) -> None:
    """Raises DriftError describing every stale/missing block, including a
    missing file. A missing file is drift, not a clean state: Trillion's
    system prompt still expects context/self/trillion.md to exist as the
    fallback source for _load_self_knowledge() (agent/system_prompt.py), so
    an accidental deletion must fail this check the same way a stale block
    would, not pass silently."""
    if not os.path.isfile(path):
        raise DriftError(
            f"{path} is missing. Run `python -m agent.selfknowledge --refresh` to create it."
        )

    with open(path, encoding="utf-8") as f:
        existing = f.read()

    blocks = render.render_blocks()
    stale = []

    for name in ("capabilities", "config-gating"):
        try:
            current = parser.extract_auto_block(existing, name)
        except parser.BlockNotFoundError:
            stale.append(f"AUTO block {name!r} is missing")
            continue
        if current.strip() != blocks[name].strip():
            stale.append(f"AUTO block {name!r} is stale")

    try:
        current_slim = parser.extract_slim_block(existing)
    except parser.BlockNotFoundError:
        stale.append("SLIM block is missing")
    else:
        if current_slim.strip() != blocks["slim"].strip():
            stale.append("SLIM block is stale")

    if stale:
        raise DriftError(
            "; ".join(stale) + ". Run `python -m agent.selfknowledge --refresh` to fix."
        )
