"""
Local search index over the Aires Ai Brain vault (Tier 2 — search_notes).

Markdown files only, walked from DEFAULT_VAULT_PATH. Two directories are
excluded on principle, not just performance:

  .obsidian/            writes here corrupt Obsidian's own config — this
                         index never needs to touch it anyway (read-only).
  03-Agents/Claude-Code/ this agent's own memory log. Feeding a past
                         session's notes back into a future one's search
                         results is a cross-agent injection channel.

build_index() is resilient to per-file and per-directory read errors on
purpose: the vault mount is an rclone FUSE mount that has been observed to
report itself healthy (systemd active, mounted) while every read returns
Input/output error. A partial vault indexed is more useful than a build that
aborts at the first unreadable file. search() only ever reads the local
SQLite file this module writes — it never touches the mount, which is what
lets search_notes keep working while the mount is down.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3

DEFAULT_VAULT_PATH = os.path.expanduser("~/AiresAiBrain")
DEFAULT_INDEX_PATH = "memory/notes_index.db"

EXCLUDED_DIRS = {
    ".obsidian",
    "03-Agents/Claude-Code",
}


def _is_excluded(rel_path: str) -> bool:
    parts = rel_path.replace(os.sep, "/").split("/")
    for excluded in EXCLUDED_DIRS:
        excluded_parts = excluded.split("/")
        if parts[: len(excluded_parts)] == excluded_parts:
            return True
    return False


def build_index(
    vault_path: str = DEFAULT_VAULT_PATH, index_path: str = DEFAULT_INDEX_PATH
) -> int:
    """
    Rebuild the local index from markdown files in the vault. Returns the
    number of files indexed. Missing/unreadable vault_path yields 0, not an
    error — callers (main.py/serve.py's best-effort startup wiring) treat
    that the same as "nothing to index yet."
    """
    dirname = os.path.dirname(index_path)
    if dirname:
        os.makedirs(dirname, exist_ok=True)

    conn = sqlite3.connect(index_path)
    try:
        conn.execute("DROP TABLE IF EXISTS notes")
        conn.execute("CREATE VIRTUAL TABLE notes USING fts5(path, title, content)")

        indexed = 0
        for dirpath, dirnames, filenames in os.walk(vault_path):
            rel_dir = os.path.relpath(dirpath, vault_path)
            rel_dir = "" if rel_dir == "." else rel_dir
            dirnames[:] = [
                d
                for d in dirnames
                if not _is_excluded(f"{rel_dir}/{d}" if rel_dir else d)
            ]
            for filename in filenames:
                if not filename.lower().endswith(".md"):
                    continue
                rel_file = f"{rel_dir}/{filename}" if rel_dir else filename
                if _is_excluded(rel_file):
                    continue
                try:
                    with open(
                        os.path.join(dirpath, filename), encoding="utf-8", errors="replace"
                    ) as f:
                        content = f.read()
                except OSError:
                    continue
                conn.execute(
                    "INSERT INTO notes (path, title, content) VALUES (?, ?, ?)",
                    (rel_file, filename[: -len(".md")], content),
                )
                indexed += 1
        conn.commit()
    finally:
        conn.close()
    return indexed


def search(
    query: str, index_path: str = DEFAULT_INDEX_PATH, limit: int = 8
) -> list[dict]:
    """Full-text search over the local index. No index yet = no results,
    not an error — search_notes surfaces that as a plain string to the model."""
    if not os.path.isfile(index_path):
        return []
    conn = sqlite3.connect(index_path)
    try:
        try:
            cursor = conn.execute(
                "SELECT path, title, snippet(notes, 2, '[', ']', '...', 20) "
                "FROM notes WHERE notes MATCH ? ORDER BY rank LIMIT ?",
                (query, limit),
            )
        except sqlite3.OperationalError:
            # Malformed FTS5 query syntax (stray quote, dangling operator) —
            # no results rather than a crash the model has to reason about.
            return []
        return [
            {"path": row[0], "title": row[1], "snippet": row[2]}
            for row in cursor.fetchall()
        ]
    finally:
        conn.close()


async def health_check(vault_path: str = DEFAULT_VAULT_PATH, timeout: float = 3.0) -> bool:
    """
    Is the vault mount actually readable right now?

    Deliberately not a systemd or mount-table check — both have been
    observed reporting healthy while every real read errors out. This does
    one bounded real read (os.scandir, off the event loop, under a timeout)
    instead.
    """

    def _scan() -> bool:
        try:
            with os.scandir(vault_path) as it:
                next(it, None)
            return True
        except OSError:
            return False

    try:
        return await asyncio.wait_for(asyncio.to_thread(_scan), timeout=timeout)
    except asyncio.TimeoutError:
        return False
