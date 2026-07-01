"""
Phase 4 verification — month-to-date aggregation.

Run from the project root:
    python -m unittest tests.test_aggregate
"""

import os
import tempfile
import unittest
from datetime import datetime, timezone

from agent.cost.aggregate import build_month_to_date
from agent.cost.pricing import compute_cost
from agent.cost.storage import UsageRepo

UTC = timezone.utc
NOW = datetime(2026, 7, 15, 12, 0, 0, tzinfo=UTC)  # mid-July, fixed


class TestAggregate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmp, "usage.db")
        self.repo = UsageRepo(db_path=self.db_path)

    def tearDown(self):
        try:
            os.remove(self.db_path)
        except FileNotFoundError:
            pass
        os.rmdir(self.tmp)

    def _record(self, *, created_at, model="claude-sonnet-4-6",
                input_tokens=0, output_tokens=0, cache_read_tokens=0,
                source="conversation"):
        cost = compute_cost(
            model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read_tokens,
        )
        self.repo.record(
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read_tokens,
            cost_usd=cost,
            source=source,
            created_at=created_at,
        )

    def test_empty_table_returns_zeroed_payload(self):
        p = build_month_to_date(self.repo, now=NOW)
        self.assertEqual(p["total_cost_usd"], 0.0)
        self.assertEqual(p["calls"], 0)
        self.assertEqual(p["cache_savings_usd"], 0.0)
        self.assertEqual(p["today_cost_usd"], 0.0)
        self.assertEqual(p["by_model"], [])
        self.assertEqual(p["by_day"], [])
        self.assertEqual(p["delta"]["prev_period_cost_usd"], 0.0)
        self.assertIsNone(p["delta"]["change_pct"])
        self.assertEqual(p["month"], "2026-07")

    def test_totals_and_today(self):
        # Two calls earlier this month + one today.
        self._record(created_at="2026-07-02T09:00:00+00:00", input_tokens=1_000_000)
        self._record(created_at="2026-07-10T09:00:00+00:00", output_tokens=1_000_000)
        self._record(created_at="2026-07-15T08:00:00+00:00", input_tokens=1_000_000)  # today

        p = build_month_to_date(self.repo, now=NOW)
        # input 1M ($3) + output 1M ($15) + today input 1M ($3) = $21 MTD
        self.assertAlmostEqual(p["total_cost_usd"], 21.0)
        self.assertEqual(p["calls"], 3)
        self.assertAlmostEqual(p["today_cost_usd"], 3.0)  # only the 07-15 call

    def test_cache_savings_math(self):
        # 1M cache-read tokens on Sonnet-4: full input rate $3.00, cache-read
        # rate $0.30 → savings = $2.70.
        self._record(created_at="2026-07-05T10:00:00+00:00", cache_read_tokens=1_000_000)
        p = build_month_to_date(self.repo, now=NOW)
        expected = (
            compute_cost("claude-sonnet-4-6", input_tokens=1_000_000)
            - compute_cost("claude-sonnet-4-6", cache_read_tokens=1_000_000)
        )
        self.assertAlmostEqual(p["cache_savings_usd"], expected)
        self.assertAlmostEqual(p["cache_savings_usd"], 2.70)

    def test_per_model_breakdown_sorted_by_cost(self):
        self._record(created_at="2026-07-03T10:00:00+00:00", model="gpt-4o", input_tokens=1_000_000)         # $2.50
        self._record(created_at="2026-07-03T11:00:00+00:00", model="claude-opus-4-8", input_tokens=1_000_000)  # $15.00
        p = build_month_to_date(self.repo, now=NOW)
        self.assertEqual(len(p["by_model"]), 2)
        # Highest cost first.
        self.assertEqual(p["by_model"][0]["model"], "claude-opus-4-8")
        self.assertEqual(p["by_model"][1]["model"], "gpt-4o")

    def test_day_grouping(self):
        self._record(created_at="2026-07-02T09:00:00+00:00", input_tokens=1_000_000)
        self._record(created_at="2026-07-02T18:00:00+00:00", input_tokens=1_000_000)
        self._record(created_at="2026-07-10T09:00:00+00:00", input_tokens=1_000_000)

        p = build_month_to_date(self.repo, now=NOW)
        self.assertEqual(len(p["by_day"]), 2)
        # Newest day first; 07-02 had two calls.
        self.assertEqual(p["by_day"][0]["day"], "2026-07-10")
        self.assertEqual(p["by_day"][1]["day"], "2026-07-02")
        self.assertEqual(p["by_day"][1]["calls"], 2)

    def test_month_over_month_delta(self):
        # Previous-month spend inside the same-elapsed window (before 06-15 12:00).
        self._record(created_at="2026-06-10T09:00:00+00:00", input_tokens=1_000_000)  # $3 last month
        # This-month spend.
        self._record(created_at="2026-07-05T09:00:00+00:00", input_tokens=2_000_000)  # $6 this month

        p = build_month_to_date(self.repo, now=NOW)
        self.assertAlmostEqual(p["delta"]["prev_period_cost_usd"], 3.0)
        self.assertAlmostEqual(p["total_cost_usd"], 6.0)
        self.assertAlmostEqual(p["delta"]["change_usd"], 3.0)
        self.assertAlmostEqual(p["delta"]["change_pct"], 100.0)

    def test_prev_month_row_outside_window_excluded(self):
        # A June row AFTER the same-elapsed point (06-20 > 06-15 12:00) must not
        # count toward the previous-period comparison.
        self._record(created_at="2026-06-20T09:00:00+00:00", input_tokens=1_000_000)
        p = build_month_to_date(self.repo, now=NOW)
        self.assertEqual(p["delta"]["prev_period_cost_usd"], 0.0)

    def test_by_source_breakdown(self):
        self._record(created_at="2026-07-02T09:00:00+00:00", input_tokens=1_000_000, source="conversation")  # $3
        self._record(created_at="2026-07-02T10:00:00+00:00", input_tokens=2_000_000, source="scout")         # $6
        p = build_month_to_date(self.repo, now=NOW)
        self.assertEqual(len(p["by_source"]), 2)
        # Sorted by cost, highest first.
        self.assertEqual(p["by_source"][0]["source"], "scout")
        self.assertAlmostEqual(p["by_source"][0]["cost_usd"], 6.0)
        self.assertEqual(p["by_source"][1]["source"], "conversation")

    def test_budget_states(self):
        # $30 of spend this month.
        self._record(created_at="2026-07-05T09:00:00+00:00", input_tokens=10_000_000)  # $30

        # No budget → state 'none'.
        p = build_month_to_date(self.repo, now=NOW)
        self.assertEqual(p["budget"]["state"], "none")
        self.assertIsNone(p["budget"]["limit_usd"])

        # $100 budget → 30% used → ok.
        p = build_month_to_date(self.repo, now=NOW, monthly_budget=100.0)
        self.assertEqual(p["budget"]["state"], "ok")
        self.assertAlmostEqual(p["budget"]["used_pct"], 30.0)

        # $35 budget → ~85.7% → warn (>= 80%).
        p = build_month_to_date(self.repo, now=NOW, monthly_budget=35.0)
        self.assertEqual(p["budget"]["state"], "warn")

        # $20 budget → 150% → over.
        p = build_month_to_date(self.repo, now=NOW, monthly_budget=20.0)
        self.assertEqual(p["budget"]["state"], "over")
        self.assertAlmostEqual(p["budget"]["used_pct"], 150.0)

    def test_empty_payload_has_new_sections(self):
        p = build_month_to_date(self.repo, now=NOW, monthly_budget=50.0)
        self.assertEqual(p["by_source"], [])
        self.assertEqual(p["budget"]["state"], "ok")
        self.assertEqual(p["budget"]["used_pct"], 0.0)


if __name__ == "__main__":
    unittest.main()
