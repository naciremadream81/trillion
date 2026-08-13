"""
search_notes — full-text search over the local notes index (Tier 2).

Read-only by construction: this module ships no write tool at all, so
there's no flag to accidentally flip on. Queries never touch the vault
mount directly — see agent/notes/index.py for why the index is a local
SQLite file refreshed separately, rather than live vault reads.
"""

from __future__ import annotations

from ..notes.index import DEFAULT_INDEX_PATH, search
from ..safety.risk import READ_ONLY
from .base import BaseTool


class SearchNotesTool(BaseTool):
    name = "search_notes"
    description = (
        "Search Sean's notes vault by keyword. Returns matching note titles, "
        "paths, and short snippets. Read-only — there is no way to write or "
        "delete notes through this tool."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "The search query."},
        },
        "required": ["query"],
    }
    # Tier 6: observes the local index, changes nothing.
    risk = READ_ONLY

    def __init__(self, index_path: str = DEFAULT_INDEX_PATH) -> None:
        self._index_path = index_path

    async def run(self, query: str = "", **_) -> str:
        if not query.strip():
            return "[search_notes rejected: empty query]"
        results = search(query, index_path=self._index_path)
        if not results:
            return f"No notes found for {query!r}."
        lines = [
            f"{i}. {r['title']} ({r['path']})\n   {r['snippet']}"
            for i, r in enumerate(results, start=1)
        ]
        return "\n".join(lines)
