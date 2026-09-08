"""
The scout's doctrine and hunting lanes as editable documents —
playbook/opportunity-scout.md Tiers 2, 3 and 4.

Before this module the scout's instructions were a Python f-string and its
subject matter was one env var (TRILLION_FACTORY_AUTONOMOUS_THEMES). Both
are the kind of thing an operator retunes constantly and a programmer
touches once, which is the wrong way round: retuning meant editing code and
restarting a process.

So there are three layers, and a read never has to know which one answered:

    override (agent_documents row)   set by the operator, no deploy needed
      ↓ falls back to
    shipped file (DOCTRINE.md / LANES.md, next to this module)
      ↓ falls back to
    a hardcoded emergency string     so a deleted file degrades, never crashes

`effective_document(key, path)` collapses that to one call.

THE GOVERNING RULE FOR THIS WHOLE MODULE: **nothing here may raise.** The
moment a human can edit a document, it can be edited badly — and the caller
is a scheduled job that ticks at an hour nobody is watching. Every parse
failure, missing file, permission error and empty override resolves to a
usable value plus a log line saying what was wrong and what the format is.
The scout running on a generic brief is a bad day; the scheduler dying on an
IndexError is a silent outage.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)

_HERE = os.path.dirname(os.path.abspath(__file__))
DOCTRINE_PATH = os.path.join(_HERE, "DOCTRINE.md")
LANES_PATH = os.path.join(_HERE, "LANES.md")

DOCTRINE_KEY = "scout_doctrine"
LANES_KEY = "scout_lanes"

# Last-resort text if BOTH the override and the shipped file are unusable.
# Deliberately terse and obviously generic: if this ever reaches a model, the
# report it produces should look different enough that someone notices.
EMERGENCY_DOCTRINE = (
    "You are an opportunity scout. Research real problems people are having "
    "online and report what you find, with a source for every claim. You "
    "recommend; a human decides."
)
EMERGENCY_LANE_LABEL = "general"
EMERGENCY_LANE_BRIEF = (
    "No hunting lanes are configured. Search broadly within the themes you "
    "were given and report the strongest problems you can evidence."
)

# Which lane each weekday draws. A plain identity mapping today — Monday
# takes lane 0, Tuesday lane 1, and so on — kept as an explicit table because
# the operator's rotation is a scheduling decision, not an arithmetic one,
# and this is where a "no scouting on Sundays" style rule would live.
WEEKDAY_TO_LANE: dict[int, int] = {0: 0, 1: 1, 2: 2, 3: 3, 4: 4, 5: 5, 6: 6}

# How long a document override may be stale in a process that didn't write
# it. main.py and serve.py are separate processes over one SQLite file with
# no LISTEN/NOTIFY (see agent/factory/dispatch.py's RegistryWatcher for the
# same constraint), so "publish a notification" is a bounded poll here. 30s
# matches the watcher's cadence: an operator edit lands on the next run
# without a restart, which is the claim Tier 4 actually makes.
OVERRIDE_TTL_SECONDS = 30.0


# ── Layer 1: the shipped file, cached on mtime ──────────────────────────────

_file_cache: dict[str, tuple[float, int, str]] = {}  # path -> (mtime, size, body)
_file_lock = threading.Lock()


def load_document(path: str) -> str:
    """
    Read a document from disk, caching on (mtime, size).

    Returns "" — never raises — if the file is missing or unreadable, so the
    caller's `or` chain can fall through to the next layer. Size joins mtime
    in the cache key because a coarse filesystem timestamp can make two
    edits inside the same second look identical.
    """
    try:
        stat = os.stat(path)
    except OSError as e:
        logger.error("scout document %s is unreadable (%s); falling back", path, e)
        return ""

    key = (stat.st_mtime, stat.st_size)
    with _file_lock:
        cached = _file_cache.get(path)
        if cached is not None and (cached[0], cached[1]) == key:
            return cached[2]

    try:
        with open(path, encoding="utf-8") as fh:
            body = fh.read()
    except OSError as e:
        logger.error("scout document %s could not be read (%s); falling back", path, e)
        return ""

    with _file_lock:
        _file_cache[path] = (stat.st_mtime, stat.st_size, body)
    return body


# ── Layer 2: the operator override, cached in process ───────────────────────


class DocumentOverrides:
    """
    An in-process cache of the agent_documents table.

    Whole-cache refresh rather than per-key invalidation, on the playbook's
    reasoning: a cache this small isn't worth the bug surface of partial
    updates. `repo` is anything with all_documents() — BuildRepo in
    production, a stub in the tests.

    A repo that raises (database locked, file gone) is not an error the
    caller should see: the previous snapshot is kept and the read falls
    through to the shipped file if there was never a snapshot at all.
    """

    def __init__(self, repo, *, ttl_seconds: float = OVERRIDE_TTL_SECONDS, clock=time.monotonic):
        self._repo = repo
        self._ttl = ttl_seconds
        self._clock = clock
        self._cache: dict[str, str] = {}
        self._fetched_at: float | None = None
        self._lock = threading.Lock()

    def refresh(self) -> None:
        """Reload every override. Called on a TTL miss, and callable directly
        by whoever just wrote one so their own process sees it immediately."""
        try:
            documents = self._repo.all_documents()
        except Exception as e:  # noqa: BLE001 — a broken read must not break a run
            logger.warning("could not refresh scout document overrides (%s); using last known", e)
            return
        with self._lock:
            self._cache = documents
            self._fetched_at = self._clock()

    def get(self, key: str) -> str | None:
        """The override for `key`, or None if the operator hasn't set one."""
        with self._lock:
            fresh = self._fetched_at is not None and (self._clock() - self._fetched_at) < self._ttl
        if not fresh:
            self.refresh()
        with self._lock:
            return self._cache.get(key)


