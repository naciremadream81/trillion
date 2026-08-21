"""
Tests for the Bitcoin mining tracker — playbooks/btc-mining-tracker.md.

Parsing is tested against **real captured responses** in
tests/fixtures/ocean/, not hand-written shapes. That matters here more than
usual: the playbook's central instruction is "do not invent field names or
endpoint shapes", and Ocean's real responses contain three things nobody
would guess — two upstream typos (`lastest_share_ts`, `fees_colected_satoshis`)
and hashrates as strings because they exceed 2^53. A test written against an
imagined response would pass while the code returned zeros forever.

Run from the project root:
    python -m unittest tests.test_mining
"""

import json
import os
import pathlib
import shutil
import tempfile
import unittest

from agent.mining.client import (
    SATS_PER_BTC,
    MiningPoolError,
    OceanClient,
    classify_worker,
    parse_earnings_total_btc,
    parse_payouts,
    parse_statsnap,
    parse_user_hashrate,
    parse_workers,
)
from agent.mining.storage import MiningRepo, btc_to_sats, sats_to_btc

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "ocean"


def fixture(name):
    return json.loads((FIXTURES / name).read_text())


class TestParsingRealResponses(unittest.TestCase):
    def test_statsnap_reads_unpaid_and_next_block(self):
        snap = parse_statsnap(fixture("statsnap.json"))
        self.assertIsInstance(snap.unpaid_btc, float)
        self.assertGreater(snap.estimated_next_block_btc, 0)

    def test_statsnap_reads_the_misspelled_last_share_field(self):
        # `lastest_share_ts` is Ocean's own spelling. Writing the name you'd
        # expect yields None forever, silently.
        self.assertIsNotNone(parse_statsnap(fixture("statsnap.json")).last_share_at)

    def test_hashrates_keep_full_precision(self):
        # ~9.3e17 h/s is past 2^53. Parsed as a float it would round; the
        # API sends strings for exactly this reason.
        snap = parse_statsnap(fixture("statsnap.json"))
        raw = int(fixture("statsnap.json")["result"]["hashrate_60s"])
        self.assertEqual(snap.hashrates[60], raw)
        self.assertIsInstance(snap.hashrates[60], int)

    def test_user_hashrate_reads_all_eight_windows(self):
        snap = parse_user_hashrate(fixture("user_hashrate.json"))
        self.assertEqual(
            sorted(snap.hashrates), [60, 300, 600, 1800, 3600, 10800, 43200, 86400]
        )
        self.assertTrue(all(v > 0 for v in snap.hashrates.values()))

    def test_user_hashrate_reads_worker_count(self):
        self.assertGreater(parse_user_hashrate(fixture("user_hashrate.json")).active_worker_count, 0)

    def test_workers_parse_through_the_list_wrapper(self):
        # `workers` maps a name to a LIST containing one dict. Treating it
        # as a bare dict yields nothing.
        workers = parse_workers(fixture("user_hashrate_full.json"))
        self.assertTrue(workers)
        self.assertTrue(all(w.worker_name for w in workers))
        self.assertTrue(all(w.hashrate_60s >= 0 for w in workers))

    def test_workers_also_accept_a_bare_dict(self):
        # Defensive: if Ocean ever drops the list wrapper, don't break.
        payload = {"result": {"workers": {"rig1": {"hashrate_60s": "100", "hashrate_3600s": "100"}}}}
        self.assertEqual(parse_workers(payload)[0].worker_name, "rig1")

    def test_payouts_convert_satoshis_to_btc(self):
        payouts = parse_payouts(fixture("earnpay.json"))
        self.assertTrue(payouts)
        raw = fixture("earnpay.json")["result"]["payouts"][0]["total_satoshis_net_paid"]
        self.assertAlmostEqual(payouts[0].amount_btc, raw / SATS_PER_BTC, places=10)

    def test_payout_timestamps_are_stamped_utc(self):
        # Ocean sends naive ISO strings. Left naive, a machine that isn't on
        # UTC would silently shift every payout.
        self.assertIn("+00:00", parse_payouts(fixture("earnpay.json"))[0].paid_at)

    def test_earnings_total_reads_the_misspelled_fee_field_neighbours(self):
        self.assertGreater(parse_earnings_total_btc(fixture("earnpay.json")), 0)

    def test_empty_and_malformed_payloads_do_not_raise(self):
        for payload in ({}, {"result": None}, {"result": {}}, None,
                        {"result": {"workers": "junk"}}, {"result": {"payouts": ["junk"]}}):
            with self.subTest(payload=payload):
                parse_statsnap(payload)
                parse_user_hashrate(payload)
                parse_workers(payload)
                parse_payouts(payload)


