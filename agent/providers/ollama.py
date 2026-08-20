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

from ._openai_tools import to_openai_messages, to_openai_tools
from .base import BaseProvider, TextChunk, ToolCall, ProviderResponse, TokenUsage


class OllamaProvider(BaseProvider):
    def __init__(self, model: str | None = None) -> None:
        self.base_url = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434").rstrip("/")
        self._model = model or os.getenv("OLLAMA_MODEL", "llama3.2")

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

        Tool support is real but conditional: only some local models are
        trained to emit tool calls, and Ollama simply ignores the `tools`
        field on the ones that aren't. That silent no-op used to extend to
        this whole provider — `tools` was accepted as a parameter and never
        sent — so switching to Ollama produced an agent that could talk but
        had quietly lost every capability. Now the tools go out, and a model
        that never calls one gets a single log line rather than nothing.
        """
        ollama_messages = [{"role": "system", "content": system}] + to_openai_messages(messages)
        collected_text = ""
        final_data: dict = {}
        raw_tool_calls: list[dict] = []

        payload = {
            "model": self._model,
            "messages": ollama_messages,
            "stream": True,
        }
        ollama_tools = to_openai_tools(tools)
        if ollama_tools:
            payload["tools"] = ollama_tools

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

                        message = data.get("message", {}) or {}
                        content = message.get("content", "")
                        if content:
                            collected_text += content
                            yield TextChunk(text=content)

                        # Unlike OpenAI, Ollama emits a tool call whole rather
                        # than as argument fragments, and `arguments` arrives
                        # already decoded as an object — so there is nothing to
                        # accumulate or parse here, only to collect.
                        for call in message.get("tool_calls") or []:
                            if isinstance(call, dict):
                                raw_tool_calls.append(call)

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

            tool_calls = []
            for index, call in enumerate(raw_tool_calls):
                function = call.get("function") or {}
                name = function.get("name") or ""
                if not name:
                    continue
                arguments = function.get("arguments")
                if isinstance(arguments, str):
                    # Some builds stringify it anyway; accept both.
                    try:
                        arguments = json.loads(arguments) if arguments.strip() else {}
                    except json.JSONDecodeError:
                        arguments = {}
                if not isinstance(arguments, dict):
                    arguments = {}
                tool_calls.append(
                    ToolCall(id=call.get("id") or f"call_{index}", name=name, arguments=arguments)
                )

            if ollama_tools and not tool_calls and not collected_text.strip():
                # Degrade loudly rather than silently: an empty reply from a
                # model that was offered tools is the signature of a model
                # that doesn't support them.
                print(
                    f"[ollama] '{self._model}' returned nothing when offered "
                    f"{len(ollama_tools)} tool(s) — it may not support tool calling. "
                    "Try a tool-capable model (e.g. llama3.1+, qwen2.5, mistral-nemo)."
                )

            for call in tool_calls:
                yield call

            yield ProviderResponse(
                text=collected_text,
                tool_calls=tool_calls,
                stop_reason="tool_use" if tool_calls else "end_turn",
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