# ── The two layers, collapsed into one read ─────────────────────────────────


def effective_document(key: str, path: str, overrides: DocumentOverrides | None = None) -> str:
    """
    The document in force for `key`: the operator's override if they have set
    a usable one, otherwise the file shipped at `path`, otherwise "".

    Callers never branch on which layer answered. An override that is empty
    or whitespace is treated as absent and logged — an operator who clears
    the textarea meant "reset", not "run the scout with no instructions",
    and silently obeying the literal reading is how you get a scout with an
    empty system prompt at 8:30am.
    """
    if overrides is not None:
        body = overrides.get(key)
        if body is not None:
            if body.strip():
                return body
            logger.warning(
                "scout document override %r is empty; using the shipped file at %s instead. "
                "Delete the override to make this the permanent behaviour.",
                key,
                path,
            )
    return load_document(path)


_NOTES_FENCE = re.compile(r"^-{3,}[ \t]*$", re.M)


def strip_operator_notes(text: str) -> str:
    """
    Drop a leading notes block, if the document opens with one.

    A doctrine document has two audiences. The operator needs to know where
    the file is loaded from and how an override interacts with it; the model
    needs none of that and is measurably worse for reading it — "this file is
    cached on mtime" is noise in a system prompt.

    So the convention is a horizontal rule: if a `---` line appears in the
    first part of the document, everything up to and including it is the
    operator's notes and is not sent. The same rule applies to an override,
    so an operator editing in a textarea gets the same behaviour as one
    editing the file.

    A document with no rule is sent whole — which is what a plainly-written
    override will be, and the right default. The rule only counts if it falls
    within the first _NOTES_MAX_LINES lines, so a `---` used as an ordinary
    section divider halfway down a long doctrine doesn't silently eat it.
    """
    if not text:
        return ""
    match = _NOTES_FENCE.search(text)
    if match is None:
        return text.strip()
    if text.count("\n", 0, match.start()) > _NOTES_MAX_LINES:
        return text.strip()
    return text[match.end():].strip()


# How far into a document a `---` may appear and still be read as the end of
# an operator-notes block rather than an ordinary section divider.
_NOTES_MAX_LINES = 40


