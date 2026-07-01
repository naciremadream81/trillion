"""
OpenAI provider — also covers OpenRouter and any OpenAI-compatible API.

To use OpenRouter instead of OpenAI, set in .env:
    OPENAI_BASE_URL=https://openrouter.ai/api/v1
    OPENAI_API_KEY=sk-or-...
    OPENAI_MODEL=anthropic/claude-opus-4  (or any OpenRouter model string)

The provider doesn't care which endpoint it's talking to.
"""

import os
from typing import AsyncIterator

from openai import AsyncOpenAI, APIConnectionError, APIStatusError, RateLimitError

from .base import BaseProvider, TextChunk, ToolCall, ProviderResponse, TokenUsage


class OpenAIProvider(BaseProvider):
    def __init__(self) -> None:
        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            raise EnvironmentError(
                "OPENAI_API_KEY is not set. "
                "Add it to your .env file (or OPENROUTER_API_KEY if using OpenRouter)."
            )
        base_url = os.environ.get("OPENAI_BASE_URL")  # None = default OpenAI endpoint
        self.client = AsyncOpenAI(api_key=api_key, base_url=base_url)
        self._model = os.getenv("OPENAI_MODEL", "gpt-4o")

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
        Stream a reply from OpenAI (or OpenRouter).

        OpenAI puts the system prompt as the first message, not a separate
        field — we inject it here transparently.
        """
        openai_messages = [{"role": "system", "content": system}] + messages
        collected_text = ""

        api_kwargs: dict = dict(
            model=self._model,
            messages=openai_messages,
            stream=True,
            # Ask the stream to include a final usage summary. With this set,
            # the last chunk carries `usage` and an EMPTY `choices` list — the
            # loop below must guard against that or it would IndexError.
            stream_options={"include_usage": True},
        )
        # Tier 2: OpenAI tool format differs from Anthropic's.
        # We'll handle the translation in the tool registry later.
        if tools:
            api_kwargs["tools"] = tools

        try:
            usage_obj = None
            returned_model = None
            stream = await self.client.chat.completions.create(**api_kwargs)
            async for chunk in stream:
                if getattr(chunk, "model", None):
                    returned_model = chunk.model
                # The usage-only final chunk has choices == [].
                if chunk.choices:
                    delta = chunk.choices[0].delta
                    if delta.content:
                        collected_text += delta.content
                        yield TextChunk(text=delta.content)
                if getattr(chunk, "usage", None):
                    usage_obj = chunk.usage

            # Map OpenAI's usage onto the neutral shape. Note: OpenAI's
            # prompt_tokens is cache-INCLUSIVE, so subtract the cached portion
            # to avoid double-counting it at the full input rate.
            usage = None
            if usage_obj is not None:
                prompt_tokens = getattr(usage_obj, "prompt_tokens", 0) or 0
                details = getattr(usage_obj, "prompt_tokens_details", None)
                cached = (getattr(details, "cached_tokens", 0) or 0) if details else 0
                usage = TokenUsage(
                    input_tokens=max(prompt_tokens - cached, 0),
                    output_tokens=getattr(usage_obj, "completion_tokens", 0) or 0,
                    cache_write_tokens=0,  # OpenAI doesn't bill cache writes separately
                    cache_read_tokens=cached,
                )

            yield ProviderResponse(
                text=collected_text,
                usage=usage,
                model=returned_model,
            )

        except RateLimitError:
            msg = "\n[Rate-limited by OpenAI — wait a moment and try again.]"
            yield TextChunk(text=msg)
            yield ProviderResponse(text=collected_text + msg)

        except APIConnectionError as e:
            msg = f"\n[Can't reach OpenAI right now ({e}). Check your connection.]"
            yield TextChunk(text=msg)
            yield ProviderResponse(text=collected_text + msg)

        except APIStatusError as e:
            msg = f"\n[OpenAI returned an error ({e.status_code}: {e.message}).]"
            yield TextChunk(text=msg)
            yield ProviderResponse(text=collected_text + msg)

        except Exception as e:  # noqa: BLE001
            msg = f"\n[Something unexpected went wrong: {e}]"
            yield TextChunk(text=msg)
            yield ProviderResponse(text=collected_text + msg)
