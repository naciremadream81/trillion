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

Two hardening passes live here beyond the plain comparison:

  §2.1 rotation overlap — TRILLION_WEB_AUTH_TOKEN_PREV is accepted alongside
  the current token so a rotation doesn't have to be simultaneous across
  every caller. See is_authorized().

  §1.4 rate-limit + lockout — repeated failures from one address lock that
  address out for a fixed window. See AuthRateLimiter.
"""

from __future__ import annotations

import hmac
import math
import time

from aiohttp import web

PROTECTED_PREFIX = "/api/"
EXEMPT_PATHS = frozenset({"/api/security/csp-report"})

# §1.4 defaults, straight from the playbook: N failures inside W seconds
# locks the address out for L seconds.
FAILURE_LIMIT = 10
FAILURE_WINDOW_SECONDS = 300.0
LOCKOUT_SECONDS = 900.0

# Ceiling on how many addresses we track at once. Without it, an attacker
# spraying spoofed source addresses grows this dict without bound — the
# limiter would become the memory-exhaustion bug it exists to prevent.
# Expired entries are pruned first; only if that isn't enough do we evict.
MAX_TRACKED_ADDRESSES = 4096


def _matches(candidate: str, token: str) -> bool:
    """
    Constant-time compare of one credential against one configured token.

    The credential is compared as bytes, not str, because a hostile header
    can otherwise turn a 401 into a 500 two different ways:
    hmac.compare_digest() raises TypeError on non-ASCII str (e.g. `Bearer é`),
    and a raw non-UTF-8 byte (e.g. 0xE9) arrives from aiohttp already decoded
    with surrogateescape as '\\udce9', which a plain .encode("utf-8") then
    rejects with UnicodeEncodeError. Encoding with the same surrogateescape
    handler round-trips whatever bytes were actually on the wire, so both
    cases become an ordinary constant-time non-match.
    """
    if not token:
        return False
    return hmac.compare_digest(
        candidate.encode("utf-8", "surrogateescape"),
        token.encode("utf-8", "surrogateescape"),
    )


def is_authorized(headers, token: str, prev_token: str = "") -> bool:
    """
    True if `headers` (a Mapping — aiohttp's CIMultiDict or a plain dict in
    tests) carries a matching `Authorization: Bearer <token>` header. An
    empty token means auth isn't configured at all, so every request is
    authorized — split out like headers.py's apply_security_headers() so
    the logic is testable without an aiohttp Request or event loop.

    The scheme is matched case-insensitively (RFC 7235 §2.1: auth scheme
    names are case-insensitive, so `bearer <token>` is as valid as
    `Bearer <token>`).

    §2.1 rotation overlap: `prev_token`, when set, is accepted as well as
    `token`, through the same constant-time comparison. This is what makes a
    rotation a sequence of independent steps — set PREV to the outgoing
    value, roll the new value out to callers one at a time, then clear PREV —
    instead of a flag-day cutover where every caller has to change at once.
    An empty `prev_token` (the default, and the steady state) accepts nothing
    extra: _matches() returns False on an empty token rather than treating it
    as a wildcard.

    Both comparisons run even when the first one matches. Short-circuiting on
    the current token would make "matched current" measurably faster than
    "matched previous", which leaks which of the two a caller is holding.
    """
    if not token:
        return True
    scheme, _, value = headers.get("Authorization", "").partition(" ")
    if scheme.lower() != "bearer":
        return False
    current_ok = _matches(value, token)
    prev_ok = _matches(value, prev_token)
    return current_ok or prev_ok


class AuthRateLimiter:
    """
    §1.4 — per-address failure window with lockout, in process memory.

    Holds, per address, the timestamps of recent auth failures. Once
    `limit` failures land inside `window_seconds`, that address is locked
    out for `lockout_seconds` and every request from it is refused with 429
    until the lock expires.

    In process memory by design, same posture as DispatchActivity in
    agent/factory/dispatch.py: this is a single-user assistant, a restart
    clears the state, and a restart also clears whatever the attacker was
    part-way through. Nothing here is worth a database.

    `clock` is injectable so the tests can advance time without sleeping.
    Defaults to time.monotonic, not time.time — a system clock adjustment
    (NTP step, DST on a naive clock) must not shorten or extend a lockout.
    """

    def __init__(
        self,
        *,
        limit: int = FAILURE_LIMIT,
        window_seconds: float = FAILURE_WINDOW_SECONDS,
        lockout_seconds: float = LOCKOUT_SECONDS,
        max_addresses: int = MAX_TRACKED_ADDRESSES,
        clock=time.monotonic,
    ) -> None:
        self._limit = limit
        self._window = window_seconds
        self._lockout = lockout_seconds
        self._max_addresses = max_addresses
        self._clock = clock
        # address -> list of failure timestamps (pruned to the window)
        self._failures: dict[str, list[float]] = {}
        # address -> monotonic time the lockout expires
        self._locked_until: dict[str, float] = {}

    def retry_after(self, address: str) -> int | None:
        """
        Seconds remaining on this address's lockout, or None if it isn't
        locked. Never returns 0: a `Retry-After: 0` invites an immediate
        retry that would just 429 again, so the last fractional second
        rounds up to 1.
        """
        if not address:
            return None
        expires = self._locked_until.get(address)
        if expires is None:
            return None
        remaining = expires - self._clock()
        if remaining <= 0:
            # Lock served. Drop it *and* the failures that caused it, so the
            # address starts clean rather than re-locking on its next miss.
            self._locked_until.pop(address, None)
            self._failures.pop(address, None)
            return None
        return max(1, math.ceil(remaining))

    def record_failure(self, address: str) -> None:
        """Count one failed attempt; lock the address out if it hit the limit."""
        if not address:
            return
        now = self._clock()
        self._evict_if_needed(now)
        recent = [t for t in self._failures.get(address, []) if now - t < self._window]
        recent.append(now)
        self._failures[address] = recent
        if len(recent) >= self._limit:
            self._locked_until[address] = now + self._lockout

    def record_success(self, address: str) -> None:
        """
        Clear an address's failure history after a good request.

        The playbook's rule is that success doesn't *increment* any counter.
        Clearing goes one step further, and is the right call here: a caller
        that just proved it holds the token is not the attacker this limiter
        exists to slow down, and leaving stale failures around would let a
        legitimate client trip a lockout hours later on its first typo.

        Deliberately does not clear an active lockout — a locked address
        never reaches token validation in the first place (see the
        middleware), so a success while locked is not a reachable state.
        """
        if not address:
            return
        self._failures.pop(address, None)

    def _evict_if_needed(self, now: float) -> None:
        """
        Keep the tracked-address dicts bounded. Prunes anything already
        expired first; only if that leaves us at the ceiling does it drop
        the entries closest to expiry, which are the ones nearest to being
        forgotten anyway.
        """
        if len(self._failures) < self._max_addresses:
            return
        for addr in [a for a, exp in self._locked_until.items() if exp <= now]:
            self._locked_until.pop(addr, None)
            self._failures.pop(addr, None)
        for addr in [
            a
            for a, times in self._failures.items()
            if a not in self._locked_until and all(now - t >= self._window for t in times)
        ]:
            self._failures.pop(addr, None)
        while len(self._failures) >= self._max_addresses:
            oldest = min(self._failures, key=lambda a: max(self._failures[a]))
            self._failures.pop(oldest, None)
            self._locked_until.pop(oldest, None)


def client_address(request: web.Request) -> str:
    """
    The address a rate-limit bucket is keyed on.

    Deliberately `request.remote` — the peer we're actually talking to — and
    never X-Forwarded-For. XFF is caller-supplied: an attacker who can set it
    gets a fresh bucket on every request and is never limited at all, which
    turns the limiter into decoration. The cost of this choice is that behind
    a reverse proxy (the deployment README.md describes for a non-loopback
    bind) every client shares the proxy's bucket, so one attacker hammering
    the proxy can lock Sean out too. That's the fail-closed direction of the
    trade, and a lockout is 15 minutes rather than permanent.
    """
    return request.remote or ""


def bearer_auth_middleware(token: str, prev_token: str = "", limiter: AuthRateLimiter | None = None):
    """
    Build a middleware bound to `token` (settings.web_auth_token at app
    build time). A factory rather than a plain @web.middleware coroutine
    like security_headers_middleware because the token varies per app
    instance (tests build several apps with different tokens).

    `limiter` is per-app for the same reason; each built app gets its own
    unless one is passed in (the tests pass one with a fake clock).
    """
    limiter = limiter or AuthRateLimiter()

    @web.middleware
    async def middleware(request: web.Request, handler):
        path = request.path
        if not path.startswith(PROTECTED_PREFIX) or path in EXEMPT_PATHS:
            return await handler(request)
        if not token:
            # Auth isn't configured; there is nothing to fail, so there is
            # nothing to rate-limit either.
            return await handler(request)

        address = client_address(request)

        # Lockout is checked *before* the credential, not after. A locked
        # address is refused on the basis of its recent history, and letting
        # a correct token walk past an active lockout would make the lock
        # meaningless the moment an attacker guessed right.
        retry_after = limiter.retry_after(address)
        if retry_after is not None:
            return web.json_response(
                {"error": "too many failed attempts"},
                status=429,
                headers={"Retry-After": str(retry_after)},
            )

        if not is_authorized(request.headers, token, prev_token):
            limiter.record_failure(address)
            return web.json_response({"error": "unauthorized"}, status=401)

        limiter.record_success(address)
        return await handler(request)

    return middleware
