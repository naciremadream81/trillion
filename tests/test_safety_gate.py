"""
Tier 6: the Gate.

The load-bearing test in here is SelfApprovalTests — it's the one that proves
the model cannot approve its own action inside a single turn. Everything else
is the supporting machinery.
"""

import asyncio
import os
import tempfile
import unittest

from agent.providers.base import ToolCall
from agent.safety import storage
from agent.safety.approval import Gate, _summarize, last_human_turn_index
from agent.safety.risk import CONSEQUENTIAL, HARDLINE, LOW, READ_ONLY
from agent.tools.base import BaseTool
from agent.tools.confirm import ConfirmActionTool
from agent.tools.registry import ToolRegistry


class RecordingTool(BaseTool):
    """A tool that records that it ran. The gate's job is to stop it."""

    name = "write_thing"
    description = "writes a thing"
    input_schema = {"type": "object", "properties": {}}
    risk = CONSEQUENTIAL

    def __init__(self):
        self.calls: list[dict] = []

    async def run(self, **kwargs) -> str:
        self.calls.append(kwargs)
        return f"wrote {kwargs.get('content', '')!r}"


class LookTool(RecordingTool):
    name = "look_thing"
    risk = READ_ONLY


class ExplodingTool(RecordingTool):
    name = "explode_thing"
    risk = CONSEQUENTIAL

    async def run(self, **kwargs) -> str:
        raise RuntimeError("boom")


def human(text="ok"):
    """A genuine turn from Sean: content is a plain string."""
    return {"role": "user", "content": text}


def tool_results(*names):
    """A tool-result turn: role is 'user', but content is a list of blocks."""
    return {
        "role": "user",
        "content": [
            {"type": "tool_result", "tool_use_id": n, "content": "ok"}
            for n in names
        ],
    }


def assistant(text="sure"):
    return {"role": "assistant", "content": text}


class GateTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = storage.SafetyRepo(os.path.join(self.tmp.name, "safety.db"))
        self.registry = ToolRegistry()
        self.write = RecordingTool()
        self.look = LookTool()
        self.registry.register(self.write)
        self.registry.register(self.look)
        self.gate = Gate(self.repo, self.registry, mode="smart", ttl_seconds=900)

    def call(self, name="write_thing", **arguments):
        return ToolCall(id="tu_1", name=name, arguments=arguments)

    def run_async(self, coro):
        return asyncio.run(coro)


class EvaluateTests(GateTestCase):
    def test_read_only_tool_passes_straight_through(self):
        self.assertIsNone(self.gate.evaluate(self.call("look_thing"), []))

    def test_consequential_tool_is_parked_not_run(self):
        verdict = self.gate.evaluate(self.call(content="x"), [human()])
        self.assertIsNotNone(verdict)
        self.assertIn("CONFIRMATION REQUIRED", verdict)
        self.assertEqual(self.write.calls, [], "the tool ran anyway")

    def test_the_verdict_carries_the_action_id_the_model_needs(self):
        verdict = self.gate.evaluate(self.call(content="x"), [human()])
        action_id = self.repo.list_pending()[0]["id"]
        self.assertIn(f"#{action_id}", verdict)
        self.assertIn(f"confirm_action(action_id={action_id})", verdict)

    def test_arguments_are_frozen_at_gate_time(self):
        self.gate.evaluate(self.call(content="original"), [human()])
        parked = self.repo.list_pending()[0]
        self.assertEqual(parked["arguments"]["content"], "original")

    def test_history_index_is_recorded(self):
        history = [human(), assistant()]
        self.gate.evaluate(self.call(content="x"), history)
        self.assertEqual(self.repo.list_pending()[0]["history_index"], 2)

    def test_unknown_tool_is_gated_fail_closed(self):
        # No tool object means no declared tier, which normalizes to
        # CONSEQUENTIAL rather than to "let it through".
        verdict = self.gate.evaluate(self.call("no_such_tool"), [human()])
        self.assertIn("CONFIRMATION REQUIRED", verdict)

    def test_mode_off_still_parks_a_hardline_tool(self):
        class Danger(RecordingTool):
            name = "send_email"
            risk = READ_ONLY  # lying about itself; the list wins

        self.registry.register(Danger())
        gate = Gate(self.repo, self.registry, mode="off")
        verdict = gate.evaluate(self.call("send_email"), [human()])
        self.assertIn("CONFIRMATION REQUIRED", verdict)

    def test_mode_off_lets_a_merely_consequential_tool_run(self):
        gate = Gate(self.repo, self.registry, mode="off")
        self.assertIsNone(gate.evaluate(self.call(content="x"), [human()]))

    def test_mode_manual_parks_a_low_tool(self):
        class Cheap(RecordingTool):
            name = "cheap_thing"
            risk = LOW

        self.registry.register(Cheap())
        gate = Gate(self.repo, self.registry, mode="manual")
        self.assertIn(
            "CONFIRMATION REQUIRED",
            gate.evaluate(self.call("cheap_thing"), [human()]),
        )

    def test_gating_is_audited(self):
        self.gate.evaluate(self.call(content="x"), [human()])
        events = [e["event"] for e in self.repo.recent_audit()]
        self.assertIn(storage.EVENT_GATED, events)


