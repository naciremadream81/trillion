"""
The confirmation gate — where AGENT.md's promise becomes a mechanism.

The flow, end to end:

  1. The model calls a tool. Agent._run_tool asks the Gate about it first.
  2. If the call is consequential, the Gate does NOT run it. It writes a
     pending_actions row with the arguments frozen, and returns a string that
     goes back to the model as the tool_result — telling it to ask Sean.
  3. The model tells Sean what it wants to do. Sean says yes (or doesn't).
  4. The model calls confirm_action(action_id=N). The Gate checks that a real
     human turn arrived after the action was parked, then executes the frozen
     arguments through the registry.

Why deferred-in-band rather than a blocking callback: a spoken "yes" arrives
over /api/transcribe → /api/chat, which is the very request a blocking
callback would be parked inside. Deferring keeps CLI, web, and voice on one
code path and needs no transport change — the adapter invariant holds.

Why frozen arguments: the model asks Sean about writing 4 lines to a scratch
file and then approves writing 4,000 to ~/.ssh/authorized_keys. Approving
action #47 runs #47's arguments, whatever the model would prefer by now.
"""

from __future__ import annotations

import re

from ..providers.base import ToolCall
from . import storage
from .risk import (
    blocked_while_paused,
    needs_confirmation,
    normalize_mode,
    normalize_risk,
)

# How much of a single argument value to show Sean before truncating. Long
# enough to recognize a path or a subject line, short enough that a 40KB file
# body doesn't scroll the actual question off his screen.
_MAX_VALUE_CHARS = 120

# How much of a tool's return value gets copied into the audit log. The audit
# log is a record of decisions, not a second copy of every file Trillion reads.
_MAX_RESULT_PREVIEW = 500


def last_human_turn_index(history: list[dict]) -> int:
    """
    Index of the most recent genuine message from Sean, or -1 if there is none.

    The subtlety this exists for: tool results are appended to history as
    ``{"role": "user", "content": [ ... tool_result blocks ... ]}`` (see
    agent/core.py). A naive `role == "user"` scan is therefore satisfied by
    the agent's *own* tool output, which would hand the model a way to
    manufacture its own approval. A genuine human turn carries a plain string
    (agent/core.py appends `content=user_input`); a tool-result turn carries a
    list. That difference is the whole defense.
    """
    for i in range(len(history) - 1, -1, -1):
        msg = history[i]
        if msg.get("role") == "user" and isinstance(msg.get("content"), str):
            return i
    return -1


# Word-boundary match, case-insensitive: "no", "don't"/"do not", "stop",
# "cancel", "wait", "never mind"/"nevermind". Deliberately a keyword net, not
# NLP — it's a safety net for the common ways of saying no, not a parser for
# every possible phrasing.
_DENIAL_PATTERN = re.compile(
    r"\b(no|nope|don'?t|do not|stop|cancel|wait|never ?mind)\b", re.IGNORECASE
)

# The ways of saying yes. This exists because absence-of-no is not consent:
# "not yet", "why is that necessary?", and "I haven't decided" all clear the
# denial net above while being plainly not a yes, and this gate's whole
# promise is an *explicit* yes.
#
# The asymmetry is the point. Requiring an affirmative makes the failure mode
# a refusal — the gate asks again, Sean says "yes", done — whereas inferring
# consent from silence makes the failure mode a consequential action he never
# agreed to. So this net is allowed to be too narrow, and must not be too wide.
_AFFIRMATION_PATTERN = re.compile(
    r"\b(yes|yeah|yep|yup|yah|sure|ok|okay|k|fine|go ahead|go for it|"
    r"do it|send it|ship it|please do|sounds good|approved?|confirm(ed|ing)?|"
    r"affirmative|permission granted|green ?light)\b",
    re.IGNORECASE,
)


def _reads_as_denial(text: str) -> bool:
    """True if `text` reads like Sean saying no rather than yes."""
    return bool(_DENIAL_PATTERN.search(text))


def _reads_as_approval(text: str) -> bool:
    """
    True only if `text` contains a recognizable yes.

    Denial wins ties: "I don't think yes is right here" contains an
    affirmation keyword and is obviously not consent, so callers check
    _reads_as_denial first and this never has to adjudicate it.
    """
    return bool(_AFFIRMATION_PATTERN.search(text))


