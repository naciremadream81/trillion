"""
The meeting and the chair — playbook/the-board.md Tiers 4 and 5.

Tier 4 is a fan-out: one model call per seat, each system prompt containing
exactly ONE dossier. Never one call playing several parts. If this module
ever grows a prompt that says "you are playing the following advisors", the
failure mode the whole design exists to prevent has been rebuilt.

Tier 5 is the chair — Trillion itself. The seats are well-read but blind; the
chair is the only participant that has read the live business figures. Its job
is to name the split before the agreement, discount seats using their own
documented blind spots, and treat abstention as abstention.

EVERY FIELD A MODEL RETURNS IS TREATED AS HOSTILE. Not because the model is
adversarial, but because a schema-declared boolean comes back as the string
`"false"` often enough, and in Python a non-empty string is truthy. A seat
recorded as abstaining when it did not means Sean is told a named person
declined to answer when they didn't. So: check identity, not truthiness;
coerce every scalar; drop anything structural where a sentence belonged.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field

from .dossier import gate_citations

logger = logging.getLogger(__name__)

# A truncated response means the meeting was paid for and produced nothing —
# a seat cut off mid-JSON wastes the whole call. The chair's limit is larger
# because it consumes every seat's output.
SEAT_MAX_TOKENS = 1500
CHAIR_MAX_TOKENS = 2500

# Per-meeting ceiling, and a per-call share so no single seat can eat it.
DEFAULT_MEETING_CEILING_USD = 0.75


class BudgetExhausted(RuntimeError):
    """Raised before any call when the ceiling cannot cover the meeting."""


# ── Hostile-input coercion ──────────────────────────────────────────────────


def as_bool(value) -> bool:
    """
    A boolean from whatever the model actually sent.

    Identity, not truthiness. `bool("false")` is True, and that single line
    is the difference between "this advisor abstained" and "this advisor did
    not abstain" being reported under a real person's name.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"true", "yes", "y", "1"}
    return False


def as_text(value, limit: int = 4000) -> str:
    """
    A sentence from whatever arrived, or "" if a structure did.

    A dict or list where prose belonged is not prose; stringifying it would
    put `{'position': ...}` on screen under an advisor's name.
    """
    if isinstance(value, str):
        return value.strip()[:limit]
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return ""


def as_confidence(value) -> float:
    """A confidence clamped to [0, 1]; anything unparseable is 0.5."""
    try:
        if isinstance(value, bool):
            return 0.5
        number = float(value)
    except (TypeError, ValueError):
        return 0.5
    if number != number:  # NaN
        return 0.5
    return max(0.0, min(1.0, number))


# ── One seat's contribution ─────────────────────────────────────────────────


@dataclass
class Opinion:
    seat_id: str
    seat_name: str
    position: str = ""
    reasoning: str = ""
    citations: list = field(default_factory=list)
    confidence: float = 0.5
    changes_mind: str = ""
    abstained: bool = False
    failed: bool = False
    failure_reason: str = ""

    @property
    def unsourced(self) -> bool:
        """Spoke, but cited nothing.

        Not an error — an advisor can hold a view their doctrine doesn't
        cover — but it IS the seat talking without support, and Sean should
        see that at a glance rather than have it read like the rest."""
        return not self.abstained and not self.failed and not self.citations

    @property
    def spoke(self) -> bool:
        return not self.abstained and not self.failed


