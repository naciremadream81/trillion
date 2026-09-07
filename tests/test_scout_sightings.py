"""
Tests for scout telemetry — playbook/opportunity-scout.md Tier 7.

The claim this tier makes is that every run leaves behind a record that makes
later runs smarter for free: four of five candidates used to be discarded, and
kept instead they are the cheapest signal in the system. The delta between two
snapshots of the same problem is something nobody paid for.

So the tests here are about accumulation, not storage mechanics: the same
problem seen twice must be ONE row with a count of two, not two rows, or the
delta doesn't exist.

Run from the project root:
    python -m unittest tests.test_scout_sightings
"""

import os
import shutil
import tempfile
import unittest

from agent.factory.software.opportunity_scout import (
    _already_seen_note,
    _scout_system_prompt,
    fingerprint,
)
from agent.factory.software.scheduler import _record_sightings
from agent.factory.software.storage import BuildRepo


def candidate(problem, url="", evidence="e"):
    return {"problem": problem, "evidence": evidence, "source_url": url}


class TestFingerprint(unittest.TestCase):
    def test_the_same_url_is_the_same_finding_however_it_is_worded(self):
        a = candidate("Exports break every Monday", "https://example.com/thread/1")
        b = candidate("The weekly export keeps failing", "https://example.com/thread/1")
        self.assertEqual(fingerprint(a), fingerprint(b))

    def test_trailing_slashes_and_query_strings_are_not_identity(self):
        base = fingerprint(candidate("p", "https://example.com/thread/1"))
        for variant in (
            "https://example.com/thread/1/",
            "https://example.com/thread/1?utm_source=x",
            "https://example.com/thread/1#comment-4",
            "HTTPS://Example.com/thread/1",
        ):
            with self.subTest(variant=variant):
                self.assertEqual(fingerprint(candidate("p", variant)), base)

    def test_different_urls_are_different_findings(self):
        self.assertNotEqual(
            fingerprint(candidate("p", "https://example.com/1")),
            fingerprint(candidate("p", "https://example.com/2")),
        )

    def test_falls_back_to_the_problem_when_there_is_no_url(self):
        self.assertEqual(
            fingerprint(candidate("  Exports   break  ")),
            fingerprint(candidate("exports break")),
        )

    def test_a_missing_field_does_not_raise(self):
        self.assertTrue(fingerprint({}))