def _summarize(tool_name: str, arguments: dict | None) -> str:
    """
    A one-line rendering of the call, for Sean's eyes.

    Long values collapse to a length so the shape of the action stays legible:
    ``write_project_file(relative_path='notes.md', content=<412 chars>)``.
    """
    parts: list[str] = []
    for key, value in (arguments or {}).items():
        text = value if isinstance(value, str) else repr(value)
        if len(text) > _MAX_VALUE_CHARS:
            parts.append(f"{key}=<{len(text)} chars>")
        elif isinstance(value, str):
            parts.append(f"{key}={value!r}")
        else:
            parts.append(f"{key}={text}")
    return f"{tool_name}({', '.join(parts)})"


class Gate:
    """
    Decides whether a tool call runs now, waits for Sean, or is refused.

    Constructed with the registry rather than a callable because the registry
    already exists before the Agent does in both adapters, and because
    executing approved actions *through* the registry means they pass every
    check the registry applies (Tier 6's sanitization lands there next) rather
    than bypassing them on the strength of having been approved once.
    """

    def __init__(
        self,
        repo: storage.SafetyRepo,
        registry,  # ToolRegistry
        *,
        mode: str = "smart",
        ttl_seconds: int = 900,
        paused: bool = False,
    ) -> None:
        self.repo = repo
        self.registry = registry
        self.mode = normalize_mode(mode)
        self.ttl_seconds = ttl_seconds
        self._paused = paused

    # ── Kill switch ──────────────────────────────────────────────────────────

    @property
    def is_paused(self) -> bool:
        return self._paused

    def pause(self) -> None:
        """Stop Trillion from acting. It keeps talking and keeps reading."""
        if not self._paused:
            self._paused = True
            self.repo.log(storage.EVENT_PAUSED)

    def resume(self) -> None:
        if self._paused:
            self._paused = False
            self.repo.log(storage.EVENT_RESUMED)

    # ── The gate itself ──────────────────────────────────────────────────────

    def evaluate(
        self,
        tool_call: ToolCall,
        history: list[dict],
    ) -> str | None:
        """
        Screen a tool call before it runs.

        Returns None to mean "let it run". Returns a string to mean "don't run
        it — hand this to the model as the tool_result instead". A returned
        string is always either a refusal or a request for confirmation; the
        model is never told the action succeeded.
        """
        tool = self.registry.get(tool_call.name)
        risk = normalize_risk(getattr(tool, "risk", None))
        declared = getattr(tool, "requires_confirmation", None)

        if self._paused and blocked_while_paused(tool_call.name, risk):
            self.repo.log(
                storage.EVENT_BLOCKED_PAUSED, tool_name=tool_call.name
            )
            return (
                f"[PAUSED] Trillion is paused, so `{tool_call.name}` did not run. "
                "Read-only tools and conversation still work. Tell Sean it's "
                "paused and that /resume lifts it — don't try to work around it."
            )

        if not needs_confirmation(
            tool_name=tool_call.name,
            risk=risk,
            declared=declared,
            mode=self.mode,
        ):
            return None

        summary = _summarize(tool_call.name, tool_call.arguments)
        action_id = self.repo.create_pending(
            tool_name=tool_call.name,
            arguments=tool_call.arguments,
            summary=summary,
            risk=risk,
            history_index=len(history),
            ttl_seconds=self.ttl_seconds,
        )
        minutes = max(1, self.ttl_seconds // 60)
        return (
            f"[CONFIRMATION REQUIRED — action #{action_id}]\n"
            f"You asked to run: {summary}\n"
            "That's a consequential action, so it needs Sean's explicit yes "
            "first. Tell him plainly what you're about to do and ask him to "
            "confirm. Do not retry this tool. When he agrees — in his own next "
            f"message, not your own reasoning — call confirm_action(action_id={action_id}).\n"
            "The arguments above are frozen; approving runs exactly them.\n"
            f"This request expires in {minutes} minutes."
        )

    async def confirm(self, action_id: int, history: list[dict]) -> str:
        """
        Approve and immediately execute a parked action.

        Every refusal path here returns a string for the model rather than
        raising: a failed confirmation is information the model should relay
        to Sean, not a crash.
        """
        self.repo.expire_stale()
        action = self.repo.get(action_id)
        if action is None:
            return f"[No pending action #{action_id}. Nothing was run.]"

        status = action["status"]
        if status != storage.PENDING:
            return (
                f"[Action #{action_id} is '{status}', not pending. Nothing was "
                "run. If it's still needed, start it over as a new action.]"
            )

        # The self-approval defense. A genuine human turn — content is a
        # string, not a list of tool_result blocks — must have arrived after
        # the action was parked.
        human_index = last_human_turn_index(history)
        if human_index < action["history_index"]:
            self.repo.log(
                storage.EVENT_SELF_APPROVAL_REFUSED,
                tool_name=action["tool_name"],
                action_id=action_id,
            )
            return (
                f"[REFUSED — action #{action_id} cannot be self-approved.]\n"
                "Sean has not said anything since this action was parked. Ask "
                "him, out loud, in your reply, and wait for his answer. His "
                "next message is what unlocks this."
            )

        # A genuine human turn arriving is necessary but not sufficient: "no,
        # don't do that" is a real turn too, and a model that calls
        # confirm_action over it anyway (mistakenly or otherwise) must not
        # get the frozen action executed. This is a keyword net, not NLP —
        # it catches the common ways of saying no, not every phrasing.
        human_turn = history[human_index]["content"]
        if _reads_as_denial(human_turn):
            self.repo.log(
                storage.EVENT_DENIAL_OVERRIDE_REFUSED,
                tool_name=action["tool_name"],
                action_id=action_id,
            )
            return (
                f"[REFUSED — action #{action_id} was not approved.]\n"
                "Sean's last message reads like a refusal, not a yes. Do not "
                "call confirm_action again for this id unless he actually "
                "agrees."
            )

        # Not-a-no is not a yes. Without this, every turn that misses the
        # denial keywords counts as consent — "not yet", "why is that
        # necessary?", "I haven't decided" would all execute the action. This
        # gate promises an explicit yes, so it requires one.
        if not _reads_as_approval(human_turn):
            self.repo.log(
                storage.EVENT_UNCLEAR_CONSENT_REFUSED,
                tool_name=action["tool_name"],
                action_id=action_id,
            )
            return (
                f"[REFUSED — action #{action_id} has no clear approval.]\n"
                "Sean's last message is neither a yes nor a no. Don't read it "
                "as permission. Ask him again, plainly, and wait for an actual "
                "yes before calling confirm_action for this id."
            )

        # Pausing after the ask but before the approval still blocks: this is
        # the only place that knows a confirmation goes on to *execute*.
        if self._paused:
            self.repo.log(
                storage.EVENT_BLOCKED_PAUSED,
                tool_name=action["tool_name"],
                action_id=action_id,
            )
            return (
                f"[PAUSED] Trillion is paused, so approved action #{action_id} "
                "did not run. It's still pending; /resume then confirm again."
            )

        self.repo.approve(action_id)

        # Execute the frozen arguments through the registry — the same path
        # any other tool call takes.
        try:
            result = await self.registry.run(
                ToolCall(
                    id=f"confirmed-{action_id}",
                    name=action["tool_name"],
                    arguments=action["arguments"],
                )
            )
        except Exception as e:  # noqa: BLE001
            # The action was genuinely approved and genuinely attempted, so it
            # is EXECUTED even though it failed — leaving it APPROVED would
            # make it look re-runnable, and it isn't.
            self.repo.mark_executed(action_id, result_preview=f"failed: {e}")
            return f"[Approved action #{action_id} failed: {e}]"

        self.repo.mark_executed(
            action_id, result_preview=str(result)[:_MAX_RESULT_PREVIEW]
        )
        return result

    def deny(self, action_id: int, reason: str = "") -> str:
        """Sean said no. Human-facing text, used by the CLI."""
        action = self.repo.get(action_id)
        if action is None:
            return f"No pending action #{action_id}."
        if action["status"] != storage.PENDING:
            return f"Action #{action_id} is already '{action['status']}'."
        self.repo.deny(action_id, reason=reason)
        return f"Denied #{action_id} ({action['summary']})."
