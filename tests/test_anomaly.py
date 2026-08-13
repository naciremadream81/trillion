"""
Tests for agent/security/anomaly.py (agent-security.md §3.2).

Run from the project root:
    python -m unittest tests.test_anomaly
"""

import unittest

from agent.security.anomaly import AnomalyGate


class FakeClock:
    def __init__(self, start: float = 0.0):
        self._now = start

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


class TestAnomalyGate(unittest.TestCase):
    def test_uncapped_tool_never_blocked(self):
        gate = AnomalyGate(caps={}, clock=FakeClock())
        for _ in range(100):
            self.assertIsNone(gate.check("search_notes"))

    def test_calls_under_cap_all_succeed(self):
        clock = FakeClock()
        gate = AnomalyGate(caps={"forget_fact": (3, 86400.0)}, clock=clock)
        self.assertIsNone(gate.check("forget_fact"))
        self.assertIsNone(gate.check("forget_fact"))
        self.assertIsNone(gate.check("forget_fact"))

    def test_call_exceeding_cap_is_blocked(self):
        clock = FakeClock()
        gate = AnomalyGate(caps={"forget_fact": (3, 86400.0)}, clock=clock)
        gate.check("forget_fact")
        gate.check("forget_fact")
        gate.check("forget_fact")
        blocked = gate.check("forget_fact")
        self.assertIsNotNone(blocked)
        self.assertEqual(blocked["count"], 3)
        self.assertEqual(blocked["limit"], 3)
        self.assertEqual(blocked["window_seconds"], 86400.0)

    def test_blocked_call_is_not_recorded(self):
        clock = FakeClock()
        gate = AnomalyGate(caps={"forget_fact": (1, 3600.0)}, clock=clock)
        self.assertIsNone(gate.check("forget_fact"))
        # This call is over the cap and must not be recorded.
        self.assertIsNotNone(gate.check("forget_fact"))
        # A true ceiling, not a one-strike lockout: still exactly one call
        # on record, so nothing has ratcheted the cap down further.
        blocked_again = gate.check("forget_fact")
        self.assertEqual(blocked_again["count"], 1)

    def test_window_resets_after_elapsed_time(self):
        clock = FakeClock()
        gate = AnomalyGate(caps={"draft_email": (2, 3600.0)}, clock=clock)
        gate.check("draft_email")
        gate.check("draft_email")
        self.assertIsNotNone(gate.check("draft_email"))

        clock.advance(3600.1)
        self.assertIsNone(gate.check("draft_email"))

    def test_caps_are_independent_per_tool(self):
        clock = FakeClock()
        gate = AnomalyGate(
            caps={"forget_fact": (1, 3600.0), "draft_email": (1, 3600.0)},
            clock=clock,
        )
        self.assertIsNone(gate.check("forget_fact"))
        self.assertIsNotNone(gate.check("forget_fact"))
        # draft_email's cap is untouched by forget_fact's usage.
        self.assertIsNone(gate.check("draft_email"))

    def test_default_caps_cover_the_expected_tools(self):
        from agent.security.anomaly import DEFAULT_CAPS

        for name in ("forget_fact", "write_project_file", "run_project_tests",
                     "remember_fact", "draft_email"):
            self.assertIn(name, DEFAULT_CAPS)

        for name in ("search_notes", "web_search", "read_project_file",
                      "query_analytics", "confirm_action"):
            self.assertNotIn(name, DEFAULT_CAPS)


if __name__ == "__main__":
    unittest.main()
