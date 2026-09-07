"""
Board storage — playbook/the-board.md Tier 6.

Two tables and two rules that were both learned expensively.

**Retire, never delete.** Deleting a doctrine entry frees its id for reuse,
and the next entry to inherit `D3` silently re-points every stored citation
at *different content*. That is a wrong attribution under a real person's
name, not a broken link — far worse, and invisible. So `retire_entry()` is
the only removal path and the id stays spoken for forever.

**Sean never edits a verification state.** The server decides it. Editing an
entry's substance drops it to `user` automatically, because the adversarial
fact-check no longer covers what it now says. Without that rule the editor is
a way to stamp an unchecked claim `sourced` — the exact laundering this
design exists to prevent.

Meetings snapshot their citations at write time: each cited entry's title and
source are stored ON the meeting. A stored meeting then renders its own
citations without re-reading a dossier that may since have changed, and stays
readable when it has.

File-backed dossiers stay as shipped defaults; edits live here. Every read
goes through effective_seats() so a surface can't accidentally show the file
while another shows the edit.
"""

from __future__ import annotations

import json
import os
from dataclasses import replace
from datetime import datetime, timezone

from .. import storage_utils
from .dossier import ACTIVE, RETIRED, USER, DoctrineEntry

SCHEMA = """
CREATE TABLE IF NOT EXISTS board_meetings (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    question        TEXT    NOT NULL,
    unprompted      INTEGER NOT NULL DEFAULT 0,
    seat_ids        TEXT    NOT NULL DEFAULT '[]',
    opinions        TEXT    NOT NULL DEFAULT '[]',
    unanimity_possible INTEGER NOT NULL DEFAULT 0,
    spoken          TEXT    NOT NULL DEFAULT '',
    detail          TEXT    NOT NULL DEFAULT '',
    recommendation  TEXT    NOT NULL DEFAULT '',
    unanimous       INTEGER NOT NULL DEFAULT 0,
    guard_corrected INTEGER NOT NULL DEFAULT 0,
    cost_usd        REAL    NOT NULL DEFAULT 0,
    created_at      TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_board_meetings_at ON board_meetings(created_at);

-- One row per EDITED entry. An unedited entry has no row and is served from
-- the shipped file, so the file stays the readable source of truth for
-- everything nobody has touched.
CREATE TABLE IF NOT EXISTS board_entry_edits (
    seat_id      TEXT NOT NULL,
    entry_id     TEXT NOT NULL,
    title        TEXT NOT NULL DEFAULT '',
    source       TEXT NOT NULL DEFAULT '',
    body         TEXT NOT NULL DEFAULT '',
    verification TEXT NOT NULL DEFAULT 'user',
    status       TEXT NOT NULL DEFAULT 'active',
    updated_at   TEXT NOT NULL,
    PRIMARY KEY (seat_id, entry_id)
);
"""


