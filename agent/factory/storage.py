"""
Storage for the Agent Factory: spawn tasks, research reports, spawned agents.

Same pattern as agent/cost/storage.py — stdlib sqlite3, schema applied
idempotently on every repo construction, one short-lived connection per
operation (thread-safety under asyncio, negligible overhead).

Three tables:
  spawn_tasks      one row per spawn request, tracks state-machine status
  research_reports one row per completed research pass for a spawn task
  spawned_agents   one row per approved, live specialist agent

JSON-shaped columns (competencies, tools_available, tools_wishlist,
design_patterns, sources, tool_allowlist) are stored as TEXT and
json.dumps/json.loads'd at the boundary — sqlite has no array type and this
repo has no ORM, so plain TEXT + json is the least-machinery option.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone

# Terminal/non-terminal states for the spawn_tasks state machine. Kept here
# (not just in code that reads this module) so storage and pipeline agree on
# the vocabulary without importing each other.
PENDING = "PENDING"
RESEARCHING = "RESEARCHING"
DRAFTING_SPEC = "DRAFTING_SPEC"
WRITING_PROMPT = "WRITING_PROMPT"
AWAITING_APPROVAL = "AWAITING_APPROVAL"
APPROVED = "APPROVED"
REJECTED = "REJECTED"
FAILED = "FAILED"

TERMINAL_STATUSES = {APPROVED, REJECTED, FAILED}

# Legal status transitions, keyed by current status. Any write not listed
# here is refused loudly (InvalidTransition) rather than silently corrupting
# the state machine — a bug elsewhere in the pipeline should surface as a
# clear error, not a task stuck in an inconsistent status.
_VALID_TRANSITIONS: dict[str, set[str]] = {
    # AWAITING_APPROVAL is reachable from every pre-approval status, not just
    # WRITING_PROMPT: set_draft() is also exercised directly against a fresh
    # PENDING task (unit tests, and a from-scratch revision re-run).
    PENDING: {RESEARCHING, AWAITING_APPROVAL, FAILED},
    RESEARCHING: {DRAFTING_SPEC, AWAITING_APPROVAL, FAILED},
    DRAFTING_SPEC: {WRITING_PROMPT, AWAITING_APPROVAL, FAILED},
    WRITING_PROMPT: {AWAITING_APPROVAL, FAILED},
    AWAITING_APPROVAL: {APPROVED, PENDING, FAILED},  # PENDING = rejected w/ feedback, re-running
}


class InvalidTransition(ValueError):
    """Raised when a spawn_tasks status write isn't a legal state-machine transition."""

