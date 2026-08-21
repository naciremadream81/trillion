"""
Tests for agent/security/csp_reports.py and the enforce switch in
agent/security/headers.py — agent-security.md §2.2.

The gap these cover: the report endpoint used to hand its line to print(),
which on a host with no persistent journal is /dev/null. The collection
step of §2.2 ("read the resulting violations, widen the policy only by what
actually got blocked") therefore had nothing to read, and the flip to
enforcing could never be made on evidence.

Run from the project root:
    python -m unittest tests.test_csp_reports
"""

import json
import os
import shutil
import tempfile
import unittest

from agent.security.csp_reports import (
    CspReportRepo,
    CspViolation,
    parse_report,
    record_report,
)
from agent.security.headers import CSP_POLICY, apply_security_headers


class TestParseReport(unittest.TestCase):
    def test_legacy_report_uri_shape(self):
        body = json.dumps(
            {
                "csp-report": {
                    "violated-directive": "script-src",
                    "blocked-uri": "https://evil.example/x.js",
                    "document-uri": "http://localhost:8123/",
                }
            }
        )
        v = parse_report(body)
        self.assertEqual(v.directive, "script-src")
        self.assertEqual(v.blocked_uri, "https://evil.example/x.js")
        self.assertEqual(v.document_uri, "http://localhost:8123/")

    def test_reporting_api_list_shape(self):
        body = json.dumps(
            [
                {
                    "type": "csp-violation",
                    "body": {
                        "effectiveDirective": "font-src",
                        "blockedURL": "https://fonts.gstatic.com/a.woff2",
                        "documentURL": "http://localhost:8123/",
                    },
                }
            ]
        )
        v = parse_report(body)
        self.assertEqual(v.directive, "font-src")
        self.assertEqual(v.blocked_uri, "https://fonts.gstatic.com/a.woff2")

    def test_bare_object_shape(self):
        v = parse_report(json.dumps({"effective-directive": "img-src"}))
        self.assertEqual(v.directive, "img-src")

    def test_malformed_body_still_produces_a_row(self):
        # Losing an unparseable report would hide exactly the violations
        # most worth seeing.
        v = parse_report("not json at all")
        self.assertEqual(v.directive, "")
        self.assertEqual(v.raw_body, "not json at all")

    def test_empty_body_does_not_raise(self):
        self.assertEqual(parse_report("").directive, "")

    def test_oversized_fields_are_bounded(self):
        body = json.dumps(
            {"csp-report": {"violated-directive": "x" * 5000, "blocked-uri": "y" * 5000}}
        )
        v = parse_report(body)
        self.assertLessEqual(len(v.directive), 200)
        self.assertLessEqual(len(v.blocked_uri), 500)

    def test_unexpected_json_types_do_not_raise(self):
        for body in ("123", '"a string"', "null", "[1, 2, 3]", '{"csp-report": 5}'):
            with self.subTest(body=body):
                self.assertEqual(parse_report(body).directive, "")


class TestCspReportRepo(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.repo = CspReportRepo(os.path.join(self.tmp, "csp.db"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_save_and_count(self):
        self.repo.save(CspViolation(directive="script-src", blocked_uri="https://a/x.js"))
        self.assertEqual(self.repo.count(), 1)

    def test_directive_summary_groups_and_counts(self):
        for _ in range(3):
            self.repo.save(
                CspViolation(directive="font-src", blocked_uri="https://fonts.gstatic.com/a")
            )
        self.repo.save(CspViolation(directive="img-src", blocked_uri="https://cdn/x.png"))
        summary = self.repo.directive_summary()
        by_directive = {row["directive"]: row for row in summary}
        self.assertEqual(by_directive["font-src"]["hits"], 3)
        self.assertEqual(by_directive["img-src"]["hits"], 1)

    def test_directive_summary_respects_limit(self):
        for i in range(10):
            self.repo.save(CspViolation(directive=f"d{i}", blocked_uri=f"u{i}"))
        self.assertEqual(len(self.repo.directive_summary(limit=4)), 4)

    def test_prune_keeps_the_newest(self):
        for i in range(20):
            self.repo.save(CspViolation(directive="script-src", blocked_uri=f"u{i}"))
        removed = self.repo.prune(keep=5)
        self.assertEqual(removed, 15)
        self.assertEqual(self.repo.count(), 5)
        remaining = {row["blocked_uri"] for row in self.repo.directive_summary()}
        self.assertIn("u19", remaining)
        self.assertNotIn("u0", remaining)

    def test_record_report_never_raises_on_a_bad_path(self):
        # This runs on an unauthenticated endpoint; a storage error must not
        # become a 500 an attacker can trigger at will.
        repo = CspReportRepo(os.path.join(self.tmp, "csp.db"))
        repo.db_path = os.path.join(self.tmp, "no-such-dir", "nested", "x.db")
        record_report('{"csp-report": {"violated-directive": "script-src"}}', repo)

    def test_record_report_persists(self):
        record_report(
            '{"csp-report": {"violated-directive": "media-src", "blocked-uri": "blob:x"}}',
            self.repo,
        )
        summary = self.repo.directive_summary()
        self.assertEqual(summary[0]["directive"], "media-src")


class TestCspEnforceSwitch(unittest.TestCase):
    def test_report_only_by_default(self):
        headers: dict = {}
        apply_security_headers(headers)
        self.assertEqual(headers["Content-Security-Policy-Report-Only"], CSP_POLICY)
        self.assertNotIn("Content-Security-Policy", headers)

    def test_enforcing_sends_both_headers(self):
        # The report-only header stays on after the flip, so a policy that's
        # too tight is still observable rather than silently breaking the UI.
        headers: dict = {}
        apply_security_headers(headers, enforce=True)
        self.assertEqual(headers["Content-Security-Policy"], CSP_POLICY)
        self.assertEqual(headers["Content-Security-Policy-Report-Only"], CSP_POLICY)

    def test_policy_never_allows_unsafe_eval(self):
        # Standing constraint from the playbook, in both modes.
        self.assertNotIn("unsafe-eval", CSP_POLICY)

    def test_policy_keeps_the_report_sink_wired(self):
        self.assertIn("report-uri /api/security/csp-report", CSP_POLICY)


if __name__ == "__main__":
    unittest.main()