def default_db_path() -> str:
    return os.getenv("TRILLION_BOARD_DB", "board.db")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class BoardRepo:
    def __init__(self, db_path: str | None = None) -> None:
        self.db_path = db_path or default_db_path()
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    def _connect(self):
        return storage_utils.connect(self.db_path)

    # ── Meetings ─────────────────────────────────────────────────────────────

    def record_meeting(
        self,
        *,
        question: str,
        seats,
        opinions,
        synthesis,
        cost_usd: float = 0.0,
        unprompted: bool = False,
    ) -> int:
        """
        Store a meeting, snapshotting each cited entry's title and source.

        The snapshot is why a meeting from three weeks ago still renders
        correctly after its dossier has been edited: it carries its own
        citation text rather than a pointer into a file that has moved on.
        """
        by_seat = {seat.id: {e.id: e for e in seat.visible_doctrine()} for seat in seats}
        payload = []
        for opinion in opinions:
            entries = by_seat.get(opinion.seat_id, {})
            payload.append({
                "seat_id": opinion.seat_id,
                "seat_name": opinion.seat_name,
                "position": opinion.position,
                "reasoning": opinion.reasoning,
                "confidence": opinion.confidence,
                "changes_mind": opinion.changes_mind,
                "abstained": opinion.abstained,
                "failed": opinion.failed,
                "failure_reason": opinion.failure_reason,
                "unsourced": opinion.unsourced,
                "citations": [
                    {
                        "id": cid,
                        "title": entries[cid].title if cid in entries else "",
                        "source": entries[cid].source if cid in entries else "",
                        "verification": entries[cid].verification if cid in entries else USER,
                    }
                    for cid in opinion.citations
                ],
            })

        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO board_meetings
                    (question, unprompted, seat_ids, opinions, spoken, detail,
                     recommendation, unanimous, unanimity_possible,
                     guard_corrected, cost_usd, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    question,
                    1 if unprompted else 0,
                    json.dumps([seat.id for seat in seats]),
                    json.dumps(payload),
                    synthesis.spoken,
                    synthesis.detail,
                    synthesis.recommendation,
                    1 if synthesis.unanimous else 0,
                    # Stored alongside so a past meeting can show WHY it
                    # wasn't unanimous: a room that split and a room of one
                    # read very differently, and this is the only thing that
                    # tells them apart later.
                    1 if synthesis.unanimity_possible else 0,
                    1 if synthesis.guard_corrected else 0,
                    float(cost_usd),
                    _now(),
                ),
            )
            return int(cur.lastrowid)

    def _hydrate(self, row) -> dict:
        meeting = dict(row)
        meeting["seat_ids"] = json.loads(meeting["seat_ids"] or "[]")
        meeting["opinions"] = json.loads(meeting["opinions"] or "[]")
        meeting["unanimous"] = bool(meeting["unanimous"])
        meeting["unanimity_possible"] = bool(meeting["unanimity_possible"])
        meeting["unprompted"] = bool(meeting["unprompted"])
        meeting["guard_corrected"] = bool(meeting["guard_corrected"])
        return meeting

    def get_meeting(self, meeting_id: int) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM board_meetings WHERE id = ?", (meeting_id,)
            ).fetchone()
        return None if row is None else self._hydrate(row)

    def recent_meetings(self, limit: int = 20) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM board_meetings ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self._hydrate(row) for row in rows]

    # ── Entry edits ──────────────────────────────────────────────────────────

    def edit_entry(self, seat_id: str, entry_id: str, *, title: str, source: str, body: str) -> None:
        """
        Save an edit. Verification is NOT a parameter — the server sets it.

        Any substantive edit drops the entry to `user`, because the
        adversarial fact-check that produced `sourced` was run against
        different text. There is deliberately no way for a caller to pass a
        verification state in; that argument is the laundering hole.
        """
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO board_entry_edits
                    (seat_id, entry_id, title, source, body, verification, status, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, COALESCE(
                    (SELECT status FROM board_entry_edits WHERE seat_id = ? AND entry_id = ?),
                    ?), ?)
                ON CONFLICT(seat_id, entry_id) DO UPDATE SET
                    title = excluded.title, source = excluded.source,
                    body = excluded.body, verification = excluded.verification,
                    updated_at = excluded.updated_at
                """,
                (seat_id, entry_id.upper(), title, source, body, USER,
                 seat_id, entry_id.upper(), ACTIVE, _now()),
            )

    def retire_entry(self, seat_id: str, entry_id: str) -> None:
        """
        Retire an entry. The only removal path there is.

        The row survives so the id can never be handed to a different entry
        later. A retired entry is withheld from its seat but still renders
        correctly on every meeting that already cited it.
        """
        entry_id = entry_id.upper()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM board_entry_edits WHERE seat_id = ? AND entry_id = ?",
                (seat_id, entry_id),
            ).fetchone()
            if row is None:
                conn.execute(
                    """
                    INSERT INTO board_entry_edits
                        (seat_id, entry_id, title, source, body, verification, status, updated_at)
                    VALUES (?, ?, '', '', '', ?, ?, ?)
                    """,
                    (seat_id, entry_id, USER, RETIRED, _now()),
                )
            else:
                conn.execute(
                    "UPDATE board_entry_edits SET status = ?, updated_at = ? "
                    "WHERE seat_id = ? AND entry_id = ?",
                    (RETIRED, _now(), seat_id, entry_id),
                )

    def restore_entry(self, seat_id: str, entry_id: str) -> None:
        """Un-retire. The id was never freed, so this is safe."""
        with self._connect() as conn:
            conn.execute(
                "UPDATE board_entry_edits SET status = ?, updated_at = ? "
                "WHERE seat_id = ? AND entry_id = ?",
                (ACTIVE, _now(), seat_id, entry_id.upper()),
            )

    def edits_for(self, seat_id: str) -> dict[str, dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM board_entry_edits WHERE seat_id = ?", (seat_id,)
            ).fetchall()
        return {row["entry_id"]: dict(row) for row in rows}


def effective_seat(seat, repo: BoardRepo | None):
    """
    One seat with its edits applied.

    THE single read path. If a surface reads the file directly while another
    reads through here, an edit that reaches one and not the other means the
    board quietly disagrees with itself and nothing errors.
    """
    if repo is None:
        return seat
    edits = repo.edits_for(seat.id)
    if not edits:
        return seat

    doctrine = []
    for entry in seat.doctrine:
        edit = edits.get(entry.id)
        if edit is None:
            doctrine.append(entry)
            continue
        if edit["status"] == RETIRED:
            doctrine.append(replace(entry, status=RETIRED))
            continue
        doctrine.append(DoctrineEntry(
            id=entry.id,
            title=edit["title"] or entry.title,
            source=edit["source"] or entry.source,
            body=edit["body"] or entry.body,
            # Server-owned. An edited entry is `user`, always.
            verification=edit["verification"],
            status=ACTIVE,
        ))
    return replace(seat, doctrine=tuple(doctrine))


def effective_seats(seats, repo: BoardRepo | None) -> list:
    return [effective_seat(seat, repo) for seat in seats]