def doctrine_prompt(overrides: DocumentOverrides | None = None) -> str:
    """The scout's system prompt, from DOCTRINE.md or its override, with any
    leading operator-notes block removed."""
    body = strip_operator_notes(effective_document(DOCTRINE_KEY, DOCTRINE_PATH, overrides))
    return body or EMERGENCY_DOCTRINE


# ── Layer 3: lanes, parsed out of a document ────────────────────────────────


@dataclass(frozen=True)
class Lane:
    """One hunting strategy. `label` is the stable identity that gets stored
    on whatever this lane finds; `title` is the operator's own heading."""

    label: str
    title: str
    brief: str


EMERGENCY_LANE = Lane(EMERGENCY_LANE_LABEL, "General", EMERGENCY_LANE_BRIEF)

_HEADING = re.compile(r"^##[ \t]+(.+?)[ \t]*$", re.M)
_LANES_FORMAT_HINT = (
    "Expected a markdown document where each `## Heading` starts one lane and "
    "the text beneath it is that lane's brief."
)


def slugify(heading: str) -> str:
    """
    A heading reduced to a stable label.

    Slugified rather than stored verbatim so that reformatting a heading —
    adding a hyphen, changing case, fixing a typo's capitalisation — doesn't
    silently fork the historical record of which lane found what. Returns ""
    for a heading with no alphanumerics at all, which parse_lanes() treats as
    an unusable lane rather than minting a nameless one.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", heading.strip().lower()).strip("-")
    return slug


def parse_lanes(text: str) -> list[Lane]:
    """
    Parse `## Heading` + body pairs into lanes, in document order.

    Never raises. Junk in gives an empty list out, which is the caller's
    signal to fall back — that is the contract every degradation path in this
    module is written against. Prose above the first heading is the
    operator's note to themselves and is skipped.
    """
    if not text or not text.strip():
        return []

    matches = list(_HEADING.finditer(text))
    lanes: list[Lane] = []
    for i, match in enumerate(matches):
        title = match.group(1).strip()
        label = slugify(title)
        if not label:
            logger.warning("skipping a hunting lane whose heading %r has no usable label", title)
            continue
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        brief = text[match.end():end].strip()
        if not brief:
            logger.warning("skipping hunting lane %r: the heading has no brief beneath it", title)
            continue
        lanes.append(Lane(label=label, title=title, brief=brief))
    return lanes


def active_lanes(overrides: DocumentOverrides | None = None) -> list[Lane]:
    """
    The lanes in force: the override's if it parses to at least one, else the
    shipped file's, else the single emergency lane.

    An override that parses to zero lanes is the failure the playbook calls
    out by name — it means the operator saved something that isn't in the
    format — so the log line says what the format is rather than only that
    something went wrong.
    """
    if overrides is not None:
        body = overrides.get(LANES_KEY)
        if body is not None and body.strip():
            lanes = parse_lanes(body)
            if lanes:
                return lanes
            logger.warning(
                "the scout lanes override parsed to zero lanes; using the shipped %s instead. %s",
                LANES_PATH,
                _LANES_FORMAT_HINT,
            )

    lanes = parse_lanes(load_document(LANES_PATH))
    if lanes:
        return lanes

    logger.error(
        "no usable hunting lanes in %s; falling back to one generic lane. %s",
        LANES_PATH,
        _LANES_FORMAT_HINT,
    )
    return [EMERGENCY_LANE]


def lane_index(day, count: int) -> int:
    """
    Which lane a given date draws.

    The modulo is the whole point. Without it, deleting a lane turns any
    weekday mapped past the new end into an IndexError — a scheduled job that
    dies at 8:30am on a day nobody is watching. A positional index into a
    list a human can edit is always taken modulo the live count.

    `count` <= 0 returns 0 rather than raising ZeroDivisionError; callers pair
    this with a lane list that is never empty, and a crash here would defeat
    the purpose of the guarantee.
    """
    if count <= 0:
        return 0
    return WEEKDAY_TO_LANE.get(day.weekday(), day.weekday()) % count


def lane_for(day, overrides: DocumentOverrides | None = None) -> Lane:
    """The one lane that fires on `day`. One lane per run, rotation positional."""
    lanes = active_lanes(overrides)
    return lanes[lane_index(day, len(lanes))]


# ── Tier 5: what the operator may edit, and how it is validated ─────────────


def _validate_doctrine(body: str) -> str | None:
    """None if `body` is a usable doctrine, else why not."""
    if not strip_operator_notes(body):
        return (
            "The doctrine is empty. If you meant to go back to the shipped "
            "default, use Revert instead — saving an empty document would "
            "leave the scout with no instructions."
        )
    return None


def _validate_lanes(body: str) -> str | None:
    """None if `body` parses to at least one lane, else why not."""
    if not parse_lanes(body):
        return (
            "No hunting lanes found. " + _LANES_FORMAT_HINT + " Use Revert to "
            "go back to the shipped lanes."
        )
    return None


@dataclass(frozen=True)
class EditableDocument:
    """
    One document the operator may edit from the UI.

    Registered EXPLICITLY, never discovered by naming convention. A
    convention over the agent_documents table would let any future code path
    expose an internal prompt key by accident simply by naming a row
    "<something>_prompt"; an explicit tuple means a document is editable only
    because somebody decided it should be.
    """

    key: str
    label: str
    description: str
    path: str


# The registry. Every entry MUST have a real shipped file behind it — a UI
# that shows an empty textarea and "no default available" for a document that
# is loaded on every run doesn't error, it lies, and the operator concludes
# the feature doesn't exist. tests/test_scout_doctrine.py asserts that
# against this tuple itself rather than a hand-copied list, because a copy in
# a test file drifts from the original, both stay green, and the bug ships.
EDITABLE_DOCUMENTS: tuple[EditableDocument, ...] = (
    EditableDocument(
        key=DOCTRINE_KEY,
        label="Doctrine",
        description="The scout's system prompt: thesis, hard kills, order of authority, rubric.",
        path=DOCTRINE_PATH,
    ),
    EditableDocument(
        key=LANES_KEY,
        label="Hunting lanes",
        description="One lane fires per run. Heading order is the rotation order.",
        path=LANES_PATH,
    ),
)

_VALIDATORS = {DOCTRINE_KEY: _validate_doctrine, LANES_KEY: _validate_lanes}


def find_editable(key: str) -> EditableDocument | None:
    """The registered document for `key`, or None if it isn't editable.

    Callers route by this rather than by whatever key arrived in a URL. A
    write that trusts the URL will happily paste a lane document over the
    doctrine — same table, no error, no way to notice."""
    for document in EDITABLE_DOCUMENTS:
        if document.key == key:
            return document
    return None


def validate_document(key: str, body: str) -> str | None:
    """None if `body` is a usable value for `key`, else the reason it isn't."""
    validator = _VALIDATORS.get(key)
    return validator(body) if validator else None


def document_state(document: EditableDocument, overrides: DocumentOverrides | None = None) -> dict:
    """
    Everything the UI needs about one document.

    `is_overridden` is per-document on purpose. "The scout has an override"
    and "the scout's doctrine has an override" are different facts: the first
    belongs on a header badge, the second gates the doctrine's own Revert
    button. Conflating them makes reverting the doctrine a no-op delete that
    looks like it worked.
    """
    default = load_document(document.path)
    override = overrides.get(document.key) if overrides is not None else None
    return {
        "key": document.key,
        "label": document.label,
        "description": document.description,
        "effective": override if (override and override.strip()) else default,
        "default": default,
        "is_overridden": bool(override and override.strip()),
        "editable": True,
    }


def all_document_states(overrides: DocumentOverrides | None = None) -> list[dict]:
    """Every editable document, in registry order."""
    return [document_state(document, overrides) for document in EDITABLE_DOCUMENTS]
