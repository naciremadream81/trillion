"""
Model pricing and the cost function.

The one thing to keep updated in this whole feature is MODEL_PRICING.
Provider rates change over time — when they do, edit the table here and
every historical row can be re-costed from its stored token counts.

Rates are expressed in **US dollars per one million tokens**, one rate per
token class the provider bills separately:

    input        — normal prompt tokens
    output       — generated tokens
    cache_write  — tokens written into the prompt cache (Anthropic: ~1.25x
                   input for the 5-minute TTL)
    cache_read   — tokens served from the cache (a cache hit; ~0.1x input)

Matching is by **longest prefix**, because model strings carry version and
date suffixes: "claude-sonnet-4-6-20260514" resolves to the "claude-sonnet-4"
family entry. An unknown model returns 0.0 and logs a warning (once) — a row
is still recorded with its token counts so it can be backfilled later.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Rates:
    """Per-million-token rates, in USD, for one model family."""
    input: float
    output: float
    cache_write: float = 0.0
    cache_read: float = 0.0


# ── The rate table ────────────────────────────────────────────────────────────
# Keyed by model-family PREFIX. Verify against the provider's published pricing
# and update when it changes. Anthropic cache rates assume the 5-minute TTL
# (cache_write ≈ 1.25x input, cache_read ≈ 0.1x input).
MODEL_PRICING: dict[str, Rates] = {
    # ── Anthropic (primary provider) ──────────────────────────────────────────
    "claude-opus-4":   Rates(input=15.0, output=75.0, cache_write=18.75, cache_read=1.50),
    "claude-sonnet-4": Rates(input=3.0,  output=15.0, cache_write=3.75,  cache_read=0.30),
    "claude-haiku-4":  Rates(input=1.0,  output=5.0,  cache_write=1.25,  cache_read=0.10),

    # ── OpenAI / OpenRouter ───────────────────────────────────────────────────
    # OpenAI bills cached input reads at ~0.5x input and does not charge
    # separately to write the cache, so cache_write mirrors the input rate.
    # NOTE: OpenRouter model strings (e.g. "anthropic/claude-opus-4") won't
    # match these prefixes and will resolve to 0.0 until added here.
    "gpt-4o-mini": Rates(input=0.15, output=0.60, cache_write=0.15, cache_read=0.075),
    "gpt-4o":      Rates(input=2.50, output=10.0, cache_write=2.50, cache_read=1.25),

    # ── Local models (Ollama) ─────────────────────────────────────────────────
    # Local inference is free. Listed explicitly so common local models resolve
    # to $0 without logging an "unknown model" warning on every turn.
    "llama":   Rates(input=0.0, output=0.0),
    "mistral": Rates(input=0.0, output=0.0),
    "phi":     Rates(input=0.0, output=0.0),
    "qwen":    Rates(input=0.0, output=0.0),
    "gemma":   Rates(input=0.0, output=0.0),
}

# Remember which unknown models we've already warned about, so an unpriced
# model logs once rather than on every single call.
_warned_models: set[str] = set()


def _match_rates(model: str) -> Rates | None:
    """
    Resolve a (possibly version-suffixed) model string to a Rates entry by
    longest matching prefix. Returns None if nothing matches.

    Longest-prefix so that a more specific family wins: "gpt-4o-mini-2026-01"
    matches "gpt-4o-mini", not the shorter "gpt-4o".
    """
    best_key: str | None = None
    for key in MODEL_PRICING:
        if model.startswith(key) and (best_key is None or len(key) > len(best_key)):
            best_key = key
    return MODEL_PRICING[best_key] if best_key is not None else None


def compute_cost(
    model: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_write_tokens: int = 0,
    cache_read_tokens: int = 0,
) -> float:
    """
    Compute the USD cost of one LLM call from its token counts.

    Unknown model → 0.0 (never raises), with a one-time warning naming the
    model. The caller should still record the row so cost can be backfilled
    once a rate is added.
    """
    rates = _match_rates(model)
    if rates is None:
        if model not in _warned_models:
            _warned_models.add(model)
            logger.warning(
                "No pricing entry for model %r — costing this and future calls "
                "at $0.00. Add it to MODEL_PRICING in agent/cost/pricing.py.",
                model,
            )
        return 0.0

    cost = (
        input_tokens * rates.input
        + output_tokens * rates.output
        + cache_write_tokens * rates.cache_write
        + cache_read_tokens * rates.cache_read
    ) / 1_000_000

    return cost
