"""
The handoff system — orchestration.md Tier 5, "propose, don't chain".

The tempting multi-agent pattern is also the dangerous one: agent A finishes
and auto-calls agent B, which auto-calls agent C. Errors compound invisibly
and you get a confident, wrong answer three hops deep with no human
checkpoint. Tier 5's rule is that **the human is the circuit-breaker** —
agents propose the graph of work, Sean approves each edge.

The whole design decision here is what this module *doesn't* build. A
handoff is "dispatch agent X with task T", which is exactly the shape of a
consequential action, and Trillion already has a mechanism for parking one
of those until Sean says yes: agent/safety/. So a proposal is written as a
pending_actions row whose tool_name is the target's `dispatch_to_<slug>` and
whose arguments are frozen at proposal time. That buys, for free and with no
second code path to keep honest:

  - `/pending-actions` lists it,
  - `confirm_action` executes it — with the arguments Sean was shown, not
    whatever the model would prefer by the time he answers,
  - `/deny` rejects it,
  - the TTL expires it if he never answers,
  - the audit log records all of it,
  - and the self-approval defense applies unchanged: approving requires a
    genuine human turn in the *main* conversation after the proposal was
    parked, which no agent can manufacture for itself.

The specialist proposing cannot approve its own proposal, by construction:
ConfirmActionTool is factory_allowed = False, so it is never in a spawned
agent's registry to begin with.

Note which agent's history the index comes from. The proposal is made by a
specialist, mid-dispatch, nested inside a turn of Sean's main conversation.
The turn that counts as consent is Sean's next message in *that* conversation
— hence history_provider, which reads the main Agent's history rather than
the specialist's scratch history.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Callable

from ..safety.risk import READ_ONLY
from ..tools.base import BaseTool

# An artifact is a *reference* — a path, an id, a URL. Never an inline blob.
# The playbook's reasoning ("pass references, not payloads") is about keeping
# handoffs small and serializable, but there's a sharper reason to enforce it
# here: an inline payload is untrusted content riding into the next agent's
# prompt through a channel that looks like metadata. A path has to be opened
# by a tool, and that tool's result goes through the untrusted-content scrub
# in agent/tools/registry.py. A blob smuggled in as an "artifact" would not.
ARTIFACT_MAX_CHARS = 512
MAX_ARTIFACTS = 20
MAX_PRECONDITIONS = 10
MAX_TASK_CHARS = 4000
MAX_REASON_CHARS = 500


@dataclass
class Handoff:
    """One agent's recommendation for what should happen next."""

    target_agent: str
    reason: str
    task: str
    artifacts: dict = field(default_factory=dict)
    preconditions: list = field(default_factory=list)
    confidence: float = 0.5

    def to_dict(self) -> dict:
        return {
            "target_agent": self.target_agent,
            "reason": self.reason,
            "task": self.task,
            "artifacts": dict(self.artifacts),
            "preconditions": list(self.preconditions),
            "confidence": self.confidence,
        }


def _looks_like_a_payload(value: str) -> bool:
    """
    Whether an artifact value is prose/content rather than a reference.

    Two signals, both cheap and both hard to trip accidentally: a newline
    (no path, id, or URL contains one) and sheer length. A caller with a
    genuine long URL is under the limit; a caller pasting a file's contents
    is not.
    """
    return "\n" in value or "\r" in value or len(value) > ARTIFACT_MAX_CHARS


