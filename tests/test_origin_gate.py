"""
Tests for agent/security/origin.py and its wiring into serve.py — the CSRF
gate on state-changing /api/ routes.

The vulnerability this closes was reproduced against a live server before the
module existed: a cross-origin `POST /api/security/cve-scan` returned 200 and
shelled out to pip-audit, and a cross-origin form POST to /api/chat with
`enctype="text/plain"` drove a full agent turn — aiohttp's request.json() does
not validate Content-Type, so no preflight ever fired.

Requires aiohttp (a project dependency). Run from the project root:
    python -m unittest tests.test_origin_gate
"""

import os
import shutil
import tempfile
import unittest

from aiohttp.test_utils import AioHTTPTestCase

import serve as serve_module
from agent.providers.base import BaseProvider, ProviderResponse, TextChunk, TokenUsage
from agent.security.headers import SECURITY_HEADERS
from agent.security.origin import allowed_hostnames, check_origin
from agent.tools.registry import ToolRegistry

LOCAL = "127.0.0.1"


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


class TestCheckOrigin(unittest.TestCase):
    """The decision function on its own — plain dicts, no event loop."""

    def test_safe_methods_never_gated(self):
        forged = {"Origin": "https://evil.example"}
        for method in ("GET", "HEAD", "OPTIONS"):
            with self.subTest(method=method):
                self.assertIsNone(check_origin(method, "/api/usage", forged, LOCAL))

    def test_non_api_paths_never_gated(self):
        forged = {"Origin": "https://evil.example"}
        self.assertIsNone(check_origin("POST", "/", forged, LOCAL))
        self.assertIsNone(check_origin("POST", "/index.html", forged, LOCAL))

    def test_csp_report_exempt(self):
        # The browser posts these itself; we cannot influence its headers, and
        # gating them would discard the telemetry the CSP work depends on.
        forged = {"Origin": "https://evil.example"}
        self.assertIsNone(
            check_origin("POST", "/api/security/csp-report", forged, LOCAL)
        )

    def test_cross_origin_post_refused(self):
        for path in ("/api/chat", "/api/security/cve-scan", "/api/heartbeat/dismiss"):
            with self.subTest(path=path):
                reason = check_origin(
                    "POST", path, {"Origin": "https://evil.example"}, LOCAL
                )
                self.assertIsNotNone(reason)
                self.assertIn("evil.example", reason)

    def test_all_guarded_methods_refused(self):
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            with self.subTest(method=method):
                self.assertIsNotNone(
                    check_origin(
                        method, "/api/chat", {"Origin": "https://evil.example"}, LOCAL
                    )
                )

    def test_same_origin_post_allowed(self):
        for origin in (
            "http://127.0.0.1:8123",
            "http://localhost:8123",
            "http://[::1]:8123",
            "http://localhost",
        ):
            with self.subTest(origin=origin):
                self.assertIsNone(
                    check_origin("POST", "/api/chat", {"Origin": origin}, LOCAL)
                )

    def test_origin_null_refused(self):
        # A sandboxed iframe sends this. Distinct from absent, and never
        # something to trust.
        reason = check_origin("POST", "/api/chat", {"Origin": "null"}, LOCAL)
        self.assertEqual(reason, "Origin: null")

    def test_origin_substring_lookalikes_refused(self):
        # The check is on the parsed hostname, not a substring match, so none
        # of these sneak past by containing an allowed name.
        for origin in (
            "http://localhost.evil.example",
            "http://127.0.0.1.evil.example",
            "http://evil.example/?x=localhost",
            "http://evil.example#localhost",
            "http://localhost@evil.example",
        ):
            with self.subTest(origin=origin):
                self.assertIsNotNone(
                    check_origin("POST", "/api/chat", {"Origin": origin}, LOCAL)
                )

    def test_hostile_fetch_site_refused_even_without_origin(self):
        for site in ("cross-site", "same-site"):
            with self.subTest(site=site):
                reason = check_origin(
                    "POST", "/api/chat", {"Sec-Fetch-Site": site}, LOCAL
                )
                self.assertEqual(reason, f"Sec-Fetch-Site: {site}")

    def test_fetch_site_beats_origin(self):
        # Browser-attested and unforgeable by page script, so it is believed
        # over Origin in both directions.
        self.assertIsNone(
            check_origin(
                "POST",
                "/api/chat",
                {"Sec-Fetch-Site": "same-origin", "Origin": "http://localhost:8123"},
                LOCAL,
            )
        )
        self.assertIsNotNone(
            check_origin(
                "POST",
                "/api/chat",
                {"Sec-Fetch-Site": "cross-site", "Origin": "http://localhost:8123"},
                LOCAL,
            )
        )

    def test_fetch_site_none_allowed(self):
        # A typed URL or a bookmark — user-initiated, not page-initiated.
        self.assertIsNone(
            check_origin("POST", "/api/chat", {"Sec-Fetch-Site": "none"}, LOCAL)
        )

    def test_fetch_site_case_and_whitespace_tolerant(self):
        for raw in (" Cross-Site ", "CROSS-SITE", "cross-site"):
            with self.subTest(raw=repr(raw)):
                self.assertIsNotNone(
                    check_origin("POST", "/api/chat", {"Sec-Fetch-Site": raw}, LOCAL)
                )

    def test_no_browser_headers_allowed(self):
        # curl, systemd, the CLI. A caller that can omit both headers is
        # already running an arbitrary HTTP client and has no use for CSRF;
        # refusing here only breaks the reverse-proxy path in the runbook.
        self.assertIsNone(check_origin("POST", "/api/chat", {}, LOCAL))

    def test_foreign_host_refused_even_when_origin_matches_it(self):
        # DNS rebinding: the browser sends Host and Origin that agree with
        # each other, so an Origin-vs-Host comparison alone would pass this.
        reason = check_origin(
            "POST",
            "/api/chat",
            {"Host": "rebind.evil.example", "Origin": "http://rebind.evil.example"},
            LOCAL,
        )
        self.assertIsNotNone(reason)
        self.assertIn("rebind.evil.example", reason)

    def test_loopback_hosts_accepted_on_loopback_bind(self):
        for host in ("127.0.0.1:8123", "localhost:8123", "[::1]:8123"):
            with self.subTest(host=host):
                self.assertIsNone(
                    check_origin("POST", "/api/chat", {"Host": host}, LOCAL)
                )

    def test_non_loopback_bind_allows_only_its_own_name(self):
        allowed = allowed_hostnames("trillion.internal")
        self.assertEqual(allowed, frozenset({"trillion.internal"}))
        self.assertIsNone(
            check_origin(
                "POST",
                "/api/chat",
                {"Host": "trillion.internal", "Origin": "https://trillion.internal"},
                "trillion.internal",
            )
        )
        self.assertIsNotNone(
            check_origin(
                "POST",
                "/api/chat",
                {"Host": "trillion.internal", "Origin": "http://localhost:8123"},
                "trillion.internal",
            )
        )

    def test_loopback_bind_allows_all_loopback_spellings(self):
        allowed = allowed_hostnames(LOCAL)
        self.assertIn("localhost", allowed)
        self.assertIn("127.0.0.1", allowed)

    def test_malformed_headers_refused_not_raised(self):
        # An attacker controls these strings verbatim; a parse failure must be
        # a refusal, not a 500.
        for origin in ("http://[", "://", "http://%", "\udce9"):
            with self.subTest(origin=repr(origin)):
                self.assertIsNotNone(
                    check_origin("POST", "/api/chat", {"Origin": origin}, LOCAL)
                )

    # -- Wildcard bind (TRILLION_WEB_HOST=0.0.0.0 / ::) --
    #
    # Regression: the Host allowlist for a wildcard bind used to be
    # frozenset({"0.0.0.0"}), which no real browser ever sends as a Host
    # value, so every legitimate same-origin request was refused before
    # Sec-Fetch-Site was even read.

    def test_wildcard_bind_allowed_hostnames_is_none(self):
        self.assertIsNone(allowed_hostnames("0.0.0.0"))
        self.assertIsNone(allowed_hostnames("::"))

    def test_wildcard_bind_same_origin_request_allowed(self):
        # The case the bug broke: a real LAN Host, Sec-Fetch-Site attesting
        # same-origin, on a wildcard bind. Must not be refused on Host.
        for web_host in ("0.0.0.0", "::"):
            with self.subTest(web_host=web_host):
                self.assertIsNone(
                    check_origin(
                        "POST",
                        "/api/chat",
                        {
                            "Host": "192.168.1.50:8123",
                            "Sec-Fetch-Site": "same-origin",
                        },
                        web_host,
                    )
                )

    def test_wildcard_bind_hostile_fetch_site_still_refused(self):
        self.assertIsNotNone(
            check_origin(
                "POST",
                "/api/chat",
                {"Host": "192.168.1.50:8123", "Sec-Fetch-Site": "cross-site"},
                "0.0.0.0",
            )
        )

    def test_wildcard_bind_null_origin_still_refused(self):
        reason = check_origin(
            "POST", "/api/chat", {"Host": "192.168.1.50:8123", "Origin": "null"}, "0.0.0.0"
        )
        self.assertEqual(reason, "Origin: null")

    def test_wildcard_bind_origin_without_fetch_site_refused(self):
        # No Sec-Fetch-Site and no way to verify the Origin's hostname against
        # a wildcard bind — refused rather than trusted, per the module
        # docstring's documented residual.
        reason = check_origin(
            "POST",
            "/api/chat",
            {"Host": "192.168.1.50:8123", "Origin": "http://192.168.1.50:8123"},
            "0.0.0.0",
        )
        self.assertIsNotNone(reason)
        self.assertIn("cannot be verified", reason)

    def test_wildcard_bind_no_browser_headers_still_allowed(self):
        # curl / systemd / a reverse proxy — unchanged by the wildcard case.
        self.assertIsNone(check_origin("POST", "/api/chat", {}, "0.0.0.0"))