class TestWorkerClassification(unittest.TestCase):
    def test_producing_and_keeping_up_is_online(self):
        self.assertEqual(classify_worker(100, 100), "online")

    def test_well_below_its_own_baseline_is_degraded(self):
        self.assertEqual(classify_worker(20, 100), "degraded")

    def test_stopped_after_hashing_is_offline(self):
        self.assertEqual(classify_worker(0, 100), "offline")

    def test_never_seen_is_unknown_not_offline(self):
        # A worker with no history either way is not offline — there is
        # nothing to be offline from, and calling it offline would alert for
        # every worker name that ever existed.
        self.assertEqual(classify_worker(0, 0), "unknown")

    def test_classification_is_self_calibrating(self):
        # A 2 TH/s miner and a 200 TH/s miner are judged against themselves,
        # not against a shared threshold meaningless for one of them.
        self.assertEqual(classify_worker(2_000_000_000_000, 2_000_000_000_000), "online")
        self.assertEqual(classify_worker(2_000_000_000_000, 200_000_000_000_000), "degraded")


class TestUnitConversion(unittest.TestCase):
    def test_btc_to_sats_rounds_rather_than_truncating(self):
        # int() truncation loses a satoshi on almost every value that isn't
        # exactly representable in binary floating point.
        self.assertEqual(btc_to_sats(0.14297234), 14297234)
        self.assertEqual(btc_to_sats(0.1), 10_000_000)

    def test_round_trip_is_stable(self):
        for btc in (0.0, 0.00000001, 0.1, 0.14297234, 6.25):
            with self.subTest(btc=btc):
                self.assertAlmostEqual(sats_to_btc(btc_to_sats(btc)), btc, places=10)


class TestStorage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.repo = MiningRepo(os.path.join(self.tmp, "mining.db"))
        self.wallet_id = self.repo.ensure_wallet("bc1qtest")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_ensure_wallet_is_idempotent(self):
        self.assertEqual(self.repo.ensure_wallet("bc1qtest"), self.wallet_id)

    def test_payout_ingestion_is_idempotent(self):
        # The playbook's core storage requirement: re-poll a sliding window
        # every slow tick and dedupe here. Overlap is free; missed payouts
        # are not.
        payouts = parse_payouts(fixture("earnpay.json"))
        first = self.repo.record_payouts(self.wallet_id, payouts)
        second = self.repo.record_payouts(self.wallet_id, payouts)
        self.assertGreater(first, 0)
        self.assertEqual(second, 0)

    def test_a_genuinely_new_payout_is_counted(self):
        payouts = parse_payouts(fixture("earnpay.json"))
        self.repo.record_payouts(self.wallet_id, payouts)
        new = type(payouts[0])(paid_at="2030-01-01T00:00:00+00:00", amount_btc=0.5)
        self.assertEqual(self.repo.record_payouts(self.wallet_id, payouts + [new]), 1)

    def test_payouts_sharing_a_txid_are_both_kept(self):
        # Generation-transaction payouts share a txid by construction, so a
        # txid-keyed unique index would silently discard real payouts.
        payout = parse_payouts(fixture("earnpay.json"))[0]
        other = type(payout)(
            paid_at="2030-01-02T00:00:00+00:00",
            amount_btc=0.25,
            on_chain_txid=payout.on_chain_txid,
        )
        self.repo.record_payouts(self.wallet_id, [payout])
        self.assertEqual(self.repo.record_payouts(self.wallet_id, [other]), 1)

    def test_latest_workers_drops_a_worker_that_vanished(self):
        Worker = parse_workers(fixture("user_hashrate_full.json"))[0].__class__
        self.repo.record_workers(
            self.wallet_id,
            [Worker("rig1", 10, 10, 10, "online"), Worker("rig2", 10, 10, 10, "online")],
            captured_at="2026-01-01T00:00:00+00:00",
        )
        self.repo.record_workers(
            self.wallet_id, [Worker("rig1", 10, 10, 10, "online")],
            captured_at="2026-01-01T00:01:00+00:00",
        )
        names = {w["worker_name"] for w in self.repo.latest_workers(self.wallet_id)}
        self.assertEqual(names, {"rig1"})

    def test_prune_drops_old_snapshots_but_never_payouts(self):
        self.repo.record_snapshot(
            self.wallet_id, hashrates={60: 1}, unpaid_btc=0, estimated_next_block_btc=0,
            active_worker_count=1, last_share_at=None, btc_usd=None,
            captured_at="2020-01-01T00:00:00+00:00",
        )
        self.repo.record_payouts(self.wallet_id, parse_payouts(fixture("earnpay.json")))
        payouts_before = len(self.repo.recent_payouts(self.wallet_id, limit=50))
        self.assertGreater(self.repo.prune(retention_days=30), 0)
        self.assertIsNone(self.repo.latest_snapshot(self.wallet_id))
        self.assertEqual(len(self.repo.recent_payouts(self.wallet_id, limit=50)), payouts_before)

    def test_summary_reads_a_full_tick(self):
        snap = parse_statsnap(fixture("statsnap.json"))
        rates = parse_user_hashrate(fixture("user_hashrate.json"))
        self.repo.record_snapshot(
            self.wallet_id, hashrates=rates.hashrates, unpaid_btc=snap.unpaid_btc,
            estimated_next_block_btc=snap.estimated_next_block_btc,
            active_worker_count=rates.active_worker_count,
            last_share_at=snap.last_share_at, btc_usd=73720.0,
        )
        self.repo.record_workers(self.wallet_id, parse_workers(fixture("user_hashrate_full.json")))
        self.repo.record_payouts(self.wallet_id, parse_payouts(fixture("earnpay.json")))
        summary = self.repo.summary("bc1qtest").to_dict()
        self.assertGreater(summary["hashrate_60s"], 0)
        self.assertGreater(summary["payouts_30d_btc"], 0)
        self.assertIsNotNone(summary["payouts_30d_usd"])
        self.assertGreaterEqual(summary["workers_online"] + summary["workers_degraded"], 1)

    def test_summary_without_a_price_omits_fiat_rather_than_guessing(self):
        self.repo.record_snapshot(
            self.wallet_id, hashrates={60: 1}, unpaid_btc=1.0, estimated_next_block_btc=0,
            active_worker_count=1, last_share_at=None, btc_usd=None,
        )
        self.assertIsNone(self.repo.summary("bc1qtest").to_dict()["unpaid_usd"])