def validate(handoff: Handoff, known_agents: set) -> list:
    """
    Return a list of human-readable problems, empty if the handoff is sound.

    Returns errors rather than raising for the same reason tools return
    errors as data (orchestration.md Tier 3): the model should read what was
    wrong and fix it, not have the dispatch die on an exception.
    """
    errors = []

    target = (handoff.target_agent or "").strip()
    if not target:
        errors.append("target_agent is required.")
    elif target not in known_agents:
        known = ", ".join(sorted(known_agents)) or "(none active)"
        errors.append(
            f"target_agent {target!r} is not an active specialist. Active agents: {known}."
        )

    if not (handoff.task or "").strip():
        errors.append("task is required — say what the next agent should actually do.")
    elif len(handoff.task) > MAX_TASK_CHARS:
        errors.append(f"task is longer than {MAX_TASK_CHARS} characters.")

    if not (handoff.reason or "").strip():
        errors.append("reason is required — one sentence on why this handoff is worth making.")
    elif len(handoff.reason) > MAX_REASON_CHARS:
        errors.append(f"reason is longer than {MAX_REASON_CHARS} characters.")

    if not isinstance(handoff.artifacts, dict):
        errors.append("artifacts must be an object mapping a name to a path, id, or URL.")
    else:
        if len(handoff.artifacts) > MAX_ARTIFACTS:
            errors.append(f"more than {MAX_ARTIFACTS} artifacts.")
        for name, value in handoff.artifacts.items():
            if not isinstance(value, str):
                errors.append(f"artifact {name!r} must be a string reference.")
            elif _looks_like_a_payload(value):
                errors.append(
                    f"artifact {name!r} looks like inline content, not a reference. "
                    "Pass a path, id, or URL the next agent can read for itself."
                )

    if not isinstance(handoff.preconditions, list):
        errors.append("preconditions must be a list of short strings.")
    elif len(handoff.preconditions) > MAX_PRECONDITIONS:
        errors.append(f"more than {MAX_PRECONDITIONS} preconditions.")

    try:
        confidence = float(handoff.confidence)
    except (TypeError, ValueError):
        errors.append("confidence must be a number between 0 and 1.")
    else:
        if not 0.0 <= confidence <= 1.0:
            errors.append("confidence must be between 0 and 1.")

    return errors


def phrase_confidence(confidence: float) -> str:
    """
    How strongly to voice the offer. The playbook is explicit that confidence
    is for *phrasing*, not for deciding — a low-confidence handoff still gets
    offered, it just gets offered more tentatively. Nothing here ever routes
    around Sean.
    """
    if confidence >= 0.8:
        return "This looks worth doing"
    if confidence >= 0.5:
        return "You might want to"
    return "Low confidence, but flagging it"


def format_offer(handoff: Handoff, action_id: int, proposer: str) -> str:
    """The text the main agent reads back to Sean as a conversational offer."""
    lines = [
        f"[HANDOFF PROPOSED — id {action_id}]",
        f"{proposer} suggests handing off to '{handoff.target_agent}'.",
        f"{phrase_confidence(float(handoff.confidence))}: {handoff.reason}",
        f"Task: {handoff.task}",
    ]
    if handoff.artifacts:
        refs = ", ".join(f"{k}={v}" for k, v in handoff.artifacts.items())
        lines.append(f"It should read: {refs}")
    if handoff.preconditions:
        checks = "; ".join(str(p) for p in handoff.preconditions)
        lines.append(f"Check first: {checks}")
    lines.append(
        "Nothing has been dispatched. Tell Sean what this would do and wait for "
        f"his answer; if he agrees, call confirm_action(action_id={action_id})."
    )
    return "\n".join(lines)


