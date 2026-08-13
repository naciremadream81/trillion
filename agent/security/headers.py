"""
Security response headers + CSP — agent-security.md §2.2.

An aiohttp middleware that stamps every response with the headers that
don't require per-route judgment: clickjacking, MIME-sniffing, referrer
leakage, and a locked-down Permissions-Policy that only allows what the
voice UI actually uses (microphone + autoplay, both same-origin, per
index.html's push-to-talk + TTS playback).

CSP ships as Content-Security-Policy-Report-Only, never enforcing, per the
standing constraint ("ship CSP in report-only mode first, never enforcing
on the first deploy") — flipping to enforcing is a manual step once a full
session shows zero violations in the /api/security/csp-report log (see
serve.py). 'unsafe-eval' is never included, also per standing constraint.
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

CSP_REPORT_ONLY_POLICY = (
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


def apply_security_headers(headers) -> None:
    """
    Stamp the fixed headers plus the report-only CSP onto a MutableMapping
    of response headers (aiohttp's CIMultiDict, or a plain dict in tests).
    Split out of the middleware below so it's testable without an aiohttp
    app or event loop.
    """
    for name, value in SECURITY_HEADERS.items():
        headers[name] = value
    headers["Content-Security-Policy-Report-Only"] = CSP_REPORT_ONLY_POLICY
    headers["Reporting-Endpoints"] = REPORTING_ENDPOINTS_HEADER


@web.middleware
async def security_headers_middleware(request: web.Request, handler):
    response = await handler(request)
    apply_security_headers(response.headers)
    return response