class TestClientConfig(unittest.TestCase):
    def test_no_address_fails_loudly_at_construction(self):
        with self.assertRaises(MiningPoolError):
            OceanClient("")

    def test_endpoint_paths_match_the_captured_ones(self):
        client = OceanClient("bc1qtest")
        self.assertEqual(client.base_url, "https://api.ocean.xyz")


class TestChecksSelfSkip(unittest.TestCase):
    def test_no_wallet_configured_means_no_checks(self):
        # Self-skipping rather than failing, mirroring the Code Sentinel: an
        # unconfigured feature should be absent, not an error every tick.
        from agent.heartbeat.checks.mining import build_mining_checks

        class Settings:
            mining_wallet = ""
            heartbeat_fast_cadence_seconds = 60
            heartbeat_slow_cadence_seconds = 1800

        self.assertEqual(build_mining_checks(Settings()), [])


class FakeClient:
    """Serves captured fixtures instead of hitting Ocean."""

    address = "bc1qtest"

    def __init__(self, workers_payload=None, fail=False):
        self._workers = workers_payload or fixture("user_hashrate_full.json")
        self._fail = fail

    async def statsnap(self):
        if self._fail:
            raise MiningPoolError("pool down")
        return fixture("statsnap.json")

    async def user_hashrate(self):
        if self._fail:
            raise MiningPoolError("pool down")
        return fixture("user_hashrate.json")

    async def user_hashrate_full(self):
        if self._fail:
            raise MiningPoolError("pool down")
        return self._workers

    async def earnpay(self, since_ts):
        if self._fail:
            raise MiningPoolError("pool down")
        return fixture("earnpay.json")


def workers_payload(spec):
    """spec: {name: (hashrate_60s, hashrate_3600s)}"""
    return {"result": {"workers": {
        name: [{"hashrate_60s": str(h60), "hashrate_3600s": str(h3600),
                "hashrate_86400s": str(h3600)}]
        for name, (h60, h3600) in spec.items()
    }}}


