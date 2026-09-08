"""
Routing: who has standing, and is this a board question at all —
playbook/the-board.md Tier 3.

One cheap decision before an expensive fan-out. A full meeting is one call
per seat plus a chair call, so the few milliseconds spent here are the
cheapest money the feature spends.

Name matching is deterministic and runs FIRST: if the operator named an
advisor out loud, there is nothing for a model to decide. Only an unnamed
question falls through to domain matching.

Two things bite, both of them silently:

**Unicode.** Trillion is voice-driven. A dictated "O'Leary" arrives with a
curly apostrophe (U+2019) and a naive comparison against the ASCII `O'Leary`
in a dossier fails — no error, no match, and every spoken request for that
advisor falls through to a guess. Non-breaking spaces and soft hyphens do the
same thing and are invisible in a log.

**First-name collisions with the operator.** If an advisor shares a first
name with Sean, a bare "what would Sean do" is ambiguous and must not route.
The surname is required in that case.
"""

from __future__ import annotations

import re
import unicodedata

# Four seats: enough for genuine disagreement, few enough to stay affordable
# and readable. Each one is a paid call.
DEFAULT_MAX_SEATS = 4

# The operator's own name, for the collision rule below.
OPERATOR_FIRST_NAMES = frozenset({"sean"})

# Characters that look like ASCII punctuation and are not. Normalized before
# any name comparison.
_LOOKALIKES = {
    "‘": "'", "’": "'", "‛": "'", "ʼ": "'", "＇": "'",
    "“": '"', "”": '"',
    "‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-",
    " ": " ", " ": " ", " ": " ", " ": " ",
    "­": "",           # soft hyphen — invisible, and breaks equality
    "​": "", "﻿": "",
}


def normalize_name(text: str) -> str:
    """
    Fold a name to something two spellings of it can be compared on.

    NFKC first (so fullwidth and compatibility forms collapse), then the
    lookalike table, then case and whitespace. Punctuation is dropped
    entirely rather than normalized further: "O'Leary", "O Leary" and
    "OLeary" should all match, and no real advisor's identity rests on an
    apostrophe.
    """
    folded = unicodedata.normalize("NFKC", text or "")
    folded = "".join(_LOOKALIKES.get(ch, ch) for ch in folded)
    folded = folded.casefold()
    folded = re.sub(r"[^\w\s]", "", folded)
    return re.sub(r"\s+", " ", folded).strip()


def _name_parts(name: str) -> tuple[str, list[str]]:
    parts = normalize_name(name).split()
    return (parts[0] if parts else ""), parts[1:]


def seats_named_in(question: str, seats) -> list:
    """
    Seats the question names explicitly, in roster order.

    A full name always matches. A bare surname matches. A bare FIRST name
    matches only when it isn't one of the operator's own — otherwise "ask
    Sean about this" would convene a seat because of a coincidence of
    first names, which is worse than not routing at all.
    """
    haystack = normalize_name(question)
    if not haystack:
        return []
    padded = f" {haystack} "

    def mentions(token: str) -> bool:
        return bool(token) and f" {token} " in padded

    matched = []
    for seat in seats:
        first, rest = _name_parts(seat.name)
        full = normalize_name(seat.name)
        if full and full in haystack:
            matched.append(seat)
            continue
        if any(mentions(part) for part in rest):
            matched.append(seat)
            continue
        # Bare first name — only when it can't be confused with the operator.
        if first and first not in OPERATOR_FIRST_NAMES and mentions(first):
            matched.append(seat)
    return matched


def seats_by_domain(question: str, seats) -> list:
    """
    Seats whose declared domains appear in the question.

    A blunt keyword match on purpose. It is a cheap pre-filter that saves a
    model call when the question is obviously about pricing or hiring; the
    router call below is what handles everything subtler.
    """
    haystack = normalize_name(question)
    if not haystack:
        return []
    return [
        seat for seat in seats
        if any(normalize_name(domain) in haystack for domain in seat.domains)
    ]


# ── The decline gate ────────────────────────────────────────────────────────

# Written as concrete examples rather than categories, because the abstract
# version is what fails. A gate phrased "refuse personal, medical, legal"
# once declined "Should I cut this product loose?" as "a personal business
# decision about your own company" — which is *precisely* the question a
# board exists for. The word "personal" meant health and family; the model
# read it as "about you".
#
# So: name what to refuse by example, and then say the core use case out
# loud, because erring toward decline is actively worse than erring toward
# convening.
DECLINE_CRITERIA = """\
Decline ONLY these:
- Writing, reviewing or debugging code.
- Medical or health questions — symptoms, diagnoses, treatment, medication.
- Legal questions — contracts to interpret, liability, regulatory exposure.
- Family and relationship matters.
- Factual lookups with one right answer ("what is our MRR", "when did X ship").
  Those are questions for a tool, not for four advisors.

Convene for everything else. In particular:

**Business decisions about Sean's own company are the CORE USE CASE, no
matter how personal they feel.** "Should I cut this product loose?", "Am I
charging too little?", "Should I hire before I have the revenue?", "Is this
the wrong market?" — all of these are exactly what the board is for. That a
decision is his, is hard, and is about something he built does not make it
personal in the sense above. Never decline a question for being a judgement
call; a judgement call is the only kind of question worth convening over."""


def build_router_prompt(seats, max_seats: int = DEFAULT_MAX_SEATS) -> str:
    """The router's system prompt: the roster, the criteria, the cap."""
    roster = "\n".join(
        f"- {seat.id} — {seat.name}"
        + (f", {seat.seat}" if seat.seat else "")
        + f" (domains: {', '.join(seat.domains)})"
        for seat in seats
    )
    return (
        "You are the board's router. You decide two things about a question: "
        "whether it is a question for the board at all, and which seats have "
        "standing on it.\n\n"
        f"{DECLINE_CRITERIA}\n\n"
        "## The roster\n\n"
        f"{roster}\n\n"
        "## Standing\n\n"
        "A seat has standing when the question falls in a domain it actually "
        "holds doctrine on. Prefer seats that will DISAGREE with each other — "
        "a room that already agrees tells Sean nothing. Do not seat an "
        "advisor merely because they are famous or because the question is "
        "vaguely business-shaped.\n\n"
        f"Choose at most {max_seats} seats. Fewer is fine; two who genuinely "
        "disagree beat four who don't.\n\n"
        "Reply with ONLY a JSON object:\n"
        '{"convene": true, "seats": ["seat-id", ...], "reason": "one sentence"}\n'
        'or {"convene": false, "seats": [], "reason": "one sentence saying '
        'which decline criterion applies"}'
    )


def select_seats(seat_ids, seats, max_seats: int = DEFAULT_MAX_SEATS) -> list:
    """
    Resolve router-returned ids to real seats: deduplicated, capped, in
    roster order.

    De-duplication is not tidiness. A router naming one advisor twice would
    otherwise buy two calls to the same dossier AND produce two identical
    opinions — which the unanimity guard in meeting.py counts as two voices,
    letting one advisor clear the consensus bar by agreeing with himself.
    """
    if not isinstance(seat_ids, (list, tuple)):
        return []
    wanted = {str(i).strip().lower() for i in seat_ids if isinstance(i, (str, int))}
    chosen = [seat for seat in seats if seat.id.lower() in wanted]
    return chosen[:max(0, max_seats)]
