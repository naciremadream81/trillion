"""Tier 6: the tier/mode decision table and the hardline list."""

import unittest

from agent.safety.risk import (
    CONSEQUENTIAL,
    HARDLINE,
    HARDLINE_TOOLS,
    LOW,
    MANUAL,
    OFF,
    READ_ONLY,
    SMART,
    blocked_while_paused,
    is_hardline,
    needs_confirmation,
    normalize_mode,
    normalize_risk,
)


class NormalizeTests(unittest.TestCase):
    def test_known_modes_pass_through(self):
        for mode in (OFF, SMART, MANUAL):
            self.assertEqual(normalize_mode(mode), mode)

    def test_mode_is_case_and_space_insensitive(self):
        self.assertEqual(normalize_mode("  Manual "), MANUAL)

    def test_unknown_mode_falls_back_to_smart_not_off(self):
        # A typo in .env must not silently disable the gate.
        self.assertEqual(normalize_mode("of"), SMART)
        self.assertEqual(normalize_mode(""), SMART)
        self.assertEqual(normalize_mode(None), SMART)

    def test_unknown_risk_fails_closed(self):
        self.assertEqual(normalize_risk("mostly-harmless"), CONSEQUENTIAL)
        self.assertEqual(normalize_risk(None), CONSEQUENTIAL)


class HardlineTests(unittest.TestCase):
    def test_named_tool_is_hardline_regardless_of_declared_tier(self):
        # The whole point: the list wins over what the tool says about itself.
        self.assertTrue(is_hardline("send_email", READ_ONLY))

    def test_hardline_tier_is_hardline_even_when_unlisted(self):
        self.assertTrue(is_hardline("some_new_tool", HARDLINE))

    def test_ordinary_tool_is_not_hardline(self):
        self.assertFalse(is_hardline("web_search", READ_ONLY))

    def test_mode_off_cannot_clear_a_hardline_tool(self):
        for name in sorted(HARDLINE_TOOLS):
            self.assertTrue(
                needs_confirmation(
                    tool_name=name, risk=READ_ONLY, declared=False, mode=OFF
                ),
                f"{name} escaped the gate in mode=off",
            )


class NeedsConfirmationTests(unittest.TestCase):
    def test_never_gated_wins_over_everything(self):
        self.assertFalse(
            needs_confirmation(
                tool_name="confirm_action",
                risk=HARDLINE,
                declared=True,
                mode=MANUAL,
            )
        )

    def test_smart_gates_consequential_only(self):
        self.assertFalse(self._smart(READ_ONLY))
        self.assertFalse(self._smart(LOW))
        self.assertTrue(self._smart(CONSEQUENTIAL))

    def test_manual_gates_everything_but_read_only(self):
        self.assertFalse(self._manual(READ_ONLY))
        self.assertTrue(self._manual(LOW))
        self.assertTrue(self._manual(CONSEQUENTIAL))

    def test_off_gates_nothing_beyond_hardline(self):
        self.assertFalse(self._off(CONSEQUENTIAL))
        self.assertFalse(self._off(LOW))

    def test_declared_true_forces_a_gate_on_a_low_tool(self):
        self.assertTrue(
            needs_confirmation(
                tool_name="x", risk=LOW, declared=True, mode=SMART
            )
        )

    def test_declared_false_exempts_a_consequential_tool(self):
        self.assertFalse(
            needs_confirmation(
                tool_name="x", risk=CONSEQUENTIAL, declared=False, mode=SMART
            )
        )

    def test_undeclared_tool_is_gated_in_smart_mode(self):
        # The fail-closed default: forgetting to declare costs a prompt, not
        # the gate.
        self.assertTrue(
            needs_confirmation(
                tool_name="brand_new_tool", risk=None, declared=None, mode=SMART
            )
        )

    def _smart(self, risk):
        return needs_confirmation(
            tool_name="x", risk=risk, declared=None, mode=SMART
        )

    def _manual(self, risk):
        return needs_confirmation(
            tool_name="x", risk=risk, declared=None, mode=MANUAL
        )

    def _off(self, risk):
        return needs_confirmation(
            tool_name="x", risk=risk, declared=None, mode=OFF
        )


class BlockedWhilePausedTests(unittest.TestCase):
    def test_read_only_survives_the_kill_switch(self):
        self.assertFalse(blocked_while_paused("web_search", READ_ONLY))

    def test_confirm_action_itself_survives(self):
        # It's read-only; Gate.confirm() is what refuses while paused.
        self.assertFalse(blocked_while_paused("confirm_action", READ_ONLY))

    def test_everything_else_is_blocked(self):
        for risk in (LOW, CONSEQUENTIAL, HARDLINE):
            self.assertTrue(blocked_while_paused("x", risk))

    def test_undeclared_tool_is_blocked(self):
        self.assertTrue(blocked_while_paused("x", None))


if __name__ == "__main__":
    unittest.main()
