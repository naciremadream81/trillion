"""
Best-effort usage capture.

This is the one module in the whole feature with a hard rule:
**recording cost must never slow or break a conversation turn.** The entire
body of record_usage is wrapped in a catch-all — DB down, malformed usage
object, anything — it logs and moves on. A metrics feature that can take
down the agent is worse than no feature.

The repo is wired in via a module-level setter called once at startup, rather
than threaded through the LLM client's constructor and every call site. Less
churn, and it mirrors how other optional integrations are usually wired.
"""

from __future__ import annotations

import logging

from .pricing import compute_cost

logger = logging.getLogger(__name__)

# Set once at startup via set_usage_repo(). None = cost tracking disabled
# (record_usage becomes a no-op), so the agent runs fine without it.
_repo = None


def set_usage_repo(repo) -> None:
    """Register the repo record_usage writes to. Call once at startup."""
    global _repo
    _repo = repo


def record_usage(model: str, usage, source: str = "conversation") -> None:
    """
    Record one LLM call's token usage and computed cost. Best-effort.

    `usage` is a TokenUsage (or any object exposing the same attributes).
    Missing or None token fields are tolerated and treated as 0. If no repo is
    registered, or `usage` is None, this is a silent no-op.

    NEVER raises. Any failure is logged and swallowed.
    """
    try:
        if _repo is None or usage is None:
            return

        # getattr(..., 0) or 0 tolerates both missing attributes and None values
        # (e.g. a provider that omits cache fields entirely).
        input_tokens = getattr(usage, "input_tokens", 0) or 0
        output_tokens = getattr(usage, "output_tokens", 0) or 0
        cache_write_tokens = getattr(usage, "cache_write_tokens", 0) or 0
        cache_read_tokens = getattr(usage, "cache_read_tokens", 0) or 0

        cost = compute_cost(
            model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_write_tokens=cache_write_tokens,
            cache_read_tokens=cache_read_tokens,
        )

        _repo.record(
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_write_tokens=cache_write_tokens,
            cache_read_tokens=cache_read_tokens,
            cost_usd=cost,
            source=source,
        )
    except Exception:  # noqa: BLE001 — best-effort is the law here.
        logger.warning("record_usage failed; skipping (best-effort).", exc_info=True)
