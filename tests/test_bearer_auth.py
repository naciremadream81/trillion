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
from agent.security.auth import AuthRateLimiter, bearer_auth_middleware, is_authorized
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


class TestRotationOverlap(unittest.TestCase):
    """§2.1 — PREV is accepted alongside CURRENT during a rotation window."""

    def test_current_token_accepted_with_prev_set(self):
        self.assertTrue(is_authorized({"Authorization": "Bearer new"}, "new", "old"))

    def test_prev_token_accepted_during_rotation(self):
        self.assertTrue(is_authorized({"Authorization": "Bearer old"}, "new", "old"))

    def test_prev_token_rejected_once_rotation_window_closes(self):
        self.assertFalse(is_authorized({"Authorization": "Bearer old"}, "new"))
        self.assertFalse(is_authorized({"Authorization": "Bearer old"}, "new", ""))

    def test_empty_prev_is_not_a_wildcard(self):
        # The bug this guards: treating an empty configured PREV as "matches
        # anything" would authorize every request the moment PREV is cleared.
        self.assertFalse(is_authorized({"Authorization": "Bearer "}, "new", ""))
        self.assertFalse(is_authorized({"Authorization": "Bearer"}, "new", ""))

    def test_unrelated_token_still_rejected_with_prev_set(self):
        self.assertFalse(is_authorized({"Authorization": "Bearer other"}, "new", "old"))

    def test_no_token_configured_still_open_regardless_of_prev(self):
        self.assertTrue(is_authorized({}, "", "old"))


class FakeClock:
    """Monotonic-shaped clock the limiter tests advance by hand."""

    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class TestAuthRateLimiter(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.limiter = AuthRateLimiter(
            limit=10, window_seconds=300.0, lockout_seconds=900.0, clock=self.clock
        )

    def test_not_locked_before_the_limit(self):
        for _ in range(9):
            self.limiter.record_failure("1.2.3.4")
        self.assertIsNone(self.limiter.retry_after("1.2.3.4"))

    def test_locked_on_the_nth_failure(self):
        for _ in range(10):
            self.limiter.record_failure("1.2.3.4")
        self.assertEqual(self.limiter.retry_after("1.2.3.4"), 900)

    def test_lockout_expires_and_clears_the_history(self):
        for _ in range(10):
            self.limiter.record_failure("1.2.3.4")
        self.clock.advance(900.0)
        self.assertIsNone(self.limiter.retry_after("1.2.3.4"))
        # The failures that caused the lock are gone too, so one more miss
        # must not immediately re-lock.
        self.limiter.record_failure("1.2.3.4")
        self.assertIsNone(self.limiter.retry_after("1.2.3.4"))

    def test_retry_after_never_returns_zero(self):
        for _ in range(10):
            self.limiter.record_failure("1.2.3.4")
        self.clock.advance(899.5)
        self.assertEqual(self.limiter.retry_after("1.2.3.4"), 1)

    def test_failures_outside_the_window_do_not_accumulate(self):
        for _ in range(9):
            self.limiter.record_failure("1.2.3.4")
        self.clock.advance(301.0)
        for _ in range(9):
            self.limiter.record_failure("1.2.3.4")
        self.assertIsNone(self.limiter.retry_after("1.2.3.4"))

    def test_success_clears_the_failure_history(self):
        for _ in range(9):
            self.limiter.record_failure("1.2.3.4")
        self.limiter.record_success("1.2.3.4")
        for _ in range(9):
            self.limiter.record_failure("1.2.3.4")
        self.assertIsNone(self.limiter.retry_after("1.2.3.4"))

    def test_addresses_are_tracked_independently(self):
        for _ in range(10):
            self.limiter.record_failure("1.2.3.4")
        self.assertEqual(self.limiter.retry_after("1.2.3.4"), 900)
        self.assertIsNone(self.limiter.retry_after("5.6.7.8"))

    def test_tracked_addresses_stay_bounded(self):
        # Without the cap, spraying spoofed source addresses grows the
        # limiter without bound — the memory bug it exists to prevent.
        limiter = AuthRateLimiter(max_addresses=64, clock=self.clock)
        for i in range(500):
            limiter.record_failure(f"10.0.{i // 256}.{i % 256}")
        self.assertLessEqual(len(limiter._failures), 64)

    def test_empty_address_is_ignored(self):
        for _ in range(20):
            self.limiter.record_failure("")
        self.assertIsNone(self.limiter.retry_after(""))


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
                "TRILLION_CSP_REPORT_DB",
                "GITHUB_TOKEN",
                "TRILLION_GITHUB_WATCHED_REPOS",
                "TRILLION_WEB_AUTH_TOKEN",
            )
        }
        os.environ["TRILLION_FACTORY_DB"] = os.path.join(self.tmp, "factory.db")
        os.environ["TRILLION_NOTES_VAULT_PATH"] = os.path.join(self.tmp, "vault")
        os.environ["TRILLION_NOTES_INDEX_PATH"] = os.path.join(self.tmp, "notes_index.db")
        os.environ["TRILLION_HEARTBEAT_DB"] = os.path.join(self.tmp, "heartbeat.db")
        os.environ["TRILLION_CSP_REPORT_DB"] = os.path.join(self.tmp, "csp_reports.db")
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
                "TRILLION_CSP_REPORT_DB",
                "GITHUB_TOKEN",
                "TRILLION_GITHUB_WATCHED_REPOS",
                "TRILLION_WEB_AUTH_TOKEN",
            )
        }
        os.environ["TRILLION_FACTORY_DB"] = os.path.join(self.tmp, "factory.db")
        os.environ["TRILLION_NOTES_VAULT_PATH"] = os.path.join(self.tmp, "vault")
        os.environ["TRILLION_NOTES_INDEX_PATH"] = os.path.join(self.tmp, "notes_index.db")
        os.environ["TRILLION_HEARTBEAT_DB"] = os.path.join(self.tmp, "heartbeat.db")
        os.environ["TRILLION_CSP_REPORT_DB"] = os.path.join(self.tmp, "csp_reports.db")
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


