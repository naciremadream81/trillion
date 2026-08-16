"""
AUTO/SLIM marker-block parsing for context/self/trillion.md.

Two block kinds:
  AUTO — <!-- AUTO-START: name --> ... <!-- AUTO-END: name -->
         One per generator (generators.py), full detail, regenerated in place.
  SLIM — <!-- SLIM-START --> ... <!-- SLIM-END -->
         The single every-turn-injected summary, budgeted at
         generators.SLIM_CHAR_BUDGET.

replace_*() only rewrites the text strictly between a matched marker pair —
everything else, including hand-written prose living alongside the blocks,
passes through untouched. That's what lets render.py regenerate the doc
without clobbering anything a human wrote into it directly.
"""

from __future__ import annotations

import re


class BlockNotFoundError(ValueError):
    """Raised when a named marker pair isn't present in the document."""


def _auto_pattern(name: str) -> re.Pattern[str]:
    start = re.escape(f"<!-- AUTO-START: {name} -->")
    end = re.escape(f"<!-- AUTO-END: {name} -->")
    return re.compile(f"({start})(.*?)({end})", re.DOTALL)


_SLIM_PATTERN = re.compile(r"(<!-- SLIM-START -->)(.*?)(<!-- SLIM-END -->)", re.DOTALL)


def _replace(pattern: re.Pattern[str], text: str, new_content: str, missing_msg: str) -> str:
    if not pattern.search(text):
        raise BlockNotFoundError(missing_msg)
    stripped = new_content.strip("\n")
    return pattern.sub(lambda m: f"{m.group(1)}\n{stripped}\n{m.group(3)}", text, count=1)


def extract_auto_block(text: str, name: str) -> str:
    match = _auto_pattern(name).search(text)
    if match is None:
        raise BlockNotFoundError(f"no AUTO block named {name!r}")
    return match.group(2).strip("\n")


def replace_auto_block(text: str, name: str, new_content: str) -> str:
    return _replace(_auto_pattern(name), text, new_content, f"no AUTO block named {name!r}")


def extract_slim_block(text: str) -> str:
    match = _SLIM_PATTERN.search(text)
    if match is None:
        raise BlockNotFoundError("no SLIM block")
    return match.group(2).strip("\n")


def replace_slim_block(text: str, new_content: str) -> str:
    return _replace(_SLIM_PATTERN, text, new_content, "no SLIM block")


def auto_block_names(text: str) -> list[str]:
    """Names of all AUTO blocks present, in document order."""
    return re.findall(r"<!-- AUTO-START: (\S+) -->", text)