class ConfirmTests(GateTestCase):
    def _park_and_reply(self):
        """Park an action, then land a genuine human reply after it."""
        history = [human("write me a thing"), assistant()]
        self.gate.evaluate(self.call(content="hello"), history)
        action_id = self.repo.list_pending()[0]["id"]
        history.extend([tool_results("tu_1"), assistant("may I?"), human("yes")])
        return action_id, history

    def test_confirming_after_a_real_yes_executes_the_frozen_arguments(self):
        action_id, history = self._park_and_reply()
        result = self.run_async(self.gate.confirm(action_id, history))
        self.assertEqual(self.write.calls, [{"content": "hello"}])
        self.assertIn("hello", result)

    def test_executing_marks_the_action_executed(self):
        action_id, history = self._park_and_reply()
        self.run_async(self.gate.confirm(action_id, history))
        self.assertEqual(
            self.repo.get(action_id)["status"], storage.EXECUTED
        )

    def test_an_action_cannot_be_confirmed_twice(self):
        action_id, history = self._park_and_reply()
        self.run_async(self.gate.confirm(action_id, history))
        second = self.run_async(self.gate.confirm(action_id, history))
        self.assertIn("not pending", second)
        self.assertEqual(len(self.write.calls), 1)

    def test_confirming_an_unknown_id_runs_nothing(self):
        result = self.run_async(self.gate.confirm(999, [human()]))
        self.assertIn("No pending action", result)
        self.assertEqual(self.write.calls, [])

    def test_confirming_an_expired_action_runs_nothing(self):
        gate = Gate(self.repo, self.registry, ttl_seconds=-1)
        history = [human(), assistant()]
        gate.evaluate(self.call(content="x"), history)
        action_id = self.repo.list_pending()[0]["id"]
        history.append(human("yes"))
        result = self.run_async(gate.confirm(action_id, history))
        self.assertIn("expired", result)
        self.assertEqual(self.write.calls, [])

    def test_a_failing_approved_action_is_reported_not_raised(self):
        self.registry.register(ExplodingTool())
        history = [human(), assistant()]
        self.gate.evaluate(self.call("explode_thing"), history)
        action_id = self.repo.list_pending()[0]["id"]
        history.append(human("yes"))
        result = self.run_async(self.gate.confirm(action_id, history))
        self.assertIn("failed", result)
        # EXECUTED, not left APPROVED: it was attempted, so it isn't re-runnable.
        self.assertEqual(self.repo.get(action_id)["status"], storage.EXECUTED)


class SelfApprovalTests(GateTestCase):
    """
    The model must not be able to approve its own action inside one turn.

    This is structural, not a prompt instruction: approval requires a genuine
    human turn — content is a *string* — after the index recorded when the
    action was parked.
    """

    def test_confirming_in_the_same_turn_is_refused(self):
        # Exactly the state at gate time: Sean's request, then the model's
        # tool_use turn. Nothing from Sean since.
        history = [human("write me a thing"), assistant()]
        self.gate.evaluate(self.call(content="x"), history)
        action_id = self.repo.list_pending()[0]["id"]

        result = self.run_async(self.gate.confirm(action_id, history))
        self.assertIn("REFUSED", result)
        self.assertEqual(self.write.calls, [], "the model approved itself")
        self.assertEqual(self.repo.get(action_id)["status"], storage.PENDING)

    def test_a_tool_result_turn_does_not_count_as_sean_speaking(self):
        # The trap: core.py appends tool results as role="user". If the check
        # were a naive role scan, the model's own output would unlock the gate.
        history = [human("write me a thing"), assistant()]
        self.gate.evaluate(self.call(content="x"), history)
        action_id = self.repo.list_pending()[0]["id"]
        history.append(tool_results("tu_1"))

        result = self.run_async(self.gate.confirm(action_id, history))
        self.assertIn("REFUSED", result)
        self.assertEqual(self.write.calls, [])

    def test_a_same_round_tool_call_plus_confirm_pair_is_refused(self):
        # The model emits write_thing and confirm_action in one assistant turn,
        # betting that the id will exist by the time confirm runs. It does —
        # and it's still refused.
        history = [human("write me a thing"), assistant()]
        self.gate.evaluate(self.call(content="x"), history)
        action_id = self.repo.list_pending()[0]["id"]

        confirm_tool = ConfirmActionTool(self.gate, lambda: history)
        result = self.run_async(confirm_tool.run(action_id=action_id))
        self.assertIn("REFUSED", result)
        self.assertEqual(self.write.calls, [])

    def test_refusal_is_audited(self):
        history = [human(), assistant()]
        self.gate.evaluate(self.call(content="x"), history)
        action_id = self.repo.list_pending()[0]["id"]
        self.run_async(self.gate.confirm(action_id, history))
        events = [e["event"] for e in self.repo.recent_audit()]
        self.assertIn(storage.EVENT_SELF_APPROVAL_REFUSED, events)

    def test_the_action_survives_a_refusal_and_can_still_be_approved(self):
        # A refusal is not a denial: Sean can still say yes on his next turn.
        history = [human(), assistant()]
        self.gate.evaluate(self.call(content="x"), history)
        action_id = self.repo.list_pending()[0]["id"]
        self.run_async(self.gate.confirm(action_id, history))

        history.append(human("yes, go ahead"))
        result = self.run_async(self.gate.confirm(action_id, history))
        self.assertEqual(self.write.calls, [{"content": "x"}])
        self.assertIn("x", result)