class TestServeAuthRateLimit(AioHTTPTestCase):
    """§1.4 end-to-end: N+1 bad requests from one address get 429."""

    AUTH_TOKEN = "test-token-value"

    async def get_application(self):
        self.tmp = tempfile.mkdtemp()
        self._prev_env = {
            key: os.environ.get(key)
            for key in (
                "TRILLION_FACTORY_DB",
                "TRILLION_NOTES_VAULT_PATH",
                "TRILLION_NOTES_INDEX_PATH",
                "TRILLION_HEARTBEAT_DB",
                "TRILLION_CSP_REPORT_DB",
                "GITHUB_TOKEN",
                "TRILLION_GITHUB_WATCHED_REPOS",
                "TRILLION_WEB_AUTH_TOKEN",
            )
        }
        os.environ["TRILLION_FACTORY_DB"] = os.path.join(self.tmp, "factory.db")
        os.environ["TRILLION_NOTES_VAULT_PATH"] = os.path.join(self.tmp, "vault")
        os.environ["TRILLION_NOTES_INDEX_PATH"] = os.path.join(self.tmp, "notes_index.db")
        os.environ["TRILLION_HEARTBEAT_DB"] = os.path.join(self.tmp, "heartbeat.db")
        os.environ["TRILLION_CSP_REPORT_DB"] = os.path.join(self.tmp, "csp_reports.db")
        os.environ.pop("GITHUB_TOKEN", None)
        os.environ.pop("TRILLION_GITHUB_WATCHED_REPOS", None)
        os.environ["TRILLION_WEB_AUTH_TOKEN"] = self.AUTH_TOKEN

        serve_module._provider = FakeProvider()
        serve_module._registry = ToolRegistry()
        serve_module._agent = None

        app = serve_module.build_app()
        # Swap in a limiter on a clock we control, so the lockout-expiry
        # case doesn't need a 15-minute sleep.
        self.clock = FakeClock()
        self.limiter = AuthRateLimiter(
            limit=10, window_seconds=300.0, lockout_seconds=900.0, clock=self.clock
        )
        app.middlewares[-1] = bearer_auth_middleware(
            self.AUTH_TOKEN, "", self.limiter
        )
        return app

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

    async def _bad_request(self):
        return await self.client.request(
            "GET", "/api/usage", headers={"Authorization": "Bearer wrong"}
        )

    async def test_limit_bad_requests_401_then_429_with_retry_after(self):
        for _ in range(10):
            resp = await self._bad_request()
            self.assertEqual(resp.status, 401)
        resp = await self._bad_request()
        self.assertEqual(resp.status, 429)
        self.assertEqual(resp.headers["Retry-After"], "900")

    async def test_good_token_also_refused_while_locked_out(self):
        # A lockout is about the address's recent history, not this one
        # credential. Letting a correct token through would make the lock
        # meaningless the moment an attacker guessed right.
        for _ in range(10):
            await self._bad_request()
        resp = await self.client.request(
            "GET", "/api/usage", headers={"Authorization": f"Bearer {self.AUTH_TOKEN}"}
        )
        self.assertEqual(resp.status, 429)

    async def test_good_requests_resume_after_the_lockout_window(self):
        for _ in range(10):
            await self._bad_request()
        self.clock.advance(900.0)
        resp = await self.client.request(
            "GET", "/api/usage", headers={"Authorization": f"Bearer {self.AUTH_TOKEN}"}
        )
        self.assertEqual(resp.status, 200)

    async def test_successful_auth_never_trips_the_limiter(self):
        for _ in range(30):
            resp = await self.client.request(
                "GET", "/api/usage", headers={"Authorization": f"Bearer {self.AUTH_TOKEN}"}
            )
            self.assertEqual(resp.status, 200)

    async def test_429_response_still_carries_security_headers(self):
        for _ in range(11):
            resp = await self._bad_request()
        self.assertEqual(resp.status, 429)
        for name, value in SECURITY_HEADERS.items():
            self.assertEqual(resp.headers[name], value)

    async def test_exempt_csp_endpoint_unaffected_by_lockout(self):
        for _ in range(11):
            await self._bad_request()
        resp = await self.client.request(
            "POST",
            "/api/security/csp-report",
            json={"csp-report": {"violated-directive": "script-src"}},
        )
        self.assertEqual(resp.status, 204)


