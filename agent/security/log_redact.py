"""
Redaction for text before it's persisted to any log or audit trail —
agent-security.md §1.1. The real risk here isn't the model leaking a secret
in conversation (that's a provider-side concern); it's a gated tool's raw
result getting written verbatim into `SafetyRepo`'s audit_log (see
`mark_executed()` in agent/safety/storage.py), where a web-fetched page or
a shell command's stdout could contain an API key, bearer token, or DB
connection string that then sits in a local SQLite file indefinitely.

Regex passes run in a fixed order so a broader pattern (e.g. bearer header)
doesn't get a chance to eat a token an earlier, more specific pattern
(e.g. Stripe secret key) would have masked more precisely. `max_len` is
applied last and is independent of the redaction patterns themselves, so
callers can tune verbosity without touching the masking rules.
"""

from __future__ import annotations

import re

_PATTERNS: list[tuple[re.Pattern, str]] = [
    # Bearer auth headers -> mask the token, keep the header shape.
    (re.compile(r"(Authorization:\s*Bearer)\s+\S+", re.I), r"\1 <redacted>"),
    # Common provider API key prefixes (Anthropic, Stripe/OpenAI-style
    # sk-live/sk-test, GitHub personal access tokens, Slack, generic sk_).
    (re.compile(r"\bsk-ant-[A-Za-z0-9_-]{10,}\b"), "<redacted-api-key>"),
    (re.compile(r"\bsk[_-](?:live|test)[_-][A-Za-z0-9]{10,}\b", re.I), "<redacted-api-key>"),
    (re.compile(r"\bsk-[A-Za-z0-9]{10,}\b"), "<redacted-api-key>"),
    (re.compile(r"\bghp_[A-Za-z0-9]{20,}\b"), "<redacted-api-key>"),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"), "<redacted-api-key>"),
    # JWT-shaped strings: three base64url segments separated by dots.
    (re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b"), "<redacted-jwt>"),
    # Connection-string passwords: scheme://user:pass@host -> mask only pass.
    (
        re.compile(r"(\b[a-zA-Z][a-zA-Z0-9+.-]*://[^:/\s@]+:)([^@/\s]+)(@)"),
        r"\1<redacted>\3",
    ),
    # Credit-card-shaped numbers (13-19 digits, optionally grouped by
    # spaces/dashes in groups of 4) -> keep last 4 digits.
    (
        re.compile(r"\b(?:\d[ -]?){12,15}(\d{4})\b"),
        lambda m: f"<redacted-card>{m.group(1)}",
    ),
    # Email addresses -> mask the local part, keep the domain.
    (
        re.compile(r"\b[A-Za-z0-9._%+-]+(@[A-Za-z0-9.-]+\.[A-Za-z]{2,})\b"),
        r"<redacted>\1",
    ),
]


def redact(text: str, max_len: int = 500) -> str:
    """Mask high-precision secret/PII shapes in text, then cap its length.

    Never raises — an empty/None-ish input returns "". max_len is applied
    after redaction so truncation never splits a mask mid-token.
    """
    if not text:
        return ""
    result = text
    for pattern, replacement in _PATTERNS:
        result = pattern.sub(replacement, result)
    return result[:max_len]