def seat_system_prompt(seat) -> str:
    """
    One seat's system prompt: its dossier, and nothing else.

    It is not told who else is in the room, and it never sees another
    dossier. That isolation is the whole reason a fan-out costs more than
    one call — if two seats can see each other they converge, and the
    disagreement that makes a board worth convening disappears.
    """
    entries = "\n\n".join(
        f"### {e.id} — {e.title}\n"
        f"source: {e.source or 'unstated'}\n"
        f"verification: {e.verification}\n\n{e.body}"
        for e in seat.visible_doctrine()
    )
    parts = [
        f"You are {seat.name}" + (f", seated for {seat.seat}." if seat.seat else "."),
        "You are advising Sean on a decision about his business. Reason FROM "
        "the numbered doctrine below — it is your published thinking, and it "
        "is what you are here for. Cite the entries you actually used by id.",
        "Do not invent a citation. If your view rests on something not in "
        "your doctrine, say it plainly and cite nothing — an unsourced "
        "opinion honestly marked is worth far more than a fabricated source.",
        "If your doctrine genuinely does not cover this, ABSTAIN. Saying "
        "'I have no doctrine on this' is a real answer and a useful one. "
        "Inventing a plausible position is not.",
        f"## Your doctrine\n\n{entries}",
    ]
    if seat.objection:
        parts.append(
            "## What you reliably push back on\n\n" + seat.objection
            + "\n\nPush back here. Agreeing pleasantly is the one thing you "
              "are not useful for."
        )
    if seat.voice:
        parts.append("## Voice\n\n" + seat.voice + "\n\nTone only — it never substitutes for doctrine.")
    parts.append(
        "Reply with ONLY a JSON object:\n"
        '{"position": "...", "reasoning": "...", "citations": ["D1", ...], '
        '"confidence": 0.0-1.0, "changes_mind": "what would change your view", '
        '"abstain": false}'
    )
    return "\n\n".join(parts)


def _extract_json(text: str) -> dict:
    match = re.search(r"\{.*\}", text or "", re.S)
    if match is None:
        raise ValueError("no JSON object in the reply")
    return json.loads(match.group(0))


def parse_opinion(seat, reply: str) -> Opinion:
    """
    Turn one seat's raw reply into an Opinion, coercing every field.

    Never raises: a seat that returned garbage is a seat that failed, and a
    failed seat is a partial meeting, not a dead one.
    """
    opinion = Opinion(seat_id=seat.id, seat_name=seat.name)
    try:
        data = _extract_json(reply)
    except (ValueError, json.JSONDecodeError) as e:
        opinion.failed = True
        opinion.failure_reason = f"unreadable reply: {e}"
        return opinion
    if not isinstance(data, dict):
        opinion.failed = True
        opinion.failure_reason = "reply was not an object"
        return opinion

    opinion.abstained = as_bool(data.get("abstain"))
    opinion.position = as_text(data.get("position"))
    opinion.reasoning = as_text(data.get("reasoning"))
    opinion.confidence = as_confidence(data.get("confidence"))
    opinion.changes_mind = as_text(data.get("changes_mind"))
    # Gated against what THIS seat was shown, not the whole file.
    opinion.citations = gate_citations(data.get("citations"), seat.visible_ids())

    if not opinion.abstained and not opinion.position:
        opinion.failed = True
        opinion.failure_reason = "no position and did not abstain"
    return opinion


# ── The guards (deterministic, not prompted) ────────────────────────────────


def unanimity_possible(opinions) -> bool:
    """
    Whether the word "unanimous" is even AVAILABLE for this room.

    Named for what it actually computes. Fewer than two non-abstaining seats
    can never be unanimous — one voice is not a consensus, and a model will
    cheerfully call it one — so this is the floor below which the word is
    meaningless, and it is computed rather than asked for precisely because
    the model gets it wrong.

    It deliberately does NOT decide whether two seats that spoke agree; that
    is a judgement about content and it belongs to the chair. The two are
    combined in parse_synthesis(): the chair may claim agreement, but only
    where this floor allows the claim to be made at all.

    Calling this `is_unanimous` was a bug, not a naming quibble: a split
    board of two would have been stored as unanimous, and the prose guard —
    which fires on the computed verdict — would have had nothing to fire on
    in the most common case there is.

    KNOWN LIMIT, stated plainly because it is the board's weakest joint: when
    two or more seats speak, whether they actually AGREE is decided by the
    chair, and nothing downstream can check it. A chair that calls two
    opposing positions unanimous is believed. That is not fixable
    deterministically — judging whether "raise the price" and "kill the
    product" are the same answer is the semantic problem the chair exists to
    solve — so the mitigation is upstream: the chair prompt names the split
    as the thing it is for, and the seat prompts push each advisor to argue
    rather than agree. Read a meeting that claims unanimity with that in
    mind.
    """
    return len([o for o in opinions if o.spoke]) >= 2


_UNANIMITY_CLAIM = re.compile(
    r"\b(unanimous|unanimity|all (?:four|three|two|the seats?|advisors?) agree|"
    r"the board agrees|everyone agrees|no disagreement)\b",
    re.I,
)


