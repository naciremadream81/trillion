"""
Tests for per-session Agent isolation in serve.py's /api/chat handler.

A single shared Agent (and so a single shared conversation history) across
every browser tab/user would interleave concurrent chats into one history
list. Each session — identified by a server-set cookie — must get its own
Agent, reused across turns within that session and evicted once
_MAX_CHAT_SESSIONS is exceeded.

Run from the project root:
    python -m unittest tests.test_serve_chat_sessions
"""

from aiohttp.test_utils import AioHTTPTestCase

import serve as serve_module
from agent.providers.base import BaseProvider, ProviderResponse, TextChunk, TokenUsage
from agent.tools.registry import ToolRegistry


class FakeProvider(BaseProvider):
    @property
    def model_name(self):
        return "fake-model"

    async def stream(self, messages, system, tools=None):
        yield TextChunk(text="ok")
        yield ProviderResponse(text="ok", tool_calls=[], usage=TokenUsage(), model=self.model_name)


class TestChatSessionIsolation(AioHTTPTestCase):
    async def get_application(self):
        serve_module._provider = FakeProvider()
        serve_module._registry = ToolRegistry()
        serve_module._agent_sessions.clear()
        return serve_module.build_app()

    def tearDown(self):
        super().tearDown()
        serve_module._provider = None
        serve_module._registry = None
        serve_module._agent_sessions.clear()

    async def test_missing_cookie_gets_a_fresh_session_cookie_set(self):
        resp = await self.client.post("/api/chat", json={"message": "hi"})
        self.assertIn(serve_module._SESSION_COOKIE, resp.cookies)

    async def test_two_sessions_get_independent_agents(self):
        await self.client.post(
            "/api/chat", json={"message": "hi"}, headers={"Cookie": "trillion_session=session-a"}
        )
        await self.client.post(
            "/api/chat", json={"message": "hi"}, headers={"Cookie": "trillion_session=session-b"}
        )
        self.assertEqual(set(serve_module._agent_sessions.keys()), {"session-a", "session-b"})
        self.assertIsNot(
            serve_module._agent_sessions["session-a"], serve_module._agent_sessions["session-b"]
        )

    async def test_same_session_id_reuses_the_same_agent_across_turns(self):
        await self.client.post(
            "/api/chat", json={"message": "hi"}, headers={"Cookie": "trillion_session=session-a"}
        )
        first_agent = serve_module._agent_sessions["session-a"]
        await self.client.post(
            "/api/chat", json={"message": "again"}, headers={"Cookie": "trillion_session=session-a"}
        )
        self.assertIs(serve_module._agent_sessions["session-a"], first_agent)
        self.assertEqual(len(serve_module._agent_sessions), 1)
        self.assertEqual(len(first_agent.history), 4)  # 2 user turns + 2 assistant replies

    async def test_session_cap_evicts_oldest_session_first(self):
        original_cap = serve_module._MAX_CHAT_SESSIONS
        serve_module._MAX_CHAT_SESSIONS = 2
        try:
            for session_id in ("session-a", "session-b", "session-c"):
                await self.client.post(
                    "/api/chat",
                    json={"message": "hi"},
                    headers={"Cookie": f"trillion_session={session_id}"},
                )
            self.assertEqual(set(serve_module._agent_sessions.keys()), {"session-b", "session-c"})
        finally:
            serve_module._MAX_CHAT_SESSIONS = original_cap


if __name__ == "__main__":
    import unittest

    unittest.main()
