"""
Tests for agent/factory/software/doctrine.py — playbook/opportunity-scout.md
Tiers 2, 3 and 4.

The playbook is explicit that the degradation paths get tests rather than
assertions in a comment, because every one of them is reached by a scheduled
job at an hour nobody is watching. So there is one test per row of its
failure table, plus the two claims Tier 4 actually makes: an override takes
effect without a restart, and deleting it brings the file default back.

Run from the project root:
    python -m unittest tests.test_scout_doctrine
"""

import os
import shutil
import tempfile
import unittest
from datetime import date

from agent.factory.software import doctrine
from agent.factory.software.doctrine import (
    DOCTRINE_KEY,
    EMERGENCY_DOCTRINE,
    EMERGENCY_LANE_LABEL,
    LANES_KEY,
    DocumentOverrides,
    active_lanes,
    doctrine_prompt,
    effective_document,
    lane_for,
    lane_index,
    load_document,
    strip_operator_notes,
    parse_lanes,
    slugify,
)
from agent.factory.software.storage import BuildRepo


class FakeClock:
    """Monotonic-shaped clock the TTL tests advance by hand."""

    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class StubRepo:
    """Anything with all_documents() is a valid override source."""

    def __init__(self, documents=None, raises=False):
        self.documents = dict(documents or {})
        self.raises = raises
        self.reads = 0

    def all_documents(self):
        self.reads += 1
        if self.raises:
            raise RuntimeError("database is locked")
        return dict(self.documents)