class ProposeHandoffTool(BaseTool):
    """
    The tool a spawned specialist calls to recommend the next step.

    It does not dispatch. It parks a proposal and returns the offer text,
    which travels back to the main agent as this specialist's tool result.
    """

    name = "propose_handoff"
    description = (
        "Recommend that another specialist agent take the next step on this work. "
        "This does NOT run anything — it puts the recommendation in front of Sean, "
        "who decides. Use it when you have finished your part and a different "
        "specialist is genuinely better placed to continue. Pass artifacts as "
        "paths, ids, or URLs the next agent can read for itself, never as pasted "
        "content. Say plainly in `reason` why the handoff is worth making, and set "
        "`confidence` honestly — a low number is useful information, not a failure."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "target_agent": {
                "type": "string",
                "description": "Slug of the specialist that should take the next step.",
            },
            "reason": {
                "type": "string",
                "description": "One sentence on why this handoff is worth making.",
            },
            "task": {
                "type": "string",
                "description": "The task to hand the next agent, in plain language.",
            },
            "artifacts": {
                "type": "object",
                "description": "Name -> path/id/URL the next agent should read. References only.",
                "additionalProperties": {"type": "string"},
            },
            "preconditions": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Things Sean should verify before accepting.",
            },
            "confidence": {
                "type": "number",
                "description": "0 to 1 — how strongly you vouch for this handoff.",
            },
        },
        "required": ["target_agent", "reason", "task"],
    }

    # A specialist must be able to reach this — proposing is its whole point.
    factory_allowed = True

    # READ_ONLY, and deliberately so: proposing executes nothing. The gate
    # belongs on the *dispatch* this parks, not on the act of asking. Gating
    # the proposal itself would deadlock — a spawned specialist can't reach
    # confirm_action (ConfirmActionTool.factory_allowed is False), so its own
    # gated call could never be approved by anyone.
    risk = READ_ONLY
    requires_confirmation = False

    # The offer text is assembled here from validated fields, but `reason`,
    # `task`, and the artifact values are model-authored strings that may
    # themselves quote untrusted material the specialist read. Leave the
    # scrub on.
    trusted_output = False

    def __init__(
        self,
        safety_repo,
        history_provider: Callable[[], list],
        active_agents_provider: Callable[[], set],
        proposer_slug: str,
        ttl_seconds: int = 3600,
        dispatch_tool_namer: Callable[[str], str] | None = None,
    ) -> None:
        self.safety_repo = safety_repo
        self.history_provider = history_provider
        self.active_agents_provider = active_agents_provider
        self.proposer_slug = proposer_slug
        self.ttl_seconds = ttl_seconds
        if dispatch_tool_namer is None:
            from .dispatch import dispatch_tool_name as _namer

            dispatch_tool_namer = _namer
        self.dispatch_tool_namer = dispatch_tool_namer

    async def run(self, **kwargs) -> str:
        handoff = Handoff(
            target_agent=str(kwargs.get("target_agent") or "").strip(),
            reason=str(kwargs.get("reason") or "").strip(),
            task=str(kwargs.get("task") or "").strip(),
            artifacts=kwargs.get("artifacts") or {},
            preconditions=kwargs.get("preconditions") or [],
            confidence=kwargs.get("confidence", 0.5),
        )

        try:
            known = set(self.active_agents_provider() or set())
        except Exception:
            known = set()
        # A specialist proposing a handoff to itself is a loop, not a plan.
        known.discard(self.proposer_slug)

        errors = validate(handoff, known)
        if errors:
            return "[handoff rejected] " + " ".join(errors)

        try:
            action_id = self.safety_repo.create_pending(
                tool_name=self.dispatch_tool_namer(handoff.target_agent),
                arguments={"message": handoff.task},
                summary=(
                    f"Hand off to '{handoff.target_agent}' "
                    f"(proposed by {self.proposer_slug}): {handoff.reason}"
                ),
                risk="consequential",
                history_index=len(self.history_provider() or []),
                ttl_seconds=self.ttl_seconds,
            )
        except Exception as e:  # noqa: BLE001 — errors cross this boundary as data
            return f"[handoff could not be recorded: {type(e).__name__}: {e}]"

        try:
            self.safety_repo.log(
                "handoff_proposed",
                tool_name=self.dispatch_tool_namer(handoff.target_agent),
                action_id=action_id,
                detail=json.loads(json.dumps(handoff.to_dict())),
            )
        except Exception:
            pass  # the audit line is a nicety; the parked action is the record

        return format_offer(handoff, action_id, self.proposer_slug)
