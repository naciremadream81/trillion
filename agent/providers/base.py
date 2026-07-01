"""
The provider seam.

Every model provider implements BaseProvider. The rest of the harness
only talks to this interface — it never touches an SDK directly.

This is the one place to add retries, cost logging, or fallback logic
without touching the conversation loop.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import AsyncIterator


# ── What a provider streams back ─────────────────────────────────────────────

@dataclass
class TextChunk:
    """A fragment of the assistant's reply as it streams in."""
    text: str


@dataclass
class ToolCall:
    """
    The model wants to invoke a tool.
    Added in Tier 2 — the stream loop handles these automatically.
    """
    id: str
    name: str
    arguments: dict


@dataclass
class TokenUsage:
    """
    Provider-neutral token counts for one API call.

    Each provider maps its SDK's own usage shape onto this so the rest of the
    harness (and cost tracking) never has to know provider-specific field names.
    All counts default to 0 so a partial/missing usage object is still valid.

    Convention: `input_tokens` EXCLUDES cache tokens (matching Anthropic, where
    cache reads/writes are reported separately). Providers whose input count is
    cache-inclusive (OpenAI) subtract the cached portion before mapping.
    """
    input_tokens: int = 0
    output_tokens: int = 0
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0


@dataclass
class ProviderResponse:
    """
    The complete result of one model turn.
    Yielded last by stream() after all TextChunks (and ToolCalls) are done.
    """
    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    stop_reason: str = "end_turn"
    # Cost tracking (best-effort): the token usage and the model the API
    # actually returned. Left as None on error paths where no usage arrives.
    usage: "TokenUsage | None" = None
    model: str | None = None


# ── The seam itself ───────────────────────────────────────────────────────────

class BaseProvider(ABC):
    """
    Thin interface between the agent core and any model provider.

    Implementors yield TextChunk objects as text arrives, then yield
    a final ProviderResponse summarizing the complete turn.

    Tier 2 note: when the model calls a tool, yield a ToolCall object
    mid-stream, then continue yielding the remaining text and the final
    ProviderResponse. The agent core handles the rest.
    """

    @abstractmethod
    async def stream(
        self,
        messages: list[dict],
        system: str,
        tools: list[dict] | None = None,
    ) -> AsyncIterator[TextChunk | ToolCall | ProviderResponse]:
        """
        Async generator. Yields:
          - TextChunk   — text fragments as they arrive
          - ToolCall    — (Tier 2+) when the model wants a tool
          - ProviderResponse — exactly once, at the very end
        """
        ...

    @property
    @abstractmethod
    def model_name(self) -> str:
        """Human-readable model identifier, for logs and the system prompt."""
        ...
