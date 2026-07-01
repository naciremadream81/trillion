"""
Anthropic Claude provider.

Uses the official anthropic SDK with streaming.
Tier 2 note: tool_use content blocks are surfaced as ToolCall objects
once that tier is built — the structure is already here, just dormant.
"""

import os
from typing import AsyncIterator

from anthropic import AsyncAnthropic, APIConnectionError, APIStatusError, RateLimitError

from .base import BaseProvider, TextChunk, ToolCall, ProviderResponse, TokenUsage


class ClaudeProvider(BaseProvider):
    def __init__(self) -> None:
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise EnvironmentError(
                "ANTHROPIC_API_KEY is not set. "
                "Add it to your .env file — never paste it in source code."
            )
        self.client = AsyncAnthropic(api_key=api_key)
        self._model = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6")

    @property
    def model_name(self) -> str:
        return self._model

    async def stream(
        self,
        messages: list[dict],
        system: str,
        tools: list[dict] | None = None,
    ) -> AsyncIterator[TextChunk | ToolCall | ProviderResponse]:
        """
        Stream a reply from Claude.

        Tier 1: yields TextChunks then a ProviderResponse with no tool_calls.
        Tier 2: will also yield ToolCall objects when the model requests them.
        """
        collected_text = ""
        collected_tool_calls: list[ToolCall] = []

        # Tier 2: pass tools into the API call when the registry provides them.
        api_kwargs: dict = dict(
            model=self._model,
            max_tokens=4096,
            system=system,
            messages=messages,
        )
        if tools:
            api_kwargs["tools"] = tools

        try:
            async with self.client.messages.stream(**api_kwargs) as s:
                # ── Text stream ───────────────────────────────────────────────
                async for text in s.text_stream:
                    collected_text += text
                    yield TextChunk(text=text)

                # ── Tool calls (Tier 2) ───────────────────────────────────────
                # The final message contains complete tool_use blocks.
                # We surface them here so the core loop can act on them.
                final = await s.get_final_message()
                for block in final.content:
                    if block.type == "tool_use":
                        tc = ToolCall(
                            id=block.id,
                            name=block.name,
                            arguments=block.input,
                        )
                        collected_tool_calls.append(tc)
                        yield tc

                # Usage rides on the final message. Anthropic reports cache
                # tokens separately from input_tokens, so no adjustment needed.
                # getattr(..., 0) or 0 tolerates absent/None cache fields.
                u = final.usage
                usage = TokenUsage(
                    input_tokens=getattr(u, "input_tokens", 0) or 0,
                    output_tokens=getattr(u, "output_tokens", 0) or 0,
                    cache_write_tokens=getattr(u, "cache_creation_input_tokens", 0) or 0,
                    cache_read_tokens=getattr(u, "cache_read_input_tokens", 0) or 0,
                )

                yield ProviderResponse(
                    text=collected_text,
                    tool_calls=collected_tool_calls,
                    stop_reason=final.stop_reason or "end_turn",
                    usage=usage,
                    model=getattr(final, "model", None),
                )

        except RateLimitError:
            msg = "\n[Rate-limited by Anthropic — wait a moment and try again.]"
            yield TextChunk(text=msg)
            yield ProviderResponse(text=collected_text + msg)

        except APIConnectionError as e:
            msg = f"\n[Can't reach Anthropic right now ({e}). Check your connection.]"
            yield TextChunk(text=msg)
            yield ProviderResponse(text=collected_text + msg)

        except APIStatusError as e:
            msg = f"\n[Anthropic returned an error ({e.status_code}: {e.message}).]"
            yield TextChunk(text=msg)
            yield ProviderResponse(text=collected_text + msg)

        except Exception as e:  # noqa: BLE001
            msg = f"\n[Something unexpected went wrong: {e}]"
            yield TextChunk(text=msg)
            yield ProviderResponse(text=collected_text + msg)
