"""
Tests for agent/security/headers.py and its wiring into serve.py
(agent-security.md §2.2).

Requires aiohttp (a project dependency). Run from the project root:
    python -m unittest tests.test_security_headers
"""

import os
import shutil
import tempfile
import unittest

from aiohttp.test_utils import AioHTTPTestCase

import serve as serve_module
from agent.providers.base import BaseProvider, ProviderResponse, TextChunk, TokenUsage
from agent.security.headers import SECURITY_HEADERS, apply_security_headers
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


class TestApplySecurityHeaders(unittest.TestCase):
    def test_all_fixed_headers_present(self):
        headers = {}
        apply_security_headers(headers)
        for name, value in SECURITY_HEADERS.items():
            self.assertEqual(headers[name], value)

    def test_csp_ships_report_only_not_enforcing(self):
        headers = {}
        apply_security_headers(headers)
        self.assertIn("Content-Security-Policy-Report-Only", headers)
        self.assertNotIn("Content-Security-Policy", headers)

    def test_csp_never_includes_unsafe_eval(self):
        headers = {}
        apply_security_headers(headers)
        self.assertNotIn("unsafe-eval", headers["Content-Security-Policy-Report-Only"])

    def test_reporting_endpoints_header_present(self):
        headers = {}
        apply_security_headers(headers)
        self.assertIn("Reporting-Endpoints", headers)
        self.assertIn("/api/security/csp-report", headers["Reporting-Endpoints"])


class TestServeSecurityHeaders(AioHTTPTestCase):
    async def get_application(self):
        self.tmp = tempfile.mkdtemp()

        # Mirror test_heartbeat_endpoints.py's isolation: build_app() also
        # starts the Factory watcher, notes index build, and heartbeat
        # scheduler on startup, so point every one of them at this test's
        # temp dir rather than real project state.
        self._prev_env = {
            key: os.environ.get(key)
            for key in (
                "TRILLION_FACTORY_DB",
                "TRILLION_NOTES_VAULT_PATH",
                "TRILLION_NOTES_INDEX_PATH",
                "TRILLION_HEARTBEAT_DB",
                "GITHUB_TOKEN",
                "TRILLION_GITHUB_WATCHED_REPOS",
            )
        }
        os.environ["TRILLION_FACTORY_DB"] = os.path.join(self.tmp, "factory.db")
        os.environ["TRILLION_NOTES_VAULT_PATH"] = os.path.join(self.tmp, "vault")
        os.environ["TRILLION_NOTES_INDEX_PATH"] = os.path.join(self.tmp, "notes_index.db")
        os.environ["TRILLION_HEARTBEAT_DB"] = os.path.join(self.tmp, "heartbeat.db")
        os.environ.pop("GITHUB_TOKEN", None)
        os.environ.pop("TRILLION_GITHUB_WATCHED_REPOS", None)

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

    async def test_index_response_carries_security_headers(self):
        resp = await self.client.request("GET", "/")
        for name, value in SECURITY_HEADERS.items():
            self.assertEqual(resp.headers[name], value)
        self.assertIn("Content-Security-Policy-Report-Only", resp.headers)

    async def test_api_response_carries_security_headers(self):
        resp = await self.client.request("GET", "/api/usage")
        for name, value in SECURITY_HEADERS.items():
            self.assertEqual(resp.headers[name], value)

    async def test_csp_report_endpoint_accepts_violation_and_returns_204(self):
        resp = await self.client.request(
            "POST",
            "/api/security/csp-report",
            json={"csp-report": {"violated-directive": "script-src"}},
        )
        self.assertEqual(resp.status, 204)

    async def test_csp_report_endpoint_tolerates_malformed_body(self):
        resp = await self.client.request(
            "POST",
            "/api/security/csp-report",
            data=b"not json",
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(resp.status, 204)


if __name__ == "__main__":
    unittest.main()
