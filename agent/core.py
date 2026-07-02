"""
The agent core — the conversation loop.

This is the brain. Everything else in the harness is in service of this.

Tier 1: text in → text out, with streaming and session history.
Tier 2: adds tool execution (tool registry injected here).
Tier 3: voice wraps this — the typed turn entry point stays unchanged.
Tier 4: memory_facts gets loaded here from the persistent store.
Tier 5: the heartbeat calls turn() with a synthetic first turn.
Tier 6: the confirmation gate sits between tool selection and tool execution.

The rule: one core, many adapters. Never fork the logic for voice vs text.
"""

from typing import AsyncIterator

from .cost.recorder import record_usage
from .providers.base import BaseProvider, TextChunk, ToolCall, ProviderResponse
from .system_prompt import build_system_prompt
from .turn_taking import is_signoff


class Agent:
    """
    The conversation loop.

    Usage (Tier 1):
        agent = Agent(provider=ClaudeProvider())
        async for chunk in agent.turn("What should I work on today?"):
            print(chunk, end="", flush=True)
    """

    def __init__(
        self,
        provider: BaseProvider,
        memory_facts: list[str] | None = None,
        tool_registry=None,  # ToolRegistry, added in Tier 2
    ) -> None:
        self.provider = provider
        self.tool_registry = tool_registry
        self.history: list[dict] = []
        self.system = build_system_prompt(memory_facts=memory_facts)

    # ── Public interface ──────────────────────────────────────────────────────

    async def turn(self, user_input: str) -> AsyncIterator[str]:
        """
        Process one turn of conversation.

        Appends the user message, streams the reply, appends the assistant
        message. Yields text chunks as they arrive so callers can display
        them incrementally.

        In Tier 2, this loop also handles tool calls: if the model requests
        a tool, execute it, feed the result back, and keep going until the
        model is done — all transparently.
        """
        # Tier 5: if the user is just signing off, don't take the last word.
        # Runs before anything is recorded or sent — a goodbye costs nothing.
        # Only ends a conversation the assistant has actually been part of.
        has_assistant_spoken = any(m.get("role") == "assistant" for m in self.history)
        if is_signoff(user_input, has_assistant_spoken=has_assistant_spoken):
            return

        self.history.append({"role": "user", "content": user_input})

        # Allow the model to call tools in a loop (Tier 2).
        # In Tier 1 there are no tools, so this runs exactly once.
        MAX_TOOL_ROUNDS = 8  # guard against a runaway tool-calling loop
        rounds = 0
        while True:
            rounds += 1
            collected_text = ""
            tool_calls: list[ToolCall] = []
            final_response: ProviderResponse | None = None

            # Stream the provider's response
            tools_schema = (
                self.tool_registry.schemas() if self.tool_registry else None
            )
            async for event in self.provider.stream(
                messages=self.history,
                system=self.system,
                tools=tools_schema,
            ):
                if isinstance(event, TextChunk):
                    collected_text += event.text
                    yield event.text
                elif isinstance(event, ToolCall):
                    tool_calls.append(event)
                elif isinstance(event, ProviderResponse):
                    final_response = event

            # ── Best-effort cost capture for this API round-trip ──────────────
            # One row per API call, so multi-round tool turns each get recorded.
            # record_usage never raises — it can't slow or break the turn.
            if final_response is not None:
                record_usage(
                    model=final_response.model or self.provider.model_name,
                    usage=final_response.usage,
                    source="conversation",
                )

            # No tool calls → we're done with this turn
            if not tool_calls:
                self.history.append(
                    {"role": "assistant", "content": collected_text}
                )
                break

            # ── Tier 2: handle tool calls (Anthropic tool-use format) ──────
            # The assistant turn must carry the tool_use blocks, and the tool
            # results come back as a *user* turn of tool_result blocks — this is
            # the exact shape the Claude API requires for a tool round-trip.
            # (OpenAI's tool format differs; wiring that is a separate task —
            # only Claude drives tools today.)
            assistant_content: list[dict] = []
            if collected_text.strip():
                assistant_content.append({"type": "text", "text": collected_text})
            for tc in tool_calls:
                assistant_content.append(
                    {"type": "tool_use", "id": tc.id, "name": tc.name, "input": tc.arguments}
                )
            self.history.append({"role": "assistant", "content": assistant_content})

            # Execute each tool; feed all results back as one user turn.
            tool_results: list[dict] = []
            for tc in tool_calls:
                result = await self._run_tool(tc)
                tool_results.append(
                    {"type": "tool_result", "tool_use_id": tc.id, "content": result}
                )
            self.history.append({"role": "user", "content": tool_results})

            # Safety valve: stop looping if the model keeps calling tools.
            if rounds >= MAX_TOOL_ROUNDS:
                self.history.append(
                    {"role": "assistant",
                     "content": "[Stopped after too many tool calls in one turn.]"}
                )
                yield "\n[Stopped after too many tool calls.]"
                break
            # Otherwise loop: the model now sees the tool results and continues.

    def reset(self) -> None:
        """Clear the in-session history. Memory (Tier 4) is unaffected."""
        self.history = []

    def update_memory(self, memory_facts: list[str]) -> None:
        """
        Reload memory facts into the system prompt.
        Called by the memory store (Tier 4) when facts change.
        """
        self.system = build_system_prompt(memory_facts=memory_facts)

    # ── Internal helpers ──────────────────────────────────────────────────────

    async def _run_tool(self, tc: ToolCall) -> str:
        """
        Execute a tool call. Returns the result as a string for the model.

        Tier 2 builds this out. For now it's a safe stub.
        Tier 6 wraps this with the confirmation gate for consequential tools.
        """
        if self.tool_registry is None:
            return f"[No tool registry available. Tool '{tc.name}' could not run.]"
        try:
            return await self.tool_registry.run(tc)
        except Exception as e:  # noqa: BLE001
            # Return the error to the model — let it reason about the failure
            # and explain it to Sean rather than crashing.
            return f"[Tool '{tc.name}' failed: {e}]"
