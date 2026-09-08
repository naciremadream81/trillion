"""
Tests for the board of advisors — playbook/the-board.md.

The fixtures name FICTIONAL advisors. That is deliberate and not cosmetic: a
dossier attributes numbered claims to a named person, and inventing citations
for a real one — even in a test file — produces exactly the artefact this
whole design exists to prevent. Nothing here should ever be copied into
agent/board/seats/.

The emphasis is on the parts the playbook says must be deterministic rather
than prompted: the citation gate, the unanimity guard, the prose/verdict
consistency check, and the hostile-input coercion.

Run from the project root:
    python -m unittest tests.test_board
"""

import asyncio
import os
import shutil
import tempfile
import unittest
from datetime import date

from agent.board.convene import Declined, hold_meeting, route
from agent.board.dossier import (
    RETIRED,
    SOURCED,
    USER,
    DossierError,
    gate_citations,
    load_roster,
    load_seat,
    parse_dossier_text,
)
from agent.board.meeting import (
    BudgetExhausted,
    Opinion,
    as_bool,
    as_confidence,
    as_text,
    contradicts_unanimity,
    convene,
    unanimity_possible,
    parse_opinion,
    parse_synthesis,
    seat_system_prompt,
)
from agent.board.research import apply_factcheck
from agent.board.routing import (
    normalize_name,
    seats_by_domain,
    seats_named_in,
    select_seats,
)
from agent.board.storage import BoardRepo, effective_seat

DOSSIER = """\
---
id: fictional-pricer
name: A. Fictional
seat: Pricing and packaging
domains: pricing, packaging
status: active
---

## Doctrine

### D1 — Price on value
source: An Invented Book (2021), ch. 4
verification: sourced

Charge for the outcome, not the hours.

### D2 — Never discount to close
source: A Made-Up Talk (2022)
verification: user

A discount trains the buyer to wait.

## Characteristic objection

Asks what the buyer would pay if you doubled the price.

## Blind spots

Nothing here transfers to regulated markets or hardware margins.

## Voice

Blunt, numeric, impatient with hedging.
"""

SECOND_DOSSIER = DOSSIER.replace("fictional-pricer", "fictional-operator") \
                        .replace("A. Fictional", "B. Imaginary O'Leary") \
                        .replace("domains: pricing, packaging", "domains: hiring, operations")


def run(coro):
    return asyncio.run(coro)


def make_ask(replies):
    """An ask_model that pops canned replies; records what it was shown."""
    calls = []

    async def ask_model(system="", prompt="", max_tokens=0):
        calls.append({"system": system, "prompt": prompt, "max_tokens": max_tokens})
        return replies.pop(0) if replies else "{}"

    ask_model.calls = calls
    return ask_model


# ── Tier 1: the parser ──────────────────────────────────────────────────────


class TestParser(unittest.TestCase):
    def test_parses_a_well_formed_dossier(self):
        seat = parse_dossier_text(DOSSIER)
        self.assertEqual(seat.id, "fictional-pricer")
        self.assertEqual(seat.domains, ("pricing", "packaging"))
        self.assertEqual([e.id for e in seat.doctrine], ["D1", "D2"])
        self.assertEqual(seat.doctrine[0].verification, SOURCED)
        self.assertEqual(seat.doctrine[1].verification, USER)
        self.assertIn("regulated markets", seat.blind_spots)
        self.assertIn("Blunt", seat.voice)

    def test_a_dossier_with_no_domains_is_rejected(self):
        text = DOSSIER.replace("domains: pricing, packaging\n", "")
        with self.assertRaises(DossierError) as ctx:
            parse_dossier_text(text)
        self.assertIn("routed", str(ctx.exception))

    def test_a_dossier_with_no_doctrine_is_rejected(self):
        text = DOSSIER.split("## Doctrine")[0] + "## Voice\n\nTerse.\n"
        with self.assertRaises(DossierError):
            parse_dossier_text(text)

    def test_duplicate_doctrine_ids_are_rejected_not_collapsed(self):
        # A citation to D1 when two entries claim D1 is ambiguous, and
        # anti-fabrication machinery that fails open is not machinery.
        text = DOSSIER.replace("### D2 —", "### D1 —")
        with self.assertRaises(DossierError) as ctx:
            parse_dossier_text(text)
        self.assertIn("duplicate", str(ctx.exception).lower())

    def test_ids_are_explicit_not_positional(self):
        # Reordering must not renumber anything: stored citations point at
        # ids, and a positional scheme would silently re-target them.
        entries = DOSSIER.split("## Doctrine\n\n")[1].split("## Characteristic")[0]
        first, second = entries.split("### D2")
        reordered = (
            DOSSIER.split("## Doctrine\n\n")[0]
            + "## Doctrine\n\n### D2" + second + first
            + "## Characteristic" + DOSSIER.split("## Characteristic")[1]
        )
        seat = parse_dossier_text(reordered)
        by_id = {e.id: e.title for e in seat.doctrine}
        self.assertEqual(by_id["D1"], "Price on value")
        self.assertEqual(by_id["D2"], "Never discount to close")

    def test_a_lowercase_id_is_normalized(self):
        seat = parse_dossier_text(DOSSIER.replace("### D2", "### d2"))
        self.assertIn("D2", [e.id for e in seat.doctrine])

    def test_an_unknown_verification_state_falls_back_to_user(self):
        # Never fail open on the trust flag.
        seat = parse_dossier_text(DOSSIER.replace("verification: sourced", "verification: verified"))
        self.assertEqual(seat.doctrine[0].verification, USER)


