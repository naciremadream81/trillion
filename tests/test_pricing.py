"""
Phase 1 verification — pricing.

Run from the project root:
    python -m unittest tests.test_pricing
"""

import unittest

from agent.cost import pricing
from agent.cost.pricing import compute_cost


class TestComputeCost(unittest.TestCase):
    def test_exact_family_match(self):
        # 1M input tokens at the Sonnet-4 input rate ($3.00 / MTok).
        self.assertAlmostEqual(
            compute_cost("claude-sonnet-4", input_tokens=1_000_000),
            3.0,
        )

    def test_version_suffixed_prefix_match(self):
        # A dated model string still resolves to its family entry.
        self.assertAlmostEqual(
            compute_cost("claude-sonnet-4-6-20260514", input_tokens=1_000_000),
            3.0,
        )
        self.assertAlmostEqual(
            compute_cost("claude-opus-4-8", output_tokens=1_000_000),
            75.0,
        )

    def test_longest_prefix_wins(self):
        # "gpt-4o-mini-2026-01" starts with BOTH "gpt-4o" and "gpt-4o-mini";
        # the longer, more specific key must win.
        self.assertAlmostEqual(
            compute_cost("gpt-4o-mini-2026-01", input_tokens=1_000_000),
            0.15,
        )
        # Sanity: the plain gpt-4o family resolves to its own (higher) rate.
        self.assertAlmostEqual(
            compute_cost("gpt-4o-2026-01", input_tokens=1_000_000),
            2.50,
        )

    def test_unknown_model_returns_zero_and_warns(self):
        model = "totally-made-up-model-zzz"
        # Ensure a clean slate so the warning fires on this run.
        pricing._warned_models.discard(model)
        with self.assertLogs(pricing.logger, level="WARNING") as cm:
            cost = compute_cost(model, input_tokens=5_000, output_tokens=5_000)
        self.assertEqual(cost, 0.0)
        self.assertTrue(any(model in line for line in cm.output))

    def test_all_token_classes_priced(self):
        # Sonnet-4: input 3, output 15, cache_write 3.75, cache_read 0.30 / MTok.
        cost = compute_cost(
            "claude-sonnet-4-6",
            input_tokens=1_000,
            output_tokens=1_000,
            cache_write_tokens=1_000,
            cache_read_tokens=1_000,
        )
        expected = (1_000 * 3.0 + 1_000 * 15.0 + 1_000 * 3.75 + 1_000 * 0.30) / 1_000_000
        self.assertAlmostEqual(cost, expected)
        self.assertAlmostEqual(cost, 0.02205)

    def test_local_model_is_free_without_warning(self):
        # Local models are listed at $0, so no "unknown model" warning.
        with self.assertNoLogs(pricing.logger, level="WARNING"):
            self.assertEqual(compute_cost("llama3.2", input_tokens=1_000_000), 0.0)


if __name__ == "__main__":
    unittest.main()
