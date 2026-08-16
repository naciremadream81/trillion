"""
Dependency CVE scanning — agent-security.md §3.4.

Wraps `pip-audit` as a subprocess (env from subprocess_env.shell_minimal() —
a dependency scanner has no legitimate use for ANTHROPIC_API_KEY,
GITHUB_TOKEN, or any other Trillion secret) and persists every scan to a
`cve_scans` table so `GET /api/security/cve-status` always has a last-known
answer, even between scans.

pip-audit's own exit code is *not* an error signal here: it returns 1 when
vulnerabilities are found (the interesting case) and 0 when clean. The only
real failure modes are the binary being missing (`FileNotFoundError`) and
unparseable output — both are recorded as a scan row with `error_message`
set rather than raised, per the spec ("a 'scanner not installed' outcome is
recorded as an audit-row... not a crash"), so the shield indicator (§3.5)
always has something to read.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .subprocess_env import shell_minimal


def _pip_audit_binary() -> str:
    """Resolve pip-audit next to the running interpreter when possible.

    serve.py is normally launched as `.venv/bin/python serve.py` — the venv
    is never *activated*, so `.venv/bin` isn't on PATH and a bare
    `asyncio.create_subprocess_exec("pip-audit", ...)` reliably fails with
    FileNotFoundError even though pip-audit is installed right next to the
    interpreter that's running this code. Falls back to a bare PATH lookup
    for environments where it's installed globally instead of in a venv.
    """
    candidate = os.path.join(os.path.dirname(sys.executable), "pip-audit")
    return candidate if os.path.exists(candidate) else "pip-audit"

SCHEMA = """
CREATE TABLE IF NOT EXISTS cve_scans (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    cve_count      INTEGER NOT NULL,
    findings_json  TEXT    NOT NULL DEFAULT '[]',
    scanner_version TEXT,
    error_message  TEXT,
    generated_at   TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cve_scans_generated_at ON cve_scans(generated_at);
"""


def default_db_path() -> str:
    """Where scan history lives. Override with $TRILLION_CVE_SCAN_DB."""
    return os.getenv("TRILLION_CVE_SCAN_DB", "cve_scans.db")


@dataclass
class CveScanResult:
    cve_count: int
    findings: list[dict] = field(default_factory=list)
    scanner_version: str | None = None
    error_message: str | None = None
    generated_at: str = ""

    def __post_init__(self) -> None:
        if not self.generated_at:
            self.generated_at = datetime.now(timezone.utc).isoformat()


async def run_pip_audit(timeout_seconds: float = 120.0) -> CveScanResult:
    """Run `pip-audit --format json` against the current environment.

    Never raises: a missing binary, a timeout, or unparseable output all
    come back as a CveScanResult with error_message set and cve_count=0.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            _pip_audit_binary(),
            "--format",
            "json",
            "--progress-spinner",
            "off",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=shell_minimal(),
        )
    except FileNotFoundError:
        return CveScanResult(cve_count=0, error_message="pip-audit is not installed")

    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_seconds)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return CveScanResult(cve_count=0, error_message=f"pip-audit timed out after {timeout_seconds}s")

    # pip-audit exits 1 when it finds vulnerabilities — that's success, not
    # an error, so exit code alone never gates parsing.
    try:
        payload = json.loads(stdout.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        detail = stderr.decode("utf-8", errors="replace").strip()
        return CveScanResult(
            cve_count=0,
            error_message=f"could not parse pip-audit output: {detail[:500]}" if detail else "could not parse pip-audit output",
        )

    dependencies = payload.get("dependencies", []) if isinstance(payload, dict) else []
    findings = [
        {"package": dep.get("name"), "version": dep.get("version"), "vulns": dep.get("vulns", [])}
        for dep in dependencies
        if dep.get("vulns")
    ]
    cve_count = sum(len(dep["vulns"]) for dep in findings)
    return CveScanResult(cve_count=cve_count, findings=findings)


class CveScanRepo:
    """Persists CveScanResults and answers 'what's the latest scan'."""

    def __init__(self, db_path: str | None = None) -> None:
        self.db_path = db_path or default_db_path()
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    def save(self, result: CveScanResult) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO cve_scans
                    (cve_count, findings_json, scanner_version, error_message, generated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    result.cve_count,
                    json.dumps(result.findings),
                    result.scanner_version,
                    result.error_message,
                    result.generated_at,
                ),
            )
            return int(cur.lastrowid)

    def latest(self) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM cve_scans ORDER BY generated_at DESC, id DESC LIMIT 1"
            ).fetchone()
        if row is None:
            return None
        return {
            "cve_count": row["cve_count"],
            "findings": json.loads(row["findings_json"]),
            "scanner_version": row["scanner_version"],
            "error_message": row["error_message"],
            "generated_at": row["generated_at"],
        }


async def scan_and_persist(repo: CveScanRepo | None = None) -> dict:
    """Run a fresh scan and persist it. Returns the same shape as `latest()`."""
    repo = repo or CveScanRepo()
    result = await run_pip_audit()
    repo.save(result)
    return {
        "cve_count": result.cve_count,
        "findings": result.findings,
        "scanner_version": result.scanner_version,
        "error_message": result.error_message,
        "generated_at": result.generated_at,
    }