def contradicts_unanimity(spoken: str, unanimous: bool) -> bool:
    """
    True if the spoken line claims consensus the stored verdict denies.

    This exists because it happened: the guard set `unanimous: false`,
    worked perfectly, and the prose read aloud said "the board is unanimous"
    anyway. A guard whose result the summary can ignore is decoration.

    `unanimous` here is the FINAL verdict — the chair's claim already gated
    by unanimity_possible() — not the floor. Checking against the floor
    alone was the bug: two seats that flatly disagreed cleared it, and any
    consensus claim then passed unchallenged.
    """
    if unanimous:
        return False
    return bool(_UNANIMITY_CLAIM.search(spoken or ""))


# ── The fan-out ─────────────────────────────────────────────────────────────


async def _ask_seat(seat, ask_model, question: str, brief: str) -> Opinion:
    try:
        reply = await ask_model(
            system=seat_system_prompt(seat),
            prompt=(
                f"## The question\n\n{question}\n\n"
                + (f"## Context on the business\n\n{brief}\n\n" if brief else "")
                + "Give your position."
            ),
            max_tokens=SEAT_MAX_TOKENS,
        )
    except Exception as e:  # noqa: BLE001 — one seat failing is a partial meeting
        logger.warning("board: seat %s failed (%s)", seat.id, e)
        return Opinion(
            seat_id=seat.id, seat_name=seat.name,
            failed=True, failure_reason=f"{type(e).__name__}: {e}",
        )
    return parse_opinion(seat, reply)


async def convene(seats, ask_model, question: str, *, brief: str = "",
                  ceiling_usd: float = DEFAULT_MEETING_CEILING_USD) -> list:
    """
    One call per seat, concurrently, each in isolation. Returns an Opinion
    per seat — including the ones that failed.

    The ceiling is checked BEFORE anything is spent. A cost check that only
    runs after a call returns makes "spend nothing" spend a whole fan-out
    before anything notices.
    """
    if ceiling_usd <= 0:
        raise BudgetExhausted("the board's cost ceiling is zero — no meeting was held")
    if not seats:
        return []
    return list(await asyncio.gather(
        *(_ask_seat(seat, ask_model, question, brief) for seat in seats)
    ))


# ── The chair ───────────────────────────────────────────────────────────────


def chair_system_prompt() -> str:
    return (
        "You are Trillion, chairing Sean's board of advisors. You have read "
        "the live business figures; the seats have not — they are well-read "
        "and blind, and you are the only participant who can check their "
        "advice against what is actually true here.\n\n"
        "Your job, in this order:\n\n"
        "1. **Name the split before the agreement.** Where the board divided "
        "is the information. Where it agreed is usually the obvious.\n"
        "2. **Discount seats using their own documented blind spots.** Each "
        "opinion arrives with the blind spots its dossier declares. When a "
        "position falls inside one, say so plainly and weight it down.\n"
        "3. **Treat abstention as abstention, never as assent.** A seat that "
        "declined to answer did not agree with anyone.\n"
        "4. **Flag anything resting on an entry marked `user`** as Sean's own "
        "assumption, not that advisor's documented view. Otherwise a belief "
        "he typed in himself comes back wearing an advisor's name and reads "
        "as outside corroboration.\n"
        "5. Say plainly where the board is weak on THIS question.\n\n"
        "Reply with ONLY a JSON object:\n"
        '{"spoken": "one or two sentences, leading with the split", '
        '"detail": "the full synthesis, markdown", '
        '"recommendation": "what you would actually do", '
        '"unanimous": false, '
        '"confidence": 0.0-1.0}\n\n'
        "Set `unanimous` true ONLY if every seat that spoke reached "
        "substantially the same position. Two seats recommending opposite "
        "actions is not unanimity, however politely they phrased it. If the "
        "computed verdict below says unanimity is impossible, your value is "
        "ignored and a claim of consensus in your prose is overwritten."
    )


