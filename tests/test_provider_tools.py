"""
Tests for tool support on the non-Claude providers.

The gap: agent/providers/openai_provider.py forwarded Anthropic-shaped tool
schemas verbatim, and agent/providers/ollama.py accepted `tools` and never
sent it. Both meant TRILLION_PROVIDER=openai / =ollama produced an agent
that could still talk but had silently lost every capability — README
advertised a one-env-var provider swap that quietly dropped every tool.

Run from the project root:
    python -m unittest tests.test_provider_tools
"""

import json
import unittest

from agent.providers._openai_tools import (
    MAX_TOOL_RESULT_CHARS,
    ToolCallAccumulator,
    to_openai_messages,
    to_openai_tools,
)


class TestToolSchemaTranslation(unittest.TestCase):
    def test_anthropic_schema_becomes_an_openai_function(self):
        out = to_openai_tools(
            [{"name": "search", "description": "Look things up",
              "input_schema": {"type": "object", "properties": {"q": {"type": "string"}}}}]
        )
        self.assertEqual(out[0]["type"], "function")
        self.assertEqual(out[0]["function"]["name"], "search")
        self.assertEqual(out[0]["function"]["description"], "Look things up")
        # `parameters` is a rename of `input_schema`; the JSON Schema inside
        # is identical, which is why this is not a rewrite.
        self.assertEqual(out[0]["function"]["parameters"]["properties"], {"q": {"type": "string"}})

    def test_empty_and_none_produce_none_not_an_empty_list(self):
        # Some OpenAI-compatible endpoints reject `tools: []`.
        self.assertIsNone(to_openai_tools([]))
        self.assertIsNone(to_openai_tools(None))

    def test_a_schemaless_tool_still_gets_a_parameters_object(self):
        out = to_openai_tools([{"name": "ping", "description": "d"}])
        self.assertEqual(out[0]["function"]["parameters"], {"type": "object", "properties": {}})

    def test_malformed_entries_are_skipped_not_forwarded(self):
        self.assertIsNone(to_openai_tools([{"description": "no name"}, "junk"]))


class TestMessageTranslation(unittest.TestCase):
    def test_plain_turns_pass_through(self):
        out = to_openai_messages(
            [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}]
        )
        self.assertEqual(out, [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ])

    def test_tool_use_blocks_become_tool_calls(self):
        out = to_openai_messages([{
            "role": "assistant",
            "content": [
                {"type": "text", "text": "checking"},
                {"type": "tool_use", "id": "a1", "name": "search", "input": {"q": "x"}},
            ],
        }])
        self.assertEqual(out[0]["content"], "checking")
        call = out[0]["tool_calls"][0]
        self.assertEqual(call["id"], "a1")
        self.assertEqual(call["function"]["name"], "search")
        # OpenAI wants the arguments as a JSON *string*, not an object.
        self.assertEqual(json.loads(call["function"]["arguments"]), {"q": "x"})

    def test_a_toolonly_assistant_turn_has_null_content(self):
        # Explicit None, not "": an empty string reads as the assistant
        # having said nothing rather than having only called a tool.
        out = to_openai_messages([{
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "a1", "name": "t", "input": {}}],
        }])
        self.assertIsNone(out[0]["content"])

    def test_each_tool_result_becomes_its_own_tool_message(self):
        # The asymmetry that bites: Anthropic batches every result of a
        # parallel round into one user turn; OpenAI needs them separate and
        # each keyed to its own tool_call_id, or the model can't tell which
        # result belongs to which call.
        out = to_openai_messages([{
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "a1", "content": "first"},
                {"type": "tool_result", "tool_use_id": "a2", "content": "second"},
            ],
        }])
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0], {"role": "tool", "tool_call_id": "a1", "content": "first"})
        self.assertEqual(out[1], {"role": "tool", "tool_call_id": "a2", "content": "second"})

    def test_block_shaped_tool_result_content_is_flattened(self):
        out = to_openai_messages([{
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "a1",
                         "content": [{"type": "text", "text": "hello"}]}],
        }])
        self.assertEqual(out[0]["content"], "hello")

    def test_an_enormous_tool_result_is_bounded(self):
        # History is re-sent every round, so an unbounded result is paid for
        # repeatedly.
        out = to_openai_messages([{
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "a1", "content": "x" * 500_000}],
        }])
        self.assertLess(len(out[0]["content"]), MAX_TOOL_RESULT_CHARS + 100)
        self.assertIn("truncated", out[0]["content"])

    def test_a_full_tool_round_trip_translates_in_order(self):
        history = [
            {"role": "user", "content": "what is X"},
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "a1", "name": "search", "input": {"q": "X"}}]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "a1", "content": "X is 42"}]},
            {"role": "assistant", "content": "X is 42."},
        ]
        out = to_openai_messages(history)
        self.assertEqual([m["role"] for m in out], ["user", "assistant", "tool", "assistant"])


