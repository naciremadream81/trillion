"""
Tests for agent/security/audit.py (§3.5) and the GET /api/security/status
endpoint it powers.

Never a live network call, per this project's test convention: cve-scan
history is seeded directly into a temp CveScanRepo rather than hitting
pip-audit or PyPI.

Run from the project root:
    python -m unittest tests.test_security_audit
"""

import datetime as _datetime
import os
import shutil
import tempfile
import unittest

from aiohttp.test_utils import AioHTTPTestCase

from agent.config import Settings
from agent.security.audit import audit, compute_score
from agent.security.cve_scan import CveScanRepo, CveScanResult
from agent.tools.base import BaseTool
from agent.tools.registry import ToolRegistry


class FakeReadOnlyTool(BaseTool):
    name = "fake_read_only"
    risk = "read_only"

    async def run(self, **kwargs) -> str:
        return ""


class FakeGatedTool(BaseTool):
    name = "fake_gated"
    risk = "consequential"

    async def run(self, **kwargs) -> str:
        return ""


class FakeUngatedLowTool(BaseTool):
    name = "fake_low"
    risk = "low"

    async def run(self, **kwargs) -> str:
        return ""


class TestComputeScore(unittest.TestCase):
    def test_no_signals_is_perfect_green(self):
        result = compute_score([])
        self.assertEqual(result["score"], 100)
        self.assertEqual(result["color"], "green")

    def test_score_clamped_at_zero(self):
        from agent.security.audit import Signal

        signals = [Signal("x", "X", "bad", -500, "critical")]
        result = compute_score(signals)
        self.assertEqual(result["score"], 0)
        self.assertEqual(result["color"], "red")

    def test_green_amber_red_boundaries(self):
        from agent.security.audit import Signal

        self.assertEqual(compute_score([Signal("x", "X", "v", -15, "warning")])["color"], "green")
        self.assertEqual(compute_score([Signal("x", "X", "v", -16, "warning")])["color"], "amber")
        self.assertEqual(compute_score([Signal("x", "X", "v", -40, "warning")])["color"], "amber")
        self.assertEqual(compute_score([Signal("x", "X", "v", -41, "critical")])["color"], "red")


