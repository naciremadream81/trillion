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
from agent.security.headers import (
    SECURITY_HEADERS,
    apply_security_headers,
    build_csp_policies,
    inline_asset_hashes,
)
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


_FIXTURE_HTML = """<html><head>
<style>body { color: red; }</style>
</head><body>
<script type="importmap">{"imports": {}}</script>
<script type="module">console.log("a");</script>
<script>console.log("b");</script>
<script src="/vendor/external.js"></script>
</body></html>"""


class TestInlineAssetHashes(unittest.TestCase):
    def test_finds_one_style_block(self):
        script_hashes, style_hashes = inline_asset_hashes(_FIXTURE_HTML)
        self.assertEqual(len(style_hashes), 1)

    def test_finds_every_inline_script_including_importmap(self):
        script_hashes, _style_hashes = inline_asset_hashes(_FIXTURE_HTML)
        # importmap + module + plain script = 3; the src= one is excluded.
        self.assertEqual(len(script_hashes), 3)

    def test_external_script_src_is_excluded(self):
        script_hashes, _style_hashes = inline_asset_hashes(_FIXTURE_HTML)
        for h in script_hashes:
            self.assertNotIn("external.js", h)

    def test_hashes_are_stable_for_identical_content(self):
        a = inline_asset_hashes(_FIXTURE_HTML)
        b = inline_asset_hashes(_FIXTURE_HTML)
        self.assertEqual(a, b)

    def test_hash_changes_if_inline_content_changes(self):
        changed = _FIXTURE_HTML.replace("color: red", "color: blue")
        a = inline_asset_hashes(_FIXTURE_HTML)
        b = inline_asset_hashes(changed)
        self.assertNotEqual(a, b)


class TestBuildCspPolicies(unittest.TestCase):
    def test_enforcing_policy_has_no_unsafe_inline(self):
        enforcing, _candidate = build_csp_policies(_FIXTURE_HTML)
        self.assertNotIn("'unsafe-inline'", enforcing)

    def test_enforcing_policy_carries_hash_sources(self):
        enforcing, _candidate = build_csp_policies(_FIXTURE_HTML)
        self.assertIn("'sha256-", enforcing)

    def test_neither_policy_includes_unsafe_eval(self):
        enforcing, candidate = build_csp_policies(_FIXTURE_HTML)
        self.assertNotIn("unsafe-eval", enforcing)
        self.assertNotIn("unsafe-eval", candidate)

    def test_enforcing_connect_src_drops_websocket_schemes(self):
        enforcing, _candidate = build_csp_policies(_FIXTURE_HTML)
        self.assertNotIn("ws:", enforcing)
        self.assertNotIn("wss:", enforcing)

    def test_candidate_is_stricter_than_enforcing_on_img_src(self):
        enforcing, candidate = build_csp_policies(_FIXTURE_HTML)
        self.assertIn("img-src 'self' data: blob:", enforcing)
        self.assertIn("img-src 'self';", candidate)


class TestApplySecurityHeaders(unittest.TestCase):
    def test_all_fixed_headers_present(self):
        headers = {}
        apply_security_headers(headers, "enforcing-policy", "candidate-policy")
        for name, value in SECURITY_HEADERS.items():
            self.assertEqual(headers[name], value)

    def test_csp_ships_both_enforcing_and_report_only(self):
        headers = {}
        apply_security_headers(headers, "enforcing-policy", "candidate-policy")
        self.assertEqual(headers["Content-Security-Policy"], "enforcing-policy")
        self.assertEqual(headers["Content-Security-Policy-Report-Only"], "candidate-policy")

    def test_reporting_endpoints_header_present(self):
        headers = {}
        apply_security_headers(headers, "enforcing-policy", "candidate-policy")
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
        self.assertIn("Content-Security-Policy", resp.headers)
        self.assertIn("Content-Security-Policy-Report-Only", resp.headers)

    async def test_enforcing_csp_matches_real_index_html_hashes(self):
        # build_app() reads the real index.html at PROJECT_ROOT — assert the
        # served header is exactly what build_csp_policies() computes from
        # that same file, not a stale or hand-copied string.
        from agent.security.headers import build_csp_policies

        with open(os.path.join(serve_module.PROJECT_ROOT, "index.html"), "r", encoding="utf-8") as f:
            expected_enforcing, expected_candidate = build_csp_policies(f.read())
        resp = await self.client.request("GET", "/")
        self.assertEqual(resp.headers["Content-Security-Policy"], expected_enforcing)
        self.assertEqual(resp.headers["Content-Security-Policy-Report-Only"], expected_candidate)

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
