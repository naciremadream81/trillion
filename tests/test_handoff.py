"""
Tests for agent/factory/handoff.py — orchestration.md Tier 5,
"propose, don't chain".

The properties that matter, in the playbook's own words:
  - "Confirm that *nothing* dispatches to the target agent until the human
    explicitly accepts."
  - "Confirm artifacts ride as references and the receiving agent reads them
    from the path/ID, not from an inlined payload."
  - "the human is the circuit-breaker."

Run from the project root:
    python -m unittest tests.test_handoff
"""

import asyncio
import os
import shutil
import tempfile
import unittest

from agent.factory.handoff import (
    Handoff,
    ProposeHandoffTool,
    format_offer,
    phrase_confidence,
    validate,
)
from agent.safety.risk import READ_ONLY
from agent.safety.storage import PENDING, SafetyRepo


def run(coro):
    return asyncio.run(coro)


class TestValidate(unittest.TestCase):
    def setUp(self):
        self.known = {"researcher", "writer"}

    def test_a_sound_handoff_has_no_errors(self):
        h = Handoff("researcher", "needs sources", "find three papers on X")
        self.assertEqual(validate(h, self.known), [])

    def test_unknown_target_is_rejected(self):
        h = Handoff("nobody", "why", "what")
        errors = validate(h, self.known)
        self.assertTrue(any("not an active specialist" in e for e in errors))

    def test_missing_task_is_rejected(self):
        self.assertTrue(any("task is required" in e for e in validate(
            Handoff("researcher", "why", "   "), self.known)))

    def test_missing_reason_is_rejected(self):
        self.assertTrue(any("reason is required" in e for e in validate(
            Handoff("researcher", "", "do it"), self.known)))

    def test_inline_payload_artifact_is_rejected(self):
        # "Pass references, not payloads" — with teeth. An inline blob is
        # untrusted content riding into the next agent's prompt through a
        # channel that looks like metadata and skips the registry's scrub.
        h = Handoff("researcher", "why", "what", artifacts={"draft": "line one\nline two"})
        self.assertTrue(any("looks like inline content" in e for e in validate(h, self.known)))

    def test_overlong_artifact_is_rejected(self):
        h = Handoff("researcher", "why", "what", artifacts={"blob": "x" * 5000})
        self.assertTrue(any("looks like inline content" in e for e in validate(h, self.known)))

    def test_ordinary_references_are_accepted(self):
        h = Handoff(
            "researcher", "why", "what",
            artifacts={
                "spec": "docs/specs/2026-08-20-thing.md",
                "build": "generated-projects/thing",
                "issue": "https://github.com/naciremadream81/trillion/issues/12",
            },
        )
        self.assertEqual(validate(h, self.known), [])

    def test_non_string_artifact_is_rejected(self):
        h = Handoff("researcher", "why", "what", artifacts={"n": 5})
        self.assertTrue(any("must be a string reference" in e for e in validate(h, self.known)))

    def test_confidence_out_of_range_is_rejected(self):
        for bad in (-0.1, 1.5, 42):
            with self.subTest(bad=bad):
                h = Handoff("researcher", "why", "what", confidence=bad)
                self.assertTrue(any("between 0 and 1" in e for e in validate(h, self.known)))

    def test_non_numeric_confidence_is_rejected(self):
        h = Handoff("researcher", "why", "what", confidence="high")
        self.assertTrue(any("must be a number" in e for e in validate(h, self.known)))

    def test_too_many_artifacts_is_rejected(self):
        h = Handoff("researcher", "why", "what",
                    artifacts={f"a{i}": f"path/{i}" for i in range(50)})
        self.assertTrue(any("more than" in e for e in validate(h, self.known)))


class TestPhrasing(unittest.TestCase):
    def test_confidence_changes_phrasing_not_the_offer(self):
        # Confidence is for *phrasing*. A low-confidence handoff is still
        # offered — nothing here ever routes around Sean.
        self.assertNotEqual(phrase_confidence(0.9), phrase_confidence(0.2))

    def test_offer_names_the_action_id_and_target(self):
        h = Handoff("writer", "draft is ready to polish", "tighten the intro")
        offer = format_offer(h, 7, "researcher")
        self.assertIn("id 7", offer)
        self.assertIn("writer", offer)
        self.assertIn("researcher", offer)
        self.assertIn("confirm_action(action_id=7)", offer)

    def test_offer_lists_artifacts_and_preconditions(self):
        h = Handoff(
            "writer", "why", "what",
            artifacts={"draft": "docs/draft.md"},
            preconditions=["the build passed"],
        )
        offer = format_offer(h, 1, "researcher")
        self.assertIn("docs/draft.md", offer)
        self.assertIn("the build passed", offer)


