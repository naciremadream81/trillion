"""
The dossier format, its parser, and the citation gate — playbook/the-board.md
Tier 1.

**A dossier is a knowledge base, not a personality.** The tempting version of
this feature is a system prompt saying "you are <advisor>, be direct and talk
about offers", which produces an impression of a person. What this holds
instead is a file of numbered, sourced principles that a model reasons *from*.
The voice section is the smallest part of the file on purpose, and it is kept
away from the doctrine so that tone can never substitute for substance.

Two properties the rest of the board depends on:

**Doctrine ids are explicit in the file, never derived from ordering.** `D3`
means whatever the line marked `D3` says, forever. If ids were positional,
reordering entries would silently re-point every citation on every meeting
already stored — a wrong attribution, which is worse than a broken link.

**The parser never takes the board down.** A malformed dossier loses its own
seat and logs why; it does not raise. Equally it must never vanish silently —
a quorum that quietly shrinks is worse than a stale one — so every rejection
is logged at warning level with the reason and the file.

FILE FORMAT
-----------

    ---
    id: fictional-advisor
    name: A. Fictional
    seat: Pricing and packaging
    domains: pricing, packaging, positioning
    status: active
    ---

    ## Doctrine

    ### D1 — Price on value, not cost
    source: Some Book (2021), ch. 4
    verification: sourced

    The body of the entry, one or more paragraphs.

    ## Characteristic objection

    What this person reliably pushes back on, and their opening questions.

    ## Blind spots

    Where this doctrine does not transfer, stated plainly.

    ## Voice

    Tone only.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# Verification states. The server owns these — see storage.py's edit path.
# `sourced` survived the adversarial fact-check in research.py; `user` was
# typed in by the operator, or is an entry whose substance has been edited
# since the check ran, which is the same thing as far as trust goes.
SOURCED = "sourced"
USER = "user"
VERIFICATION_STATES = (SOURCED, USER)

ACTIVE = "active"
RETIRED = "retired"

_FRONTMATTER = re.compile(r"\A---[ \t]*\n(.*?)\n---[ \t]*\n", re.S)
_SECTION = re.compile(r"^##[ \t]+(.+?)[ \t]*$", re.M)
_ENTRY = re.compile(r"^###[ \t]+([A-Za-z]+\d+)[ \t]*(?:[—:-][ \t]*(.*?))?[ \t]*$", re.M)
_FIELD = re.compile(r"^([A-Za-z_]+):[ \t]*(.*)$", re.M)


@dataclass(frozen=True)
class DoctrineEntry:
    """One numbered, sourced principle."""

    id: str
    title: str
    source: str
    body: str
    verification: str = USER
    status: str = ACTIVE

    @property
    def is_retired(self) -> bool:
        return self.status == RETIRED


@dataclass(frozen=True)
class Seat:
    """One advisor, as the board sees them."""

    id: str
    name: str
    seat: str
    domains: tuple[str, ...]
    doctrine: tuple[DoctrineEntry, ...]
    objection: str = ""
    blind_spots: str = ""
    voice: str = ""
    status: str = ACTIVE
    path: str = ""

    def visible_doctrine(self) -> tuple[DoctrineEntry, ...]:
        """The entries a seat is actually shown.

        Retired entries are excluded. This is deliberately NOT "everything in
        the file": the citation gate validates against what the seat was
        shown, and once retirement exists the two diverge. The gap between
        them is a seat citing something that was withheld from it."""
        return tuple(e for e in self.doctrine if not e.is_retired)

    def visible_ids(self) -> set[str]:
        return {e.id for e in self.visible_doctrine()}


class DossierError(ValueError):
    """Raised only by parse_dossier_text(); load_seat() logs and returns None."""


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    match = _FRONTMATTER.match(text)
    if match is None:
        raise DossierError("no frontmatter block")
    meta = {}
    for line in match.group(1).splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        meta[key.strip().lower()] = value.strip()
    return meta, text[match.end():]


def _split_sections(body: str) -> dict[str, str]:
    """`## Heading` → the text beneath it, keyed lowercase."""
    matches = list(_SECTION.finditer(body))
    sections = {}
    for i, match in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        sections[match.group(1).strip().lower()] = body[match.end():end].strip()
    return sections


def _parse_entries(doctrine_text: str) -> list[DoctrineEntry]:
    matches = list(_ENTRY.finditer(doctrine_text))
    entries = []
    for i, match in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(doctrine_text)
        chunk = doctrine_text[match.end():end]

        # Leading `key: value` lines are the entry's metadata; everything
        # after the first blank line or non-field line is the body.
        fields = {}
        body_lines = []
        in_body = False
        for line in chunk.splitlines():
            if not in_body:
                if not line.strip():
                    continue
                field_match = _FIELD.match(line)
                if field_match:
                    fields[field_match.group(1).strip().lower()] = field_match.group(2).strip()
                    continue
                in_body = True
            body_lines.append(line)

        verification = fields.get("verification", USER).strip().lower()
        if verification not in VERIFICATION_STATES:
            verification = USER
        status = fields.get("status", ACTIVE).strip().lower()
        if status not in (ACTIVE, RETIRED):
            status = ACTIVE

        entries.append(DoctrineEntry(
            id=match.group(1).strip().upper(),
            title=(match.group(2) or "").strip(),
            source=fields.get("source", "").strip(),
            body="\n".join(body_lines).strip(),
            verification=verification,
            status=status,
        ))
    return entries


def parse_dossier_text(text: str, path: str = "") -> Seat:
    """
    Parse one dossier. Raises DossierError with a specific reason.

    Three rejections, each because the seat could not function:

      no domains   — it could never be routed to, so it would sit in the
                     roster forever and never be called.
      no doctrine  — there is nothing to cite, so every opinion it gave
                     would be unsourced by construction.
      duplicate id — a citation to D3 when two entries claim D3 is
                     ambiguous, and anti-fabrication machinery that fails
                     open is not machinery.
    """
    meta, body = _parse_frontmatter(text)

    seat_id = meta.get("id", "").strip()
    name = meta.get("name", "").strip()
    if not seat_id or not name:
        raise DossierError("frontmatter needs both an id and a name")

    domains = tuple(
        d.strip().lower() for d in meta.get("domains", "").split(",") if d.strip()
    )
    if not domains:
        raise DossierError("no domains — this seat could never be routed to")

    sections = _split_sections(body)
    entries = _parse_entries(sections.get("doctrine", ""))
    if not entries:
        raise DossierError("no doctrine entries — there would be nothing to cite")

    seen = set()
    duplicates = sorted({e.id for e in entries if e.id in seen or seen.add(e.id)})
    if duplicates:
        raise DossierError(f"duplicate doctrine ids: {', '.join(duplicates)}")

    status = meta.get("status", ACTIVE).strip().lower()
    return Seat(
        id=seat_id,
        name=name,
        seat=meta.get("seat", "").strip(),
        domains=domains,
        doctrine=tuple(entries),
        objection=sections.get("characteristic objection", ""),
        blind_spots=sections.get("blind spots", ""),
        voice=sections.get("voice", ""),
        status=status if status in (ACTIVE, RETIRED) else ACTIVE,
        path=path,
    )


def load_seat(path: str) -> Seat | None:
    """
    Parse one dossier file, or return None having logged why.

    Never raises: a hand-edited file must lose its own seat, not the whole
    roster. Read tolerantly — a file saved in the wrong encoding degrades to
    a logged warning rather than an exception that takes out every other
    advisor alongside it.
    """
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError as e:
        logger.warning("board: could not read dossier %s (%s); that seat is unavailable", path, e)
        return None
    try:
        return parse_dossier_text(text, path=path)
    except DossierError as e:
        logger.warning("board: dossier %s is not usable (%s); that seat is unavailable", path, e)
        return None


def load_roster(directory: str) -> list[Seat]:
    """
    Every usable seat in `directory`, sorted by id.

    A missing directory is an empty roster and a logged warning, not a crash:
    a board with no seats declines every question, which is a degraded board
    rather than a broken agent.
    """
    try:
        names = sorted(
            n for n in os.listdir(directory)
            # README.md documents the format; a leading underscore is the
            # conventional "not a seat" marker. Neither is a malformed
            # dossier, so neither should log a warning every time the
            # roster loads — a warning that always fires is one nobody reads.
            if n.endswith(".md") and n.lower() != "readme.md" and not n.startswith("_")
        )
    except OSError as e:
        logger.warning("board: no roster at %s (%s)", directory, e)
        return []

    seats = []
    for name in names:
        seat = load_seat(os.path.join(directory, name))
        if seat is not None and seat.status == ACTIVE:
            seats.append(seat)
    return sorted(seats, key=lambda s: s.id)


def gate_citations(returned, shown_ids) -> list[str]:
    """
    Keep only the citations the seat was actually shown.

    A pure function on purpose — it is the anti-fabrication core, and it
    should be testable without a model, a file, or a network.

    Uppercase-normalized, de-duplicated, order preserved. Everything else is
    discarded silently: a model that invented `D9` gets no `D9`, and the
    caller sees a shorter list rather than an error, because one bad citation
    is not a reason to throw away a good opinion.

    `shown_ids` is what the seat was SHOWN, never everything in the file.
    Once retirement exists those two sets diverge, and the difference between
    them is precisely a seat citing something withheld from it.
    """
    if not isinstance(returned, (list, tuple)):
        return []
    valid = {str(i).strip().upper() for i in shown_ids}
    out: list[str] = []
    for item in returned:
        # A structural value where a citation id belonged is not a citation.
        if not isinstance(item, (str, int)):
            continue
        candidate = str(item).strip().upper()
        if candidate in valid and candidate not in out:
            out.append(candidate)
    return out
