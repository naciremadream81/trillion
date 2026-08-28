"""
Cross-origin (CSRF) request gate — the piece audit.py's csrf-origin-gate
signal has been reporting as "absent" since §3.5 landed.

This was not a theoretical gap. Verified against a running server before this
module existed: `POST /api/security/cve-scan` takes no body at all and shells
out to pip-audit, and `POST /api/chat` reads its body with `request.json()`,
which aiohttp does *not* Content-Type-validate. So a plain cross-origin HTML
form with `enctype="text/plain"` — no preflight, no custom headers, nothing a
browser blocks — drove a full agent turn and real token spend from an attacker
page. Both returned 200.

The session cookie is already SameSite=Strict, which is why this is a spend and
side-effect problem rather than a session-hijack one: the forged request just
gets a fresh session. That is still an attacker spending Sean's money and
triggering subprocesses on his machine.

## How a request is judged

Three signals, in order, on state-changing methods only:

1. **`Sec-Fetch-Site`** wins when present. Browsers set it themselves and page
   JavaScript cannot forge or suppress it, which makes it strictly better
   evidence than Origin. `same-origin` and `none` (a typed URL or bookmark)
   pass; `cross-site` and `same-site` are refused.
2. **`Origin`** is checked next, against the host the request itself arrived
   on. `null` — what a sandboxed iframe sends — is refused rather than treated
   as absent.
3. **Neither header** means the caller is not a browser: curl, a systemd unit,
   a reverse proxy, the CLI. Those are allowed through.

Rule 3 deserves the scrutiny, because "missing header allows the request" is
usually how a gate gets bypassed. It doesn't here: the attack this module stops
is a *browser* being told to make a request, and no browser released in the
last several years omits both headers on a cross-origin POST. An attacker who
can send neither is already running arbitrary HTTP clients against the machine,
and has no need for CSRF. Refusing them instead would only break curl and the
reverse-proxy deployment in docs/incident-runbook.md — which is exactly how a
safety rail earns itself a permanent `disabled` flag (see the same reasoning at
agent/factory/dispatch.py:73).

## Host allowlist

The Origin check alone compares two attacker-influenced values: in a DNS
rebinding attack the browser sends `Host: evil.example` *and*
`Origin: http://evil.example`, which match each other perfectly. So the host is
independently checked against what the server was configured to bind. On the
default loopback bind that means only 127.0.0.1/localhost/::1 are accepted, and
rebinding — whose whole point is arriving under an attacker-controlled name —
fails.

## Wildcard binds (0.0.0.0, ::)

`TRILLION_WEB_HOST=0.0.0.0` is a supported configuration — startup_guard.py
allows any non-loopback bind once an auth token is set — and it breaks the
hostname comparison above in a specific way: the browser's Host header carries
whatever LAN address or domain actually reached the server ("192.168.1.50" or
a real domain), never the literal wildcard string. An allowlist of `{"0.0.0.0"}`
therefore matches nothing a real client ever sends, and every legitimate
request would be refused before `Sec-Fetch-Site` is even read.

There is no config here that names the server's externally reachable
hostname(s), so `allowed_hostnames()` returns `None` for a wildcard bind — "not
checkable by name" — and both the Host check and the Origin-hostname check are
skipped. `Sec-Fetch-Site` does not need a hostname to prove same-origin (the
browser makes that determination itself), so it remains fully authoritative.
The gap this opens: an old browser that omits Fetch Metadata (pre-2021
Firefox, pre-2023 Safari) sending only `Origin` on a wildcard-bind deployment
can't have that Origin verified, and is refused rather than trusted blindly —
a documented residual specific to wildcard binds, not the default loopback
case.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from aiohttp import web

from .startup_guard import _LOOPBACK_HOSTS

# Same scope as auth.py's bearer gate, deliberately: two middlewares that guard
# the same surface should not disagree about what that surface is.
PROTECTED_PREFIX = "/api/"

# The browser's Reporting API POSTs CSP violations on its own, from the page's
# own context, with no way for us to influence the headers it attaches. Gating
# it would silently discard exactly the telemetry the CSP work depends on.
EXEMPT_PATHS = frozenset({"/api/security/csp-report"})

# GET/HEAD/OPTIONS are not gated: they're supposed to be side-effect free, and
# every GET route here is a read.
#
# WITH ONE EXCEPTION — a WebSocket upgrade. It arrives as a GET, so it would
# sail through the rule above, and unlike fetch() it is NOT subject to the
# same-origin policy: any page on the internet can open a socket to
# ws://localhost:8123 and the browser will neither block it nor ask for CORS
# permission. /api/transcribe/stream relays audio to a paid Deepgram account
# and returns the transcript, so an ungated upgrade is a stranger spending
# Sean's credits and reading what his microphone hears. Upgrades are
# therefore guarded on the same three signals as a POST — see
# _is_websocket_upgrade below.
GUARDED_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# Sec-Fetch-Site values that mean "this request did not originate from a page
# on this origin". "same-site" is included: a sibling subdomain is a different
# origin, and on a single-user loopback deployment there is no legitimate
# sibling.
_HOSTILE_FETCH_SITES = frozenset({"cross-site", "same-site"})


# A wildcard bind has no single "this is the server's own address" — see the
# module docstring's "Wildcard binds" section.
_WILDCARD_BIND_HOSTS = frozenset({"0.0.0.0", "::", ""})


def allowed_hostnames(web_host: str) -> frozenset[str] | None:
    """
    The hostnames a request may claim to have arrived on, or None if that
    can't be determined by name (a wildcard bind).

    Loopback binds accept all three spellings of loopback because the browser
    uses whichever one is in the URL bar. A non-loopback, non-wildcard bind
    accepts only the exact configured host — the point is to reject a request
    that arrived under some *other* name that happens to resolve here, which
    is what DNS rebinding does.
    """
    if web_host in _WILDCARD_BIND_HOSTS:
        return None
    if web_host in _LOOPBACK_HOSTS:
        return frozenset(_LOOPBACK_HOSTS)
    return frozenset({web_host})


def _hostname_of(value: str) -> str:
    """
    The bare hostname from a Host header or an Origin URL, port and brackets
    stripped. Uses urlsplit for both so that IPv6 literals ("[::1]:8123") are
    handled by the stdlib rather than by a str.split(":") that would mangle
    them.
    """
    if not value:
        return ""
    candidate = value if "//" in value else f"//{value}"
    try:
        hostname = urlsplit(candidate).hostname
    except ValueError:
        return ""
    return (hostname or "").lower()


def _is_websocket_upgrade(headers) -> bool:
    """
    Whether this request is a WebSocket handshake.

    `Connection` is a comma-separated list of tokens ("keep-alive, Upgrade"),
    so this is a membership test rather than an equality one — matching on
    the whole header value would miss real handshakes from real browsers.
    Both header values are case-insensitive per RFC 6455 §4.1.
    """
    upgrade = (headers.get("Upgrade") or "").strip().lower()
    if upgrade != "websocket":
        return False
    connection = (headers.get("Connection") or "").lower()
    return "upgrade" in {token.strip() for token in connection.split(",")}


def check_origin(
    method: str,
    path: str,
    headers,
    web_host: str,
) -> str | None:
    """
    Decide whether a request may proceed. Returns None to allow, or a short
    reason string to refuse with.

    Split out from the middleware — like auth.py's is_authorized() and
    headers.py's apply_security_headers() — so the decision can be tested
    against a plain dict, with no aiohttp Request and no event loop.
    """
    if method.upper() not in GUARDED_METHODS and not _is_websocket_upgrade(headers):
        return None
    if not path.startswith(PROTECTED_PREFIX) or path in EXEMPT_PATHS:
        return None

    allowed = allowed_hostnames(web_host)

    # The host the request claims to have arrived on. Checked before Origin so
    # that a rebinding attack — where Host and Origin agree with each other but
    # neither is us — is refused on the value the attacker actually needed to
    # control. Skipped entirely on a wildcard bind (allowed is None): there is
    # no configured hostname to compare against, so Sec-Fetch-Site below does
    # this job instead.
    host_name = _hostname_of(headers.get("Host", ""))
    if allowed is not None and host_name and host_name not in allowed:
        return f"host {host_name!r} is not an address this server serves"

    fetch_site = (headers.get("Sec-Fetch-Site") or "").strip().lower()
    if fetch_site in _HOSTILE_FETCH_SITES:
        return f"Sec-Fetch-Site: {fetch_site}"
    if fetch_site:
        # "same-origin" or "none" — browser-attested, unforgeable by page
        # script, and better evidence than Origin. Believe it and stop.
        return None

    origin = (headers.get("Origin") or "").strip()
    if not origin:
        # No Sec-Fetch-Site and no Origin: not a browser. See the module
        # docstring for why this is allowed rather than refused.
        return None
    if origin.lower() == "null":
        # A sandboxed iframe, a data: URL, or a redirect that lost its origin.
        # Distinct from absent, and never something we want to trust.
        return "Origin: null"

    if allowed is None:
        # Wildcard bind, and Sec-Fetch-Site (checked above) didn't already
        # resolve this. There's no hostname to compare Origin against, so an
        # unverifiable claim is refused rather than trusted blindly — see
        # "Wildcard binds" in the module docstring.
        return f"origin {origin!r} cannot be verified against a wildcard bind"

    origin_name = _hostname_of(origin)
    if not origin_name or origin_name not in allowed:
        return f"origin {origin!r} is not allowed"
    return None


def origin_check_middleware(web_host: str):
    """
    Build a middleware bound to `web_host` (settings.web_host at app build
    time). A factory for the same reason bearer_auth_middleware() is one: the
    value varies per app instance, and tests build several apps.
    """

    @web.middleware
    async def middleware(request: web.Request, handler):
        reason = check_origin(
            request.method, request.path, request.headers, web_host
        )
        if reason is not None:
            return web.json_response(
                {"error": "cross-origin request refused", "detail": reason},
                status=403,
            )
        return await handler(request)

    return middleware
