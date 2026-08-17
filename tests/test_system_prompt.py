"""
Tests for the system prompt assembly (agent/system_prompt.py).

The load-bearing behavior here is _load_context_docs()'s manifest: every doc
it returns is injected into every single turn, so "the manifest is
authoritative when present" is a token-budget guarantee, not a preference. A
regression that silently reverts to the glob would quietly re-add whatever
generated docs happen to be sitting in context/ to every request.

Run from the project root:
    python -m unittest tests.test_system_prompt
"""

import os
import tempfile
import unittest
from unittest.mock import patch

from agent import system_prompt


class ContextDirCase(unittest.TestCase):
    """Points the module's module-level paths at a temp context/ directory."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._patches = [
            patch.object(system_prompt, "_CONTEXT_DIR", self.tmp),
            patch.object(
                system_prompt, "_MANIFEST_PATH", os.path.join(self.tmp, "_manifest.toml")
            ),
            patch.object(
                system_prompt, "_SELF_KNOWLEDGE_PATH", os.path.join(self.tmp, "self", "trillion.md")
            ),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()

    def write(self, relative_path: str, content: str) -> None:
        path = os.path.join(self.tmp, relative_path)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)


class TestLoadContextDocs(ContextDirCase):
    def test_globs_all_top_level_md_when_no_manifest(self):
        self.write("a-schema.md", "DOC A")
        self.write("b-schema.md", "DOC B")
        docs = system_prompt._load_context_docs()
        self.assertIn("DOC A", docs)
        self.assertIn("DOC B", docs)

    def test_manifest_is_authoritative_unlisted_doc_is_not_loaded(self):
        self.write("listed.md", "LISTED DOC")
        self.write("unlisted.md", "UNLISTED DOC")
        self.write("_manifest.toml", 'docs = ["listed.md"]')
        docs = system_prompt._load_context_docs()
        self.assertIn("LISTED DOC", docs)
        self.assertNotIn("UNLISTED DOC", docs)

    def test_manifest_preserves_listed_order(self):
        self.write("zebra.md", "ZEBRA")
        self.write("apple.md", "APPLE")
        self.write("_manifest.toml", 'docs = ["zebra.md", "apple.md"]')
        docs = system_prompt._load_context_docs()
        self.assertLess(docs.index("ZEBRA"), docs.index("APPLE"))

    def test_manifest_can_name_a_subdirectory(self):
        # P8's generated docs land under context/self/, which the top-level
        # glob would never have found.
        self.write("self/trillion.md", "GENERATED SELF DOC")
        self.write("_manifest.toml", 'docs = ["self/trillion.md"]')
        self.assertIn("GENERATED SELF DOC", system_prompt._load_context_docs())

    def test_listed_but_missing_file_is_skipped_quietly(self):
        self.write("present.md", "PRESENT")
        self.write("_manifest.toml", 'docs = ["present.md", "not-written-yet.md"]')
        docs = system_prompt._load_context_docs()
        self.assertEqual(docs, "PRESENT")

    def test_empty_docs_list_loads_nothing(self):
        # An empty list is a real choice ("inject no context docs"), not a
        # missing manifest — it must not fall back to the glob.
        self.write("would-be-globbed.md", "GLOBBED")
        self.write("_manifest.toml", "docs = []")
        self.assertEqual(system_prompt._load_context_docs(), "")

    def test_path_traversal_entry_is_refused(self):
        outside = os.path.join(os.path.dirname(self.tmp), "outside-secret.md")
        with open(outside, "w", encoding="utf-8") as f:
            f.write("SECRET OUTSIDE CONTEXT")
        self.addCleanup(os.remove, outside)
        self.write("_manifest.toml", f'docs = ["../{os.path.basename(outside)}"]')
        self.assertNotIn(
            "SECRET OUTSIDE CONTEXT", system_prompt._load_context_docs()
        )

    def test_absolute_path_entry_is_refused(self):
        outside = os.path.join(os.path.dirname(self.tmp), "outside-abs.md")
        with open(outside, "w", encoding="utf-8") as f:
            f.write("SECRET ABSOLUTE")
        self.addCleanup(os.remove, outside)
        self.write("_manifest.toml", f'docs = ["{outside}"]')
        self.assertNotIn("SECRET ABSOLUTE", system_prompt._load_context_docs())

    def test_malformed_manifest_falls_back_to_glob(self):
        self.write("a-schema.md", "DOC A")
        self.write("_manifest.toml", "docs = [this is not valid toml")
        self.assertIn("DOC A", system_prompt._load_context_docs())

    def test_missing_context_dir_returns_empty(self):
        with patch.object(system_prompt, "_CONTEXT_DIR", "/nonexistent/context/dir"):
            self.assertEqual(system_prompt._load_context_docs(), "")


class TestBuildSystemPrompt(ContextDirCase):
    def test_includes_base_prompt(self):
        prompt = system_prompt.build_system_prompt()
        self.assertIn("You are Trillion", prompt)

    def test_memory_facts_are_injected_as_bullets(self):
        prompt = system_prompt.build_system_prompt(memory_facts=["Ships on a Pi 5"])
        self.assertIn("What you know about Sean", prompt)
        self.assertIn("- Ships on a Pi 5", prompt)

    def test_no_facts_section_without_facts(self):
        self.assertNotIn(
            "What you know about Sean", system_prompt.build_system_prompt()
        )

    def test_context_docs_appear_under_their_heading(self):
        self.write("schema.md", "TABLE events(id int)")
        self.write("_manifest.toml", 'docs = ["schema.md"]')
        prompt = system_prompt.build_system_prompt()
        self.assertIn("Databases you can query", prompt)
        self.assertIn("TABLE events(id int)", prompt)


class TestLiveSelfKnowledge(ContextDirCase):
    """
    A Codex review on PR #12 flagged that the summary was sourced from a
    committed file rather than the Agent's actual registry — a .env-enabled
    tool could be live while the prompt still claimed it wasn't. These lock
    in that a passed-in tool_registry, not context/self/trillion.md, is the
    source of truth whenever one is available.
    """

    def _registry_with(self, *tool_names):
        from agent.safety.risk import READ_ONLY
        from agent.tools.base import BaseTool
        from agent.tools.registry import ToolRegistry

        registry = ToolRegistry()
        for n in tool_names:
            tool = type(
                n, (BaseTool,),
                {"name": n, "description": "x", "risk": READ_ONLY, "run": lambda self, **kw: ""},
            )()
            registry.register(tool)
        return registry

    def test_tool_registry_drives_the_summary_not_the_committed_file(self):
        # The committed file (if any) says nothing about this tool — a live
        # registry must still surface it.
        registry = self._registry_with("made_up_probe_tool")
        prompt = system_prompt.build_system_prompt(tool_registry=registry)
        self.assertIn("made_up_probe_tool", prompt)
        self.assertIn("What you can do right now", prompt)

    def test_tool_registry_reflects_removed_tools_too(self):
        empty_registry = self._registry_with()
        prompt = system_prompt.build_system_prompt(tool_registry=empty_registry)
        self.assertIn("none right now", prompt)

    def test_falls_back_to_committed_file_when_no_registry_given(self):
        self.write(
            "self/trillion.md",
            "<!-- SLIM-START -->\nFALLBACK FILE CONTENT\n<!-- SLIM-END -->\n",
        )
        prompt = system_prompt.build_system_prompt()
        self.assertIn("FALLBACK FILE CONTENT", prompt)

    def test_no_registry_and_no_file_omits_the_section(self):
        prompt = system_prompt.build_system_prompt()
        self.assertNotIn("What you can do right now", prompt)


if __name__ == "__main__":
    unittest.main()