class TestAuditSignals(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._prev_cve_db = os.environ.get("TRILLION_CVE_SCAN_DB")
        os.environ["TRILLION_CVE_SCAN_DB"] = os.path.join(self._tmp.name, "cve_scans.db")
        self._prev_anthropic_key = os.environ.pop("ANTHROPIC_API_KEY", None)
        self._prev_provider = os.environ.pop("TRILLION_PROVIDER", None)

    def tearDown(self):
        if self._prev_cve_db is None:
            os.environ.pop("TRILLION_CVE_SCAN_DB", None)
        else:
            os.environ["TRILLION_CVE_SCAN_DB"] = self._prev_cve_db
        if self._prev_anthropic_key is not None:
            os.environ["ANTHROPIC_API_KEY"] = self._prev_anthropic_key
        if self._prev_provider is not None:
            os.environ["TRILLION_PROVIDER"] = self._prev_provider

    def _settings(self, **overrides) -> Settings:
        return Settings(**overrides)

    def test_kill_switch_active_is_critical(self):
        result = audit(self._settings(trillion_paused=True), ToolRegistry())
        sig = next(s for s in result["signals"] if s["name"] == "kill-switch")
        self.assertEqual(sig["delta"], -100)
        self.assertEqual(sig["severity"], "critical")

    def test_kill_switch_off_is_ok(self):
        result = audit(self._settings(trillion_paused=False), ToolRegistry())
        sig = next(s for s in result["signals"] if s["name"] == "kill-switch")
        self.assertEqual(sig["delta"], 0)
        self.assertEqual(sig["severity"], "ok")

    def test_llm_api_key_unset_is_critical(self):
        result = audit(self._settings(), ToolRegistry())
        sig = next(s for s in result["signals"] if s["name"] == "llm-api-key")
        self.assertEqual(sig["delta"], -50)
        self.assertEqual(sig["severity"], "critical")

    def test_llm_api_key_set_is_ok(self):
        os.environ["ANTHROPIC_API_KEY"] = "sk-ant-test"
        self.addCleanup(lambda: os.environ.pop("ANTHROPIC_API_KEY", None))
        result = audit(self._settings(), ToolRegistry())
        sig = next(s for s in result["signals"] if s["name"] == "llm-api-key")
        self.assertEqual(sig["delta"], 0)
        self.assertEqual(sig["severity"], "ok")

    def test_bearer_token_unset_on_loopback_is_ok(self):
        result = audit(self._settings(web_host="127.0.0.1", web_auth_token=""), ToolRegistry())
        sig = next(s for s in result["signals"] if s["name"] == "bearer-token")
        self.assertEqual(sig["delta"], 0)
        self.assertEqual(sig["severity"], "ok")

    def test_bearer_token_unset_on_public_bind_is_critical(self):
        result = audit(self._settings(web_host="0.0.0.0", web_auth_token=""), ToolRegistry())
        sig = next(s for s in result["signals"] if s["name"] == "bearer-token")
        self.assertEqual(sig["delta"], -30)
        self.assertEqual(sig["severity"], "critical")

    def test_approval_mode_smart_is_ok(self):
        result = audit(self._settings(confirmation_mode="smart"), ToolRegistry())
        sig = next(s for s in result["signals"] if s["name"] == "approval-mode")
        self.assertEqual(sig["delta"], 0)

    def test_approval_mode_manual_is_bonus(self):
        result = audit(self._settings(confirmation_mode="manual"), ToolRegistry())
        sig = next(s for s in result["signals"] if s["name"] == "approval-mode")
        self.assertEqual(sig["delta"], 5)

    def test_approval_mode_off_is_warning(self):
        result = audit(self._settings(confirmation_mode="off"), ToolRegistry())
        sig = next(s for s in result["signals"] if s["name"] == "approval-mode")
        self.assertEqual(sig["delta"], -25)
        self.assertEqual(sig["severity"], "warning")

    def test_dev_mode_bind_loopback_is_info(self):
        result = audit(self._settings(web_host="localhost"), ToolRegistry())
        sig = next(s for s in result["signals"] if s["name"] == "dev-mode-bind")
        self.assertEqual(sig["delta"], -1)
        self.assertEqual(sig["severity"], "info")

    def test_dev_mode_bind_public_is_critical(self):
        result = audit(self._settings(web_host="0.0.0.0"), ToolRegistry())
        sig = next(s for s in result["signals"] if s["name"] == "dev-mode-bind")
        self.assertEqual(sig["delta"], -40)
        self.assertEqual(sig["severity"], "critical")

    def test_gate_coverage_full_coverage_is_ok(self):
        registry = ToolRegistry()
        registry.register(FakeReadOnlyTool())
        registry.register(FakeGatedTool())
        result = audit(self._settings(confirmation_mode="smart"), registry)
        sig = next(s for s in result["signals"] if s["name"] == "gate-coverage")
        self.assertEqual(sig["value"], "1/1 paths")
        self.assertEqual(sig["delta"], 0)
        self.assertEqual(sig["severity"], "ok")

    def test_gate_coverage_penalizes_ungated_mutating_tools(self):
        registry = ToolRegistry()
        registry.register(FakeReadOnlyTool())
        registry.register(FakeUngatedLowTool())
        result = audit(self._settings(confirmation_mode="smart"), registry)
        sig = next(s for s in result["signals"] if s["name"] == "gate-coverage")
        self.assertEqual(sig["value"], "0/1 paths")
        self.assertEqual(sig["delta"], -5)
        self.assertEqual(sig["severity"], "critical")

    def test_gate_coverage_no_mutating_tools_is_ok(self):
        registry = ToolRegistry()
        registry.register(FakeReadOnlyTool())
        result = audit(self._settings(), registry)
        sig = next(s for s in result["signals"] if s["name"] == "gate-coverage")
        self.assertEqual(sig["value"], "0/0 paths")
        self.assertEqual(sig["delta"], 0)

    def test_hardline_blocklist_present_is_ok(self):
        result = audit(self._settings(), ToolRegistry())
        sig = next(s for s in result["signals"] if s["name"] == "hardline-blocklist")
        self.assertEqual(sig["delta"], 0)
        self.assertIn("patterns", sig["value"])

    def test_csp_status_is_report_only(self):
        result = audit(self._settings(), ToolRegistry())
        sig = next(s for s in result["signals"] if s["name"] == "csp-status")
        self.assertEqual(sig["value"], "report-only")
        self.assertEqual(sig["delta"], -10)

    def test_token_scope_audit_pending_by_default(self):
        result = audit(self._settings(), ToolRegistry())
        sig = next(s for s in result["signals"] if s["name"] == "token-scope-audit")
        self.assertEqual(sig["value"], "pending")
        self.assertEqual(sig["delta"], -3)

    def test_token_scope_audit_attested_via_env(self):
        os.environ["TRILLION_TOKEN_SCOPE_AUDITED"] = "true"
        self.addCleanup(lambda: os.environ.pop("TRILLION_TOKEN_SCOPE_AUDITED", None))
        result = audit(self._settings(), ToolRegistry())
        sig = next(s for s in result["signals"] if s["name"] == "token-scope-audit")
        self.assertEqual(sig["value"], "audited")
        self.assertEqual(sig["delta"], 0)

    def test_db_readonly_role_pending_by_default(self):
        result = audit(self._settings(), ToolRegistry())
        sig = next(s for s in result["signals"] if s["name"] == "db-readonly-role")
        self.assertEqual(sig["delta"], -3)

    def test_csrf_origin_gate_reports_absent(self):
        result = audit(self._settings(), ToolRegistry())
        sig = next(s for s in result["signals"] if s["name"] == "csrf-origin-gate")
        self.assertEqual(sig["value"], "absent")
        self.assertEqual(sig["delta"], -10)
        self.assertEqual(sig["severity"], "warning")

    def test_cve_scan_never_run_is_warning(self):
        result = audit(self._settings(), ToolRegistry())
        sig = next(s for s in result["signals"] if s["name"] == "cve-scan")
        self.assertEqual(sig["value"], "never run")
        self.assertEqual(sig["delta"], -5)

    def test_cve_scan_clean_is_ok(self):
        CveScanRepo(db_path=os.environ["TRILLION_CVE_SCAN_DB"]).save(CveScanResult(cve_count=0))
        result = audit(self._settings(), ToolRegistry())
        sig = next(s for s in result["signals"] if s["name"] == "cve-scan")
        self.assertEqual(sig["value"], "clean")
        self.assertEqual(sig["delta"], 0)

    def test_cve_scan_with_findings_is_penalized(self):
        CveScanRepo(db_path=os.environ["TRILLION_CVE_SCAN_DB"]).save(
            CveScanResult(cve_count=2, findings=[{"package": "p", "vulns": ["a", "b"]}])
        )
        result = audit(self._settings(), ToolRegistry())
        sig = next(s for s in result["signals"] if s["name"] == "cve-scan")
        self.assertEqual(sig["value"], "2 CVEs")
        self.assertEqual(sig["delta"], -10)

    def test_cve_scan_stale_is_warning(self):
        old = (_datetime.datetime.now(_datetime.timezone.utc) - _datetime.timedelta(days=30)).isoformat()
        CveScanRepo(db_path=os.environ["TRILLION_CVE_SCAN_DB"]).save(
            CveScanResult(cve_count=0, generated_at=old)
        )
        result = audit(self._settings(), ToolRegistry())
        sig = next(s for s in result["signals"] if s["name"] == "cve-scan")
        self.assertEqual(sig["value"], "stale (>14d)")
        self.assertEqual(sig["delta"], -5)

    def test_cve_scan_error_is_warning(self):
        CveScanRepo(db_path=os.environ["TRILLION_CVE_SCAN_DB"]).save(
            CveScanResult(cve_count=0, error_message="pip-audit is not installed")
        )
        result = audit(self._settings(), ToolRegistry())
        sig = next(s for s in result["signals"] if s["name"] == "cve-scan")
        self.assertEqual(sig["value"], "scanner error")
        self.assertEqual(sig["delta"], -10)

    def test_audit_returns_score_color_and_all_signals(self):
        os.environ["ANTHROPIC_API_KEY"] = "sk-ant-test"
        self.addCleanup(lambda: os.environ.pop("ANTHROPIC_API_KEY", None))
        CveScanRepo(db_path=os.environ["TRILLION_CVE_SCAN_DB"]).save(CveScanResult(cve_count=0))
        result = audit(self._settings(), ToolRegistry())
        self.assertIn("score", result)
        self.assertIn("color", result)
        self.assertEqual(len(result["signals"]), 14)


class TestServeSecurityStatusEndpoint(AioHTTPTestCase):
    async def get_application(self):
        import serve as serve_module
        from agent.providers.base import BaseProvider, ProviderResponse, TextChunk, TokenUsage

        class FakeProvider(BaseProvider):
            @property
            def model_name(self):
                return "fake-model"

            async def stream(self, messages, system, tools=None):
                yield TextChunk(text="")
                yield ProviderResponse(text="", tool_calls=[], usage=TokenUsage(), model=self.model_name)

        self.tmp = tempfile.mkdtemp()
        self._prev_env = {
            key: os.environ.get(key)
            for key in (
                "TRILLION_FACTORY_DB",
                "TRILLION_NOTES_VAULT_PATH",
                "TRILLION_NOTES_INDEX_PATH",
                "TRILLION_HEARTBEAT_DB",
                "TRILLION_CVE_SCAN_DB",
                "GITHUB_TOKEN",
                "TRILLION_GITHUB_WATCHED_REPOS",
            )
        }
        os.environ["TRILLION_FACTORY_DB"] = os.path.join(self.tmp, "factory.db")
        os.environ["TRILLION_NOTES_VAULT_PATH"] = os.path.join(self.tmp, "vault")
        os.environ["TRILLION_NOTES_INDEX_PATH"] = os.path.join(self.tmp, "notes_index.db")
        os.environ["TRILLION_HEARTBEAT_DB"] = os.path.join(self.tmp, "heartbeat.db")
        os.environ["TRILLION_CVE_SCAN_DB"] = os.path.join(self.tmp, "cve_scans.db")
        os.environ.pop("GITHUB_TOKEN", None)
        os.environ.pop("TRILLION_GITHUB_WATCHED_REPOS", None)

        self.serve_module = serve_module
        serve_module._provider = FakeProvider()
        serve_module._registry = ToolRegistry()
        serve_module._agent = None

        # Same seeding trick as TestServeCveEndpoints in test_cve_scan.py:
        # a fresh heartbeat DB treats "cve_scan" as immediately due, which
        # would fire a background scan as soon as the app starts.
        from agent.heartbeat.storage import HeartbeatRepo

        HeartbeatRepo(db_path=os.environ["TRILLION_HEARTBEAT_DB"]).set_next_due_at(
            "cve_scan", _datetime.datetime.now(_datetime.timezone.utc) + _datetime.timedelta(days=1)
        )

        return serve_module.build_app()

    def tearDown(self):
        super().tearDown()
        self.serve_module._provider = None
        self.serve_module._registry = None
        self.serve_module._agent = None
        for key, prev in self._prev_env.items():
            if prev is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = prev
        shutil.rmtree(self.tmp, ignore_errors=True)

    async def test_security_status_returns_score_color_signals(self):
        resp = await self.client.request("GET", "/api/security/status")
        self.assertEqual(resp.status, 200)
        data = await resp.json()
        self.assertIn("score", data)
        self.assertIn("color", data)
        self.assertIsInstance(data["signals"], list)
        self.assertTrue(len(data["signals"]) > 0)


if __name__ == "__main__":
    unittest.main()
