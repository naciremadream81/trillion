"""
Tests for the Anthropic prompt-caching helper.

Run: python -m unittest tests.test_caching
"""

import copy
import unittest

from agent.personality import _VOICE_CUE, append_voice_cue
from agent.providers._caching import apply_prompt_caching
from agent.system_prompt import build_system_prompt


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


class TestCachedPrefixStaysStable(unittest.TestCase):
    """
    smooth-voice_2 Tier 3's named failure mode: caching is switched on, but
    something that changes every turn sits inside the cached prefix, so the
    whole thing is re-read from scratch each turn and replies creep slower
    the deeper the conversation goes.

    Nothing here asserts a cache *hit* (that needs a live API call, and the
    real hit was confirmed against Anthropic when caching went in). These
    lock in the property that makes a hit possible — the cached prefix is
    byte-identical turn to turn, and the per-turn dynamic text lands
    strictly after every breakpoint.
    """

    def test_system_prompt_is_byte_identical_across_builds(self):
        # A timestamp, a random id, or an unsorted set anywhere in here would
        # invalidate the cached prefix on every single turn.
        self.assertEqual(build_system_prompt(), build_system_prompt())

    def test_system_prompt_stable_with_the_same_memory_facts(self):
        facts = ["Sean runs a Raspberry Pi 5", "Prefers short replies"]
        self.assertEqual(
            build_system_prompt(memory_facts=facts),
            build_system_prompt(memory_facts=list(facts)),
        )

    def test_cached_system_block_does_not_change_as_history_grows(self):
        system = build_system_prompt()
        turn2, _ = apply_prompt_caching(system, [{"role": "user", "content": "one"}])
        turn15, _ = apply_prompt_caching(system, [
            {"role": "user", "content": f"msg {i}"} for i in range(15)
        ])
        self.assertEqual(turn2, turn15)

    def test_voice_cue_lands_after_the_breakpoint_not_inside_it(self):
        # The cue text differs in spirit from turn to turn; if it were folded
        # in before caching it would sit inside the cached region and break it.
        system_blocks, msgs = apply_prompt_caching("s", [{"role": "user", "content": "hi"}])
        with_cue = append_voice_cue(msgs)

        blocks = with_cue[-1]["content"]
        self.assertEqual(blocks[-1]["text"], _VOICE_CUE)
        self.assertNotIn("cache_control", blocks[-1])
        # The breakpoint is still on the block that was last before the cue,
        # so everything up to and including "hi" remains cacheable.
        self.assertEqual(blocks[-2]["cache_control"], {"type": "ephemeral"})
        self.assertEqual(system_blocks[0]["cache_control"], {"type": "ephemeral"})

    def test_appending_the_cue_leaves_the_cached_messages_untouched(self):
        _, msgs = apply_prompt_caching("s", [{"role": "user", "content": "hi"}])
        snapshot = copy.deepcopy(msgs)
        append_voice_cue(msgs)
        self.assertEqual(msgs, snapshot)


if __name__ == "__main__":
    unittest.main()
