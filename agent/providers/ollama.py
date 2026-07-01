"""
Ollama provider — local models, no API key needed.

Works with any Ollama-served model: llama3.2, mistral, phi3, etc.
On Raspberry Pi 5 + AI HAT 2: point OLLAMA_BASE_URL at the Pi's IP
and Trillion runs entirely offline.

Default base URL assumes Ollama is running locally on the same machine.
For the Pi:
    OLLAMA_BASE_URL=http://raspberrypi.local:11434
    OLLAMA_MODEL=llama3.2
"""

import json
import os
from typing import AsyncIterator

import aiohttp

from .base import BaseProvider, TextChunk, ToolCall, ProviderResponse, TokenUsage


class OllamaProvider(BaseProvider):
    def __init__(self) -> None:
        self.base_url = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434").rstrip("/")
        self._model = os.getenv("OLLAMA_MODEL", "llama3.2")

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
        Stream a reply from a local Ollama model.

        Uses the /api/chat endpoint with stream=True.
        Ollama uses the OpenAI message format with system as first message.
        """
        ollama_messages = [{"role": "system", "content": system}] + messages
        collected_text = ""
        final_data: dict = {}

        payload = {
            "model": self._model,
            "messages": ollama_messages,
            "stream": True,
        }

        try:
            timeout = aiohttp.ClientTimeout(total=120, connect=5)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(
                    f"{self.base_url}/api/chat",
                    json=payload,
                ) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        msg = (
                            f"\n[Ollama returned {resp.status}. "
                            f"Is the model '{self._model}' pulled? "
                            f"Run: ollama pull {self._model}]\n{body}"
                        )
                        yield TextChunk(text=msg)
                        yield ProviderResponse(text=msg)
                        return

                    async for raw_line in resp.content:
                        line = raw_line.strip()
                        if not line:
                            continue
                        try:
                            data = json.loads(line)
                        except json.JSONDecodeError:
                            continue

                        content = data.get("message", {}).get("content", "")
                        if content:
                            collected_text += content
                            yield TextChunk(text=content)

                        if data.get("done"):
                            final_data = data
                            break

            # Ollama reports token counts on the final "done" message. Cost is
            # $0 for local inference, but recording tokens keeps local usage
            # visible in the dashboard alongside paid providers.
            usage = TokenUsage(
                input_tokens=final_data.get("prompt_eval_count", 0) or 0,
                output_tokens=final_data.get("eval_count", 0) or 0,
            )

            yield ProviderResponse(
                text=collected_text,
                usage=usage,
                model=self._model,
            )

        except aiohttp.ClientConnectorError:
            msg = (
                f"\n[Can't reach Ollama at {self.base_url}. "
                "Is it running? Try: ollama serve]"
            )
            yield TextChunk(text=msg)
            yield ProviderResponse(text=collected_text + msg)

        except Exception as e:  # noqa: BLE001
            msg = f"\n[Ollama error: {e}]"
            yield TextChunk(text=msg)
            yield ProviderResponse(text=collected_text + msg)