# Slugs that can never be minted as a spawned agent — either they'd collide
# with something else in Trillion, or they're reserved for a future
# hand-built specialist rather than a Factory-generated one.
RESERVED_SLUGS = {"trillion", "factory", "head-of-design"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS spawn_tasks (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    slug                TEXT,
    role_description    TEXT    NOT NULL,
    status              TEXT    NOT NULL DEFAULT 'PENDING',
    research_report_id  INTEGER,
    system_prompt       TEXT,
    tool_allowlist      TEXT,
    revision_count      INTEGER NOT NULL DEFAULT 0,
    feedback            TEXT,
    failure_reason      TEXT,
    created_by          TEXT    NOT NULL DEFAULT 'sean',
    created_at          TEXT    NOT NULL,
    updated_at          TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_spawn_tasks_status ON spawn_tasks(status);
CREATE INDEX IF NOT EXISTS idx_spawn_tasks_created_at ON spawn_tasks(created_at);

CREATE TABLE IF NOT EXISTS research_reports (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    spawn_task_id   INTEGER NOT NULL,
    domain          TEXT,
    competencies    TEXT,
    tools_available TEXT,
    tools_wishlist  TEXT,
    design_patterns TEXT,
    sources         TEXT,
    created_at      TEXT    NOT NULL,
    FOREIGN KEY (spawn_task_id) REFERENCES spawn_tasks(id)
);

CREATE TABLE IF NOT EXISTS spawned_agents (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    slug                TEXT    NOT NULL UNIQUE,
    name                TEXT    NOT NULL,
    system_prompt       TEXT    NOT NULL,
    tool_allowlist      TEXT    NOT NULL DEFAULT '[]',
    model               TEXT,
    status              TEXT    NOT NULL DEFAULT 'active',
    created_by_task_id  INTEGER NOT NULL,
    created_at          TEXT    NOT NULL,
    FOREIGN KEY (created_by_task_id) REFERENCES spawn_tasks(id)
);
"""

_JSON_LIST_FIELDS = (
    "competencies",
    "tools_available",
    "tools_wishlist",
    "design_patterns",
    "sources",
)


def default_db_path() -> str:
    """Where the factory database lives. Override with $TRILLION_FACTORY_DB."""
    return os.getenv("TRILLION_FACTORY_DB", "factory.db")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_query(text: str) -> str:
    """Lowercase + whitespace-collapse so near-duplicate role descriptions
    (extra spaces, different casing) still hit the research cache."""
    return " ".join(text.lower().split())


class FactoryRepo:
    """Reads and writes spawn_tasks / research_reports / spawned_agents."""

    def __init__(self, db_path: str | None = None) -> None:
        self.db_path = db_path or default_db_path()
        self._init_schema()

    # ── Connection / schema ───────────────────────────────────────────────────

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    def _check_transition(self, conn: sqlite3.Connection, task_id: int, new_status: str) -> None:
        """Refuse (InvalidTransition) any status write that isn't a legal
        state-machine move from the task's current status."""
        row = conn.execute("SELECT status FROM spawn_tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            raise InvalidTransition(f"spawn task {task_id} not found")
        current = row["status"]
        if new_status not in _VALID_TRANSITIONS.get(current, set()):
            raise InvalidTransition(
                f"spawn task {task_id}: illegal status transition {current} -> {new_status}"
            )

    # ── Spawn tasks: writes ──────────────────────────────────────────────────

    def create_spawn_task(self, role_description: str, created_by: str = "sean") -> int:
        ts = _now()
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO spawn_tasks (role_description, status, created_by, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (role_description, PENDING, created_by, ts, ts),
            )
            return int(cur.lastrowid)

    def update_status(self, task_id: int, status: str, failure_reason: str | None = None) -> None:
        with self._connect() as conn:
            self._check_transition(conn, task_id, status)
            conn.execute(
                "UPDATE spawn_tasks SET status = ?, failure_reason = ?, updated_at = ? WHERE id = ?",
                (status, failure_reason, _now(), task_id),
            )

    def set_draft(self, task_id: int, *, slug: str, system_prompt: str, tool_allowlist: list[str]) -> None:
        """Save the drafted slug/system prompt/tool allowlist, moving the task to AWAITING_APPROVAL."""
        with self._connect() as conn:
            self._check_transition(conn, task_id, AWAITING_APPROVAL)
            conn.execute(
                """
                UPDATE spawn_tasks
                SET slug = ?, system_prompt = ?, tool_allowlist = ?, status = ?, updated_at = ?
                WHERE id = ?
                """,
                (slug, system_prompt, json.dumps(tool_allowlist), AWAITING_APPROVAL, _now(), task_id),
            )

    def record_rejection(self, task_id: int, feedback: str) -> int:
        """
        Bump revision_count and store feedback for another pass. Returns the
        new revision_count so the caller can compare against the cap (3) and
        auto-fail via update_status(..., FAILED) instead of looping forever.
        """
        with self._connect() as conn:
            self._check_transition(conn, task_id, PENDING)
            conn.execute(
                """
                UPDATE spawn_tasks
                SET feedback = ?, revision_count = revision_count + 1, status = ?, updated_at = ?
                WHERE id = ?
                """,
                (feedback, PENDING, _now(), task_id),
            )
            row = conn.execute(
                "SELECT revision_count FROM spawn_tasks WHERE id = ?", (task_id,)
            ).fetchone()
        return int(row["revision_count"])

    # ── Spawn tasks: reads ───────────────────────────────────────────────────

    def get_spawn_task(self, task_id: int) -> dict | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM spawn_tasks WHERE id = ?", (task_id,)).fetchone()
        return dict(row) if row else None

    def list_pending_approval(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM spawn_tasks WHERE status = ? ORDER BY id", (AWAITING_APPROVAL,)
            ).fetchall()
        return [dict(r) for r in rows]

    def count_spawns_today(self) -> int:
        """Spawn tasks created since UTC midnight — the daily-cap check."""
        start_of_day = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00")
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM spawn_tasks WHERE created_at >= ?", (start_of_day,)
            ).fetchone()
        return int(row["n"])

    # ── Research reports ─────────────────────────────────────────────────────

    def save_research_report(self, task_id: int, *, domain: str, competencies: list[str],
                              tools_available: list[str], tools_wishlist: list[str],
                              design_patterns: list[str], sources: list[str]) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO research_reports
                    (spawn_task_id, domain, competencies, tools_available,
                     tools_wishlist, design_patterns, sources, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id, domain,
                    json.dumps(competencies), json.dumps(tools_available),
                    json.dumps(tools_wishlist), json.dumps(design_patterns),
                    json.dumps(sources), _now(),
                ),
            )
            report_id = int(cur.lastrowid)
            conn.execute(
                "UPDATE spawn_tasks SET research_report_id = ? WHERE id = ?",
                (report_id, task_id),
            )
        return report_id

    def get_research_report(self, report_id: int) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM research_reports WHERE id = ?", (report_id,)
            ).fetchone()
        if not row:
            return None
        result = dict(row)
        for field in _JSON_LIST_FIELDS:
            result[field] = json.loads(result[field]) if result[field] else []
        return result

    def find_recent_research(self, role_description: str, within_hours: int = 24) -> dict | None:
        """
        Return the most recent research_reports row for a near-duplicate
        role_description created within the window, or None. Powers Tier 1's
        cache-hit path so re-spawning a near-identical role doesn't re-burn
        an LLM call. "Near-duplicate" means equal after _normalize_query()
        (lowercase, whitespace-collapsed) — not fuzzy matching.
        """
        target = _normalize_query(role_description)
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=within_hours)).isoformat()
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT r.*, t.role_description AS _role_description
                FROM research_reports r
                JOIN spawn_tasks t ON t.id = r.spawn_task_id
                WHERE r.created_at >= ?
                ORDER BY r.created_at DESC
                """,
                (cutoff,),
            ).fetchall()
        for row in rows:
            if _normalize_query(row["_role_description"]) == target:
                result = dict(row)
                del result["_role_description"]
                for field in _JSON_LIST_FIELDS:
                    result[field] = json.loads(result[field]) if result[field] else []
                return result
        return None

    # ── Spawned agents ───────────────────────────────────────────────────────

    def slug_taken(self, slug: str) -> bool:
        if slug in RESERVED_SLUGS:
            return True
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM spawned_agents WHERE slug = ? AND status = 'active'", (slug,)
            ).fetchone()
        return row is not None

    def approve(self, task_id: int) -> int:
        """
        Promote an AWAITING_APPROVAL task to a live spawned_agents row.
        Raises ValueError if the task isn't in that state or its slug
        collides with a reserved/existing one — callers should catch this
        and surface it as a rejection reason rather than a crash.
        """
        task = self.get_spawn_task(task_id)
        if not task or task["status"] != AWAITING_APPROVAL:
            raise ValueError(f"spawn task {task_id} is not awaiting approval")
        slug = task["slug"]
        if not slug or self.slug_taken(slug):
            raise ValueError(f"slug {slug!r} is reserved or already in use")

        with self._connect() as conn:
            self._check_transition(conn, task_id, APPROVED)
            cur = conn.execute(
                """
                INSERT INTO spawned_agents
                    (slug, name, system_prompt, tool_allowlist, status, created_by_task_id, created_at)
                VALUES (?, ?, ?, ?, 'active', ?, ?)
                """,
                (slug, slug, task["system_prompt"], task["tool_allowlist"], task_id, _now()),
            )
            agent_id = int(cur.lastrowid)
            conn.execute(
                "UPDATE spawn_tasks SET status = ?, updated_at = ? WHERE id = ?",
                (APPROVED, _now(), task_id),
            )
        return agent_id

    def reject(self, task_id: int, feedback: str) -> str:
        """
        Reject with feedback. Returns the resulting status: PENDING (another
        revision pass is allowed) or FAILED (revision cap of 3 hit).
        """
        MAX_REVISIONS = 3
        new_count = self.record_rejection(task_id, feedback)
        if new_count >= MAX_REVISIONS:
            self.update_status(task_id, FAILED, failure_reason="max revision rounds exceeded")
            return FAILED
        return PENDING

    def list_active_agents(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM spawned_agents WHERE status = 'active' ORDER BY id"
            ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["tool_allowlist"] = json.loads(d["tool_allowlist"]) if d["tool_allowlist"] else []
            result.append(d)
        return result

    def disable_agent(self, slug: str) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE spawned_agents SET status = 'disabled' WHERE slug = ?", (slug,))
