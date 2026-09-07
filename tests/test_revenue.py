"""
Tests for the revenue celebration — playbook/money-celebration.md.

The two things worth defending:

**Never double-celebrate, never miss.** Dedup at the source, and a
`celebrated` fact that is separate from `detected` so a payment that landed
with the screen closed is still waiting when a screen next opens. That gap is
what the playbook calls the heart of the feature.

**Only real money in.** A refund that throws a party is worse than a missed
celebration, and Stripe's list query cannot be trusted to have filtered — the
per-record re-check is the actual filter.

Run from the project root:
    python -m unittest tests.test_revenue
"""

import asyncio
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from agent.heartbeat.checks.revenue import RevenuePollCheck
from agent.revenue.storage import RevenueRepo
from agent.revenue.stripe_client import (
    StripeError,
    customer_label,
    is_real_money_in,
    to_payment,
)


def run(coro):
    return asyncio.run(coro)


def charge(**overrides):
    base = {
        "id": "ch_1", "status": "succeeded", "paid": True, "refunded": False,
        "disputed": False, "amount": 130000, "amount_captured": 130000,
        "currency": "usd", "created": 1757000000,
        "billing_details": {"name": "A Customer"},
    }
    base.update(overrides)
    return base


# ── Phase 1: only real money in ─────────────────────────────────────────────


class TestRealMoneyFilter(unittest.TestCase):
    def test_a_successful_captured_charge_counts(self):
        self.assertTrue(is_real_money_in(charge()))

    def test_a_failed_charge_does_not(self):
        self.assertFalse(is_real_money_in(charge(status="failed")))

    def test_a_pending_charge_does_not(self):
        self.assertFalse(is_real_money_in(charge(status="pending", paid=False)))

    def test_an_unpaid_charge_does_not(self):
        self.assertFalse(is_real_money_in(charge(paid=False)))

    def test_a_refunded_charge_does_not(self):
        # A refund celebrating is worse than a missed celebration.
        self.assertFalse(is_real_money_in(charge(refunded=True)))

    def test_a_disputed_charge_does_not(self):
        self.assertFalse(is_real_money_in(charge(disputed=True)))

    def test_a_zero_amount_charge_does_not(self):
        self.assertFalse(is_real_money_in(charge(amount_captured=0)))

    def test_an_authorized_but_uncaptured_charge_does_not(self):
        # Authorized is not money yet.
        self.assertFalse(is_real_money_in(charge(amount=130000, amount_captured=0)))

    def test_a_partial_capture_counts_for_what_was_taken(self):
        self.assertTrue(is_real_money_in(charge(amount=130000, amount_captured=50000)))
        self.assertEqual(to_payment(charge(amount_captured=50000))["amount_minor"], 50000)

    def test_junk_does_not_raise(self):
        for value in (None, "ch_1", [], 7):
            with self.subTest(value=value):
                self.assertFalse(is_real_money_in(value))

    def test_the_label_prefers_a_name_then_an_email(self):
        self.assertEqual(customer_label(charge()), "A Customer")
        self.assertEqual(
            customer_label(charge(billing_details={"email": "a@example.com"})),
            "a@example.com",
        )

    def test_a_charge_with_no_label_is_empty_not_an_id(self):
        # An id is not a name.
        self.assertEqual(customer_label(charge(billing_details={}, description=None)), "")

    def test_the_timestamp_survives_a_missing_created(self):
        self.assertTrue(to_payment(charge(created=None))["paid_at"])


# ── Phase 1 + 2: dedup and the celebrated fact ──────────────────────────────


