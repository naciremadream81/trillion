"""
Security response headers + CSP — agent-security.md §2.2.

An aiohttp middleware that stamps every response with the headers that
don't require per-route judgment: clickjacking, MIME-sniffing, referrer
leakage, and a locked-down Permissions-Policy that only allows what the
voice UI actually uses (microphone + autoplay, both same-origin, per
index.html's push-to-talk + TTS playback).

CSP ships report-only by default, per the standing constraint ("ship CSP in
report-only mode first, never enforcing on the first deploy"). Enforcing is
opt-in via TRILLION_CSP_ENFORCE, and when it's on *both* headers go out: the
enforcing one stops the violation, the report-only one keeps the reports
flowing so a policy that's too tight is visible rather than silent.

The flip is config, not code, on purpose — the evidence for making it lives
in agent/security/csp_reports.py, which is what the report endpoint now
writes to. Read `GET /api/security/csp-violations`, widen the policy by what
actually got blocked, and only then set the flag. Do not flip it on a guess;
an over-tight enforcing policy breaks the UI in ways that look like
unrelated bugs. 'unsafe-eval' is never included, also per standing constraint.
'unsafe-inline' is retained for script-src/style-src because index.html is
a single-file UI shell with inline <script>/<style> blocks — moving to
nonces is a larger refactor, a known gap rather than solved here.

Three.js now serves from /vendor/ (see: vendor Three.js instead of unpkg
CDN), so script-src stays same-origin-only — no third-party CDN needed.
Google Fonts is the one real external origin the UI uses
(index.html:7-9), so style-src/font-src allowlist it explicitly rather
than widening to '*'.
"""

from __future__ import annotations

from aiohttp import web

CSP_POLICY = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline'; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
    "font-src 'self' data: https://fonts.gstatic.com; "
    "img-src 'self' data: blob:; "
    "media-src 'self' blob:; "
    "connect-src 'self' ws: wss:; "
    "frame-ancestors 'none'; "
    "base-uri 'self'; "
    "form-action 'self'; "
    "object-src 'none'; "
    "report-uri /api/security/csp-report; "
    "report-to csp-endpoint"
)

# The policy is one string used in both modes, so the old name is now only
# half-true. Kept as an alias rather than churned through every call site.
CSP_REPORT_ONLY_POLICY = CSP_POLICY

REPORTING_ENDPOINTS_HEADER = 'csp-endpoint="/api/security/csp-report"'

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "X-Frame-Options": "DENY",
    "Permissions-Policy": (
        "microphone=(self), autoplay=(self), camera=(), geolocation=(), "
        "interest-cohort=()"
    ),
}


def apply_security_headers(headers, enforce: bool = False) -> None:
    """
    Stamp the fixed headers plus the CSP onto a MutableMapping of response
    headers (aiohttp's CIMultiDict, or a plain dict in tests). Split out of
    the middleware below so it's testable without an aiohttp app or event
    loop.

    The report-only header goes out in both modes. That is deliberate: with
    enforcement on, a directive that is too tight would otherwise fail
    silently in the browser console, and the whole point of keeping the
    report sink alive is that a bad policy stays observable after the flip,
    not just before it.
    """
    for name, value in SECURITY_HEADERS.items():
        headers[name] = value
    headers["Content-Security-Policy-Report-Only"] = CSP_POLICY
    if enforce:
        headers["Content-Security-Policy"] = CSP_POLICY
    headers["Reporting-Endpoints"] = REPORTING_ENDPOINTS_HEADER


def security_headers_middleware_factory(enforce: bool = False):
    """
    Build the middleware bound to an enforcement mode. A factory for the
    same reason bearer_auth_middleware is one: the setting varies per app
    instance, and the tests build several apps with different ones.
    """

    @web.middleware
    async def middleware(request: web.Request, handler):
        response = await handler(request)
        apply_security_headers(response.headers, enforce)
        return response

    return middleware


# Report-only middleware, kept as a module-level name because several tests
# and serve.py's original wiring reference it directly.
security_headers_middleware = security_headers_middleware_factory(False)