class DenialOverrideTests(GateTestCase):
    """
    A genuine human turn arriving is necessary but not sufficient: the model
    calling confirm_action over an explicit "no" must not execute the frozen
    action either.
    """

    def test_confirming_after_an_explicit_no_is_refused(self):
        history = [human("write me a thing"), assistant()]
        self.gate.evaluate(self.call(content="x"), history)
        action_id = self.repo.list_pending()[0]["id"]
        history.append(human("no, don't do that"))

        result = self.run_async(self.gate.confirm(action_id, history))
        self.assertIn("REFUSED", result)
        self.assertEqual(self.write.calls, [], "the model ran a denied action")
        self.assertEqual(self.repo.get(action_id)["status"], storage.PENDING)

    def test_confirming_after_stop_or_cancel_is_refused(self):
        for reply in ("stop", "cancel that", "wait, no", "never mind"):
            with self.subTest(reply=reply):
                history = [human("write me a thing"), assistant()]
                self.gate.evaluate(self.call(content="x"), history)
                action_id = self.repo.list_pending()[0]["id"]
                history.append(human(reply))

                result = self.run_async(self.gate.confirm(action_id, history))
                self.assertIn("REFUSED", result)
                self.assertEqual(self.write.calls, [])

    def test_denial_override_is_audited(self):
        history = [human("write me a thing"), assistant()]
        self.gate.evaluate(self.call(content="x"), history)
        action_id = self.repo.list_pending()[0]["id"]
        history.append(human("no"))
        self.run_async(self.gate.confirm(action_id, history))

        events = [e["event"] for e in self.repo.recent_audit()]
        self.assertIn(storage.EVENT_DENIAL_OVERRIDE_REFUSED, events)

    def test_the_action_survives_a_denial_override_and_can_still_be_approved(self):
        history = [human("write me a thing"), assistant()]
        self.gate.evaluate(self.call(content="x"), history)
        action_id = self.repo.list_pending()[0]["id"]
        history.append(human("no"))
        self.run_async(self.gate.confirm(action_id, history))

        history.append(human("actually yes, go ahead"))
        result = self.run_async(self.gate.confirm(action_id, history))
        self.assertEqual(self.write.calls, [{"content": "x"}])
        self.assertIn("x", result)

    def test_normal_affirmative_replies_are_not_flagged_as_denials(self):
        for reply in ("yes", "yes, go ahead", "do it", "sure", "sounds good", "ok"):
            with self.subTest(reply=reply):
                self.write.calls.clear()
                history = [human("write me a thing"), assistant()]
                self.gate.evaluate(self.call(content="x"), history)
                action_id = self.repo.list_pending()[0]["id"]
                history.append(human(reply))

                result = self.run_async(self.gate.confirm(action_id, history))
                self.assertNotIn("REFUSED", result)
                self.assertEqual(self.write.calls, [{"content": "x"}])