class TestServeRotationOverlap(AioHTTPTestCase):
    """§2.1 end-to-end: both tokens authenticate while PREV is set."""

    CURRENT = "current-token-value"
    PREV = "previous-token-value"

    async def get_application(self):
        self.tmp = tempfile.mkdtemp()
        self._prev_env = {
            key: os.environ.get(key)
            for key in (
                "TRILLION_FACTORY_DB",
                "TRILLION_NOTES_VAULT_PATH",
                "TRILLION_NOTES_INDEX_PATH",
                "TRILLION_HEARTBEAT_DB",
                "TRILLION_CSP_REPORT_DB",
                "GITHUB_TOKEN",
                "TRILLION_GITHUB_WATCHED_REPOS",
                "TRILLION_WEB_AUTH_TOKEN",
                "TRILLION_WEB_AUTH_TOKEN_PREV",
            )
        }
        os.environ["TRILLION_FACTORY_DB"] = os.path.join(self.tmp, "factory.db")
        os.environ["TRILLION_NOTES_VAULT_PATH"] = os.path.join(self.tmp, "vault")
        os.environ["TRILLION_NOTES_INDEX_PATH"] = os.path.join(self.tmp, "notes_index.db")
        os.environ["TRILLION_HEARTBEAT_DB"] = os.path.join(self.tmp, "heartbeat.db")
        os.environ["TRILLION_CSP_REPORT_DB"] = os.path.join(self.tmp, "csp_reports.db")
        os.environ.pop("GITHUB_TOKEN", None)
        os.environ.pop("TRILLION_GITHUB_WATCHED_REPOS", None)
        os.environ["TRILLION_WEB_AUTH_TOKEN"] = self.CURRENT
        os.environ["TRILLION_WEB_AUTH_TOKEN_PREV"] = self.PREV

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

    async def test_current_token_accepted(self):
        resp = await self.client.request(
            "GET", "/api/usage", headers={"Authorization": f"Bearer {self.CURRENT}"}
        )
        self.assertEqual(resp.status, 200)

    async def test_previous_token_accepted_during_the_window(self):
        resp = await self.client.request(
            "GET", "/api/usage", headers={"Authorization": f"Bearer {self.PREV}"}
        )
        self.assertEqual(resp.status, 200)

    async def test_unrelated_token_still_rejected(self):
        resp = await self.client.request(
            "GET", "/api/usage", headers={"Authorization": "Bearer neither"}
        )
        self.assertEqual(resp.status, 401)


if __name__ == "__main__":
    unittest.main()
