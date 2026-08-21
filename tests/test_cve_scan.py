"""
Tests for agent/security/cve_scan.py (§3.4), agent/heartbeat/checks/cve_scan.py,
and the two serve.py endpoints it powers.

pip-audit itself hits PyPI's OSV vulnerability database over the network, so
run_pip_audit() is exercised here via a monkeypatched
asyncio.create_subprocess_exec rather than the real binary — never a live
API call, per this project's test convention.

Run from the project root:
    python -m unittest tests.test_cve_scan
"""

import asyncio
import json
import os
import shutil
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from aiohttp.test_utils import AioHTTPTestCase

from agent.heartbeat.checks.cve_scan import CveScanCheck
from agent.providers.base import BaseProvider, ProviderResponse, TextChunk, TokenUsage
from agent.security.cve_scan import (
    CveScanRepo,
    CveScanResult,
    run_pip_audit,
    scan_and_persist,
)


class FakeProc:
    def __init__(self, stdout: bytes, stderr: bytes = b""):
        self._stdout = stdout
        self._stderr = stderr
        self.killed = False

    async def communicate(self):
        return self._stdout, self._stderr

    def kill(self):
        self.killed = True

    async def wait(self):
        return None


def _payload(findings):
    return json.dumps({"dependencies": findings}).encode("utf-8")


class TestCveScanResult(unittest.TestCase):
    def test_generated_at_defaults_when_not_given(self):
        result = CveScanResult(cve_count=0)
        self.assertTrue(result.generated_at)

    def test_generated_at_preserved_when_given(self):
        result = CveScanResult(cve_count=0, generated_at="2026-01-01T00:00:00+00:00")
        self.assertEqual(result.generated_at, "2026-01-01T00:00:00+00:00")