class _Delta:
    """Shaped like an OpenAI streamed tool_call fragment."""

    def __init__(self, index, id=None, name=None, arguments=None):
        self.index = index
        self.id = id
        self.function = type("F", (), {"name": name, "arguments": arguments})()


class TestToolCallAccumulator(unittest.TestCase):
    def test_fragments_reassemble_into_one_call(self):
        acc = ToolCallAccumulator()
        acc.add_delta([_Delta(0, id="c1", name="search", arguments='{"q":')])
        acc.add_delta([_Delta(0, arguments='"hello"}')])
        self.assertEqual(acc.finish(), [("c1", "search", {"q": "hello"})])

    def test_keyed_on_index_because_later_fragments_carry_no_id(self):
        acc = ToolCallAccumulator()
        acc.add_delta([_Delta(0, id="c1", name="a", arguments="{}")])
        acc.add_delta([_Delta(1, id="c2", name="b", arguments="{}")])
        self.assertEqual([c[1] for c in acc.finish()], ["a", "b"])

    def test_unparseable_arguments_still_yield_the_call(self):
        # The model asked for the tool; letting it run and fail with a plain
        # error beats silently dropping a call it is waiting on.
        acc = ToolCallAccumulator()
        acc.add_delta([_Delta(0, id="c1", name="search", arguments="{not json")])
        self.assertEqual(acc.finish(), [("c1", "search", {})])

    def test_empty_arguments_become_an_empty_dict(self):
        acc = ToolCallAccumulator()
        acc.add_delta([_Delta(0, id="c1", name="ping", arguments="")])
        self.assertEqual(acc.finish(), [("c1", "ping", {})])

    def test_a_nameless_fragment_is_dropped(self):
        acc = ToolCallAccumulator()
        acc.add_delta([_Delta(0, id="c1", arguments="{}")])
        self.assertEqual(acc.finish(), [])

    def test_dict_shaped_deltas_work_too(self):
        acc = ToolCallAccumulator()
        acc.add_delta([{"index": 0, "id": "c1", "function": {"name": "t", "arguments": '{"a":1}'}}])
        self.assertEqual(acc.finish(), [("c1", "t", {"a": 1})])

    def test_a_missing_id_falls_back_to_a_positional_one(self):
        acc = ToolCallAccumulator()
        acc.add_delta([_Delta(0, name="t", arguments="{}")])
        self.assertEqual(acc.finish()[0][0], "call_0")

    def test_non_object_arguments_are_rejected(self):
        acc = ToolCallAccumulator()
        acc.add_delta([_Delta(0, id="c1", name="t", arguments="[1,2,3]")])
        self.assertEqual(acc.finish(), [("c1", "t", {})])


class _FakeChunk:
    def __init__(self, content=None, tool_calls=None, usage=None, model="gpt-4o"):
        self.model = model
        self.usage = usage
        if content is None and tool_calls is None:
            self.choices = []
        else:
            delta = type("D", (), {"content": content, "tool_calls": tool_calls})()
            self.choices = [type("C", (), {"delta": delta})()]


