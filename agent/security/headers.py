"""
Security response headers + CSP — agent-security.md §2.2.

An aiohttp middleware that stamps every response with the headers that
don't require per-route judgment: clickjacking, MIME-sniffing, referrer
leakage, and a locked-down Permissions-Policy that only allows what the
voice UI actually uses (microphone + autoplay, both same-origin, per
index.html's push-to-talk + TTS playback).

## CSP: enforcing + a stricter report-only candidate

CSP now ships enforcing (`Content-Security-Policy`), not just report-only.
The standing constraint this replaces — "report-only first, enforcing is a
manual flip once a session shows zero violations" — is why this took a
separate step rather than landing with the original report-only policy: a
real session's csp-report log was the gate, and it's clean.

'unsafe-eval' is never included, per standing constraint, unchanged.
'unsafe-inline' for script-src/style-src is now *replaced* rather than kept:
index.html is a single-file UI shell served via `web.FileResponse` with no
per-request templating, so nonces don't fit (a nonce must be freshly
generated and stamped into the markup on every response), but the inline
<script>/<style> blocks are static, and their SHA-256 hashes are not — see
`inline_asset_hashes()` and `build_csp_policies()` below. This is real
enforcement: an injected inline `<script>` a nonce/hash doesn't recognize is
blocked, not just reported.

Content-Security-Policy-Report-Only now carries a *stricter candidate*
instead of a duplicate of the enforcing policy — the next tightening step's
data source. It drops `data:`/`blob:` from img-src and `data:` from
font-src, which index.html's markup doesn't currently use (its one Blob use,
the TTS playback path, is in media-src and stays). If that holds under real
traffic, the enforcing policy can drop them too in a follow-up; if it
doesn't, the report tells us why before anything breaks.

connect-src drops the `ws:`/`wss:` scheme-sources the old report-only policy
carried: index.html opens zero websockets (chat streams over a
`StreamResponse`, not a socket — see serve.py's `chat()`), and a
scheme-source with no host restriction permits *any* host on that scheme,
which is the single biggest thing enforcing mode was supposed to fix.

Three.js now serves from /vendor/ (see: vendor Three.js instead of unpkg
CDN), so script-src stays same-origin-only plus the inline hashes — no
third-party CDN origin needed. Google Fonts is the one real external origin
the UI uses (index.html:7-9), so style-src/font-src allowlist it explicitly
rather than widening to '*'.
"""

from __future__ import annotations

import base64
import hashlib
import re

from aiohttp import web

# Matches a browser's own hash-source algorithm: the exact bytes between the
# tags, nothing trimmed or normalized. <script src="..."> (external, none
# currently in index.html) is excluded — hashing its src attribute would
# produce a hash that matches nothing a browser ever computes.
_INLINE_STYLE_RE = re.compile(rb"<style\b[^>]*>(.*?)</style>", re.DOTALL | re.IGNORECASE)
_INLINE_SCRIPT_RE = re.compile(rb"<script\b([^>]*)>(.*?)</script>", re.DOTALL | re.IGNORECASE)
_SRC_ATTR_RE = re.compile(rb"\bsrc\s*=", re.IGNORECASE)

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


def _sha256_source(content: bytes) -> str:
    digest = hashlib.sha256(content).digest()
    return "'sha256-" + base64.b64encode(digest).decode("ascii") + "'"


def inline_asset_hashes(index_html: str) -> tuple[frozenset[str], frozenset[str]]:
    """
    CSP hash-sources for every inline <script>/<style> block in index.html —
    (script_hashes, style_hashes). `<script type="importmap">` is included:
    CSP's script-src governs it the same as any other inline script.

    Takes the file's raw text rather than a path so this stays a pure
    function callable from a unit test with no filesystem fixture.
    """
    raw = index_html.encode("utf-8")
    style_hashes = frozenset(_sha256_source(m.group(1)) for m in _INLINE_STYLE_RE.finditer(raw))
    script_hashes = frozenset(
        _sha256_source(m.group(2))
        for m in _INLINE_SCRIPT_RE.finditer(raw)
        if not _SRC_ATTR_RE.search(m.group(1))
    )
    return script_hashes, style_hashes


def build_csp_policies(index_html: str) -> tuple[str, str]:
    """
    (enforcing_policy, stricter_candidate_policy) — see the module
    docstring's "CSP: enforcing + a stricter report-only candidate" section
    for what each one is and why they differ.
    """
    script_hashes, style_hashes = inline_asset_hashes(index_html)
    script_src = " ".join(["'self'", *sorted(script_hashes)])
    style_src = " ".join(["'self'", *sorted(style_hashes), "https://fonts.googleapis.com"])

    enforcing = (
        "default-src 'self'; "
        f"script-src {script_src}; "
        f"style-src {style_src}; "
        "font-src 'self' data: https://fonts.gstatic.com; "
        "img-src 'self' data: blob:; "
        "media-src 'self' blob:; "
        "connect-src 'self'; "
        "frame-ancestors 'none'; "
        "base-uri 'self'; "
        "form-action 'self'; "
        "object-src 'none'; "
        "report-uri /api/security/csp-report; "
        "report-to csp-endpoint"
    )
    candidate = (
        "default-src 'self'; "
        f"script-src {script_src}; "
        f"style-src {style_src}; "
        "font-src 'self' https://fonts.gstatic.com; "
        "img-src 'self'; "
        "media-src 'self' blob:; "
        "connect-src 'self'; "
        "frame-ancestors 'none'; "
        "base-uri 'self'; "
        "form-action 'self'; "
        "object-src 'none'; "
        "report-uri /api/security/csp-report; "
        "report-to csp-endpoint"
    )
    return enforcing, candidate


def apply_security_headers(headers, csp_enforcing: str, csp_report_only: str) -> None:
    """
    Stamp the fixed headers plus both CSP headers onto a MutableMapping of
    response headers (aiohttp's CIMultiDict, or a plain dict in tests).
    Split out of the middleware below so it's testable without an aiohttp
    app or event loop.

    Takes the two policies as arguments rather than reading a module
    constant because they're derived from index.html's actual bytes
    (`build_csp_policies`) — computed once at app-build time, not
    hand-maintained here as a string that could drift from the markup.
    """
    for name, value in SECURITY_HEADERS.items():
        headers[name] = value
    headers["Content-Security-Policy"] = csp_enforcing
    headers["Content-Security-Policy-Report-Only"] = csp_report_only
    headers["Reporting-Endpoints"] = REPORTING_ENDPOINTS_HEADER


def security_headers_middleware(csp_enforcing: str, csp_report_only: str):
    """
    Build a middleware bound to the two CSP policies computed at app-build
    time — a factory for the same reason bearer_auth_middleware() and
    origin_check_middleware() are: the value varies per app instance, and
    tests build several apps against different index.html fixtures.
    """

    @web.middleware
    async def middleware(request: web.Request, handler):
        response = await handler(request)
        apply_security_headers(response.headers, csp_enforcing, csp_report_only)
        return response

    return middleware