class TestRepo(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.repo = RevenueRepo(os.path.join(self.tmp, "revenue.db"))

    def add(self, charge_id="ch_1", amount=130000, paid_at=None):
        return self.repo.record_payment(
            charge_id=charge_id, amount_minor=amount,
            paid_at=paid_at or datetime.now(timezone.utc).isoformat(),
        )

    def test_the_same_payment_is_recorded_once(self):
        self.assertTrue(self.add())
        self.assertFalse(self.add())
        self.assertEqual(len(self.repo.recent()), 1)

    def test_a_new_payment_is_uncelebrated(self):
        self.add()
        self.assertEqual(len(self.repo.read_catchup()["payments"]), 1)

    def test_marking_celebrated_removes_it_from_catchup(self):
        self.add()
        self.assertEqual(self.repo.mark_celebrated(["ch_1"]), 1)
        self.assertEqual(self.repo.read_catchup()["payments"], [])

    def test_marking_twice_is_idempotent(self):
        self.add()
        self.repo.mark_celebrated(["ch_1"])
        self.assertEqual(self.repo.mark_celebrated(["ch_1"]), 0)

    def test_re_detecting_a_celebrated_payment_does_not_revive_it(self):
        # The nastiest double-celebration path: an overlapping poll re-sees a
        # charge the screen already showed.
        self.add()
        self.repo.mark_celebrated(["ch_1"])
        self.assertFalse(self.add())
        self.assertEqual(self.repo.read_catchup()["payments"], [])

    def test_a_payment_that_landed_while_the_screen_was_closed_still_waits(self):
        # The heart of the feature. Nothing marks it celebrated, so it is
        # still there whenever a screen next asks.
        self.add(paid_at=(datetime.now(timezone.utc) - timedelta(hours=6)).isoformat())
        self.assertEqual(len(self.repo.read_catchup()["payments"]), 1)

    def test_a_payment_older_than_the_window_is_not_replayed(self):
        self.add(paid_at=(datetime.now(timezone.utc) - timedelta(days=3)).isoformat())
        self.assertEqual(self.repo.read_catchup()["payments"], [])

    def test_a_burst_is_capped_and_the_remainder_is_reported_not_dropped(self):
        # Never a silent truncation.
        for i in range(10):
            self.add(charge_id=f"ch_{i}")
        result = self.repo.read_catchup(cap=6)
        self.assertEqual(len(result["payments"]), 6)
        self.assertEqual(result["withheld"], 4)

    def test_catchup_is_newest_first(self):
        now = datetime.now(timezone.utc)
        self.add(charge_id="old", paid_at=(now - timedelta(hours=2)).isoformat())
        self.add(charge_id="new", paid_at=now.isoformat())
        self.assertEqual([p["charge_id"] for p in self.repo.read_catchup()["payments"]],
                         ["new", "old"])

    def test_marking_an_unknown_id_is_harmless(self):
        self.assertEqual(self.repo.mark_celebrated(["nope"]), 0)

    def test_marking_nothing_is_harmless(self):
        self.assertEqual(self.repo.mark_celebrated([]), 0)
        self.assertEqual(self.repo.mark_celebrated(None), 0)


# ── The poll ────────────────────────────────────────────────────────────────


class TestRevenuePoll(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.repo = RevenueRepo(os.path.join(self.tmp, "revenue.db"))

    def check(self, fetch):
        return RevenuePollCheck("sk_test_x", self.repo, fetch=fetch)

    def test_a_detected_payment_is_recorded(self):
        async def fetch(api_key, lookback_seconds=0):
            return [to_payment(charge())]

        notices, _ = run(self.check(fetch).run({}))
        self.assertEqual(len(self.repo.recent()), 1)
        # No notices, on purpose: a celebration is not a notification, so it
        # never enters the quiet-hours deferral path the playbook warns about.
        self.assertEqual(notices, [])

    def test_overlapping_polls_record_once(self):
        async def fetch(api_key, lookback_seconds=0):
            return [to_payment(charge())]

        run(self.check(fetch).run({}))
        run(self.check(fetch).run({}))
        self.assertEqual(len(self.repo.recent()), 1)

    def test_an_api_failure_is_survivable(self):
        async def fetch(api_key, lookback_seconds=0):
            raise StripeError("HTTP 503")

        notices, cursor = run(self.check(fetch).run({}))   # must not raise
        self.assertEqual(notices, [])

    def test_an_unexpected_error_is_survivable(self):
        async def fetch(api_key, lookback_seconds=0):
            raise RuntimeError("something else entirely")

        run(self.check(fetch).run({}))   # must not raise

    def test_one_bad_record_does_not_lose_the_others(self):
        async def fetch(api_key, lookback_seconds=0):
            return [to_payment(charge(id="ch_a")),
                    {"charge_id": "ch_b"},                       # missing fields
                    to_payment(charge(id="ch_c"))]

        run(self.check(fetch).run({}))
        self.assertEqual(sorted(p["charge_id"] for p in self.repo.recent()), ["ch_a", "ch_c"])

    def test_the_lookback_is_longer_than_the_poll_interval(self):
        # A lookback equal to the interval leaves a gap at every boundary,
        # and a payment landing in one is never seen.
        from agent.revenue.stripe_client import DEFAULT_LOOKBACK_SECONDS, DEFAULT_POLL_SECONDS

        self.assertGreater(DEFAULT_LOOKBACK_SECONDS, DEFAULT_POLL_SECONDS * 2)


if __name__ == "__main__":
    unittest.main()