class TestProposeHandoffTool(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._prev = os.environ.get("TRILLION_SAFETY_DB")
        os.environ["TRILLION_SAFETY_DB"] = os.path.join(self.tmp, "safety.db")
        self.repo = SafetyRepo(os.path.join(self.tmp, "safety.db"))
        self.history = [{"role": "user", "content": "hi"}]
        self.tool = ProposeHandoffTool(
            safety_repo=self.repo,
            history_provider=lambda: self.history,
            active_agents_provider=lambda: {"researcher", "writer"},
            proposer_slug="analyst",
        )

    def tearDown(self):
        if self._prev is None:
            os.environ.pop("TRILLION_SAFETY_DB", None)
        else:
            os.environ["TRILLION_SAFETY_DB"] = self._prev
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_proposing_parks_a_pending_action_and_dispatches_nothing(self):
        result = run(self.tool.run(
            target_agent="researcher", reason="needs sources", task="find three papers"
        ))
        self.assertIn("HANDOFF PROPOSED", result)
        pending = self.repo.list_pending()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["tool_name"], "dispatch_to_researcher")
        self.assertEqual(pending[0]["status"], PENDING)

    def test_the_parked_action_carries_the_task_as_the_dispatch_message(self):
        run(self.tool.run(
            target_agent="writer", reason="ready to polish", task="tighten the intro"
        ))
        action = self.repo.list_pending()[0]
        self.assertEqual(action["arguments"], {"message": "tighten the intro"})

    def test_history_index_is_captured_so_consent_must_come_after(self):
        # The self-approval defense: approving needs a genuine human turn
        # *after* this index, in the main conversation.
        self.history = [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}]
        run(self.tool.run(target_agent="writer", reason="r", task="t"))
        self.assertEqual(self.repo.list_pending()[0]["history_index"], 2)

    def test_a_rejected_handoff_parks_nothing(self):
        result = run(self.tool.run(
            target_agent="nobody", reason="r", task="t"
        ))
        self.assertIn("handoff rejected", result)
        self.assertEqual(self.repo.list_pending(), [])

    def test_a_specialist_cannot_hand_off_to_itself(self):
        tool = ProposeHandoffTool(
            safety_repo=self.repo,
            history_provider=lambda: self.history,
            active_agents_provider=lambda: {"analyst", "writer"},
            proposer_slug="analyst",
        )
        result = run(tool.run(target_agent="analyst", reason="r", task="t"))
        self.assertIn("handoff rejected", result)
        self.assertEqual(self.repo.list_pending(), [])

    def test_inline_payload_parks_nothing(self):
        result = run(self.tool.run(
            target_agent="writer", reason="r", task="t",
            artifacts={"draft": "a very long\npasted document"},
        ))
        self.assertIn("handoff rejected", result)
        self.assertEqual(self.repo.list_pending(), [])

    def test_a_storage_failure_comes_back_as_data_not_an_exception(self):
        # orchestration.md Tier 3: errors cross a boundary as values.
        class Broken:
            def create_pending(self, **kwargs):
                raise RuntimeError("db gone")

        tool = ProposeHandoffTool(
            safety_repo=Broken(),
            history_provider=lambda: [],
            active_agents_provider=lambda: {"writer"},
            proposer_slug="analyst",
        )
        result = run(tool.run(target_agent="writer", reason="r", task="t"))
        self.assertIn("could not be recorded", result)

    def test_a_broken_agent_lookup_does_not_raise(self):
        def boom():
            raise RuntimeError("factory.db gone")

        tool = ProposeHandoffTool(
            safety_repo=self.repo,
            history_provider=lambda: [],
            active_agents_provider=boom,
            proposer_slug="analyst",
        )
        result = run(tool.run(target_agent="writer", reason="r", task="t"))
        self.assertIn("handoff rejected", result)

    def test_the_proposal_is_written_to_the_audit_log(self):
        run(self.tool.run(target_agent="writer", reason="r", task="t"))
        events = [row["event"] for row in self.repo.recent_audit()]
        self.assertIn("handoff_proposed", events)

    def test_the_tool_is_reachable_by_a_specialist_and_never_gated(self):
        # Gating the proposal itself would deadlock: a spawned specialist
        # cannot reach confirm_action, so its own gated call could never be
        # approved by anyone.
        self.assertTrue(ProposeHandoffTool.factory_allowed)
        self.assertFalse(ProposeHandoffTool.requires_confirmation)
        self.assertEqual(ProposeHandoffTool.risk, READ_ONLY)


class TestHandoffApprovalPath(unittest.TestCase):
    """The end-to-end property: nothing dispatches until Sean says yes."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.repo = SafetyRepo(os.path.join(self.tmp, "safety.db"))
        self.history = [{"role": "user", "content": "look into this"}]
        self.tool = ProposeHandoffTool(
            safety_repo=self.repo,
            history_provider=lambda: self.history,
            active_agents_provider=lambda: {"writer"},
            proposer_slug="analyst",
        )

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_denied_handoff_never_becomes_approved(self):
        run(self.tool.run(target_agent="writer", reason="r", task="t"))
        action_id = self.repo.list_pending()[0]["id"]
        self.repo.deny(action_id, "not now")
        self.assertEqual(self.repo.list_pending(), [])
        self.assertNotEqual(self.repo.get(action_id)["status"], PENDING)

    def test_approval_freezes_the_task_it_was_proposed_with(self):
        # Approving action N runs N's arguments, whatever the model would
        # prefer by the time Sean answers.
        run(self.tool.run(target_agent="writer", reason="r", task="the original task"))
        action_id = self.repo.list_pending()[0]["id"]
        approved = self.repo.approve(action_id)
        self.assertEqual(approved["arguments"], {"message": "the original task"})


if __name__ == "__main__":
    unittest.main()
