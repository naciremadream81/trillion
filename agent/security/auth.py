"""
Bearer-token auth middleware — the request-time counterpart to
startup_guard.py's bind-time check.

startup_guard.check_bind_safety() only checks TRILLION_WEB_AUTH_TOKEN once,
at process startup, to decide whether serve.py may bind a non-loopback host
at all. Nothing validated the token on an actual request until this module —
docs/incident-runbook.md's TRILLION_WEB_AUTH_TOKEN section and the security
shield's csrf-origin-gate signal (agent/security/audit.py) both flagged the
same honest gap: rotating the token used to revoke nothing, because nothing
checked it per-request.

Scope: protects everything under /api/ except the CSP report-only endpoint,
which the browser's Reporting API POSTs automatically with no way to attach
a custom header. The static UI shell (/, /index.html, /vendor/) stays
public — it's markup and vendored JS, not a capability — so the page still
loads; whoever calls /api/chat (etc.) from a non-loopback deployment is
responsible for attaching the header themselves.

When settings.web_auth_token is empty (the loopback-only default), this
middleware is a no-op — mirrors audit.py's _bearer_token() "not required
(loopback)" signal.
"""

from __future__ import annotations

import hmac

from aiohttp import web

PROTECTED_PREFIX = "/api/"
EXEMPT_PATHS = frozenset({"/api/security/csp-report"})


def is_authorized(headers, token: str) -> bool:
    """
    True if `headers` (a Mapping — aiohttp's CIMultiDict or a plain dict in
    tests) carries a matching `Authorization: Bearer <token>` header. An
    empty token means auth isn't configured at all, so every request is
    authorized — split out like headers.py's apply_security_headers() so
    the logic is testable without an aiohttp Request or event loop.
    """
    if not token:
        return True
    scheme, _, value = headers.get("Authorization", "").partition(" ")
    return scheme == "Bearer" and hmac.compare_digest(value, token)


def bearer_auth_middleware(token: str):
    """
    Build a middleware bound to `token` (settings.web_auth_token at app
    build time). A factory rather than a plain @web.middleware coroutine
    like security_headers_middleware because the token varies per app
    instance (tests build several apps with different tokens).
    """

    @web.middleware
    async def middleware(request: web.Request, handler):
        path = request.path
        if path.startswith(PROTECTED_PREFIX) and path not in EXEMPT_PATHS:
            if not is_authorized(request.headers, token):
                return web.json_response({"error": "unauthorized"}, status=401)
        return await handler(request)

    return middleware
