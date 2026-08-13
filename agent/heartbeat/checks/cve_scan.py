"""
Weekly dependency CVE scan, riding the heartbeat scheduler's per-check
cadence (agent/heartbeat/scheduler.py) instead of a separate cron — this is
the tick machinery P6 built generically so P7/P11 checks like this one
don't need their own loop. See agent-security.md §3.4.

The scan itself and its full findings are persisted separately to
`cve_scans.db` via CveScanRepo (agent/security/cve_scan.py) — that's the
audit trail `/api/security/cve-status` reads. This Check only decides
whether the *result* of a scan is worth surfacing as a heartbeat notice,
and dedups on cve_count in its cursor so a stable set of known CVEs
doesn't renotify every week.
"""

from __future__ import annotations

from .base import Notice
from ...security.cve_scan import CveScanRepo, run_pip_audit

WEEKLY_SECONDS = 7 * 24 * 3600.0


class CveScanCheck:
    name = "cve_scan"

    def __init__(self, cadence_seconds: float = WEEKLY_SECONDS, repo: CveScanRepo | None = None) -> None:
        self.cadence_seconds = cadence_seconds
        self._repo = repo or CveScanRepo()

    async def run(self, cursor: dict) -> tuple[list[Notice], dict]:
        result = await run_pip_audit()
        self._repo.save(result)

        if result.error_message:
            return [], cursor

        new_cursor = dict(cursor)
        last_count = new_cursor.get("last_notified_count")
        notices: list[Notice] = []
        if result.cve_count > 0 and result.cve_count != last_count:
            packages = ", ".join(f["package"] for f in result.findings[:5])
            notices.append(
                Notice(
                    severity="warning",
                    message=(
                        f"pip-audit found {result.cve_count} known CVE(s) across this project's "
                        f"dependencies (including {packages}). Unpatched dependency CVEs are the "
                        f"most common real-world entry point. I'd run `pip-audit` locally and "
                        f"upgrade the flagged packages."
                    ),
                )
            )
        new_cursor["last_notified_count"] = result.cve_count
        return notices, new_cursor
