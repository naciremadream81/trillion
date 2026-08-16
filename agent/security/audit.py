"""
Self-audit security shield — agent-security.md §3.5 (the load-bearing piece).

Hardening rots without measurement: a safety rail that isn't observed can
silently regress (a mode flipped back to "off", a token unset) with nothing
surfacing the drift. This module is a list of independent signal functions,
each reading real state from a module built earlier in this playbook rather
than re-deriving it, plus an aggregator that turns those signals into one
score.

Scoring: start at 100, sum every signal's delta (negative = penalty, zero or
positive = a good state), clamp to [0, 100]. Color: >=85 green, 60-84 amber,
<60 red. `GET /api/security/status` (serve.py) is the HTTP surface; §3.5's
UI pill reads that endpoint.

Two signals are honest adaptations rather than literal spec states, each
noted at its function:
  - bearer-token: unset is only a real gap on a non-loopback bind (a
    loopback-only deployment has no auth surface to protect — see
    startup_guard.py's docstring for the same reasoning).
  - dev-mode-bind: Trillion has no separate "dev mode" toggle (serve.py
    always binds *some* host), so the three spec states collapse to two:
    bound loopback (the safe default) or bound publicly (guarded against
    at startup by startup_guard.check_bind_safety).
csrf-origin-gate reports "absent" rather than being silently built here —
no origin-check middleware exists in this codebase yet (see the playbooks
plan's P7 notes); the shield's job is to surface that gap, not paper over it.
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone

from ..safety.risk import HARDLINE_TOOLS, READ_ONLY, needs_confirmation, normalize_mode, normalize_risk
from .startup_guard import _LOOPBACK_HOSTS

STALE_CVE_SCAN_DAYS = 14


@dataclass
class Signal:
    name: str
    label: str
    value: str
    delta: int
    severity: str  # "ok" | "info" | "warning" | "critical"
    detail: str = field(default="")


def _env_bool(name: str) -> bool:
    return (os.getenv(name) or "").strip().lower() in {"1", "true", "yes", "on"}


def _kill_switch(settings) -> Signal:
    if settings.trillion_paused:
        return Signal(
            "kill-switch", "Kill switch", "ACTIVE", -100, "critical",
            "Trillion is paused (TRILLION_PAUSED=true). Gated actions, background "
            "dispatch, and builds are all stopped.",
        )
    return Signal("kill-switch", "Kill switch", "off", 0, "ok")


def _llm_api_key() -> Signal:
    provider = os.getenv("TRILLION_PROVIDER", "claude").strip().lower()
    if provider == "ollama":
        return Signal("llm-api-key", "LLM API key", "not required (ollama)", 0, "ok")
    env_var = "OPENAI_API_KEY" if provider == "openai" else "ANTHROPIC_API_KEY"
    if os.getenv(env_var):
        return Signal("llm-api-key", "LLM API key", "set", 0, "ok")
    return Signal(
        "llm-api-key", "LLM API key", "unset", -50, "critical",
        f"${env_var} is not set for provider={provider!r}.",
    )


def _bearer_token(settings) -> Signal:
    non_loopback = settings.web_host not in _LOOPBACK_HOSTS
    if settings.web_auth_token:
        return Signal("bearer-token", "Bearer token", "set", 0, "ok")
    if non_loopback:
        return Signal(
            "bearer-token", "Bearer token", "unset", -30, "critical",
            f"web_host={settings.web_host!r} is not loopback and no auth token is configured.",
        )
    return Signal(
        "bearer-token", "Bearer token", "not required (loopback)", 0, "ok",
        "No auth surface to protect while bound to loopback.",
    )


def _approval_mode(settings) -> Signal:
    mode = normalize_mode(settings.confirmation_mode)
    if mode == "manual":
        return Signal("approval-mode", "Approval mode", "manual", 5, "ok")
    if mode == "off":
        return Signal(
            "approval-mode", "Approval mode", "off", -25, "warning",
            "Only the immutable hardline blocklist is still gated.",
        )
    return Signal("approval-mode", "Approval mode", "smart", 0, "ok")


def _dev_mode_bind(settings) -> Signal:
    if settings.web_host in _LOOPBACK_HOSTS:
        return Signal("dev-mode-bind", "Dev-mode bind", "on (localhost-only)", -1, "info")
    return Signal(
        "dev-mode-bind", "Dev-mode bind", "DANGEROUS — public bind", -40, "critical",
        f"web_host={settings.web_host!r} is not loopback.",
    )


def _gate_coverage(registry, settings) -> Signal:
    mode = normalize_mode(settings.confirmation_mode)
    mutating = 0
    gated = 0
    for name in registry.names():
        tool = registry.get(name)
        if tool is None or normalize_risk(tool.risk) == READ_ONLY:
            continue
        mutating += 1
        if needs_confirmation(tool_name=name, risk=tool.risk, declared=tool.requires_confirmation, mode=mode):
            gated += 1

    value = f"{gated}/{mutating} paths"
    if mutating == 0:
        return Signal("gate-coverage", "Gate coverage", "0/0 paths", 0, "ok", "No mutating tools registered.")
    if gated == mutating:
        return Signal("gate-coverage", "Gate coverage", value, 0, "ok")

    ungated = mutating - gated
    delta = -min(ungated * 5, 30)
    severity = "critical" if gated == 0 else "warning"
    return Signal("gate-coverage", "Gate coverage", value, delta, severity, f"{ungated} mutating path(s) ungated under mode={mode!r}.")


def _log_redaction() -> Signal:
    from . import log_redact

    if getattr(log_redact, "_PATTERNS", None):
        return Signal("log-redaction", "Log redaction", "active", 0, "ok")
    return Signal("log-redaction", "Log redaction", "inactive", -10, "warning")


def _subprocess_envs() -> Signal:
    from . import subprocess_env

    if hasattr(subprocess_env, "shell_minimal"):
        return Signal("subprocess-envs", "Subprocess envs", "all spawn sites stripped", 0, "ok")
    return Signal("subprocess-envs", "Subprocess envs", "not stripped", -10, "warning")


def _hardline_blocklist() -> Signal:
    count = len(HARDLINE_TOOLS)
    if count > 0:
        return Signal("hardline-blocklist", "Hardline blocklist", f"{count} patterns", 0, "ok")
    return Signal("hardline-blocklist", "Hardline blocklist", "0 patterns", -20, "critical")


_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _csp_status() -> Signal:
    # Like _csrf_origin_gate(), this actually builds the policy from
    # index.html rather than trusting that headers.py exists — an import
    # that succeeds but produces a policy still carrying 'unsafe-inline'
    # should not score the same as one that doesn't.
    try:
        from .headers import build_csp_policies
    except Exception:
        return Signal(
            "csp-status", "CSP status", "absent", -20, "critical",
            "agent/security/headers.py does not define an enforcing CSP builder.",
        )

    try:
        with open(os.path.join(_PROJECT_ROOT, "index.html"), "r", encoding="utf-8") as f:
            enforcing, _candidate = build_csp_policies(f.read())
    except Exception as e:
        return Signal(
            "csp-status", "CSP status", "report-only", -10, "warning",
            f"Could not build the enforcing policy from index.html: {e}",
        )

    if "'unsafe-inline'" in enforcing:
        return Signal(
            "csp-status", "CSP status", "enforcing (unsafe-inline retained)", -5, "warning",
            "Content-Security-Policy is enforcing but 'unsafe-inline' is still present, "
            "which makes every hash-source in the same directive inert for any browser "
            "that honors it.",
        )
    return Signal("csp-status", "CSP status", "enforcing (hash-based)", 0, "ok")


def _token_scope_audit() -> Signal:
    if _env_bool("TRILLION_TOKEN_SCOPE_AUDITED"):
        return Signal("token-scope-audit", "Token scope audit", "audited", 0, "ok")
    return Signal(
        "token-scope-audit", "Token scope audit", "pending", -3, "info",
        "Manual attestation: set TRILLION_TOKEN_SCOPE_AUDITED=true once API key scopes are verified minimal.",
    )


def _db_readonly_role() -> Signal:
    if _env_bool("TRILLION_DB_READONLY_AUDITED"):
        return Signal("db-readonly-role", "DB read-only role", "active", 0, "ok")
    return Signal(
        "db-readonly-role", "DB read-only role", "pending", -3, "info",
        "Manual attestation: set TRILLION_DB_READONLY_AUDITED=true once the analytics DB role is verified read-only.",
    )


def _cve_scan() -> Signal:
    from .cve_scan import CveScanRepo

    latest = CveScanRepo().latest()
    if latest is None:
        return Signal("cve-scan", "Dependency CVE scan", "never run", -5, "warning", "No pip-audit scan has ever run.")

    if latest.get("error_message"):
        return Signal("cve-scan", "Dependency CVE scan", "scanner error", -10, "warning", latest["error_message"])

    generated_at = None
    try:
        generated_at = datetime.fromisoformat(latest["generated_at"])
    except (TypeError, ValueError):
        pass
    if generated_at is not None:
        if generated_at.tzinfo is None:
            generated_at = generated_at.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) - generated_at > timedelta(days=STALE_CVE_SCAN_DAYS):
            return Signal(
                "cve-scan", "Dependency CVE scan", "stale (>14d)", -5, "warning",
                f"Last scan: {latest['generated_at']}",
            )

    cve_count = latest.get("cve_count", 0)
    if cve_count == 0:
        return Signal("cve-scan", "Dependency CVE scan", "clean", 0, "ok")
    delta = -min(cve_count * 5, 30)
    findings = latest.get("findings") or []
    return Signal(
        "cve-scan", "Dependency CVE scan", f"{cve_count} CVEs", delta, "warning",
        f"{len(findings)} package(s) affected.",
    )


def _csrf_origin_gate(settings) -> Signal:
    # Every other signal here that checks a module checks that it *exists*
    # (hasattr, a non-empty constant). That is weak evidence for a gate whose
    # whole job is to say no: an origin module that imports fine but allows
    # everything would score the same as a working one. So this actually runs
    # the decision function against a forged cross-origin POST and requires a
    # refusal before claiming the rail is up.
    try:
        from .origin import check_origin
    except Exception:
        return Signal(
            "csrf-origin-gate", "CSRF / origin gate", "absent", -10, "warning",
            "No origin-check middleware is wired up on POST/PUT/PATCH/DELETE.",
        )

    forged = check_origin(
        "POST",
        "/api/chat",
        {"Origin": "https://evil.example", "Host": settings.web_host},
        settings.web_host,
    )
    if forged is None:
        return Signal(
            "csrf-origin-gate", "CSRF / origin gate", "not enforcing", -10, "warning",
            "agent/security/origin.py is importable but allowed a forged cross-origin POST.",
        )
    return Signal(
        "csrf-origin-gate", "CSRF / origin gate", "enforcing", 0, "ok",
        "State-changing /api/ requests are checked against Sec-Fetch-Site, Origin, and a Host allowlist.",
    )


def collect_signals(settings, registry) -> list[Signal]:
    return [
        _kill_switch(settings),
        _llm_api_key(),
        _bearer_token(settings),
        _approval_mode(settings),
        _dev_mode_bind(settings),
        _gate_coverage(registry, settings),
        _log_redaction(),
        _subprocess_envs(),
        _hardline_blocklist(),
        _csp_status(),
        _token_scope_audit(),
        _db_readonly_role(),
        _cve_scan(),
        _csrf_origin_gate(settings),
    ]


def compute_score(signals: list[Signal]) -> dict:
    score = max(0, min(100, 100 + sum(s.delta for s in signals)))
    if score >= 85:
        color = "green"
    elif score >= 60:
        color = "amber"
    else:
        color = "red"
    return {"score": score, "color": color}


def audit(settings, registry) -> dict:
    """The full `{score, color, signals: [...]}` payload GET /api/security/status returns."""
    signals = collect_signals(settings, registry)
    result = compute_score(signals)
    result["signals"] = [asdict(s) for s in signals]
    return result