class AffirmativeConsentTests(GateTestCase):
    """
    Absence of a "no" is not a "yes".

    Screening only for denial keywords means every turn that misses that small
    net counts as approval — "not yet", "why is that necessary?", "I haven't
    decided" would each execute a consequential action Sean never agreed to.
    The gate's promise is an *explicit* yes, so it requires one.

    The asymmetry is deliberate: an unrecognized yes costs one more round trip,
    an unrecognized no costs an action he didn't authorize.
    """

    def _park(self, history):
        self.gate.evaluate(self.call(content="x"), history)
        return self.repo.list_pending()[0]["id"]

    def test_ambiguous_replies_are_not_treated_as_approval(self):
        for reply in (
            "not yet",
            "why is that necessary?",
            "I haven't decided",
            "hmm",
            "what would that even do",
            "hold off for a sec",
        ):
            with self.subTest(reply=reply):
                self.write.calls.clear()
                history = [human("write me a thing"), assistant()]
                action_id = self._park(history)
                history.append(human(reply))

                result = self.run_async(self.gate.confirm(action_id, history))
                self.assertIn("REFUSED", result)
                self.assertEqual(
                    self.write.calls, [], f"{reply!r} was read as consent"
                )
                self.assertEqual(self.repo.get(action_id)["status"], storage.PENDING)

    def test_unclear_consent_is_audited_separately_from_a_denial(self):
        # Distinct events because they mean different things: a denial is Sean
        # saying no, an unclear reply may just mean the keyword net is too
        # narrow for how he talks. Only one of those is a tuning signal.
        history = [human("write me a thing"), assistant()]
        action_id = self._park(history)
        history.append(human("why is that necessary?"))
        self.run_async(self.gate.confirm(action_id, history))

        events = [e["event"] for e in self.repo.recent_audit()]
        self.assertIn(storage.EVENT_UNCLEAR_CONSENT_REFUSED, events)
        self.assertNotIn(storage.EVENT_DENIAL_OVERRIDE_REFUSED, events)

    def test_an_unclear_reply_leaves_the_action_approvable(self):
        history = [human("write me a thing"), assistant()]
        action_id = self._park(history)
        history.append(human("not yet"))
        self.run_async(self.gate.confirm(action_id, history))
        self.assertEqual(self.write.calls, [])

        history.append(human("ok yes, go ahead"))
        result = self.run_async(self.gate.confirm(action_id, history))
        self.assertEqual(self.write.calls, [{"content": "x"}])
        self.assertIn("x", result)

    def test_denial_wins_when_a_reply_contains_both(self):
        # "I don't think yes is the right call" carries an affirmation keyword
        # and is plainly not consent, so the denial check has to run first.
        history = [human("write me a thing"), assistant()]
        action_id = self._park(history)
        history.append(human("I don't think yes is the right call here"))

        result = self.run_async(self.gate.confirm(action_id, history))
        self.assertIn("REFUSED", result)
        self.assertEqual(self.write.calls, [])
        events = [e["event"] for e in self.repo.recent_audit()]
        self.assertIn(storage.EVENT_DENIAL_OVERRIDE_REFUSED, events)

    def test_the_ordinary_ways_of_saying_yes_still_work(self):
        for reply in (
            "yes",
            "yep",
            "go ahead",
            "go for it",
            "do it",
            "sure thing",
            "okay",
            "sounds good",
            "approved",
            "ship it",
            "confirmed",
            "fine",
        ):
            with self.subTest(reply=reply):
                self.write.calls.clear()
                history = [human("write me a thing"), assistant()]
                action_id = self._park(history)
                history.append(human(reply))

                result = self.run_async(self.gate.confirm(action_id, history))
                self.assertNotIn("REFUSED", result)
                self.assertEqual(self.write.calls, [{"content": "x"}])


class LastHumanTurnIndexTests(unittest.TestCase):
    def test_empty_history_has_no_human_turn(self):
        self.assertEqual(last_human_turn_index([]), -1)

    def test_finds_the_most_recent_string_content_user_turn(self):
        history = [human("first"), assistant(), human("second")]
        self.assertEqual(last_human_turn_index(history), 2)

    def test_skips_tool_result_turns(self):
        history = [human("first"), assistant(), tool_results("a")]
        self.assertEqual(last_human_turn_index(history), 0)

    def test_history_of_only_tool_results_has_no_human_turn(self):
        self.assertEqual(last_human_turn_index([tool_results("a")]), -1)


