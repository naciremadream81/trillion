"""
The whole meeting, end to end — the piece the tool and the standing review
both call.

Route (one cheap call) → fan out (one call per seat, isolated) → chair (one
call) → store. Everything it needs that touches a provider arrives as
`ask_model`, so this module is testable without a network, an API key, or a
model, which is exactly what the guards in meeting.py need to be tested
against.
"""

from __future__ import annotations

import json
import logging
import os
import re

from .dossier import load_roster
from .meeting import (
    CHAIR_MAX_TOKENS,
    BudgetExhausted,
    chair_brief,
    chair_system_prompt,
    convene as fan_out,
    unanimity_possible,
    parse_synthesis,
)
from .routing import (
    DEFAULT_MAX_SEATS,
    build_router_prompt,
    seats_by_domain,
    seats_named_in,
    select_seats,
)
from .storage import BoardRepo, effective_seats

logger = logging.getLogger(__name__)

SEATS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "seats")

# The prompt that makes the standing review work. The last line is
# load-bearing: without it the likeliest failure is a board that politely
# hands the question back ("what would you like to discuss?"), which defeats
# the entire routine.
STANDING_REVIEW_QUESTION = (
    "You did not call this meeting, so there is no question but the business. "
    "Read the numbers. What would you put on the agenda this month that they "
    "are not already looking at? Name the specific number that moves you, say "
    "what it implies, and give one concrete thing to do in the next 30 days. "
    "If the numbers genuinely warrant nothing, say so plainly rather than "
    "manufacturing a concern. Do not ask what they want to discuss — this is "
    "your agenda, not theirs."
)


class BoardUnavailable(RuntimeError):
    """No usable roster. Distinct from a decline: nothing was even asked."""


class Declined(RuntimeError):
    """The router refused the question. Carries the reason."""


def load_seats(repo: BoardRepo | None = None, seats_dir: str | None = None) -> list:
    """The roster, with every edit applied. The ONLY read path."""
    return effective_seats(load_roster(seats_dir or SEATS_DIR), repo)


def _extract_json(text: str) -> dict:
    match = re.search(r"\{.*\}", text or "", re.S)
    if match is None:
        return {}
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


async def route(question: str, seats, ask_model, *, max_seats: int = DEFAULT_MAX_SEATS):
    """
    Decide whether to convene and who has standing.

    Deterministic first: an explicitly named advisor is not a judgement call,
    and paying a router call to rediscover a name Sean just said out loud is
    money for nothing. Only an unnamed question reaches the model.
    """
    named = seats_named_in(question, seats)
    if named:
        return named[:max_seats], "named explicitly"

    reply = await ask_model(
        system=build_router_prompt(seats, max_seats),
        prompt=question,
        max_tokens=400,
    )
    data = _extract_json(reply)

    # Absent or unreadable, `convene` defaults to False — the expensive
    # direction is fanning out, so an unparseable router declines rather
    # than spending four calls on a guess.
    from .meeting import as_bool, as_text

    if not as_bool(data.get("convene")):
        raise Declined(as_text(data.get("reason")) or "the board declined this question")

    chosen = select_seats(data.get("seats"), seats, max_seats)
    if not chosen:
        # The router said convene but named nobody usable. Fall back to a
        # domain match rather than declining — the question cleared the gate.
        chosen = seats_by_domain(question, seats)[:max_seats]
    if not chosen:
        raise Declined("no seat on this roster holds doctrine on that")
    return chosen, as_text(data.get("reason"))


async def hold_meeting(
    question: str,
    ask_model,
    *,
    brief: str = "",
    repo: BoardRepo | None = None,
    seats_dir: str | None = None,
    max_seats: int = DEFAULT_MAX_SEATS,
    ceiling_usd: float = 0.75,
    unprompted: bool = False,
) -> dict:
    """
    Convene, synthesize, store. Returns the meeting as stored plus its id.

    `brief` is the live business figures. The seats get a short version; only
    the chair sees everything — they are well-read and blind, and the chair
    is the one that reconciles them against what is actually true.
    """
    seats = load_seats(repo, seats_dir)
    if not seats:
        raise BoardUnavailable(
            "no seats are configured — see agent/board/seats/README.md"
        )

    chosen, reason = await route(question, seats, ask_model, max_seats=max_seats)

    # Seats see a short brief so they can be specific; the chair sees the
    # whole thing. Truncating here rather than passing `brief` through is
    # what keeps that asymmetry real instead of aspirational.
    seat_brief = brief[:800] if brief else ""
    opinions = await fan_out(
        chosen, ask_model, question, brief=seat_brief, ceiling_usd=ceiling_usd
    )

    # The deterministic floor. The chair decides whether the room actually
    # agreed; this decides whether it is allowed to say so.
    possible = unanimity_possible(opinions)
    seats_by_id = {seat.id: seat for seat in chosen}
    try:
        chair_reply = await ask_model(
            system=chair_system_prompt(),
            prompt=chair_brief(question, opinions, seats_by_id, brief, possible),
            max_tokens=CHAIR_MAX_TOKENS,
        )
    except Exception as e:  # noqa: BLE001 — the opinions are still worth keeping
        logger.warning("board: the chair failed (%s); storing the opinions anyway", e)
        chair_reply = ""

    synthesis = parse_synthesis(chair_reply, possible)
    if not synthesis.spoken:
        synthesis.spoken = (
            f"{len([o for o in opinions if o.spoke])} of {len(chosen)} seats spoke, "
            "but I couldn't synthesize them — the detail is on screen."
        )

    repo = repo or BoardRepo()
    meeting_id = repo.record_meeting(
        question=question, seats=chosen, opinions=opinions,
        synthesis=synthesis, unprompted=unprompted,
    )
    return {
        "id": meeting_id,
        "routing_reason": reason,
        "seats": [{"id": s.id, "name": s.name, "seat": s.seat} for s in chosen],
        "spoken": synthesis.spoken,
        "detail": synthesis.detail,
        "recommendation": synthesis.recommendation,
        "unanimous": synthesis.unanimous,
        "unanimity_possible": synthesis.unanimity_possible,
        "guard_corrected": synthesis.guard_corrected,
        "abstained": [o.seat_name for o in opinions if o.abstained],
        "failed": [o.seat_name for o in opinions if o.failed],
        "unsourced": [o.seat_name for o in opinions if o.unsourced],
    }
