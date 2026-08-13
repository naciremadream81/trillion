"""
Tests for the voice-persistence layer (P3 — playbooks/agent-personality.md).

Run: python -m unittest tests.test_personality
"""

import copy
import unittest

from agent.personality import (
    BANNED_OPENERS,
    NEEDLE_TOPICS,
    TONAL_CHECKPOINT,
    VOICE_EXAMPLES,
    append_voice_cue,
)
from agent.providers._caching import apply_prompt_caching


class TestVoiceVocabulary(unittest.TestCase):
    def test_voice_examples_nonempty(self):
        self.assertGreaterEqual(len(VOICE_EXAMPLES), 8)

    def test_banned_openers_nonempty(self):
        self.assertGreater(len(BANNED_OPENERS), 0)

    def test_needle_topics_nonempty(self):
        self.assertGreater(len(NEEDLE_TOPICS), 0)

    def test_tonal_checkpoint_is_short_reinforcement_text(self):
        self.assertIsInstance(TONAL_CHECKPOINT, str)
        self.assertIn("LENGTH", TONAL_CHECKPOINT)
        self.assertIn("VOICE", TONAL_CHECKPOINT)


class TestAppendVoiceCueNoOps(unittest.TestCase):
    def test_empty_messages_is_a_noop(self):
        self.assertEqual(append_voice_cue([]), [])

    def test_last_message_not_user_role_is_a_noop(self):
        messages = [{"role": "assistant", "content": [{"type": "text", "text": "hi"}]}]
        result = append_voice_cue(messages)
        self.assertEqual(result, messages)

    def test_unblocked_string_content_is_a_noop(self):
        # Content that hasn't gone through apply_prompt_caching yet is still
        # a plain string, not a block list — append_voice_cue must not touch
        # it (calling this before caching would corrupt the caching step).
        messages = [{"role": "user", "content": "hello"}]
        result = append_voice_cue(messages)
        self.assertEqual(result, messages)

    def test_tool_result_round_is_a_noop(self):
        # Both a real turn and a tool-result round are role="user" in this
        # codebase's history shape — only the content-block type tells them
        # apart. Appending here would break the tool_use/tool_result pairing
        # the API requires.
        _, msgs = apply_prompt_caching("sys", [
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "x", "content": "5"},
            ]},
        ])
        result = append_voice_cue(msgs)
        self.assertEqual(result, msgs)

    def test_mixed_text_and_tool_result_blocks_is_a_noop(self):
        _, msgs = apply_prompt_caching("sys", [
            {"role": "user", "content": [
                {"type": "text", "text": "here's the result"},
                {"type": "tool_result", "tool_use_id": "x", "content": "5"},
            ]},
        ])
        result = append_voice_cue(msgs)
        self.assertEqual(result, msgs)


class TestAppendVoiceCueAppends(unittest.TestCase):
    def test_appends_a_new_block_to_a_real_user_turn(self):
        _, msgs = apply_prompt_caching("sys", [{"role": "user", "content": "hi"}])
        result = append_voice_cue(msgs)
        content = result[-1]["content"]
        self.assertEqual(len(content), 2)
        self.assertEqual(content[-1]["type"], "text")

    def test_voice_examples_appear_inside_the_cue(self):
        _, msgs = apply_prompt_caching("sys", [{"role": "user", "content": "hi"}])
        result = append_voice_cue(msgs)
        cue_text = result[-1]["content"][-1]["text"]
        for example in VOICE_EXAMPLES[:5]:
            self.assertIn(example, cue_text)

    def test_banned_openers_appear_inside_the_cue(self):
        _, msgs = apply_prompt_caching("sys", [{"role": "user", "content": "hi"}])
        result = append_voice_cue(msgs)
        cue_text = result[-1]["content"][-1]["text"]
        for opener in BANNED_OPENERS:
            self.assertIn(opener, cue_text)

    def test_does_not_mutate_input(self):
        _, msgs = apply_prompt_caching("sys", [{"role": "user", "content": "hi"}])
        snapshot = copy.deepcopy(msgs)
        append_voice_cue(msgs)
        self.assertEqual(msgs, snapshot)

    def test_earlier_messages_untouched(self):
        _, msgs = apply_prompt_caching("sys", [
            {"role": "user", "content": "one"},
            {"role": "assistant", "content": "two"},
            {"role": "user", "content": "three"},
        ])
        result = append_voice_cue(msgs)
        self.assertEqual(result[0]["content"], "one")
        self.assertEqual(result[1]["content"], "two")


class TestByteStabilityOfCachedPrefix(unittest.TestCase):
    """
    The design risk flagged explicitly in the P3 plan: apply_prompt_caching()
    puts the ephemeral cache breakpoint on the last message's existing
    content. If append_voice_cue() altered that block instead of appending a
    fresh one after it, the next turn's replay (reconstructed cue-free from
    history) would no longer byte-match what was cached, and the
    conversation would cache-miss forever. This must be a real regression
    test, not a vibe check.
    """

    def test_cache_control_block_is_byte_identical_before_and_after_cue(self):
        _, msgs = apply_prompt_caching("sys", [{"role": "user", "content": "hi"}])
        cached_block_before = copy.deepcopy(msgs[-1]["content"][0])

        result = append_voice_cue(msgs)
        cached_block_after = result[-1]["content"][0]

        self.assertEqual(cached_block_before, cached_block_after)

    def test_cache_control_marker_still_present_and_alone_on_original_block(self):
        _, msgs = apply_prompt_caching("sys", [{"role": "user", "content": "hi"}])
        result = append_voice_cue(msgs)
        content = result[-1]["content"]

        self.assertEqual(content[0]["cache_control"], {"type": "ephemeral"})
        # The appended cue block carries no cache_control of its own — it
        # rides outside the cached prefix entirely.
        self.assertNotIn("cache_control", content[-1])

    def test_history_replay_without_cue_matches_original_cached_bytes(self):
        # Simulates next turn: history (cue-free) is replayed through
        # apply_prompt_caching again. The resulting cached block must match
        # byte-for-byte what was cached this turn, proving the cue never
        # touched persisted history.
        history = [{"role": "user", "content": "hi"}]
        _, msgs = apply_prompt_caching("sys", history)
        cued = append_voice_cue(msgs)
        self.assertNotEqual(cued, msgs)  # sanity: cue really was appended

        _, replayed = apply_prompt_caching("sys", history)
        self.assertEqual(replayed[-1]["content"][0], msgs[-1]["content"][0])


if __name__ == "__main__":
    unittest.main()
