"""
Tests for the Anthropic prompt-caching helper.

Run: python -m unittest tests.test_caching
"""

import copy
import unittest

from agent.providers._caching import apply_prompt_caching


class TestApplyPromptCaching(unittest.TestCase):
    def test_system_becomes_cached_block(self):
        system_blocks, _ = apply_prompt_caching("hello", [])
        self.assertEqual(system_blocks[0]["type"], "text")
        self.assertEqual(system_blocks[0]["text"], "hello")
        self.assertEqual(system_blocks[0]["cache_control"], {"type": "ephemeral"})

    def test_last_string_message_converted_and_marked(self):
        _, msgs = apply_prompt_caching("s", [{"role": "user", "content": "hi"}])
        block = msgs[-1]["content"][0]
        self.assertEqual(block["text"], "hi")
        self.assertEqual(block["cache_control"], {"type": "ephemeral"})

    def test_last_block_of_list_content_marked(self):
        history = [{"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "x", "content": "5"},
        ]}]
        _, msgs = apply_prompt_caching("s", history)
        self.assertEqual(msgs[-1]["content"][-1]["cache_control"], {"type": "ephemeral"})

    def test_only_the_last_message_gets_a_breakpoint(self):
        history = [
            {"role": "user", "content": "one"},
            {"role": "assistant", "content": "two"},
            {"role": "user", "content": "three"},
        ]
        _, msgs = apply_prompt_caching("s", history)
        # Earlier messages untouched (plain strings), only the last is a block.
        self.assertEqual(msgs[0]["content"], "one")
        self.assertEqual(msgs[1]["content"], "two")
        self.assertEqual(msgs[-1]["content"][0]["cache_control"], {"type": "ephemeral"})

    def test_does_not_mutate_input(self):
        history = [{"role": "user", "content": "hi"}]
        snapshot = copy.deepcopy(history)
        apply_prompt_caching("s", history)
        self.assertEqual(history, snapshot)  # caller's history unchanged

    def test_empty_history_is_fine(self):
        system_blocks, msgs = apply_prompt_caching("s", [])
        self.assertEqual(msgs, [])
        self.assertTrue(system_blocks)


if __name__ == "__main__":
    unittest.main()