class _FakeStream:
    def __init__(self, chunks):
        self._chunks = chunks

    def __aiter__(self):
        async def gen():
            for chunk in self._chunks:
                yield chunk

        return gen()


class TestOpenAIProviderEmitsToolCalls(unittest.TestCase):
    """The end-to-end property: a tool call on the wire becomes a ToolCall."""

    def _provider(self, chunks):
        import os

        prev = os.environ.get("OPENAI_API_KEY")
        os.environ["OPENAI_API_KEY"] = "test-key"
        try:
            from agent.providers.openai_provider import OpenAIProvider

            provider = OpenAIProvider()
        finally:
            if prev is None:
                os.environ.pop("OPENAI_API_KEY", None)
            else:
                os.environ["OPENAI_API_KEY"] = prev

        self.sent = {}

        async def fake_create(**kwargs):
            self.sent.update(kwargs)
            return _FakeStream(chunks)

        provider.client.chat.completions.create = fake_create
        return provider

    def _collect(self, provider, tools=None):
        import asyncio

        async def go():
            return [e async for e in provider.stream([{"role": "user", "content": "hi"}], "sys", tools)]

        return asyncio.run(go())

    def test_a_streamed_tool_call_is_yielded_as_a_ToolCall(self):
        from agent.providers.base import ProviderResponse, ToolCall

        chunks = [
            _FakeChunk(tool_calls=[_Delta(0, id="c1", name="search", arguments='{"q":')]),
            _FakeChunk(tool_calls=[_Delta(0, arguments='"hi"}')]),
        ]
        events = self._collect(self._provider(chunks), tools=[{"name": "search", "description": "d"}])
        calls = [e for e in events if isinstance(e, ToolCall)]
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].name, "search")
        self.assertEqual(calls[0].arguments, {"q": "hi"})
        final = [e for e in events if isinstance(e, ProviderResponse)][0]
        self.assertEqual(final.stop_reason, "tool_use")
        self.assertEqual(len(final.tool_calls), 1)

    def test_tools_go_out_in_openai_format_not_anthropic(self):
        # The actual regression: Anthropic-shaped schemas used to be
        # forwarded verbatim.
        self._collect(
            self._provider([_FakeChunk(content="ok")]),
            tools=[{"name": "search", "description": "d", "input_schema": {"type": "object"}}],
        )
        sent = self.sent["tools"][0]
        self.assertEqual(sent["type"], "function")
        self.assertIn("parameters", sent["function"])
        self.assertNotIn("input_schema", sent["function"])

    def test_no_tools_means_the_key_is_omitted_entirely(self):
        self._collect(self._provider([_FakeChunk(content="ok")]))
        self.assertNotIn("tools", self.sent)

    def test_a_plain_reply_still_streams_text_and_ends_cleanly(self):
        from agent.providers.base import ProviderResponse, TextChunk, ToolCall

        events = self._collect(self._provider([_FakeChunk(content="hello")]))
        self.assertEqual([e.text for e in events if isinstance(e, TextChunk)], ["hello"])
        self.assertEqual([e for e in events if isinstance(e, ToolCall)], [])
        self.assertEqual([e for e in events if isinstance(e, ProviderResponse)][0].stop_reason, "end_turn")

    def test_history_is_translated_before_being_sent(self):
        provider = self._provider([_FakeChunk(content="ok")])
        import asyncio

        history = [
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "a1", "name": "t", "input": {}}]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "a1", "content": "r"}]},
        ]

        async def go():
            return [e async for e in provider.stream(history, "sys")]

        asyncio.run(go())
        roles = [m["role"] for m in self.sent["messages"]]
        self.assertEqual(roles, ["system", "assistant", "tool"])


if __name__ == "__main__":
    unittest.main()
