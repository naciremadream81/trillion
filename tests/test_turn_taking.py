"""
Tests for sign-off detection (Tier 5).

These double as the documented behavior — when a real goodbye slips through or
a reply is wrongly suppressed, add the phrase here and adjust turn_taking.py.

Run: python -m unittest tests.test_turn_taking
"""

import unittest

from agent.turn_taking import is_signoff


def sig(text):
    return is_signoff(text, has_assistant_spoken=True)


class TestSignOffs(unittest.TestCase):
    def test_clear_signoffs_are_detected(self):
        for t in [
            "thanks", "thank you", "thanks so much", "okay thanks",
            "got it, thanks", "sounds good", "will do", "perfect", "cool",
            "great", "cool cool cool", "bye", "goodbye", "see you later",
            "right on", "no thanks", "appreciate it", "have a good one",
            "okay perfect",
        ]:
            self.assertTrue(sig(t), f"should be a sign-off: {t!r}")

    def test_self_commitment_is_a_signoff(self):
        # Committing to do it yourself, led by a positive = wrapping up.
        self.assertTrue(sig("great, I'll send that email"))
        self.assertTrue(sig("perfect, I'll do that"))


class TestVetoes(unittest.TestCase):
    def test_questions_get_a_reply(self):
        for t in [
            "thanks, can you also check the weather?",
            "sounds good, what's next",
            "cool, how about tomorrow",
            "one more thing",
            "thanks — actually, one more question",
        ]:
            self.assertFalse(sig(t), f"should reply (question/request): {t!r}")

    def test_continuations_get_a_reply(self):
        for t in [
            "great, the meeting went well",
            "okay so the revenue is up",
            "cool, so here's the plan",
        ]:
            self.assertFalse(sig(t), f"should reply (continuation): {t!r}")

    def test_commands_get_a_reply(self):
        self.assertFalse(sig("great, send that email"))
        self.assertFalse(sig("perfect, add it to my calendar"))
        self.assertFalse(sig("thanks, remind me at noon"))

    def test_lookalikes_are_not_signoffs(self):
        # "well" != "we'll", "ill" != "I'll"
        self.assertFalse(sig("well"))
        self.assertFalse(sig("ill"))

    def test_never_swallow_first_utterance(self):
        self.assertFalse(is_signoff("thanks", has_assistant_spoken=False))
        self.assertFalse(is_signoff("bye", has_assistant_spoken=False))

    def test_empty_and_long_are_not_signoffs(self):
        self.assertFalse(sig(""))
        self.assertFalse(sig("   "))
        # Too long to be a bare goodbye.
        self.assertFalse(sig("thanks that is really helpful and I learned a lot today"))


if __name__ == "__main__":
    unittest.main()