class TestMiningAlerts(unittest.IsolatedAsyncioTestCase):
    """
    The alert policy from the interview: a worker going offline is the ONLY
    condition that surfaces a notice. Everything else is recorded quietly.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.repo = MiningRepo(os.path.join(self.tmp, "mining.db"))
        from agent.mining import price

        price.reset_cache()
        price._cached_price = 73720.0  # avoid a network call in tests
        price._cached_at = float("inf")

    def tearDown(self):
        from agent.mining import price

        price.reset_cache()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _check(self, client):
        from agent.heartbeat.checks.mining import MiningLiveCheck

        return MiningLiveCheck(client, self.repo)

    async def test_a_worker_going_offline_raises_one_notice(self):
        client = FakeClient(workers_payload({"rig1": (0, 100), "rig2": (100, 100)}))
        notices, cursor = await self._check(client).run({})
        self.assertEqual(len(notices), 1)
        self.assertIn("rig1", notices[0].message)
        self.assertEqual(cursor["offline_workers"], ["rig1"])

    async def test_the_same_offline_worker_does_not_alert_every_tick(self):
        # Without this the same dead rig produces a notice every 60 seconds
        # all night — the "cries wolf" failure the playbook warns about.
        client = FakeClient(workers_payload({"rig1": (0, 100)}))
        check = self._check(client)
        _, cursor = await check.run({})
        notices, _ = await check.run(cursor)
        self.assertEqual(notices, [])

    async def test_a_worker_that_recovers_and_dies_again_alerts_twice(self):
        # Storing "ever been offline" would silently suppress every repeat
        # failure, which is the same rig failing nightly and never telling you.
        check_down = self._check(FakeClient(workers_payload({"rig1": (0, 100)})))
        _, cursor = await check_down.run({})
        check_up = self._check(FakeClient(workers_payload({"rig1": (100, 100)})))
        _, cursor = await check_up.run(cursor)
        self.assertEqual(cursor["offline_workers"], [])
        notices, _ = await check_down.run(cursor)
        self.assertEqual(len(notices), 1)

    async def test_a_never_seen_worker_does_not_alert(self):
        notices, _ = await self._check(FakeClient(workers_payload({"rig1": (0, 0)}))).run({})
        self.assertEqual(notices, [])

    async def test_a_degraded_worker_is_recorded_but_does_not_interrupt(self):
        client = FakeClient(workers_payload({"rig1": (10, 100)}))
        notices, _ = await self._check(client).run({})
        self.assertEqual(notices, [])
        statuses = {w["status"] for w in self.repo.latest_workers(self.repo.ensure_wallet("bc1qtest"))}
        self.assertEqual(statuses, {"degraded"})

    async def test_the_offline_notice_defers_under_quiet_hours(self):
        # NOT critical severity: a dead rig costs real money and still does
        # not justify a 3am wake-up. Critical is what bypasses quiet hours.
        notices, _ = await self._check(FakeClient(workers_payload({"rig1": (0, 100)}))).run({})
        self.assertNotEqual(notices[0].severity, "critical")

    async def test_a_pool_outage_records_nothing_and_alerts_nothing(self):
        # A flaky API must not become a nightly false "your rigs are down".
        notices, cursor = await self._check(FakeClient(fail=True)).run({})
        self.assertEqual(notices, [])
        self.assertEqual(cursor, {})

    async def test_a_live_tick_records_a_snapshot(self):
        await self._check(FakeClient()).run({})
        wallet_id = self.repo.ensure_wallet("bc1qtest")
        self.assertIsNotNone(self.repo.latest_snapshot(wallet_id))

    async def test_the_payout_check_ingests_idempotently_and_stays_quiet(self):
        from agent.heartbeat.checks.mining import MiningPayoutCheck

        check = MiningPayoutCheck(FakeClient(), self.repo)
        notices, cursor = await check.run({})
        self.assertEqual(notices, [])  # money arriving is recorded, not announced
        wallet_id = self.repo.ensure_wallet("bc1qtest")
        first = len(self.repo.recent_payouts(wallet_id, limit=50))
        await check.run(cursor)
        self.assertEqual(len(self.repo.recent_payouts(wallet_id, limit=50)), first)

    async def test_a_payout_outage_does_not_break_the_live_tick(self):
        # The two cadences run independently: the playbook notes payout
        # endpoints are the flakiest, and isolating them keeps live data snappy.
        from agent.heartbeat.checks.mining import MiningPayoutCheck

        notices, _ = await MiningPayoutCheck(FakeClient(fail=True), self.repo).run({})
        self.assertEqual(notices, [])
        live, _ = await self._check(FakeClient()).run({})
        self.assertEqual(live, [])


if __name__ == "__main__":
    unittest.main()
