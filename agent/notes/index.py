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
    number of files indexed. Missing/unreadable vault_path yields 0 and
    leaves any existing index untouched, rather than replacing it with an
    empty one — the module docstring's "healthy mount, every read errors"
    failure mode means os.path.isdir(vault_path) can pass while os.walk still
    can't list a single entry, so a transient outage must not erase a
    previously-good index that search() is still serving reads from.

    Builds into a scratch table and only swaps it in for "notes" when the
    build looks healthy. Reaching vault_path is necessary but not sufficient:
    the same degraded mount can serve cached directory entries while every
    file read returns EIO, which would walk the whole vault, index nothing,
    and swap a good index for an empty one. So a build that hit *any* read or
    traversal error must also come back no smaller than what's already
    indexed before it is allowed to replace it. A genuinely empty vault (clean
    walk, zero .md files) still replaces the index, matching the old behavior.
    """
    dirname = os.path.dirname(index_path)
    if dirname:
        os.makedirs(dirname, exist_ok=True)

    if not os.path.isdir(vault_path):
        return 0

    conn = sqlite3.connect(index_path)
    try:
        conn.execute("DROP TABLE IF EXISTS notes_new")
        conn.execute("CREATE VIRTUAL TABLE notes_new USING fts5(path, title, content)")

        indexed = 0
        reached_vault = False
        degraded = False

        def _on_walk_error(error: OSError) -> None:
            # os.walk swallows these silently by default, which is exactly how
            # "half the vault became unlistable" turns into a quiet truncation.
            nonlocal degraded
            degraded = True

        for dirpath, dirnames, filenames in os.walk(vault_path, onerror=_on_walk_error):
            reached_vault = True
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
                    degraded = True
                    continue
                conn.execute(
                    "INSERT INTO notes_new (path, title, content) VALUES (?, ?, ?)",
                    (rel_file, filename[: -len(".md")], content),
                )
                indexed += 1

        # A degraded build may only replace the index if it didn't lose
        # ground. First build (no "notes" table yet) counts as 0, so it always
        # lands; a full outage indexes 0 against an existing N and is dropped.
        healthy = reached_vault and (not degraded or indexed >= _indexed_count(conn))
        if healthy:
            conn.execute("DROP TABLE IF EXISTS notes")
            conn.execute("ALTER TABLE notes_new RENAME TO notes")
        else:
            conn.execute("DROP TABLE IF EXISTS notes_new")
            indexed = 0
        conn.commit()
    finally:
        conn.close()
    return indexed


def _indexed_count(conn: sqlite3.Connection) -> int:
    """How many notes the live index currently holds; 0 if there isn't one."""
    try:
        return conn.execute("SELECT count(*) FROM notes").fetchone()[0]
    except sqlite3.OperationalError:
        return 0


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