class TestRosterLoading(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def write(self, name, text):
        path = os.path.join(self.tmp, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return path

    def test_a_malformed_dossier_loses_only_its_own_seat(self):
        self.write("good.md", DOSSIER)
        self.write("bad.md", "this is not a dossier at all")
        with self.assertLogs("agent.board.dossier", level="WARNING") as logs:
            seats = load_roster(self.tmp)
        self.assertEqual([s.id for s in seats], ["fictional-pricer"])
        # It must not vanish silently — a quorum that quietly shrinks is
        # worse than a stale one.
        self.assertIn("bad.md", "\n".join(logs.output))

    def test_a_missing_roster_directory_is_an_empty_roster(self):
        with self.assertLogs("agent.board.dossier", level="WARNING"):
            self.assertEqual(load_roster(os.path.join(self.tmp, "nope")), [])

    def test_a_bad_encoding_degrades_to_a_warning_not_an_exception(self):
        # A file saved in the wrong encoding must lose nothing but a few
        # glyphs — it must not raise and take the whole roster with it.
        path = os.path.join(self.tmp, "mojibake.md")
        with open(path, "wb") as fh:
            fh.write(DOSSIER.encode("utf-8").replace(b"Blunt", b"Bl\xfcnt"))
        seat = load_seat(path)   # must not raise
        self.assertIsNotNone(seat)
        self.assertEqual(seat.id, "fictional-pricer")

    def test_readme_and_underscored_files_are_not_treated_as_seats(self):
        # They are not malformed dossiers, so they must not log a warning
        # on every roster load — a warning that always fires is one nobody
        # reads, and it would bury a real one.
        self.write("good.md", DOSSIER)
        self.write("README.md", "# How the format works\n")
        self.write("_notes.md", "scratch\n")
        seats = load_roster(self.tmp)
        self.assertEqual([s.id for s in seats], ["fictional-pricer"])

    def test_a_retired_seat_is_left_out_of_the_roster(self):
        self.write("retired.md", DOSSIER.replace("status: active", "status: retired"))
        self.assertEqual(load_roster(self.tmp), [])


# ── Tier 1: the citation gate ───────────────────────────────────────────────


class TestCitationGate(unittest.TestCase):
    def test_a_fabricated_citation_is_stripped(self):
        self.assertEqual(gate_citations(["D1", "D9"], {"D1", "D2"}), ["D1"])

    def test_case_is_normalized_and_order_preserved(self):
        self.assertEqual(gate_citations(["d2", "D1"], {"D1", "D2"}), ["D2", "D1"])

    def test_duplicates_collapse(self):
        self.assertEqual(gate_citations(["D1", "D1"], {"D1"}), ["D1"])

    def test_structural_junk_where_an_id_belonged_is_dropped(self):
        self.assertEqual(gate_citations([{"id": "D1"}, ["D1"], None, "D1"], {"D1"}), ["D1"])

    def test_a_non_list_returns_nothing(self):
        for value in ("D1", None, {"citations": ["D1"]}, 7):
            with self.subTest(value=value):
                self.assertEqual(gate_citations(value, {"D1"}), [])

    def test_the_valid_set_is_what_the_seat_was_shown_not_the_whole_file(self):
        # The retirement gap. D2 exists in the file but was withheld, so a
        # citation to it is a seat citing something it never saw.
        seat = parse_dossier_text(DOSSIER)
        from dataclasses import replace
        retired = replace(
            seat,
            doctrine=(seat.doctrine[0], replace(seat.doctrine[1], status=RETIRED)),
        )
        self.assertEqual(retired.visible_ids(), {"D1"})
        self.assertEqual(gate_citations(["D1", "D2"], retired.visible_ids()), ["D1"])


# ── Tier 3: routing ─────────────────────────────────────────────────────────


class TestRouting(unittest.TestCase):
    def setUp(self):
        self.pricer = parse_dossier_text(DOSSIER)
        self.operator = parse_dossier_text(SECOND_DOSSIER)
        self.seats = [self.pricer, self.operator]

    def test_a_curly_apostrophe_still_matches(self):
        # Voice input delivers U+2019. A naive comparison fails silently and
        # every spoken request for that advisor falls through to a guess.
        matched = seats_named_in("what would O’Leary say about this", self.seats)
        self.assertEqual([s.id for s in matched], ["fictional-operator"])

    def test_ascii_fullwidth_and_soft_hyphen_variants_all_match(self):
        for variant in ("O'Leary", "O＇Leary", "O’Leary", "O­Leary"):
            with self.subTest(variant=variant):
                matched = seats_named_in(f"ask {variant} about hiring", self.seats)
                self.assertEqual([s.id for s in matched], ["fictional-operator"])

    def test_a_full_name_matches(self):
        matched = seats_named_in("what does A. Fictional think", self.seats)
        self.assertEqual([s.id for s in matched], ["fictional-pricer"])

    def test_a_first_name_colliding_with_the_operator_does_not_route(self):
        # An advisor sharing Sean's first name must not be convened by a bare
        # mention of it — the surname is required.
        from dataclasses import replace
        collider = replace(self.pricer, name="Sean Imaginary")
        self.assertEqual(seats_named_in("what should Sean do here", [collider]), [])
        self.assertEqual(
            [s.id for s in seats_named_in("ask Sean Imaginary", [collider])],
            ["fictional-pricer"],
        )

    def test_domains_match_when_no_name_is_given(self):
        matched = seats_by_domain("how should I think about pricing", self.seats)
        self.assertEqual([s.id for s in matched], ["fictional-pricer"])

    def test_select_seats_deduplicates(self):
        # A router naming one advisor twice must not buy two calls — and must
        # never produce two identical opinions, because the unanimity guard
        # counts voices and one advisor would clear the bar by agreeing with
        # himself.
        chosen = select_seats(["fictional-pricer", "fictional-pricer"], self.seats)
        self.assertEqual([s.id for s in chosen], ["fictional-pricer"])

    def test_select_seats_caps(self):
        self.assertEqual(len(select_seats([s.id for s in self.seats], self.seats, max_seats=1)), 1)

    def test_select_seats_ignores_unknown_ids_and_junk(self):
        self.assertEqual(select_seats(["nobody", None, {"id": "x"}], self.seats), [])

    def test_normalize_name_is_idempotent(self):
        once = normalize_name("O’Leary")
        self.assertEqual(normalize_name(once), once)


class TestRouterCall(unittest.TestCase):
    def setUp(self):
        self.seats = [parse_dossier_text(DOSSIER), parse_dossier_text(SECOND_DOSSIER)]

    def test_a_named_advisor_skips_the_router_call_entirely(self):
        ask = make_ask([])
        chosen, reason = run(route("what would A. Fictional say", self.seats, ask))
        self.assertEqual([s.id for s in chosen], ["fictional-pricer"])
        self.assertEqual(ask.calls, [], "paid a router call to rediscover a name it was given")

    def test_a_decline_raises_with_the_reason(self):
        ask = make_ask(['{"convene": false, "seats": [], "reason": "this is a medical question"}'])
        with self.assertRaises(Declined) as ctx:
            run(route("what should I take for this headache", self.seats, ask))
        self.assertIn("medical", str(ctx.exception))

    def test_an_unreadable_router_reply_declines_rather_than_guessing(self):
        # The expensive direction is fanning out, so failure biases to
        # not spending four calls.
        ask = make_ask(["the router had a bad day"])
        with self.assertRaises(Declined):
            run(route("should I raise prices", self.seats, ask))

    def test_convene_true_with_no_usable_seats_falls_back_to_domains(self):
        ask = make_ask(['{"convene": true, "seats": ["who?"], "reason": "worth asking"}'])
        chosen, _ = run(route("a question about pricing", self.seats, ask))
        self.assertEqual([s.id for s in chosen], ["fictional-pricer"])

    def test_the_decline_criteria_protect_the_core_use_case(self):
        # The specific regression: a gate refusing "personal" questions once
        # declined "should I cut this product loose" as a personal business
        # decision — precisely what a board is for.
        from agent.board.routing import DECLINE_CRITERIA
        self.assertIn("CORE USE CASE", DECLINE_CRITERIA)
        self.assertIn("cut this product loose", DECLINE_CRITERIA)


# ── Tier 4: hostile input ───────────────────────────────────────────────────


class TestCoercion(unittest.TestCase):
    def test_the_string_false_is_false(self):
        # bool("false") is True, and that one line is the difference between
        # "this advisor abstained" and "this advisor did not".
        self.assertFalse(as_bool("false"))
        self.assertFalse(as_bool("False"))
        self.assertFalse(as_bool("no"))
        self.assertFalse(as_bool("0"))

    def test_real_truths_are_still_true(self):
        for value in (True, "true", "True", "yes", 1, "1"):
            with self.subTest(value=value):
                self.assertTrue(as_bool(value))

    def test_structures_and_nulls_are_false(self):
        for value in (None, {}, [], {"abstain": True}):
            with self.subTest(value=value):
                self.assertFalse(as_bool(value))

    def test_a_structure_where_prose_belonged_becomes_empty(self):
        self.assertEqual(as_text({"position": "x"}), "")
        self.assertEqual(as_text(["a", "b"]), "")
        self.assertEqual(as_text(None), "")

    def test_confidence_is_clamped_and_defaults_sanely(self):
        self.assertEqual(as_confidence(1.7), 1.0)
        self.assertEqual(as_confidence(-3), 0.0)
        self.assertEqual(as_confidence("0.8"), 0.8)
        self.assertEqual(as_confidence("very high"), 0.5)
        self.assertEqual(as_confidence(None), 0.5)
        self.assertEqual(as_confidence(True), 0.5)
        self.assertEqual(as_confidence(float("nan")), 0.5)


class TestParseOpinion(unittest.TestCase):
    def setUp(self):
        self.seat = parse_dossier_text(DOSSIER)

    def test_a_good_reply_parses(self):
        opinion = parse_opinion(self.seat, '{"position": "Raise them", '
                                           '"reasoning": "because", "citations": ["D1"], '
                                           '"confidence": 0.9, "abstain": false}')
        self.assertEqual(opinion.position, "Raise them")
        self.assertEqual(opinion.citations, ["D1"])
        self.assertFalse(opinion.abstained)
        self.assertFalse(opinion.unsourced)

    def test_a_string_false_abstain_does_not_abstain(self):
        opinion = parse_opinion(self.seat, '{"position": "Raise them", "abstain": "false"}')
        self.assertFalse(opinion.abstained)

    def test_an_abstention_is_not_a_failure(self):
        opinion = parse_opinion(self.seat, '{"abstain": true, "position": ""}')
        self.assertTrue(opinion.abstained)
        self.assertFalse(opinion.failed)
        self.assertFalse(opinion.spoke)

    def test_unreadable_output_is_a_failed_seat_not_an_exception(self):
        opinion = parse_opinion(self.seat, "I think you should raise prices, honestly")
        self.assertTrue(opinion.failed)
        self.assertFalse(opinion.abstained)

    def test_no_position_and_no_abstention_is_a_failure(self):
        opinion = parse_opinion(self.seat, '{"position": "", "abstain": false}')
        self.assertTrue(opinion.failed)

    def test_a_fabricated_citation_is_stripped_at_the_seam(self):
        opinion = parse_opinion(self.seat, '{"position": "x", "citations": ["D1", "D7"]}')
        self.assertEqual(opinion.citations, ["D1"])

    def test_a_seat_that_cites_nothing_is_flagged_unsourced(self):
        opinion = parse_opinion(self.seat, '{"position": "x", "citations": []}')
        self.assertTrue(opinion.unsourced)
        self.assertFalse(opinion.failed)


class TestIsolation(unittest.TestCase):
    def test_a_seat_prompt_contains_only_its_own_dossier(self):
        pricer = parse_dossier_text(DOSSIER)
        operator = parse_dossier_text(SECOND_DOSSIER)
        prompt = seat_system_prompt(pricer)
        self.assertIn("Price on value", prompt)
        self.assertNotIn(operator.name, prompt)
        # If this ever appears, the failure mode the design exists to
        # prevent has been rebuilt.
        self.assertNotIn("you are playing", prompt.lower())

    def test_a_retired_entry_is_withheld_from_the_seat(self):
        from dataclasses import replace
        seat = parse_dossier_text(DOSSIER)
        seat = replace(seat, doctrine=(seat.doctrine[0],
                                       replace(seat.doctrine[1], status=RETIRED)))
        prompt = seat_system_prompt(seat)
        self.assertIn("D1", prompt)
        self.assertNotIn("Never discount to close", prompt)


class TestFanOut(unittest.TestCase):
    def setUp(self):
        self.seats = [parse_dossier_text(DOSSIER), parse_dossier_text(SECOND_DOSSIER)]

    def test_a_zero_ceiling_spends_nothing(self):
        # A cost check that only runs after a call returns makes "spend
        # nothing" spend a whole fan-out before anything notices.
        ask = make_ask([])
        with self.assertRaises(BudgetExhausted):
            run(convene(self.seats, ask, "q", ceiling_usd=0))
        self.assertEqual(ask.calls, [])

    def test_one_seat_failing_leaves_a_partial_meeting_not_a_dead_one(self):
        async def ask(system="", prompt="", max_tokens=0):
            if "A. Fictional" in system:
                raise RuntimeError("timeout")
            return '{"position": "hire slower", "citations": ["D1"]}'

        opinions = run(convene(self.seats, ask, "q"))
        self.assertEqual(len(opinions), 2)
        self.assertTrue(any(o.failed for o in opinions))
        self.assertTrue(any(o.spoke for o in opinions))

    def test_each_seat_gets_its_own_call(self):
        ask = make_ask(['{"position": "a"}', '{"position": "b"}'])
        run(convene(self.seats, ask, "q"))
        self.assertEqual(len(ask.calls), 2)


# ── Tier 5: the guards ──────────────────────────────────────────────────────


class TestUnanimityGuard(unittest.TestCase):
    def speaking(self, n):
        return [Opinion(seat_id=f"s{i}", seat_name=f"S{i}", position="p") for i in range(n)]

    def test_one_voice_can_never_claim_unanimity(self):
        self.assertFalse(unanimity_possible(self.speaking(1)))

    def test_two_voices_make_the_claim_available(self):
        # Available, not established — whether they AGREE is the chair's
        # judgement, and parse_synthesis ANDs the two.
        self.assertTrue(unanimity_possible(self.speaking(2)))

    def test_abstentions_do_not_count_as_voices(self):
        opinions = self.speaking(1) + [Opinion(seat_id="a", seat_name="A", abstained=True)]
        self.assertFalse(unanimity_possible(opinions))

    def test_failures_do_not_count_as_voices(self):
        opinions = self.speaking(1) + [Opinion(seat_id="f", seat_name="F", failed=True)]
        self.assertFalse(unanimity_possible(opinions))

    def test_an_empty_room_cannot_claim_unanimity(self):
        self.assertFalse(unanimity_possible([]))

    def test_the_chair_can_withhold_the_claim_but_never_manufacture_it(self):
        # The floor allows it; the chair says the room split. Verdict: split.
        split = parse_synthesis('{"spoken": "They split.", "unanimous": false}', possible=True)
        self.assertFalse(split.unanimous)
        self.assertTrue(split.unanimity_possible)

        # The floor forbids it; the chair claims it anyway. Verdict: not
        # unanimous, and the prose is overwritten.
        forced = parse_synthesis('{"spoken": "The board is unanimous.", "unanimous": true}',
                                 possible=False)
        self.assertFalse(forced.unanimous)
        self.assertTrue(forced.guard_corrected)

    def test_a_genuine_consensus_survives(self):
        agreed = parse_synthesis('{"spoken": "The board is unanimous: raise.", "unanimous": true}',
                                 possible=True)
        self.assertTrue(agreed.unanimous)
        self.assertFalse(agreed.guard_corrected)
        self.assertIn("unanimous", agreed.spoken)


class TestSpokenMatchesVerdict(unittest.TestCase):
    def test_a_consensus_claim_is_caught_when_the_verdict_says_otherwise(self):
        # This happened: the guard was working perfectly and the prose
        # ignored it.
        for line in (
            "The board is unanimous: raise prices.",
            "All three agree you should wait.",
            "There was no disagreement in the room.",
            "The board agrees.",
        ):
            with self.subTest(line=line):
                self.assertTrue(contradicts_unanimity(line, unanimous=False))

    def test_an_honest_split_passes(self):
        self.assertFalse(contradicts_unanimity(
            "The board split on whether to raise prices.", unanimous=False))

    def test_nothing_is_flagged_when_the_room_really_was_unanimous(self):
        self.assertFalse(contradicts_unanimity("The board is unanimous.", unanimous=True))

    def test_the_synthesis_replaces_a_contradicting_line(self):
        synthesis = parse_synthesis('{"spoken": "The board is unanimous.", "detail": "d"}',
                                     possible=False)
        self.assertTrue(synthesis.guard_corrected)
        self.assertNotIn("unanimous", synthesis.spoken.lower())
        self.assertEqual(synthesis.detail, "d")

    def test_an_unreadable_chair_reply_still_yields_something_speakable(self):
        synthesis = parse_synthesis("the chair rambled", possible=False)
        self.assertTrue(synthesis.spoken)


# ── Tier 2: the adversarial merge ───────────────────────────────────────────


class TestApplyFactcheck(unittest.TestCase):
    def entries(self, n=3):
        return [{"title": f"t{i}", "source": f"s{i}", "body": f"b{i}"} for i in range(n)]

    def test_confirmed_entries_survive_and_are_marked_sourced(self):
        surviving, report = apply_factcheck(self.entries(2), [
            {"index": 0, "verdict": "confirmed"},
            {"index": 1, "verdict": "confirmed"},
        ])
        self.assertEqual([e["id"] for e in surviving], ["D1", "D2"])
        self.assertTrue(all(e["verification"] == SOURCED for e in surviving))
        self.assertEqual(len(report), 2)

    def test_a_rejected_entry_is_dropped_and_reported(self):
        surviving, report = apply_factcheck(self.entries(2), [
            {"index": 0, "verdict": "rejected", "reason": "that podcast episode does not exist"},
            {"index": 1, "verdict": "confirmed"},
        ])
        self.assertEqual([e["title"] for e in surviving], ["t1"])
        self.assertIn("does not exist", report[0]["reason"])

    def test_ids_are_renumbered_contiguously_after_a_rejection(self):
        surviving, _ = apply_factcheck(self.entries(3), [
            {"index": 0, "verdict": "rejected", "reason": "misattributed"},
            {"index": 1, "verdict": "confirmed"},
            {"index": 2, "verdict": "confirmed"},
        ])
        self.assertEqual([e["id"] for e in surviving], ["D1", "D2"])

    def test_a_correction_replaces_the_text(self):
        surviving, _ = apply_factcheck(self.entries(1), [
            {"index": 0, "verdict": "corrected", "source": "the real source",
             "body": "the narrower claim", "reason": "framed as a general law"},
        ])
        self.assertEqual(surviving[0]["source"], "the real source")
        self.assertEqual(surviving[0]["body"], "the narrower claim")
        self.assertEqual(surviving[0]["verification"], SOURCED)

    def test_an_entry_the_factcheck_ignored_is_not_confirmed(self):
        # Silence is not a verdict. Defaulting to sourced on missing input is
        # exactly the fail-open this tier exists to prevent.
        surviving, report = apply_factcheck(self.entries(2), [{"index": 0, "verdict": "confirmed"}])
        self.assertEqual(len(surviving), 1)
        self.assertEqual(report[1]["verdict"], "rejected")

    def test_an_unrecognized_verdict_is_treated_as_a_rejection(self):
        surviving, _ = apply_factcheck(self.entries(1), [{"index": 0, "verdict": "probably fine"}])
        self.assertEqual(surviving, [])

    def test_junk_results_reject_everything_rather_than_confirming_it(self):
        for results in (None, "confirmed", {}, [1, 2, 3]):
            with self.subTest(results=results):
                surviving, _ = apply_factcheck(self.entries(2), results)
                self.assertEqual(surviving, [])


# ── Tier 6: storage ─────────────────────────────────────────────────────────


class TestStorage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.repo = BoardRepo(os.path.join(self.tmp, "board.db"))
        self.seat = parse_dossier_text(DOSSIER)

    def test_an_edit_drops_the_entry_to_user_verification(self):
        # The server owns this. Otherwise the editor is a way to stamp an
        # unchecked claim as confirmed.
        self.repo.edit_entry("fictional-pricer", "D1", title="New", source="New", body="New")
        seat = effective_seat(self.seat, self.repo)
        entry = {e.id: e for e in seat.doctrine}["D1"]
        self.assertEqual(entry.verification, USER)
        self.assertEqual(entry.title, "New")

    def test_there_is_no_way_to_pass_a_verification_state_in(self):
        import inspect
        params = inspect.signature(self.repo.edit_entry).parameters
        self.assertNotIn("verification", params)

    def test_retiring_withholds_the_entry_but_keeps_the_id_spoken_for(self):
        self.repo.retire_entry("fictional-pricer", "D1")
        seat = effective_seat(self.seat, self.repo)
        self.assertEqual(seat.visible_ids(), {"D2"})
        # The row survives, so D1 can never be handed to different content.
        self.assertIn("D1", self.repo.edits_for("fictional-pricer"))

    def test_a_retired_entry_can_be_restored(self):
        self.repo.retire_entry("fictional-pricer", "D1")
        self.repo.restore_entry("fictional-pricer", "D1")
        self.assertEqual(effective_seat(self.seat, self.repo).visible_ids(), {"D1", "D2"})

    def test_an_unedited_seat_is_returned_untouched(self):
        self.assertIs(effective_seat(self.seat, self.repo), self.seat)

    def test_a_meeting_snapshots_its_citations(self):
        # So it still renders correctly after the dossier changes underneath.
        from agent.board.meeting import Synthesis
        opinion = Opinion(seat_id="fictional-pricer", seat_name="A. Fictional",
                          position="raise", citations=["D1"])
        meeting_id = self.repo.record_meeting(
            question="q", seats=[self.seat], opinions=[opinion],
            synthesis=Synthesis(spoken="s", unanimous=False),
        )
        stored = self.repo.get_meeting(meeting_id)
        citation = stored["opinions"][0]["citations"][0]
        self.assertEqual(citation["title"], "Price on value")
        self.assertIn("Invented Book", citation["source"])
        self.assertEqual(citation["verification"], SOURCED)

    def test_a_stored_meeting_survives_the_dossier_changing(self):
        from agent.board.meeting import Synthesis
        opinion = Opinion(seat_id="fictional-pricer", seat_name="A. Fictional",
                          position="raise", citations=["D1"])
        meeting_id = self.repo.record_meeting(
            question="q", seats=[self.seat], opinions=[opinion],
            synthesis=Synthesis(spoken="s"),
        )
        self.repo.edit_entry("fictional-pricer", "D1", title="Rewritten", source="", body="x")
        stored = self.repo.get_meeting(meeting_id)
        self.assertEqual(stored["opinions"][0]["citations"][0]["title"], "Price on value")

    def test_unprompted_meetings_are_distinguishable(self):
        from agent.board.meeting import Synthesis
        self.repo.record_meeting(question="q", seats=[self.seat], opinions=[],
                                 synthesis=Synthesis(spoken="s"), unprompted=True)
        self.assertTrue(self.repo.recent_meetings()[0]["unprompted"])


# ── End to end ──────────────────────────────────────────────────────────────


class TestHoldMeeting(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.seats_dir = os.path.join(self.tmp, "seats")
        os.makedirs(self.seats_dir)
        for name, text in (("a.md", DOSSIER), ("b.md", SECOND_DOSSIER)):
            with open(os.path.join(self.seats_dir, name), "w", encoding="utf-8") as fh:
                fh.write(text)
        self.repo = BoardRepo(os.path.join(self.tmp, "board.db"))

    def hold(self, replies, question="Should I raise prices?"):
        return run(hold_meeting(question, make_ask(list(replies)),
                                brief="MRR 12000", repo=self.repo,
                                seats_dir=self.seats_dir))

    def test_a_full_meeting_stores_and_returns(self):
        meeting = self.hold([
            '{"convene": true, "seats": ["fictional-pricer", "fictional-operator"], "reason": "r"}',
            '{"position": "raise", "citations": ["D1"], "confidence": 0.8}',
            '{"position": "wait", "citations": ["D2"], "confidence": 0.6}',
            '{"spoken": "The board split.", "detail": "d", "recommendation": "raise"}',
        ])
        self.assertEqual(len(meeting["seats"]), 2)
        # Two seats spoke, so the claim was available — but the chair did
        # not make it, so the verdict is a split.
        self.assertTrue(meeting["unanimity_possible"])
        self.assertFalse(meeting["unanimous"])
        self.assertEqual(meeting["spoken"], "The board split.")
        self.assertIsNotNone(self.repo.get_meeting(meeting["id"]))

    def test_one_seat_speaking_can_never_be_reported_unanimous(self):
        meeting = self.hold([
            '{"convene": true, "seats": ["fictional-pricer", "fictional-operator"], "reason": "r"}',
            '{"position": "raise", "citations": ["D1"]}',
            '{"abstain": true}',
            '{"spoken": "The board is unanimous: raise.", "detail": "d"}',
        ])
        self.assertFalse(meeting["unanimous"])
        self.assertTrue(meeting["guard_corrected"])
        self.assertNotIn("unanimous", meeting["spoken"].lower())
        # The roster sorts by id, so fictional-operator is seated first and
        # takes the first reply; the abstention lands on the pricer.
        self.assertEqual(meeting["abstained"], ["A. Fictional"])

    def test_the_chair_sees_the_full_brief_and_the_seats_see_less(self):
        long_brief = "X" * 3000
        ask = make_ask([
            '{"convene": true, "seats": ["fictional-pricer"], "reason": "r"}',
            '{"position": "raise", "citations": ["D1"]}',
            '{"spoken": "s", "detail": "d"}',
        ])
        run(hold_meeting("q", ask, brief=long_brief, repo=self.repo, seats_dir=self.seats_dir))
        seat_call, chair_call = ask.calls[1], ask.calls[2]
        self.assertLess(seat_call["prompt"].count("X"), 1000)
        self.assertEqual(chair_call["prompt"].count("X"), 3000)

    def test_a_chair_failure_still_stores_the_opinions(self):
        async def ask(system="", prompt="", max_tokens=0):
            if "chairing" in system:
                raise RuntimeError("chair exploded")
            if "router" in system:
                return '{"convene": true, "seats": ["fictional-pricer"], "reason": "r"}'
            return '{"position": "raise", "citations": ["D1"]}'

        meeting = run(hold_meeting("q", ask, repo=self.repo, seats_dir=self.seats_dir))
        self.assertTrue(meeting["spoken"])
        self.assertIsNotNone(self.repo.get_meeting(meeting["id"]))

    def test_an_empty_roster_is_reported_not_crashed(self):
        from agent.board.convene import BoardUnavailable
        empty = os.path.join(self.tmp, "empty")
        os.makedirs(empty)
        with self.assertRaises(BoardUnavailable):
            run(hold_meeting("q", make_ask([]), repo=self.repo, seats_dir=empty))

    def test_the_shipped_roster_is_empty_by_design(self):
        # A dossier whose citations were never verified is fabricated advice
        # in a real person's name. Nothing ships until research.py has run.
        from agent.board.convene import SEATS_DIR
        self.assertEqual(load_roster(SEATS_DIR), [])


if __name__ == "__main__":
    unittest.main()