class TestRecordSightings(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.repo = BuildRepo(os.path.join(self.tmp, "sf.db"))

    def report(self, candidates, selected=0):
        return {"candidates": candidates, "selected_index": selected}

    def test_every_candidate_is_recorded_not_only_the_selected_one(self):
        # The whole premise of the tier: four of five used to be thrown away.
        report = self.report([candidate(f"p{i}", f"https://example.com/{i}") for i in range(5)])
        saved = _record_sightings(self.repo, report, "open-source-friction")
        self.assertEqual(saved, 5)
        self.assertEqual(len(self.repo.recent_sightings()), 5)

    def test_the_selected_candidate_is_marked_as_such(self):
        report = self.report(
            [candidate(f"p{i}", f"https://example.com/{i}") for i in range(3)], selected=1
        )
        _record_sightings(self.repo, report, "lane")
        rows = {row["problem"]: row for row in self.repo.recent_sightings()}
        self.assertEqual(rows["p1"]["times_selected"], 1)
        self.assertEqual(rows["p0"]["times_selected"], 0)

    def test_a_malformed_candidate_does_not_discard_the_good_ones(self):
        # "Persist per item, not per report." One bad row used to mean the
        # whole run left no trace.
        report = self.report([
            candidate("good one", "https://example.com/1"),
            "not a dict at all",
            candidate("good two", "https://example.com/2"),
        ])
        saved = _record_sightings(self.repo, report, "lane")
        self.assertEqual(saved, 2)
        self.assertEqual(
            sorted(row["problem"] for row in self.repo.recent_sightings()),
            ["good one", "good two"],
        )

    def test_the_count_reported_is_what_actually_persisted(self):
        # Telling the operator "3 new finds" when zero were saved sends them
        # looking for rows that do not exist.
        report = self.report(["junk", "more junk"])
        self.assertEqual(_record_sightings(self.repo, report, "lane"), 0)

    def test_an_empty_report_records_nothing_and_does_not_raise(self):
        self.assertEqual(_record_sightings(self.repo, {}, None), 0)

    def test_two_runs_over_the_same_problem_produce_a_delta(self):
        # The Tier 7 verification: run twice against overlapping targets and
        # compute a real delta from the two snapshots.
        run_one = self.report([
            candidate("Exports break every Monday", "https://example.com/a", "one complaint"),
            candidate("Only-in-run-one", "https://example.com/b"),
        ])
        _record_sightings(self.repo, run_one, "manual-workarounds")

        run_two = self.report([
            # Same thread, different wording, better evidence.
            candidate("The Monday export keeps failing", "https://example.com/a",
                      "four complaints across three weeks"),
            candidate("Only-in-run-two", "https://example.com/c"),
        ])
        _record_sightings(self.repo, run_two, "developer-tooling-gaps")

        rows = {row["fingerprint"]: row for row in self.repo.recent_sightings()}
        self.assertEqual(len(rows), 3, "the repeat forked into two rows instead of accumulating")

        repeat = self.repo.repeat_sightings()
        self.assertEqual(len(repeat), 1)
        self.assertEqual(repeat[0]["times_seen"], 2)
        # Latest wording and evidence win — a second sighting usually has
        # better evidence, and first_seen_at still says when it started.
        self.assertEqual(repeat[0]["problem"], "The Monday export keeps failing")
        self.assertIn("three weeks", repeat[0]["evidence"])
        self.assertEqual(repeat[0]["lane"], "developer-tooling-gaps")
        self.assertLessEqual(repeat[0]["first_seen_at"], repeat[0]["last_seen_at"])

    def test_recent_sightings_respects_its_limit(self):
        report = self.report([candidate(f"p{i}", f"https://example.com/{i}") for i in range(5)])
        _record_sightings(self.repo, report, "lane")
        self.assertEqual(len(self.repo.recent_sightings(limit=2)), 2)


class TestRepetitionMemory(unittest.TestCase):
    def test_no_memory_adds_nothing_to_the_prompt(self):
        self.assertEqual(_already_seen_note([]), "")
        self.assertNotIn("Already seen", _scout_system_prompt(["themes"], None, None, []))

    def test_seen_problems_reach_the_system_prompt(self):
        seen = [{"problem": "Exports break every Monday", "times_seen": 3}]
        prompt = _scout_system_prompt(["cli tools"], None, None, seen)
        self.assertIn("Already seen", prompt)
        self.assertIn("Exports break every Monday", prompt)
        self.assertIn("seen 3x", prompt)

    def test_a_repeat_is_invited_rather_than_banned(self):
        # A problem sighted five times is a stronger signal than a fresh one;
        # forbidding the repeat would throw that away.
        note = _already_seen_note([{"problem": "p", "times_seen": 1}])
        self.assertIn("repeat", note.lower())
        self.assertIn("stronger signal", note)

    def test_a_long_problem_is_truncated_rather_than_flooding_the_prompt(self):
        note = _already_seen_note([{"problem": "x" * 500, "times_seen": 1}])
        self.assertLess(len(note.splitlines()[-1]), 160)

    def test_a_newline_in_a_stored_problem_cannot_break_the_bullet_list(self):
        note = _already_seen_note([{"problem": "line one\nline two", "times_seen": 1}])
        self.assertEqual(len([ln for ln in note.splitlines() if ln.startswith("- ")]), 1)

    def test_a_missing_times_seen_does_not_raise(self):
        self.assertIn("- p", _already_seen_note([{"problem": "p"}]))


if __name__ == "__main__":
    unittest.main()