class TempFileMixin:
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        # load_document caches on (mtime, size) keyed by path; a fresh temp
        # directory per test keeps one test's cache out of the next one's way.
        doctrine._file_cache.clear()

    def write(self, name, body):
        path = os.path.join(self.tmp, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(body)
        return path


# ── Tier 2: the document loader ─────────────────────────────────────────────


class TestLoadDocument(TempFileMixin, unittest.TestCase):
    def test_reads_a_file(self):
        path = self.write("d.md", "the doctrine")
        self.assertEqual(load_document(path), "the doctrine")

    def test_missing_file_returns_empty_rather_than_raising(self):
        self.assertEqual(load_document(os.path.join(self.tmp, "nope.md")), "")

    def test_a_directory_in_place_of_a_file_returns_empty(self):
        self.assertEqual(load_document(self.tmp), "")

    def test_an_edit_is_picked_up_without_a_restart(self):
        # The Tier 2 verification: edit one line, re-run, see the change.
        path = self.write("d.md", "first")
        self.assertEqual(load_document(path), "first")
        self.write("d.md", "second")
        self.assertEqual(load_document(path), "second")

    def test_an_edit_of_identical_length_is_still_picked_up(self):
        # Same byte count, so only the mtime distinguishes them — and on a
        # coarse filesystem clock, sometimes not even that. Caching on the
        # pair is what makes this reliable rather than usually-fine.
        path = self.write("d.md", "aaaa")
        self.assertEqual(load_document(path), "aaaa")
        os.utime(path, (2_000_000, 2_000_000))
        self.write("d.md", "bbbb")
        os.utime(path, (3_000_000, 3_000_000))
        self.assertEqual(load_document(path), "bbbb")


class TestStripOperatorNotes(unittest.TestCase):
    def test_a_leading_notes_block_is_dropped(self):
        self.assertEqual(
            strip_operator_notes("notes for the operator\n\n---\n\nthe real doctrine"),
            "the real doctrine",
        )

    def test_a_document_with_no_rule_is_sent_whole(self):
        self.assertEqual(strip_operator_notes("just the doctrine"), "just the doctrine")

    def test_a_rule_far_down_is_treated_as_a_section_divider(self):
        # Otherwise a long doctrine using --- between sections would silently
        # lose everything above the first one.
        body = "\n".join(f"line {i}" for i in range(60)) + "\n---\ntail"
        self.assertEqual(strip_operator_notes(body), body.strip())

    def test_empty_input_is_empty_output(self):
        self.assertEqual(strip_operator_notes(""), "")
        self.assertEqual(strip_operator_notes(None), "")


class TestShippedDoctrine(unittest.TestCase):
    def test_the_shipped_doctrine_loads_and_is_substantial(self):
        prompt = doctrine_prompt()
        self.assertNotEqual(prompt, EMERGENCY_DOCTRINE)
        self.assertGreater(len(prompt), 500)

    def test_the_shipped_doctrine_states_who_decides(self):
        # "You recommend, the human decides" belongs in the prompt itself,
        # not only in the architecture — a model that believes it is deciding
        # writes more confidently and less usefully.
        self.assertIn("decides", doctrine_prompt().lower())

    def test_the_operator_notes_do_not_reach_the_model(self):
        prompt = doctrine_prompt()
        self.assertNotIn("cached on mtime", prompt)
        self.assertNotIn("agent_documents", prompt)
        self.assertTrue(prompt.startswith("You are the Trillion Software Factory"), prompt[:80])

    def test_the_shipped_doctrine_lists_hard_kills_as_bullets(self):
        # "Hard kills are a list, not a paragraph" — they get skimmed, and a
        # rule without a reason attached gets rationalized around.
        prompt = doctrine_prompt()
        kills = prompt.split("## Hard kills", 1)[1].split("## Order of authority", 1)[0]
        self.assertGreaterEqual(len([ln for ln in kills.splitlines() if ln.startswith("- ")]), 5)


# ── Tier 4: overrides ───────────────────────────────────────────────────────


class TestDocumentOverrides(TempFileMixin, unittest.TestCase):
    def test_an_override_shadows_the_file(self):
        path = self.write("d.md", "from the file")
        overrides = DocumentOverrides(StubRepo({DOCTRINE_KEY: "from the override"}))
        self.assertEqual(effective_document(DOCTRINE_KEY, path, overrides), "from the override")

    def test_no_override_falls_through_to_the_file(self):
        path = self.write("d.md", "from the file")
        overrides = DocumentOverrides(StubRepo({}))
        self.assertEqual(effective_document(DOCTRINE_KEY, path, overrides), "from the file")

    def test_an_empty_override_falls_through_rather_than_blanking_the_prompt(self):
        path = self.write("d.md", "from the file")
        overrides = DocumentOverrides(StubRepo({DOCTRINE_KEY: "   \n  "}))
        self.assertEqual(effective_document(DOCTRINE_KEY, path, overrides), "from the file")

    def test_a_repo_that_raises_does_not_break_the_read(self):
        path = self.write("d.md", "from the file")
        overrides = DocumentOverrides(StubRepo(raises=True))
        self.assertEqual(effective_document(DOCTRINE_KEY, path, overrides), "from the file")

    def test_reads_inside_the_ttl_do_not_re_query(self):
        repo = StubRepo({DOCTRINE_KEY: "x"})
        clock = FakeClock()
        overrides = DocumentOverrides(repo, ttl_seconds=30, clock=clock)
        for _ in range(5):
            overrides.get(DOCTRINE_KEY)
        self.assertEqual(repo.reads, 1)

    def test_a_write_elsewhere_lands_after_the_ttl_without_a_restart(self):
        # The claim Tier 4 actually makes. Same process object throughout —
        # nothing is reconstructed, which is what "without restarting" means.
        repo = StubRepo({})
        clock = FakeClock()
        overrides = DocumentOverrides(repo, ttl_seconds=30, clock=clock)
        self.assertIsNone(overrides.get(DOCTRINE_KEY))

        repo.documents[DOCTRINE_KEY] = "set by the operator"
        clock.advance(31)
        self.assertEqual(overrides.get(DOCTRINE_KEY), "set by the operator")

    def test_deleting_an_override_restores_the_file_default(self):
        path = self.write("d.md", "from the file")
        repo = StubRepo({DOCTRINE_KEY: "from the override"})
        clock = FakeClock()
        overrides = DocumentOverrides(repo, ttl_seconds=30, clock=clock)
        self.assertEqual(effective_document(DOCTRINE_KEY, path, overrides), "from the override")

        del repo.documents[DOCTRINE_KEY]
        clock.advance(31)
        self.assertEqual(effective_document(DOCTRINE_KEY, path, overrides), "from the file")

    def test_refresh_makes_a_write_visible_immediately(self):
        # The process that wrote the override shouldn't have to wait out its
        # own TTL to see it.
        repo = StubRepo({})
        clock = FakeClock()
        overrides = DocumentOverrides(repo, ttl_seconds=30, clock=clock)
        overrides.get(DOCTRINE_KEY)
        repo.documents[DOCTRINE_KEY] = "just written"
        overrides.refresh()
        self.assertEqual(overrides.get(DOCTRINE_KEY), "just written")


class TestOverrideStorage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.repo = BuildRepo(os.path.join(self.tmp, "sf.db"))

    def test_absent_override_reads_as_none(self):
        self.assertIsNone(self.repo.get_document(LANES_KEY))

    def test_set_then_get_round_trips(self):
        self.repo.set_document(LANES_KEY, "## One\n\nbrief")
        self.assertEqual(self.repo.get_document(LANES_KEY), "## One\n\nbrief")

    def test_set_twice_replaces_rather_than_duplicating(self):
        self.repo.set_document(LANES_KEY, "first")
        self.repo.set_document(LANES_KEY, "second")
        self.assertEqual(self.repo.get_document(LANES_KEY), "second")
        self.assertEqual(list(self.repo.all_documents()), [LANES_KEY])

    def test_an_empty_override_is_distinguishable_from_no_override(self):
        self.repo.set_document(LANES_KEY, "")
        self.assertEqual(self.repo.get_document(LANES_KEY), "")
        self.assertIsNone(self.repo.get_document("something_else"))

    def test_delete_is_idempotent(self):
        self.repo.set_document(LANES_KEY, "x")
        self.repo.delete_document(LANES_KEY)
        self.repo.delete_document(LANES_KEY)
        self.assertIsNone(self.repo.get_document(LANES_KEY))

    def test_all_documents_returns_every_key(self):
        self.repo.set_document(LANES_KEY, "a")
        self.repo.set_document(DOCTRINE_KEY, "b")
        self.assertEqual(self.repo.all_documents(), {LANES_KEY: "a", DOCTRINE_KEY: "b"})

    def test_the_repo_satisfies_the_overrides_contract(self):
        self.repo.set_document(DOCTRINE_KEY, "real doctrine")
        overrides = DocumentOverrides(self.repo)
        self.assertEqual(overrides.get(DOCTRINE_KEY), "real doctrine")


# ── Tier 3: lanes ───────────────────────────────────────────────────────────


class TestSlugify(unittest.TestCase):
    def test_lowercases_and_hyphenates(self):
        self.assertEqual(slugify("Open Source Friction"), "open-source-friction")

    def test_cosmetic_reformatting_keeps_the_same_label(self):
        # The reason labels are slugs at all: renaming a heading for style
        # must not fork the record of which lane found what.
        for heading in ("Open Source Friction", "open source friction", "Open-Source Friction"):
            with self.subTest(heading=heading):
                self.assertEqual(slugify(heading), "open-source-friction")

    def test_punctuation_collapses_rather_than_accumulating(self):
        self.assertEqual(slugify("Data — trapped!! in the wrong shape"),
                         "data-trapped-in-the-wrong-shape")

    def test_a_heading_with_no_alphanumerics_has_no_label(self):
        self.assertEqual(slugify("— ***"), "")


class TestParseLanes(unittest.TestCase):
    def test_parses_headings_and_briefs_in_order(self):
        lanes = parse_lanes(
            "Prose above the first heading is ignored.\n\n"
            "## First Lane\n\nbrief one\n\n"
            "## Second Lane\n\nbrief two\n"
        )
        self.assertEqual([lane.label for lane in lanes], ["first-lane", "second-lane"])
        self.assertEqual(lanes[0].brief, "brief one")
        self.assertEqual(lanes[1].brief, "brief two")
        self.assertEqual(lanes[0].title, "First Lane")

    def test_junk_parses_to_empty_rather_than_raising(self):
        for text in ("", "   ", "no headings at all", "# H1 only", "#### too deep"):
            with self.subTest(text=text):
                self.assertEqual(parse_lanes(text), [])

    def test_none_parses_to_empty(self):
        self.assertEqual(parse_lanes(None), [])

    def test_a_heading_with_no_brief_is_skipped(self):
        lanes = parse_lanes("## Empty\n\n## Real\n\nbrief\n")
        self.assertEqual([lane.label for lane in lanes], ["real"])

    def test_a_heading_with_no_usable_label_is_skipped(self):
        lanes = parse_lanes("## ***\n\nbrief\n\n## Real\n\nbrief\n")
        self.assertEqual([lane.label for lane in lanes], ["real"])

    def test_a_multi_paragraph_brief_survives_whole(self):
        lanes = parse_lanes("## One\n\npara one\n\npara two\n")
        self.assertEqual(lanes[0].brief, "para one\n\npara two")


class TestLaneIndex(unittest.TestCase):
    def test_maps_weekdays_across_a_full_rotation(self):
        # 2026-09-07 is a Monday.
        self.assertEqual(lane_index(date(2026, 9, 7), 7), 0)
        self.assertEqual(lane_index(date(2026, 9, 11), 7), 4)

    def test_wraps_when_there_are_fewer_lanes_than_days(self):
        # The subtle one. Friday maps to index 4; with three lanes live that
        # has to become 1, not an IndexError in a job nobody is watching.
        self.assertEqual(lane_index(date(2026, 9, 11), 3), 1)

    def test_a_single_lane_serves_every_day(self):
        for day in range(7, 14):
            with self.subTest(day=day):
                self.assertEqual(lane_index(date(2026, 9, day), 1), 0)

    def test_zero_lanes_does_not_divide_by_zero(self):
        self.assertEqual(lane_index(date(2026, 9, 7), 0), 0)


class TestActiveLanes(TempFileMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self._real_lanes_path = doctrine.LANES_PATH
        self.addCleanup(setattr, doctrine, "LANES_PATH", self._real_lanes_path)

    def use_lanes_file(self, body):
        doctrine.LANES_PATH = self.write("LANES.md", body)

    def test_an_override_that_parses_wins(self):
        self.use_lanes_file("## From File\n\nfile brief\n")
        overrides = DocumentOverrides(StubRepo({LANES_KEY: "## From Override\n\noverride brief\n"}))
        self.assertEqual([lane.label for lane in active_lanes(overrides)], ["from-override"])

    def test_an_override_parsing_to_zero_lanes_falls_back_to_the_file(self):
        self.use_lanes_file("## From File\n\nfile brief\n")
        overrides = DocumentOverrides(StubRepo({LANES_KEY: "I deleted all the headings"}))
        with self.assertLogs("agent.factory.software.doctrine", level="WARNING") as logs:
            lanes = active_lanes(overrides)
        self.assertEqual([lane.label for lane in lanes], ["from-file"])
        # The log has to say what the format is, not only that it broke.
        self.assertIn("## Heading", "\n".join(logs.output))

    def test_a_missing_file_yields_the_emergency_lane(self):
        doctrine.LANES_PATH = os.path.join(self.tmp, "gone.md")
        with self.assertLogs("agent.factory.software.doctrine", level="ERROR"):
            lanes = active_lanes(None)
        self.assertEqual([lane.label for lane in lanes], [EMERGENCY_LANE_LABEL])

    def test_an_unparseable_file_yields_the_emergency_lane(self):
        self.use_lanes_file("nothing but prose")
        with self.assertLogs("agent.factory.software.doctrine", level="ERROR"):
            lanes = active_lanes(None)
        self.assertEqual([lane.label for lane in lanes], [EMERGENCY_LANE_LABEL])

    def test_a_single_lane_document_still_serves_every_scheduled_day(self):
        self.use_lanes_file("## Only One\n\nbrief\n")
        for day in range(7, 14):
            with self.subTest(day=day):
                self.assertEqual(lane_for(date(2026, 9, day), None).label, "only-one")

    def test_lane_for_never_raises_on_a_deleted_document(self):
        doctrine.LANES_PATH = os.path.join(self.tmp, "gone.md")
        with self.assertLogs("agent.factory.software.doctrine", level="ERROR"):
            lane = lane_for(date(2026, 9, 11), None)
        self.assertEqual(lane.label, EMERGENCY_LANE_LABEL)


class TestShippedLanes(unittest.TestCase):
    def test_the_shipped_lanes_file_parses(self):
        lanes = active_lanes(None)
        self.assertGreater(len(lanes), 1)
        self.assertNotIn(EMERGENCY_LANE_LABEL, [lane.label for lane in lanes])

    def test_every_shipped_lane_has_a_distinct_label(self):
        labels = [lane.label for lane in active_lanes(None)]
        self.assertEqual(len(labels), len(set(labels)))

    def test_every_weekday_resolves_to_a_real_lane(self):
        for day in range(7, 14):
            with self.subTest(day=day):
                self.assertTrue(lane_for(date(2026, 9, day), None).brief)


if __name__ == "__main__":
    unittest.main()
