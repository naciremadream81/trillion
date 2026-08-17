"""
Tests for the notes index (agent/notes/index.py) and search_notes tool
(agent/tools/notes.py).

Run from the project root:
    python -m unittest tests.test_notes
"""

import asyncio
import os
import tempfile
import unittest

from agent.notes.index import build_index, health_check, search
from agent.safety.risk import READ_ONLY
from agent.tools.notes import SearchNotesTool


def run(coro):
    return asyncio.run(coro)


def _write(path: str, content: str = "") -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


class TestBuildIndexAndSearch(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.vault = self._tmp.name
        self.index_path = os.path.join(self.vault, "_index", "notes.db")

    def tearDown(self):
        self._tmp.cleanup()

    def test_indexes_markdown_files_and_finds_them(self):
        _write(os.path.join(self.vault, "recipe.md"), "How to make sourdough bread at home.")
        indexed = build_index(self.vault, self.index_path)
        self.assertEqual(indexed, 1)

        results = search("sourdough", index_path=self.index_path)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["title"], "recipe")
        self.assertEqual(results[0]["path"], "recipe.md")

    def test_ignores_non_markdown_files(self):
        _write(os.path.join(self.vault, "notes.md"), "trillion project ideas")
        _write(os.path.join(self.vault, "image.png"), "not real png data")
        indexed = build_index(self.vault, self.index_path)
        self.assertEqual(indexed, 1)

    def test_excludes_obsidian_directory(self):
        _write(os.path.join(self.vault, ".obsidian", "workspace.md"), "obsidian config")
        _write(os.path.join(self.vault, "real-note.md"), "a real note")
        indexed = build_index(self.vault, self.index_path)
        self.assertEqual(indexed, 1)
        results = search("obsidian", index_path=self.index_path)
        self.assertEqual(results, [])

    def test_excludes_nested_claude_code_memory_log(self):
        _write(
            os.path.join(self.vault, "03-Agents", "Claude-Code", "_memory-log.md"),
            "past session details",
        )
        _write(os.path.join(self.vault, "03-Agents", "OtherAgent", "notes.md"), "other agent")
        indexed = build_index(self.vault, self.index_path)
        self.assertEqual(indexed, 1)
        results = search("past session", index_path=self.index_path)
        self.assertEqual(results, [])

    def test_missing_vault_path_indexes_zero_not_error(self):
        indexed = build_index(os.path.join(self.vault, "does-not-exist"), self.index_path)
        self.assertEqual(indexed, 0)

    def test_search_with_no_index_returns_empty_list(self):
        results = search("anything", index_path=os.path.join(self.vault, "no-index.db"))
        self.assertEqual(results, [])

    def test_search_with_malformed_query_returns_empty_list_not_raise(self):
        _write(os.path.join(self.vault, "note.md"), "some content")
        build_index(self.vault, self.index_path)
        results = search('"unterminated quote', index_path=self.index_path)
        self.assertEqual(results, [])

    def test_rebuild_replaces_prior_contents(self):
        _write(os.path.join(self.vault, "a.md"), "alpha content")
        build_index(self.vault, self.index_path)
        os.remove(os.path.join(self.vault, "a.md"))
        _write(os.path.join(self.vault, "b.md"), "beta content")
        build_index(self.vault, self.index_path)

        self.assertEqual(search("alpha", index_path=self.index_path), [])
        self.assertEqual(len(search("beta", index_path=self.index_path)), 1)

    def test_rebuild_with_empty_but_readable_vault_replaces_index(self):
        _write(os.path.join(self.vault, "old.md"), "old content")
        build_index(self.vault, self.index_path)
        self.assertEqual(len(search("old", index_path=self.index_path)), 1)

        os.remove(os.path.join(self.vault, "old.md"))
        indexed = build_index(self.vault, self.index_path)

        self.assertEqual(indexed, 0)
        self.assertEqual(search("old", index_path=self.index_path), [])

    @unittest.skipIf(os.name != "posix" or os.geteuid() == 0, "requires POSIX permission enforcement")
    def test_unreadable_vault_preserves_existing_index(self):
        # index_path lives outside self.vault so chmod'ing the vault doesn't
        # also block sqlite from reaching the index file itself.
        _write(os.path.join(self.vault, "keeper.md"), "keeper content")
        with tempfile.TemporaryDirectory() as index_dir:
            index_path = os.path.join(index_dir, "notes.db")
            build_index(self.vault, index_path)
            self.assertEqual(len(search("keeper", index_path=index_path)), 1)

            os.chmod(self.vault, 0o000)
            try:
                # os.path.isdir still succeeds (stat doesn't need to enter
                # the directory) — only os.walk's scandir actually fails.
                indexed = build_index(self.vault, index_path)
            finally:
                os.chmod(self.vault, 0o700)

            self.assertEqual(indexed, 0)
            self.assertEqual(len(search("keeper", index_path=index_path)), 1)


class TestHealthCheck(unittest.TestCase):
    def test_healthy_for_readable_directory(self):
        with tempfile.TemporaryDirectory() as vault:
            self.assertTrue(run(health_check(vault)))

    def test_unhealthy_for_nonexistent_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = os.path.join(tmp, "does-not-exist")
            self.assertFalse(run(health_check(missing)))


class TestSearchNotesTool(unittest.TestCase):
    def test_risk_is_read_only(self):
        self.assertEqual(SearchNotesTool.risk, READ_ONLY)

    def test_empty_query_rejected(self):
        tool = SearchNotesTool(index_path="/tmp/does-not-matter.db")
        result = run(tool.run(query="   "))
        self.assertIn("rejected", result)

    def test_no_results_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            index_path = os.path.join(tmp, "notes.db")
            tool = SearchNotesTool(index_path=index_path)
            result = run(tool.run(query="anything at all"))
            self.assertIn("No notes found", result)

    def test_returns_formatted_results(self):
        with tempfile.TemporaryDirectory() as vault:
            _write(os.path.join(vault, "trillion.md"), "notes about the trillion project")
            index_path = os.path.join(vault, "notes.db")
            build_index(vault, index_path)

            tool = SearchNotesTool(index_path=index_path)
            result = run(tool.run(query="trillion"))
            self.assertIn("trillion.md", result)
            self.assertIn("trillion", result.lower())


if __name__ == "__main__":
    unittest.main()
