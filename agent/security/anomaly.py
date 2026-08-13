"""
Per-tool anomaly detection — agent-security.md §3.2.

Tier 6's confirmation gate (agent/safety/approval.py) decides whether a
single call is *allowed*; it says nothing about *volume*. A model that gets
stuck in a loop calling forget_fact or write_project_file dozens of times in
a minute would sail through the gate every time if each call is individually
approved or below its risk threshold. This module is the ceiling underneath
the gate: an in-memory sliding window per tool name, checked in
ToolRegistry.run() (registry.py) before the tool actually executes.

Caps are keyed by Trillion's real tool names, not the generic categories
agent-security.md sketches (there's no literal send_email — draft_email is
draft-only by design, so it gets a generation-loop guard instead of a
send-volume one). Read-only tools (search_notes, web_search,
read_project_file, query_analytics, confirm_action) have no entry and are
never capped. forget_fact and run_project_tests are HARDLINE tools
(agent/safety/risk.py) — this gate adds a volume ceiling on top of the
hardline blocklist, it never loosens it.
"""

from __future__ import annotations

import time

DEFAULT_CAPS: dict[str, tuple[int, float]] = {
    # (limit, window_seconds)
    "forget_fact": (3, 86400.0),  # deletes memory data — tight daily cap
    "write_project_file": (30, 3600.0),
    "run_project_tests": (20, 86400.0),  # spawns a subprocess; already timeout-bounded
    "remember_fact": (30, 3600.0),
    "draft_email": (20, 3600.0),  # draft-only, but still a generation-loop guard
}


class AnomalyGate:
    """
    Tracks call timestamps per tool name and enforces a sliding-window cap.

    A blocked call is never recorded — the cap is a true ceiling that a
    tool can keep bumping against, not a one-strike lockout that disables
    the tool until a process restart.
    """

    def __init__(
        self,
        caps: dict[str, tuple[int, float]] | None = None,
        clock=time.monotonic,
    ) -> None:
        self._caps = caps if caps is not None else DEFAULT_CAPS
        self._clock = clock
        self._calls: dict[str, list[float]] = {}

    def check(self, tool_name: str) -> dict | None:
        """
        Returns None and records the call if tool_name is uncapped or under
        its cap. Returns {"count", "limit", "window_seconds"} without
        recording if the call would exceed the cap.
        """
        cap = self._caps.get(tool_name)
        if cap is None:
            return None
        limit, window_seconds = cap

        now = self._clock()
        history = self._calls.setdefault(tool_name, [])
        cutoff = now - window_seconds
        history[:] = [ts for ts in history if ts > cutoff]

        if len(history) >= limit:
            return {"count": len(history), "limit": limit, "window_seconds": window_seconds}

        history.append(now)
        return None
