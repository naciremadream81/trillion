"""
Startup guard against a public bind with no auth — agent-security.md §1.5,
adapted. §1.5 as written checks a `dev_mode` setting; Trillion has no such
setting (serve.py has always hardcoded host="127.0.0.1", with no debug mode
to gate). The plan's adaptation is the one that actually applies here:
refuse to bind any host other than loopback unless an auth token is
configured — a public bind with no token configured would expose /api/chat
and every tool it can reach to the open network with zero request-time
check. Once a token is configured, agent/security/auth.py's
bearer_auth_middleware enforces it on every /api/ request; this guard only
covers the moment before that middleware is even running.
"""

from __future__ import annotations

_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


class UnsafeBindError(RuntimeError):
    """Raised when startup would bind non-loopback with no auth configured."""


def check_bind_safety(host: str, auth_configured: bool) -> None:
    """
    Raise UnsafeBindError if host is not loopback and no auth is configured.
    Call this immediately before web.run_app(); it never mutates anything,
    it only decides whether startup may proceed.
    """
    if host in _LOOPBACK_HOSTS or auth_configured:
        return
    raise UnsafeBindError(
        f"Refusing to bind {host!r} without auth configured. Either set "
        "TRILLION_WEB_AUTH_TOKEN or rebind to localhost. A public bind "
        "with no auth would expose /api/chat to the open network."
    )
