"""
Tests for the Tier 4 memory store (agent/memory.py, agent/tools/memory.py).

Run: python -m unittest tests.test_memory
"""

import asyncio
import os
import shutil
import tempfile
import unittest

from agent.memory import append_fact, load_facts, remove_fact
from agent.safety.risk import HARDLINE_TOOLS, is_hardline
from agent.tools.memory import ForgetFactTool, RememberFactTool


class TestLoadFacts(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.path = os.path.join(self.tmpdir, "facts.md")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_missing_file_returns_empty_list(self):
        self.assertEqual(load_facts(self.path), [])

    def test_reads_bullet_lines(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("- Sean prefers dry answers.\n- Works from a Pi 5.\n")
        self.assertEqual(
            load_facts(self.path),
            ["Sean prefers dry answers.", "Works from a Pi 5."],
        )

    def test_ignores_non_bullet_lines(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("# Memory\n\n- A real fact.\nnot a bullet\n")
        self.assertEqual(load_facts(self.path), ["A real fact."])


class TestAppendFact(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.path = os.path.join(self.tmpdir, "nested", "facts.md")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_creates_missing_directories(self):
        append_fact(self.path, "First fact.")
        self.assertTrue(os.path.isfile(self.path))

    def test_returns_updated_list(self):
        result = append_fact(self.path, "First fact.")
        self.assertEqual(result, ["First fact."])

    def test_persists_across_reload(self):
        append_fact(self.path, "First fact.")
        self.assertEqual(load_facts(self.path), ["First fact."])

    def test_duplicate_fact_is_a_noop(self):
        append_fact(self.path, "Same fact.")
        result = append_fact(self.path, "Same fact.")
        self.assertEqual(result, ["Same fact."])

    def test_strips_whitespace(self):
        append_fact(self.path, "  Padded fact.  ")
        self.assertEqual(load_facts(self.path), ["Padded fact."])

    def test_hand_edit_is_respected_on_next_load(self):
        # Simulates Sean editing the file directly, then the store being
        # read again — this is Tier 4's explicit verification bar.
        append_fact(self.path, "Original fact.")
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("- Hand-edited fact.\n")
        self.assertEqual(load_facts(self.path), ["Hand-edited fact."])


class TestRemoveFact(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.path = os.path.join(self.tmpdir, "facts.md")
        append_fact(self.path, "Keep this.")
        append_fact(self.path, "Remove this.")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_removes_exact_match(self):
        removed, facts = remove_fact(self.path, "Remove this.")
        self.assertTrue(removed)
        self.assertEqual(facts, ["Keep this."])

    def test_no_match_returns_false_and_unchanged_list(self):
        removed, facts = remove_fact(self.path, "Not present.")
        self.assertFalse(removed)
        self.assertEqual(facts, ["Keep this.", "Remove this."])

    def test_partial_match_does_not_remove(self):
        removed, facts = remove_fact(self.path, "Remove")
        self.assertFalse(removed)
        self.assertEqual(len(facts), 2)

    def test_persists_removal_across_reload(self):
        remove_fact(self.path, "Remove this.")
        self.assertEqual(load_facts(self.path), ["Keep this."])


class TestRememberFactTool(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.path = os.path.join(self.tmpdir, "facts.md")
        self.seen = []
        self.tool = RememberFactTool(self.path, self.seen.append)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    async def test_saves_fact_and_calls_on_change(self):
        result = await self.tool.run(fact="Sean likes terse replies.")
        self.assertIn("Sean likes terse replies.", result)
        self.assertEqual(self.seen[-1], ["Sean likes terse replies."])

    async def test_empty_fact_is_rejected(self):
        result = await self.tool.run(fact="  ")
        self.assertIn("needs a non-empty fact", result)
        self.assertEqual(self.seen, [])

    def test_risk_is_low(self):
        from agent.safety.risk import LOW

        self.assertEqual(self.tool.risk, LOW)


class TestForgetFactTool(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.path = os.path.join(self.tmpdir, "facts.md")
        append_fact(self.path, "Fact to forget.")
        self.seen = []
        self.tool = ForgetFactTool(self.path, self.seen.append)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    async def test_removes_fact_and_calls_on_change(self):
        result = await self.tool.run(fact="Fact to forget.")
        self.assertIn("Forgot", result)
        self.assertEqual(self.seen[-1], [])

    async def test_no_exact_match_does_not_call_on_change(self):
        result = await self.tool.run(fact="Not stored.")
        self.assertIn("No exact match", result)
        self.assertEqual(self.seen, [])

    def test_name_is_hardline_gated(self):
        # forget_fact deletes data — it must be gated in every confirmation
        # mode regardless of what the tool class declares. This is what
        # actually enforces that, not the risk=HARDLINE class attribute.
        self.assertIn("forget_fact", HARDLINE_TOOLS)
        self.assertTrue(is_hardline("forget_fact", risk=None))
        self.assertTrue(is_hardline("forget_fact"))


class TestAgentWiring(unittest.TestCase):
    """
    Confirms Agent.__init__ registers both tools and that update_memory()
    rebuilds the system prompt to include newly remembered facts — the
    seam main.py/serve.py rely on for the "restart and it still knows"
    verification bar.

    Memory-tool registration requires a gate (mirrors ConfirmActionTool —
    see agent/core.py), so these tests build a real Gate rather than
    passing gate=None, the same way main.py/serve.py always do.
    """

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.path = os.path.join(self.tmpdir, "facts.md")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _make_agent(self, memory_facts=None):
        from agent.core import Agent
        from agent.providers.base import BaseProvider
        from agent.safety.approval import Gate
        from agent.safety.storage import SafetyRepo
        from agent.tools.registry import ToolRegistry

        class FakeProvider(BaseProvider):
            @property
            def model_name(self):
                return "fake-model"

            async def stream(self, messages, system, tools=None):
                return
                yield  # pragma: no cover - makes this an async generator

        registry = ToolRegistry()
        repo = SafetyRepo(os.path.join(self.tmpdir, "safety.db"))
        gate = Gate(repo, registry, mode="off")
        return Agent(
            provider=FakeProvider(),
            memory_facts=memory_facts,
            tool_registry=registry,
            gate=gate,
            memory_path=self.path,
        )

    def test_registers_both_memory_tools(self):
        agent = self._make_agent()
        self.assertIn("remember_fact", agent.tool_registry.names())
        self.assertIn("forget_fact", agent.tool_registry.names())

    def test_system_prompt_reflects_initial_memory_facts(self):
        agent = self._make_agent(memory_facts=["Sean's a co-founder."])
        self.assertIn("Sean's a co-founder.", agent.system)

    def test_remembering_a_fact_updates_the_live_system_prompt(self):
        agent = self._make_agent()
        remember = agent.tool_registry.get("remember_fact")
        asyncio.run(remember.run(fact="New durable fact."))
        self.assertIn("New durable fact.", agent.system)

    def test_next_agent_construction_sees_persisted_fact(self):
        agent = self._make_agent()
        remember = agent.tool_registry.get("remember_fact")
        asyncio.run(remember.run(fact="Survives a restart."))

        # Simulates the next process start: load_facts() reads what the
        # previous session wrote, independent of any in-memory state.
        from agent.memory import load_facts

        reloaded = self._make_agent(memory_facts=load_facts(self.path))
        self.assertIn("Survives a restart.", reloaded.system)


if __name__ == "__main__":
    unittest.main()