class TestServeOriginGate(AioHTTPTestCase):
    """End to end through build_app(), which is where the wiring can break."""

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

        serve_module._provider = FakeProvider(["hi"])
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

    async def test_forged_origin_on_chat_refused(self):
        resp = await self.client.request(
            "POST",
            "/api/chat",
            json={"message": "hi"},
            headers={"Origin": "https://evil.example"},
        )
        self.assertEqual(resp.status, 403)
        body = await resp.json()
        self.assertEqual(body["error"], "cross-origin request refused")

    async def test_text_plain_form_trick_refused(self):
        # The exact shape that worked before this middleware: a cross-origin
        # HTML form POST, which needs no preflight because text/plain is a
        # CORS-safelisted content type, against a handler that parses the body
        # as JSON regardless.
        resp = await self.client.request(
            "POST",
            "/api/chat",
            data='{"message": "drain the budget"}',
            headers={
                "Origin": "https://evil.example",
                "Content-Type": "text/plain;charset=UTF-8",
            },
        )
        self.assertEqual(resp.status, 403)

    async def test_forged_origin_on_cve_scan_refused(self):
        # No body at all, and it shells out to pip-audit — the cheapest
        # cross-origin trigger in the app.
        resp = await self.client.request(
            "POST",
            "/api/security/cve-scan",
            headers={"Origin": "https://evil.example"},
        )
        self.assertEqual(resp.status, 403)

    async def test_cross_site_fetch_metadata_refused(self):
        resp = await self.client.request(
            "POST",
            "/api/chat",
            json={"message": "hi"},
            headers={"Sec-Fetch-Site": "cross-site"},
        )
        self.assertEqual(resp.status, 403)

    async def test_same_origin_post_still_works(self):
        # The regression that matters most: the real UI must keep working.
        host = f"127.0.0.1:{self.server.port}"
        resp = await self.client.request(
            "POST",
            "/api/chat",
            json={"message": "hi"},
            headers={"Origin": f"http://{host}", "Sec-Fetch-Site": "same-origin"},
        )
        self.assertEqual(resp.status, 200)

    async def test_get_with_forged_origin_still_allowed(self):
        resp = await self.client.request(
            "GET", "/api/usage", headers={"Origin": "https://evil.example"}
        )
        self.assertEqual(resp.status, 200)

    async def test_csp_report_still_reachable_cross_origin(self):
        resp = await self.client.request(
            "POST",
            "/api/security/csp-report",
            json={"csp-report": {"violated-directive": "script-src"}},
            headers={"Origin": "https://evil.example"},
        )
        self.assertEqual(resp.status, 204)

    async def test_403_carries_security_headers(self):
        # security_headers_middleware is outermost, so a refusal is still a
        # fully-hardened response.
        resp = await self.client.request(
            "POST",
            "/api/chat",
            json={"message": "hi"},
            headers={"Origin": "https://evil.example"},
        )
        self.assertEqual(resp.status, 403)
        for name, value in SECURITY_HEADERS.items():
            self.assertEqual(resp.headers[name], value)


class TestCspReportBounded(AioHTTPTestCase):
    """The one write surface exempt from both gates."""

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

    async def test_oversized_report_truncated_not_500(self):
        huge = "A" * (serve_module.CSP_REPORT_MAX_BYTES * 4)
        resp = await self.client.request(
            "POST", "/api/security/csp-report", data=huge
        )
        self.assertEqual(resp.status, 204)

    async def test_newline_injection_does_not_500(self):
        # Newlines get collapsed so a crafted report can't forge log lines.
        resp = await self.client.request(
            "POST",
            "/api/security/csp-report",
            data="a\n[csp-violation] forged\nb",
        )
        self.assertEqual(resp.status, 204)

    async def test_malformed_body_still_204(self):
        resp = await self.client.request(
            "POST", "/api/security/csp-report", data=b"\xff\xfe not json"
        )
        self.assertEqual(resp.status, 204)


if __name__ == "__main__":
    unittest.main()
