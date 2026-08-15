"""
Tests for agent/security/auth.py and its wiring into serve.py — bearer-token
auth on /api/ routes (the request-time counterpart to startup_guard.py's
bind-time check; see docs/incident-runbook.md's TRILLION_WEB_AUTH_TOKEN
section for the gap this closes).

Requires aiohttp (a project dependency). Run from the project root:
    python -m unittest tests.test_bearer_auth
"""

import os
import shutil
import tempfile
import unittest

from aiohttp.test_utils import AioHTTPTestCase

import serve as serve_module
from agent.providers.base import BaseProvider, ProviderResponse, TextChunk, TokenUsage
from agent.security.auth import is_authorized
from agent.security.headers import SECURITY_HEADERS
from agent.tools.registry import ToolRegistry


class FakeProvider(BaseProvider):
    def __init__(self, replies=None):
        self._replies = list(replies or [])

    @property
    def model_name(self):
        return "fake-model"

    async def stream(self, messages, system, tools=None):
        text = self._replies.pop(0) if self._replies else ""
        yield TextChunk(text=text)
        yield ProviderResponse(text=text, tool_calls=[], usage=TokenUsage(), model=self.model_name)


class TestIsAuthorized(unittest.TestCase):
    def test_no_token_configured_always_authorized(self):
        self.assertTrue(is_authorized({}, ""))
        self.assertTrue(is_authorized({"Authorization": "garbage"}, ""))

    def test_missing_header_rejected_when_token_set(self):
        self.assertFalse(is_authorized({}, "secret"))

    def test_wrong_scheme_rejected(self):
        self.assertFalse(is_authorized({"Authorization": "Basic secret"}, "secret"))

    def test_wrong_token_rejected(self):
        self.assertFalse(is_authorized({"Authorization": "Bearer nope"}, "secret"))

    def test_correct_bearer_token_authorized(self):
        self.assertTrue(is_authorized({"Authorization": "Bearer secret"}, "secret"))

    def test_scheme_matched_case_insensitively(self):
        # RFC 7235 §2.1 — auth scheme names are case-insensitive, so a
        # standards-compliant client sending "bearer" must not get a 401.
        for scheme in ("bearer", "BEARER", "BeArEr"):
            with self.subTest(scheme=scheme):
                self.assertTrue(is_authorized({"Authorization": f"{scheme} secret"}, "secret"))

    def test_non_ascii_credential_rejected_not_raised(self):
        # hmac.compare_digest() raises TypeError on non-ASCII str, which an
        # unauthenticated client could use to turn a 401 into a 500. The
        # comparison happens on encoded bytes, so this is a plain non-match.
        for value in ("é", "Ã©", "éÿ", "tokén"):
            with self.subTest(value=value):
                self.assertFalse(is_authorized({"Authorization": f"Bearer {value}"}, "secret"))

    def test_surrogate_escaped_credential_rejected_not_raised(self):
        # A raw non-UTF-8 byte on the wire (0xE9) reaches us from aiohttp
        # already decoded with surrogateescape. Plain .encode("utf-8") would
        # raise UnicodeEncodeError here and 500 the request.
        for value in ("\udce9", "tok\udce9n", "\udcff\udcfe"):
            with self.subTest(value=repr(value)):
                self.assertFalse(is_authorized({"Authorization": f"Bearer {value}"}, "secret"))

    def test_non_ascii_token_still_matches_itself(self):
        # Encoding both sides means a non-ASCII configured token is usable
        # rather than permanently un-matchable.
        self.assertTrue(is_authorized({"Authorization": "Bearer tokén"}, "tokén"))
        self.assertFalse(is_authorized({"Authorization": "Bearer tokén"}, "token"))


