"""
Anthropic <-> OpenAI translation for tools and message history.

Why this exists: agent/core.py speaks one shape — Anthropic's. It builds
tool schemas as {name, description, input_schema}, records assistant turns
with `tool_use` content blocks, and feeds results back as a user turn of
`tool_result` blocks. That is the harness's internal language, and the whole
point of the provider seam is that the core never learns a second one.

Until this module, the OpenAI provider forwarded Anthropic-shaped schemas
verbatim (with a "we'll handle the translation later" comment) and the
Ollama provider accepted `tools` and silently dropped it. Both meant that
switching TRILLION_PROVIDER produced an agent that could still talk but had
quietly lost every capability — the worst kind of failure, because nothing
errors.

Shared by both providers because Ollama's /api/chat deliberately mirrors
OpenAI's schema. Where they differ is called out at each function.
"""

from __future__ import annotations

import json

# A tool result can be enormous (a file read, a search page). The history it
# lands in is re-sent on every subsequent round, so an unbounded result is
# paid for repeatedly. Anthropic's path has the same exposure; this is the
# translation boundary, so it's the honest place to bound it.
MAX_TOOL_RESULT_CHARS = 100_000


def to_openai_tools(tools: list[dict] | None) -> list[dict] | None:
    """
    {name, description, input_schema} -> {"type": "function", "function": {...}}

    Returns None for an empty/absent list so callers can omit the key
    entirely: some OpenAI-compatible endpoints reject `tools: []`.
    """
    if not tools:
        return None
    translated = []
    for tool in tools:
        if not isinstance(tool, dict) or not tool.get("name"):
            continue
        translated.append(
            {
                "type": "function",
                "function": {
                    "name": tool["name"],
                    "description": tool.get("description", ""),
                    # OpenAI calls it `parameters`; the JSON Schema inside is
                    # identical, which is why this is a rename and not a
                    # rewrite. An absent schema becomes an empty object rather
                    # than being omitted — some endpoints require the key.
                    "parameters": tool.get("input_schema") or {"type": "object", "properties": {}},
                },
            }
        )
    return translated or None


def _stringify_tool_result(content) -> str:
    """
    Anthropic accepts a tool_result `content` that is a string or a list of
    blocks. OpenAI's `tool` message wants a plain string.
    """
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
            elif isinstance(block, str):
                parts.append(block)
        text = "\n".join(parts)
    elif content is None:
        text = ""
    else:
        text = str(content)
    if len(text) > MAX_TOOL_RESULT_CHARS:
        text = text[:MAX_TOOL_RESULT_CHARS] + "\n…[truncated]"
    return text


def to_openai_messages(messages: list[dict]) -> list[dict]:
    """
    Translate agent/core.py's Anthropic-shaped history into OpenAI's.

    The three shapes core.py actually produces:

      {"role": "user", "content": "<str>"}            -> unchanged
      {"role": "assistant", "content": "<str>"}       -> unchanged
      {"role": "assistant", "content": [text?, tool_use...]}
          -> one assistant message with `tool_calls`
      {"role": "user", "content": [tool_result...]}
          -> one `tool` message *per result*

    That last asymmetry is the one that bites: Anthropic batches every result
    of a parallel tool round into a single user turn, OpenAI wants them as
    separate messages each keyed to its own tool_call_id. Collapsing them
    into one message makes the model unable to tell which result belongs to
    which call.
    """
    out: list[dict] = []
    for message in messages:
        role = message.get("role")
        content = message.get("content")

        if isinstance(content, str):
            out.append({"role": role, "content": content})
            continue

        if not isinstance(content, list):
            out.append({"role": role, "content": "" if content is None else str(content)})
            continue

        if role == "assistant":
            text_parts, tool_calls = [], []
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text":
                    text_parts.append(str(block.get("text", "")))
                elif block.get("type") == "tool_use":
                    tool_calls.append(
                        {
                            "id": block.get("id", ""),
                            "type": "function",
                            "function": {
                                "name": block.get("name", ""),
                                # OpenAI wants the arguments as a JSON *string*.
                                "arguments": json.dumps(block.get("input") or {}),
                            },
                        }
                    )
            assistant: dict = {"role": "assistant"}
            text = "".join(text_parts)
            # Explicit None, not "": the API treats an empty string as a real
            # (empty) reply, which reads as the assistant having said nothing
            # rather than having only called a tool.
            assistant["content"] = text or None
            if tool_calls:
                assistant["tool_calls"] = tool_calls
            out.append(assistant)
            continue

        # A user turn carrying tool results.
        emitted = False
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                out.append(
                    {
                        "role": "tool",
                        "tool_call_id": block.get("tool_use_id", ""),
                        "content": _stringify_tool_result(block.get("content")),
                    }
                )
                emitted = True
        if not emitted:
            text_parts = [
                str(b.get("text", ""))
                for b in content
                if isinstance(b, dict) and b.get("type") == "text"
            ]
            out.append({"role": role, "content": "".join(text_parts)})
    return out


class ToolCallAccumulator:
    """
    Reassembles OpenAI's streamed tool-call fragments into whole calls.

    OpenAI streams a tool call in pieces: an opening delta carrying the index,
    id, and function name, then further deltas carrying the `arguments` JSON a
    few characters at a time. Nothing is usable until the stream ends, so this
    buffers by index and only parses at the end.

    Keyed on `index`, not `id`, because only the first fragment of a call
    carries the id — later fragments identify themselves by position alone.
    """

    def __init__(self) -> None:
        self._calls: dict[int, dict] = {}

    def add_delta(self, tool_call_deltas) -> None:
        for delta in tool_call_deltas or []:
            index = getattr(delta, "index", None)
            if index is None and isinstance(delta, dict):
                index = delta.get("index", 0)
            index = index or 0
            slot = self._calls.setdefault(index, {"id": "", "name": "", "arguments": ""})

            call_id = getattr(delta, "id", None) or (delta.get("id") if isinstance(delta, dict) else None)
            if call_id:
                slot["id"] = call_id

            function = getattr(delta, "function", None)
            if function is None and isinstance(delta, dict):
                function = delta.get("function")
            if function is None:
                continue

            name = getattr(function, "name", None) or (
                function.get("name") if isinstance(function, dict) else None
            )
            if name:
                slot["name"] = name
            arguments = getattr(function, "arguments", None) or (
                function.get("arguments") if isinstance(function, dict) else None
            )
            if arguments:
                slot["arguments"] += arguments

    def finish(self) -> list[tuple[str, str, dict]]:
        """
        Return [(id, name, arguments_dict)] in stream order.

        A call whose arguments never parse is still returned, with `{}`. The
        model asked for the tool; letting the tool run and fail with a plain
        error it can read beats silently dropping a call it is waiting on.
        """
        results = []
        for index in sorted(self._calls):
            slot = self._calls[index]
            if not slot["name"]:
                continue
            raw = slot["arguments"].strip()
            try:
                parsed = json.loads(raw) if raw else {}
            except (ValueError, TypeError):
                parsed = {}
            if not isinstance(parsed, dict):
                parsed = {}
            results.append((slot["id"] or f"call_{index}", slot["name"], parsed))
        return results