def chair_brief(question: str, opinions, seats_by_id: dict, brief: str, possible: bool) -> str:
    """Everything the chair reads: each opinion, its citations, and the
    blind spots of the seat that gave it."""
    blocks = []
    for opinion in opinions:
        seat = seats_by_id.get(opinion.seat_id)
        if opinion.failed:
            blocks.append(f"### {opinion.seat_name}\n\nDID NOT RESPOND ({opinion.failure_reason}). "
                          "This is not agreement and not abstention — treat it as absent.")
            continue
        if opinion.abstained:
            blocks.append(f"### {opinion.seat_name}\n\nABSTAINED — no doctrine on this question. "
                          "This is not assent.")
            continue

        cited = []
        if seat is not None:
            by_id = {e.id: e for e in seat.visible_doctrine()}
            for cid in opinion.citations:
                entry = by_id.get(cid)
                if entry is not None:
                    cited.append(f"  - {cid} ({entry.verification}): {entry.title} — "
                                 f"{entry.source or 'no source stated'}")
        block = [
            f"### {opinion.seat_name}",
            f"Position: {opinion.position}",
            f"Reasoning: {opinion.reasoning}",
            f"Confidence: {opinion.confidence:.2f}",
        ]
        if opinion.changes_mind:
            block.append(f"Would change their mind: {opinion.changes_mind}")
        block.append("Citations:\n" + ("\n".join(cited) if cited else "  - NONE — this seat spoke without support."))
        if seat is not None and seat.blind_spots:
            block.append(f"This seat's documented blind spots:\n{seat.blind_spots}")
        blocks.append("\n".join(block))

    parts = [f"## The question\n\n{question}"]
    if brief:
        parts.append(f"## The live numbers (only you can see these)\n\n{brief}")
    parts.append("## What the room said\n\n" + "\n\n".join(blocks))
    parts.append(
        "## Computed verdict\n\n"
        f"unanimity_possible: {str(possible).lower()}\n\n"
        + ("Two or more seats spoke, so unanimity is AVAILABLE as a "
           "description — but only if they actually reached the same "
           "position. Decide that on the substance, not the tone."
           if possible else
           "Fewer than two seats spoke. You may NOT describe the board as "
           "unanimous, in agreement, or of one mind — not in the spoken "
           "line and not in the detail. One voice is not a consensus.")
    )
    return "\n\n".join(parts)


@dataclass
class Synthesis:
    spoken: str = ""
    detail: str = ""
    recommendation: str = ""
    confidence: float = 0.5
    # The final verdict: the chair judged the room agreed AND the floor
    # allowed the claim. Either half alone is not enough.
    unanimous: bool = False
    # The deterministic floor, kept alongside so a stored meeting can show
    # WHY it wasn't unanimous — a split room and a room of one read very
    # differently and this is the only thing that distinguishes them later.
    unanimity_possible: bool = False
    guard_corrected: bool = False


def parse_synthesis(reply: str, possible: bool) -> Synthesis:
    """
    The chair's reply, coerced — and its prose checked against the final
    verdict rather than trusted to have honoured it.

    `possible` is the deterministic floor from unanimity_possible(). The
    chair's own `unanimous` is a judgement about substance, and it is ANDed
    with the floor: the model can withhold the claim but can never
    manufacture it.
    """
    synthesis = Synthesis(unanimity_possible=possible)
    try:
        data = _extract_json(reply)
    except (ValueError, json.JSONDecodeError):
        synthesis.spoken = "The board met, but I couldn't read the synthesis back."
        return synthesis
    if not isinstance(data, dict):
        synthesis.spoken = "The board met, but the synthesis came back in the wrong shape."
        return synthesis

    synthesis.unanimous = possible and as_bool(data.get("unanimous"))
    synthesis.spoken = as_text(data.get("spoken"), limit=600)
    synthesis.detail = as_text(data.get("detail"), limit=8000)
    synthesis.recommendation = as_text(data.get("recommendation"), limit=2000)
    synthesis.confidence = as_confidence(data.get("confidence"))

    if contradicts_unanimity(synthesis.spoken, synthesis.unanimous):
        # The guard was right and the prose ignored it. Replace the sentence
        # rather than shipping a claim the computed verdict contradicts.
        synthesis.spoken = (
            "The board did not agree — I've put the split on screen rather "
            "than read you a consensus that wasn't there."
        )
        synthesis.guard_corrected = True
    return synthesis