class PauseTests(GateTestCase):
    def test_a_paused_trillion_still_runs_read_only_tools(self):
        # A kill switch that mutes the conversation is one Sean won't flip.
        self.gate.pause()
        self.assertIsNone(self.gate.evaluate(self.call("look_thing"), []))

    def test_a_paused_trillion_refuses_a_consequential_tool(self):
        self.gate.pause()
        verdict = self.gate.evaluate(self.call(content="x"), [human()])
        self.assertIn("PAUSED", verdict)
        self.assertEqual(self.write.calls, [])

    def test_pausing_does_not_park_an_action_for_later(self):
        # A blocked call is refused outright, not queued up to fire on resume.
        self.gate.pause()
        self.gate.evaluate(self.call(content="x"), [human()])
        self.assertEqual(self.repo.list_pending(), [])

    def test_pausing_after_the_ask_blocks_the_approval(self):
        history = [human(), assistant()]
        self.gate.evaluate(self.call(content="x"), history)
        action_id = self.repo.list_pending()[0]["id"]
        history.append(human("yes"))
        self.gate.pause()

        result = self.run_async(self.gate.confirm(action_id, history))
        self.assertIn("PAUSED", result)
        self.assertEqual(self.write.calls, [])
        # Still pending, so it works after /resume rather than being lost.
        self.assertEqual(self.repo.get(action_id)["status"], storage.PENDING)

    def test_resume_restores_execution(self):
        history = [human(), assistant()]
        self.gate.evaluate(self.call(content="x"), history)
        action_id = self.repo.list_pending()[0]["id"]
        history.append(human("yes"))
        self.gate.pause()
        self.run_async(self.gate.confirm(action_id, history))
        self.gate.resume()

        self.run_async(self.gate.confirm(action_id, history))
        self.assertEqual(self.write.calls, [{"content": "x"}])

    def test_pause_and_resume_are_audited(self):
        self.gate.pause()
        self.gate.resume()
        events = [e["event"] for e in self.repo.recent_audit()]
        self.assertIn(storage.EVENT_PAUSED, events)
        self.assertIn(storage.EVENT_RESUMED, events)

    def test_pausing_twice_logs_once(self):
        self.gate.pause()
        self.gate.pause()
        events = [e["event"] for e in self.repo.recent_audit()]
        self.assertEqual(events.count(storage.EVENT_PAUSED), 1)

    def test_blocking_while_paused_is_audited(self):
        self.gate.pause()
        self.gate.evaluate(self.call(content="x"), [human()])
        events = [e["event"] for e in self.repo.recent_audit()]
        self.assertIn(storage.EVENT_BLOCKED_PAUSED, events)


class DenyTests(GateTestCase):
    def test_denying_a_pending_action_stops_it(self):
        self.gate.evaluate(self.call(content="x"), [human()])
        action_id = self.repo.list_pending()[0]["id"]
        message = self.gate.deny(action_id, reason="wrong path")
        self.assertIn(f"#{action_id}", message)
        self.assertEqual(self.repo.get(action_id)["status"], storage.DENIED)

    def test_a_denied_action_cannot_then_be_confirmed(self):
        self.gate.evaluate(self.call(content="x"), [human()])
        action_id = self.repo.list_pending()[0]["id"]
        self.gate.deny(action_id)
        result = self.run_async(
            self.gate.confirm(action_id, [human(), assistant(), human("yes")])
        )
        self.assertIn("denied", result)
        self.assertEqual(self.write.calls, [])

    def test_denying_an_unknown_action_says_so(self):
        self.assertIn("No pending action", self.gate.deny(999))


class ConfirmActionToolTests(GateTestCase):
    def test_the_tool_is_never_itself_gated(self):
        tool = ConfirmActionTool(self.gate, lambda: [])
        self.registry.register(tool)
        self.assertIsNone(
            self.gate.evaluate(self.call("confirm_action"), [human()])
        )

    def test_the_tool_is_withheld_from_spawned_agents(self):
        self.assertFalse(ConfirmActionTool.factory_allowed)

    def test_a_non_numeric_action_id_is_reported_not_raised(self):
        tool = ConfirmActionTool(self.gate, lambda: [])
        self.assertIn("numeric", self.run_async(tool.run(action_id="the first one")))

    def test_a_missing_action_id_is_reported_not_raised(self):
        tool = ConfirmActionTool(self.gate, lambda: [])
        self.assertIn("numeric", self.run_async(tool.run()))


class SummarizeTests(unittest.TestCase):
    def test_short_values_are_shown_in_full(self):
        self.assertEqual(
            _summarize("t", {"path": "a.md"}), "t(path='a.md')"
        )

    def test_long_values_collapse_to_a_length(self):
        summary = _summarize("t", {"content": "x" * 500})
        self.assertIn("content=<500 chars>", summary)
        self.assertNotIn("xxxx", summary)

    def test_no_arguments_renders_cleanly(self):
        self.assertEqual(_summarize("t", None), "t()")

    def test_non_string_values_are_shown(self):
        self.assertIn("count=3", _summarize("t", {"count": 3}))


if __name__ == "__main__":
    unittest.main()