class TestCveScanRepo(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = CveScanRepo(db_path=os.path.join(self._tmp.name, "cve_scans.db"))

    def tearDown(self):
        self._tmp.cleanup()

    def test_latest_defaults_to_none(self):
        self.assertIsNone(self.repo.latest())

    def test_save_and_latest_round_trips(self):
        result = CveScanResult(
            cve_count=2,
            findings=[{"package": "foo", "version": "1.0", "vulns": ["CVE-1", "CVE-2"]}],
            scanner_version="2.10.1",
        )
        self.repo.save(result)
        latest = self.repo.latest()
        self.assertEqual(latest["cve_count"], 2)
        self.assertEqual(latest["findings"][0]["package"], "foo")
        self.assertEqual(latest["scanner_version"], "2.10.1")
        self.assertIsNone(latest["error_message"])

    def test_latest_returns_most_recent_of_several(self):
        self.repo.save(CveScanResult(cve_count=1, generated_at="2026-01-01T00:00:00+00:00"))
        self.repo.save(CveScanResult(cve_count=5, generated_at="2026-01-02T00:00:00+00:00"))
        self.assertEqual(self.repo.latest()["cve_count"], 5)

    def test_error_message_persisted(self):
        self.repo.save(CveScanResult(cve_count=0, error_message="pip-audit is not installed"))
        self.assertEqual(self.repo.latest()["error_message"], "pip-audit is not installed")


class TestRunPipAudit(unittest.IsolatedAsyncioTestCase):
    async def test_clean_scan_has_zero_cves(self):
        proc = FakeProc(_payload([{"name": "requests", "version": "2.31.0", "vulns": []}]))
        with patch("agent.security.cve_scan.asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
            result = await run_pip_audit()
        self.assertEqual(result.cve_count, 0)
        self.assertIsNone(result.error_message)
        self.assertEqual(result.findings, [])

    async def test_vulnerable_deps_are_counted_and_collected(self):
        proc = FakeProc(
            _payload(
                [
                    {"name": "clean-pkg", "version": "1.0", "vulns": []},
                    {
                        "name": "vuln-pkg",
                        "version": "0.1",
                        "vulns": [{"id": "CVE-2026-1"}, {"id": "CVE-2026-2"}],
                    },
                ]
            )
        )
        with patch("agent.security.cve_scan.asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
            result = await run_pip_audit()
        self.assertEqual(result.cve_count, 2)
        self.assertEqual(len(result.findings), 1)
        self.assertEqual(result.findings[0]["package"], "vuln-pkg")

    async def test_missing_binary_records_error_not_crash(self):
        with patch(
            "agent.security.cve_scan.asyncio.create_subprocess_exec",
            AsyncMock(side_effect=FileNotFoundError()),
        ):
            result = await run_pip_audit()
        self.assertEqual(result.cve_count, 0)
        self.assertIn("not installed", result.error_message)

    async def test_malformed_output_records_error_not_crash(self):
        proc = FakeProc(b"not json", stderr=b"pip-audit: error: boom")
        with patch("agent.security.cve_scan.asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
            result = await run_pip_audit()
        self.assertEqual(result.cve_count, 0)
        self.assertIn("could not parse", result.error_message)

    async def test_timeout_kills_process_and_records_error(self):
        proc = FakeProc(b"{}")
        with patch("agent.security.cve_scan.asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
            with patch(
                "agent.security.cve_scan.asyncio.wait_for",
                AsyncMock(side_effect=asyncio.TimeoutError()),
            ):
                result = await run_pip_audit(timeout_seconds=1.0)
        self.assertTrue(proc.killed)
        self.assertIn("timed out", result.error_message)


class TestScanAndPersist(unittest.IsolatedAsyncioTestCase):
    async def test_persists_and_returns_same_shape_as_latest(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        repo = CveScanRepo(db_path=os.path.join(self._tmp.name, "cve_scans.db"))
        proc = FakeProc(_payload([]))
        with patch("agent.security.cve_scan.asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
            result = await scan_and_persist(repo=repo)
        self.assertEqual(result["cve_count"], 0)
        self.assertEqual(repo.latest()["cve_count"], 0)


class TestCveScanCheck(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = CveScanRepo(db_path=os.path.join(self._tmp.name, "cve_scans.db"))

    def tearDown(self):
        self._tmp.cleanup()

    async def test_clean_scan_emits_no_notice(self):
        check = CveScanCheck(repo=self.repo)
        with patch(
            "agent.heartbeat.checks.cve_scan.run_pip_audit",
            AsyncMock(return_value=CveScanResult(cve_count=0)),
        ):
            notices, cursor = await check.run({})
        self.assertEqual(notices, [])
        self.assertEqual(cursor["last_notified_count"], 0)

    async def test_new_cves_emit_a_warning_notice(self):
        check = CveScanCheck(repo=self.repo)
        findings = [{"package": "vuln-pkg", "version": "0.1", "vulns": ["CVE-2026-1"]}]
        with patch(
            "agent.heartbeat.checks.cve_scan.run_pip_audit",
            AsyncMock(return_value=CveScanResult(cve_count=1, findings=findings)),
        ):
            notices, cursor = await check.run({})
        self.assertEqual(len(notices), 1)
        self.assertEqual(notices[0].severity, "warning")
        self.assertIn("vuln-pkg", notices[0].message)
        self.assertEqual(cursor["last_notified_count"], 1)

    async def test_stable_cve_count_does_not_renotify(self):
        check = CveScanCheck(repo=self.repo)
        with patch(
            "agent.heartbeat.checks.cve_scan.run_pip_audit",
            AsyncMock(return_value=CveScanResult(cve_count=3, findings=[{"package": "p", "vulns": ["a", "b", "c"]}])),
        ):
            notices, cursor = await check.run({"last_notified_count": 3})
        self.assertEqual(notices, [])

    async def test_scanner_error_emits_no_notice(self):
        check = CveScanCheck(repo=self.repo)
        with patch(
            "agent.heartbeat.checks.cve_scan.run_pip_audit",
            AsyncMock(return_value=CveScanResult(cve_count=0, error_message="pip-audit is not installed")),
        ):
            notices, cursor = await check.run({})
        self.assertEqual(notices, [])

    async def test_run_persists_scan_to_repo(self):
        check = CveScanCheck(repo=self.repo)
        with patch(
            "agent.heartbeat.checks.cve_scan.run_pip_audit",
            AsyncMock(return_value=CveScanResult(cve_count=0)),
        ):
            await check.run({})
        self.assertIsNotNone(self.repo.latest())


class FakeProvider(BaseProvider):
    @property
    def model_name(self):
        return "fake-model"

    async def stream(self, messages, system, tools=None):
        yield TextChunk(text="")
        yield ProviderResponse(text="", tool_calls=[], usage=TokenUsage(), model=self.model_name)


class TestServeCveEndpoints(AioHTTPTestCase):
    async def get_application(self):
        import serve as serve_module
        from agent.tools.registry import ToolRegistry

        self.tmp = tempfile.mkdtemp()
        self._prev_env = {
            key: os.environ.get(key)
            for key in (
                "TRILLION_FACTORY_DB",
                "TRILLION_NOTES_VAULT_PATH",
                "TRILLION_NOTES_INDEX_PATH",
                "TRILLION_HEARTBEAT_DB",
                "TRILLION_CSP_REPORT_DB",
                "TRILLION_CVE_SCAN_DB",
                "GITHUB_TOKEN",
                "TRILLION_GITHUB_WATCHED_REPOS",
            )
        }
        os.environ["TRILLION_FACTORY_DB"] = os.path.join(self.tmp, "factory.db")
        os.environ["TRILLION_NOTES_VAULT_PATH"] = os.path.join(self.tmp, "vault")
        os.environ["TRILLION_NOTES_INDEX_PATH"] = os.path.join(self.tmp, "notes_index.db")
        os.environ["TRILLION_HEARTBEAT_DB"] = os.path.join(self.tmp, "heartbeat.db")
        os.environ["TRILLION_CSP_REPORT_DB"] = os.path.join(self.tmp, "csp_reports.db")
        os.environ["TRILLION_CVE_SCAN_DB"] = os.path.join(self.tmp, "cve_scans.db")
        os.environ.pop("GITHUB_TOKEN", None)
        os.environ.pop("TRILLION_GITHUB_WATCHED_REPOS", None)

        self.serve_module = serve_module
        serve_module._provider = FakeProvider()
        serve_module._registry = ToolRegistry()
        serve_module._agent = None

        # A fresh TRILLION_HEARTBEAT_DB has no persisted next_due_at for "cve_scan",
        # and HeartbeatScheduler.tick_once treats that as immediately due - it would
        # fire (and persist a scan via CveScanCheck.run) in the background as soon
        # as the app starts. Seed a far-future due date so no test in this class
        # observes a background-triggered scan; tests exercising the direct
        # POST /api/security/cve-scan path mock the subprocess themselves.
        from agent.heartbeat.storage import HeartbeatRepo
        import datetime as _datetime

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

    async def test_cve_status_defaults_to_never_run(self):
        resp = await self.client.request("GET", "/api/security/cve-status")
        self.assertEqual(resp.status, 200)
        data = await resp.json()
        self.assertEqual(data["cve_count"], 0)
        self.assertIsNone(data["generated_at"])
        self.assertIn("no scan has run yet", data["error_message"])

    async def test_cve_status_reflects_latest_persisted_scan(self):
        repo = CveScanRepo(db_path=os.environ["TRILLION_CVE_SCAN_DB"])
        repo.save(CveScanResult(cve_count=4, findings=[{"package": "p", "vulns": ["a", "b", "c", "d"]}]))
        resp = await self.client.request("GET", "/api/security/cve-status")
        data = await resp.json()
        self.assertEqual(data["cve_count"], 4)

    async def test_cve_scan_runs_and_persists(self):
        proc = FakeProc(_payload([]))
        with patch("agent.security.cve_scan.asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
            resp = await self.client.request("POST", "/api/security/cve-scan")
        self.assertEqual(resp.status, 200)
        data = await resp.json()
        self.assertEqual(data["cve_count"], 0)

        repo = CveScanRepo(db_path=os.environ["TRILLION_CVE_SCAN_DB"])
        self.assertIsNotNone(repo.latest())


if __name__ == "__main__":
    unittest.main()