class TestServeBearerAuth(AioHTTPTestCase):
    AUTH_TOKEN = "test-token-value"

    async def get_application(self):
        self.tmp = tempfile.mkdtemp()

        # Mirror test_security_headers.py's isolation.
        self._prev_env = {
            key: os.environ.get(key)
            for key in (
                "TRILLION_FACTORY_DB",
                "TRILLION_NOTES_VAULT_PATH",
                "TRILLION_NOTES_INDEX_PATH",
                "TRILLION_HEARTBEAT_DB",
                "GITHUB_TOKEN",
                "TRILLION_GITHUB_WATCHED_REPOS",
                "TRILLION_WEB_AUTH_TOKEN",
            )
        }
        os.environ["TRILLION_FACTORY_DB"] = os.path.join(self.tmp, "factory.db")
        os.environ["TRILLION_NOTES_VAULT_PATH"] = os.path.join(self.tmp, "vault")
        os.environ["TRILLION_NOTES_INDEX_PATH"] = os.path.join(self.tmp, "notes_index.db")
        os.environ["TRILLION_HEARTBEAT_DB"] = os.path.join(self.tmp, "heartbeat.db")
        os.environ.pop("GITHUB_TOKEN", None)
        os.environ.pop("TRILLION_GITHUB_WATCHED_REPOS", None)
        os.environ["TRILLION_WEB_AUTH_TOKEN"] = self.AUTH_TOKEN

        serve_module._provider = FakeProvider()
        serve_module._registry = ToolRegistry()
        serve_module._agent = None

        return serve_module.build_app()

    def tearDown(self):
        super().tearDown()
        serve_module._provider = None
        serve_module._registry = None
        serve_module._agent = None
        for key, prev in self._prev_env.items():
            if prev is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = prev
        shutil.rmtree(self.tmp, ignore_errors=True)

    async def test_api_route_rejects_missing_header(self):
        resp = await self.client.request("GET", "/api/usage")
        self.assertEqual(resp.status, 401)

    async def test_non_ascii_header_returns_401_not_500(self):
        # Regression: comparing non-ASCII str raised TypeError inside the
        # middleware, letting an unauthenticated client force a 500.
        resp = await self.client.request(
            "GET", "/api/usage", headers={"Authorization": "Bearer é"}
        )
        self.assertEqual(resp.status, 401)

    async def test_raw_non_utf8_byte_returns_401_not_500(self):
        # Regression: 0xE9 arrives surrogate-escaped and blew up the encode.
        resp = await self.client.request(
            "GET", "/api/usage", headers={"Authorization": "Bearer \udce9"}
        )
        self.assertEqual(resp.status, 401)

    async def test_lowercase_bearer_scheme_accepted(self):
        resp = await self.client.request(
            "GET", "/api/usage", headers={"Authorization": f"bearer {self.AUTH_TOKEN}"}
        )
        self.assertEqual(resp.status, 200)

    async def test_api_route_rejects_wrong_token(self):
        resp = await self.client.request(
            "GET", "/api/usage", headers={"Authorization": "Bearer wrong-token"}
        )
        self.assertEqual(resp.status, 401)

    async def test_api_route_accepts_correct_bearer_token(self):
        resp = await self.client.request(
            "GET", "/api/usage", headers={"Authorization": f"Bearer {self.AUTH_TOKEN}"}
        )
        self.assertEqual(resp.status, 200)

    async def test_csp_report_endpoint_exempt_even_with_token_configured(self):
        resp = await self.client.request(
            "POST",
            "/api/security/csp-report",
            json={"csp-report": {"violated-directive": "script-src"}},
        )
        self.assertEqual(resp.status, 204)

    async def test_static_ui_routes_exempt_even_with_token_configured(self):
        resp = await self.client.request("GET", "/")
        self.assertEqual(resp.status, 200)

    async def test_401_response_still_carries_security_headers(self):
        resp = await self.client.request("GET", "/api/usage")
        self.assertEqual(resp.status, 401)
        for name, value in SECURITY_HEADERS.items():
            self.assertEqual(resp.headers[name], value)


class TestServeNoAuthConfigured(AioHTTPTestCase):
    async def get_application(self):
        self.tmp = tempfile.mkdtemp()
        self._prev_env = {
            key: os.environ.get(key)
            for key in (
                "TRILLION_FACTORY_DB",
                "TRILLION_NOTES_VAULT_PATH",
                "TRILLION_NOTES_INDEX_PATH",
                "TRILLION_HEARTBEAT_DB",
                "GITHUB_TOKEN",
                "TRILLION_GITHUB_WATCHED_REPOS",
                "TRILLION_WEB_AUTH_TOKEN",
            )
        }
        os.environ["TRILLION_FACTORY_DB"] = os.path.join(self.tmp, "factory.db")
        os.environ["TRILLION_NOTES_VAULT_PATH"] = os.path.join(self.tmp, "vault")
        os.environ["TRILLION_NOTES_INDEX_PATH"] = os.path.join(self.tmp, "notes_index.db")
        os.environ["TRILLION_HEARTBEAT_DB"] = os.path.join(self.tmp, "heartbeat.db")
        os.environ.pop("GITHUB_TOKEN", None)
        os.environ.pop("TRILLION_GITHUB_WATCHED_REPOS", None)
        os.environ.pop("TRILLION_WEB_AUTH_TOKEN", None)

        serve_module._provider = FakeProvider()
        serve_module._registry = ToolRegistry()
        serve_module._agent = None

        return serve_module.build_app()

    def tearDown(self):
        super().tearDown()
        serve_module._provider = None
        serve_module._registry = None
        serve_module._agent = None
        for key, prev in self._prev_env.items():
            if prev is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = prev
        shutil.rmtree(self.tmp, ignore_errors=True)

    async def test_api_route_allowed_without_header_when_no_token_configured(self):
        resp = await self.client.request("GET", "/api/usage")
        self.assertEqual(resp.status, 200)


if __name__ == "__main__":
    unittest.main()
