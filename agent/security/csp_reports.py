"""
Durable CSP-violation store — agent-security.md §2.2.

The playbook's method for getting to an enforcing CSP is explicit: ship
report-only, run every code path for a full session, read the resulting
violations, and *widen the policy only by what actually got blocked, not by
what you guessed*. That method needs violations you can read back. Until
this module, `POST /api/security/csp-report` handed its line to `print()`,
which on this host goes to a systemd unit whose journal does not persist —
so the collection step produced nothing, and the flip to enforcing could
never be made on evidence. That is the gap this closes.

Shape: one row per report, plus `directive_summary()`, which is the query
the decision actually needs — "which directives fired, how often, and
against which sources" — rather than a pile of raw lines to grep.

Reports arrive on the one endpoint exempt from both the bearer gate and the
origin gate (the browser posts them itself and we can't influence its
headers), so every field here is attacker-controlled. Consequences:

  - Bodies are stored already-truncated by the caller; nothing here grows
    without bound per row.
  - `prune()` caps total rows, because an attacker who can POST can
    otherwise fill the disk. The cap is generous — this is diagnostic data
    with a short useful life, not an audit log.
  - Nothing read out of here is ever fed to the model. It's operator data.
"""

from __future__ import annotations

import json
import os
import sqlite3

from .. import storage_utils
from dataclasses import dataclass, field
from datetime import datetime, timezone


SCHEMA = """
CREATE TABLE IF NOT EXISTS csp_violations (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    directive          TEXT    NOT NULL DEFAULT '',
    blocked_uri        TEXT    NOT NULL DEFAULT '',
    document_uri       TEXT    NOT NULL DEFAULT '',
    raw_body           TEXT    NOT NULL DEFAULT '',
    reported_at        TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_csp_violations_reported_at
    ON csp_violations(reported_at);
CREATE INDEX IF NOT EXISTS idx_csp_violations_directive
    ON csp_violations(directive);
"""

# Generous ceiling on stored rows. A real session produces a handful; this
# only bites when someone is deliberately POSTing garbage.
MAX_STORED_REPORTS = 5000


def default_db_path() -> str:
    """Where violation history lives. Override with $TRILLION_CSP_REPORT_DB."""
    return os.getenv("TRILLION_CSP_REPORT_DB", "csp_reports.db")


@dataclass
class CspViolation:
    directive: str = ""
    blocked_uri: str = ""
    document_uri: str = ""
    raw_body: str = ""
    reported_at: str = ""
    extra: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.reported_at:
            self.reported_at = datetime.now(timezone.utc).isoformat()


def parse_report(body: str) -> CspViolation:
    """
    Pull the three fields worth indexing out of a report body, keeping the
    (already truncated) raw text alongside.

    Handles both wire formats without caring which arrived: the legacy
    `report-uri` shape (`{"csp-report": {"violated-directive": ...}}`) and
    the Reporting API shape (a list of `{"type": "csp-violation", "body":
    {"effectiveDirective": ...}}`). A body that is not JSON at all, or is
    JSON of an unexpected shape, still produces a row — with empty fields
    and the raw text preserved. Losing a malformed report would hide
    exactly the violations most worth seeing.
    """
    violation = CspViolation(raw_body=body)
    try:
        parsed = json.loads(body)
    except (ValueError, TypeError):
        return violation

    candidates: list[dict] = []
    if isinstance(parsed, dict):
        if isinstance(parsed.get("csp-report"), dict):
            candidates.append(parsed["csp-report"])
        elif isinstance(parsed.get("body"), dict):
            candidates.append(parsed["body"])
        else:
            candidates.append(parsed)
    elif isinstance(parsed, list):
        for item in parsed:
            if isinstance(item, dict):
                candidates.append(item.get("body") if isinstance(item.get("body"), dict) else item)

    for report in candidates:
        if not isinstance(report, dict):
            continue
        directive = (
            report.get("effectiveDirective")
            or report.get("effective-directive")
            or report.get("violated-directive")
            or report.get("violatedDirective")
            or ""
        )
        if directive or not violation.directive:
            violation.directive = str(directive)[:200]
        blocked = report.get("blockedURL") or report.get("blocked-uri") or ""
        if blocked or not violation.blocked_uri:
            violation.blocked_uri = str(blocked)[:500]
        document = report.get("documentURL") or report.get("document-uri") or ""
        if document or not violation.document_uri:
            violation.document_uri = str(document)[:500]
        if violation.directive:
            break
    return violation


class CspReportRepo:
    """Persists CspViolations and answers 'what actually got blocked'."""

    def __init__(self, db_path: str | None = None) -> None:
        self.db_path = db_path or default_db_path()
        self._init_schema()

    def _connect(self):
        """
        Connection context manager — see agent/storage_utils.py.

        Was a bare `sqlite3.connect(...)` returned raw. Every call site wraps
        it in `with`, and sqlite3's own context manager commits without
        closing, so each request leaked a connection. Same call-site shape,
        with the close that was missing.
        """
        return storage_utils.connect(self.db_path)

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    def save(self, violation: CspViolation) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO csp_violations
                    (directive, blocked_uri, document_uri, raw_body, reported_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    violation.directive,
                    violation.blocked_uri,
                    violation.document_uri,
                    violation.raw_body,
                    violation.reported_at,
                ),
            )
            return int(cur.lastrowid)

    def count(self) -> int:
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS n FROM csp_violations").fetchone()
        return int(row["n"])

    def directive_summary(self, limit: int = 50) -> list[dict]:
        """
        The decision-shaped view: one entry per (directive, blocked source),
        with a count and when it was last seen, newest-first.

        This is what you read before widening the policy — each row is a
        concrete "this directive blocked this source N times", which either
        justifies an allowlist entry or identifies something that *should*
        stay blocked.
        """
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT directive, blocked_uri,
                       COUNT(*) AS hits,
                       MAX(reported_at) AS last_seen
                  FROM csp_violations
                 GROUP BY directive, blocked_uri
                 ORDER BY last_seen DESC, hits DESC
                 LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [
            {
                "directive": r["directive"],
                "blocked_uri": r["blocked_uri"],
                "hits": int(r["hits"]),
                "last_seen": r["last_seen"],
            }
            for r in rows
        ]

    def prune(self, keep: int = MAX_STORED_REPORTS) -> int:
        """Drop all but the newest `keep` rows. Returns how many were removed."""
        with self._connect() as conn:
            cur = conn.execute(
                """
                DELETE FROM csp_violations
                 WHERE id NOT IN (
                    SELECT id FROM csp_violations ORDER BY id DESC LIMIT ?
                 )
                """,
                (keep,),
            )
            return int(cur.rowcount or 0)


def record_report(body: str, repo: CspReportRepo | None = None) -> None:
    """
    Best-effort persist of one report body. Never raises — a violation
    report is diagnostic data arriving on an unauthenticated endpoint, and
    a storage problem must not turn into a 500 that an attacker can
    trigger at will.
    """
    try:
        repo = repo or CspReportRepo()
        repo.save(parse_report(body))
        if repo.count() > MAX_STORED_REPORTS:
            repo.prune()
    except Exception:
        pass
