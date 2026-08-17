"""
Tests for the Code Sentinel checks (agent/heartbeat/checks/code_sentinel.py).

Run from the project root:
    python -m unittest tests.test_code_sentinel
"""

import asyncio
import unittest
from datetime import datetime, timedelta, timezone

from agent.config import Settings
from agent.heartbeat.checks.code_sentinel import (
    CIFailureCheck,
    DependencyAlertCheck,
    DormantRepoCheck,
    HotfixPushCheck,
    StalePRCheck,
    build_code_sentinel_checks,
)


def run(coro):
    return asyncio.run(coro)


def iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def push_event(actor: str, branch: str, sha: str) -> dict:
    """A GitHub PushEvent. `actor` is the pusher — deliberately not the
    commit's author, which is a different field answering a different question."""
    return {
        "type": "PushEvent",
        "actor": {"login": actor},
        "payload": {"ref": f"refs/heads/{branch}", "head": sha},
    }


class FakeClient:
    def __init__(self):
        self.check_runs = {}
        self.pull_requests = {}
        self.alerts = {}
        self.branches = {}
        self.participation = {}
        self.default_branches = {}
        self.commits = {}
        self.events = {}
        self.error_repos = set()
        self.check_runs_calls = []

    async def get_default_branch(self, repo):
        if repo in self.error_repos:
            raise RuntimeError("boom")
        return self.default_branches.get(repo, "main")

    async def get_commit(self, repo, sha):
        if repo in self.error_repos:
            raise RuntimeError("boom")
        return self.commits.get((repo, sha), {})

    async def list_repo_events(self, repo):
        if repo in self.error_repos:
            raise RuntimeError("boom")
        return self.events.get(repo, [])

    async def list_check_runs(self, repo, ref):
        self.check_runs_calls.append((repo, ref))
        if repo in self.error_repos:
            raise RuntimeError("boom")
        return self.check_runs.get(repo, [])

    async def list_open_pull_requests(self, repo):
        if repo in self.error_repos:
            raise RuntimeError("boom")
        return self.pull_requests.get(repo, [])

    async def list_dependabot_alerts(self, repo):
        if repo in self.error_repos:
            raise RuntimeError("boom")
        return self.alerts.get(repo, [])

    async def list_branches(self, repo):
        if repo in self.error_repos:
            raise RuntimeError("boom")
        return self.branches.get(repo, [])

    async def get_participation(self, repo):
        if repo in self.error_repos:
            raise RuntimeError("boom")
        return self.participation.get(repo, {})


class TestCIFailureCheck(unittest.TestCase):
    def test_new_failure_creates_critical_notice_and_updates_cursor(self):
        client = FakeClient()
        client.check_runs["owner/repo"] = [
            {"id": 42, "name": "pytest", "status": "completed", "conclusion": "failure", "started_at": "2026-01-01T00:00:00Z"}
        ]
        check = CIFailureCheck(client, ["owner/repo"], cadence_seconds=60.0)
        notices, cursor = run(check.run({}))
        self.assertEqual(len(notices), 1)
        self.assertEqual(notices[0].severity, "critical")
        self.assertIn("pytest", notices[0].message)
        self.assertEqual(cursor["owner/repo"], 42)

    def test_same_failure_id_not_renotified(self):
        client = FakeClient()
        client.check_runs["owner/repo"] = [
            {"id": 42, "name": "pytest", "status": "completed", "conclusion": "failure", "started_at": "2026-01-01T00:00:00Z"}
        ]
        check = CIFailureCheck(client, ["owner/repo"], cadence_seconds=60.0)
        notices, cursor = run(check.run({"owner/repo": 42}))
        self.assertEqual(notices, [])

    def test_no_failures_returns_no_notices(self):
        client = FakeClient()
        client.check_runs["owner/repo"] = [
            {"id": 1, "name": "pytest", "status": "completed", "conclusion": "success", "started_at": "2026-01-01T00:00:00Z"}
        ]
        check = CIFailureCheck(client, ["owner/repo"], cadence_seconds=60.0)
        notices, _ = run(check.run({}))
        self.assertEqual(notices, [])

    def test_repo_error_skips_repo_without_raising(self):
        client = FakeClient()
        client.error_repos.add("owner/repo")
        check = CIFailureCheck(client, ["owner/repo"], cadence_seconds=60.0)
        notices, cursor = run(check.run({}))
        self.assertEqual(notices, [])
        self.assertEqual(cursor, {})

    def test_uses_repos_actual_default_branch_not_hardcoded_main(self):
        client = FakeClient()
        client.default_branches["owner/repo"] = "master"
        client.check_runs["owner/repo"] = [
            {"id": 42, "name": "pytest", "status": "completed", "conclusion": "failure", "started_at": "2026-01-01T00:00:00Z"}
        ]
        check = CIFailureCheck(client, ["owner/repo"], cadence_seconds=60.0)
        notices, _ = run(check.run({}))
        self.assertEqual(len(notices), 1)
        self.assertEqual(client.check_runs_calls, [("owner/repo", "master")])

    def test_notice_names_the_branch_it_actually_checked(self):
        # Querying "master" but reporting "main" sends Sean to investigate a
        # branch that isn't the one that went red.
        client = FakeClient()
        client.default_branches["owner/repo"] = "develop"
        client.check_runs["owner/repo"] = [
            {"id": 42, "name": "pytest", "status": "completed", "conclusion": "failure", "started_at": "2026-01-01T00:00:00Z"}
        ]
        check = CIFailureCheck(client, ["owner/repo"], cadence_seconds=60.0)
        notices, _ = run(check.run({}))
        self.assertIn("develop", notices[0].message)
        self.assertNotIn("main", notices[0].message)


class TestStalePRCheck(unittest.TestCase):
    def test_pr_older_than_48h_notifies(self):
        client = FakeClient()
        old = datetime.now(timezone.utc) - timedelta(hours=49)
        client.pull_requests["owner/repo"] = [{"number": 7, "title": "Fix thing", "updated_at": iso(old)}]
        check = StalePRCheck(client, ["owner/repo"], cadence_seconds=1800.0)
        notices, cursor = run(check.run({}))
        self.assertEqual(len(notices), 1)
        self.assertIn("#7", notices[0].message)
        self.assertEqual(cursor["owner/repo"], [7])

    def test_pr_within_48h_does_not_notify(self):
        client = FakeClient()
        recent = datetime.now(timezone.utc) - timedelta(hours=2)
        client.pull_requests["owner/repo"] = [{"number": 7, "title": "Fix thing", "updated_at": iso(recent)}]
        check = StalePRCheck(client, ["owner/repo"], cadence_seconds=1800.0)
        notices, _ = run(check.run({}))
        self.assertEqual(notices, [])

    def test_already_notified_pr_not_renotified(self):
        client = FakeClient()
        old = datetime.now(timezone.utc) - timedelta(hours=49)
        client.pull_requests["owner/repo"] = [{"number": 7, "title": "Fix thing", "updated_at": iso(old)}]
        check = StalePRCheck(client, ["owner/repo"], cadence_seconds=1800.0)
        notices, _ = run(check.run({"owner/repo": [7]}))
        self.assertEqual(notices, [])

    def test_closed_pr_dropped_from_cursor(self):
        client = FakeClient()
        client.pull_requests["owner/repo"] = []
        check = StalePRCheck(client, ["owner/repo"], cadence_seconds=1800.0)
        _, cursor = run(check.run({"owner/repo": [7]}))
        self.assertEqual(cursor["owner/repo"], [])

    def test_owners_own_stale_pr_is_not_notified(self):
        client = FakeClient()
        old = datetime.now(timezone.utc) - timedelta(hours=49)
        client.pull_requests["owner/repo"] = [
            {"number": 7, "title": "Fix thing", "updated_at": iso(old), "user": {"login": "Sean"}}
        ]
        check = StalePRCheck(client, ["owner/repo"], cadence_seconds=1800.0, username="sean")
        notices, cursor = run(check.run({}))
        self.assertEqual(notices, [])
        self.assertEqual(cursor["owner/repo"], [])

    def test_other_authors_stale_pr_still_notified_when_username_set(self):
        client = FakeClient()
        old = datetime.now(timezone.utc) - timedelta(hours=49)
        client.pull_requests["owner/repo"] = [
            {"number": 7, "title": "Fix thing", "updated_at": iso(old), "user": {"login": "someone-else"}}
        ]
        check = StalePRCheck(client, ["owner/repo"], cadence_seconds=1800.0, username="sean")
        notices, _ = run(check.run({}))
        self.assertEqual(len(notices), 1)


class TestDependencyAlertCheck(unittest.TestCase):
    def test_high_severity_alert_notifies(self):
        client = FakeClient()
        client.alerts["owner/repo"] = [
            {"number": 3, "security_advisory": {"severity": "high"}, "dependency": {"package": {"name": "requests"}}}
        ]
        check = DependencyAlertCheck(client, ["owner/repo"], cadence_seconds=1800.0)
        notices, cursor = run(check.run({}))
        self.assertEqual(len(notices), 1)
        self.assertEqual(notices[0].severity, "critical")
        self.assertIn("requests", notices[0].message)
        self.assertEqual(cursor["owner/repo"], [3])

    def test_low_severity_alert_ignored(self):
        client = FakeClient()
        client.alerts["owner/repo"] = [
            {"number": 3, "security_advisory": {"severity": "low"}, "dependency": {"package": {"name": "requests"}}}
        ]
        check = DependencyAlertCheck(client, ["owner/repo"], cadence_seconds=1800.0)
        notices, _ = run(check.run({}))
        self.assertEqual(notices, [])

    def test_already_notified_alert_not_repeated(self):
        client = FakeClient()
        client.alerts["owner/repo"] = [
            {"number": 3, "security_advisory": {"severity": "critical"}, "dependency": {"package": {"name": "requests"}}}
        ]
        check = DependencyAlertCheck(client, ["owner/repo"], cadence_seconds=1800.0)
        notices, _ = run(check.run({"owner/repo": [3]}))
        self.assertEqual(notices, [])


class TestHotfixPushCheck(unittest.TestCase):
    def test_new_hotfix_branch_notifies(self):
        client = FakeClient()
        client.branches["owner/repo"] = [{"name": "hotfix/urgent-fix", "commit": {"sha": "abc123"}}]
        check = HotfixPushCheck(client, ["owner/repo"], cadence_seconds=60.0)
        notices, cursor = run(check.run({}))
        self.assertEqual(len(notices), 1)
        self.assertIn("hotfix/urgent-fix", notices[0].message)
        self.assertEqual(cursor["owner/repo"]["hotfix/urgent-fix"], "abc123")

    def test_unchanged_branch_sha_not_renotified(self):
        client = FakeClient()
        client.branches["owner/repo"] = [{"name": "hotfix/urgent-fix", "commit": {"sha": "abc123"}}]
        check = HotfixPushCheck(client, ["owner/repo"], cadence_seconds=60.0)
        notices, _ = run(check.run({"owner/repo": {"hotfix/urgent-fix": "abc123"}}))
        self.assertEqual(notices, [])

    def test_new_push_to_known_branch_renotifies(self):
        client = FakeClient()
        client.branches["owner/repo"] = [{"name": "hotfix/urgent-fix", "commit": {"sha": "def456"}}]
        check = HotfixPushCheck(client, ["owner/repo"], cadence_seconds=60.0)
        notices, cursor = run(check.run({"owner/repo": {"hotfix/urgent-fix": "abc123"}}))
        self.assertEqual(len(notices), 1)
        self.assertEqual(cursor["owner/repo"]["hotfix/urgent-fix"], "def456")

    def test_non_hotfix_branch_ignored(self):
        client = FakeClient()
        client.branches["owner/repo"] = [{"name": "feature/thing", "commit": {"sha": "abc123"}}]
        check = HotfixPushCheck(client, ["owner/repo"], cadence_seconds=60.0)
        notices, _ = run(check.run({}))
        self.assertEqual(notices, [])

    def test_owners_own_hotfix_push_is_not_notified(self):
        client = FakeClient()
        client.branches["owner/repo"] = [{"name": "hotfix/urgent-fix", "commit": {"sha": "abc123"}}]
        client.events["owner/repo"] = [push_event("Sean", "hotfix/urgent-fix", "abc123")]
        check = HotfixPushCheck(client, ["owner/repo"], cadence_seconds=60.0, username="sean")
        notices, cursor = run(check.run({}))
        self.assertEqual(notices, [])
        # Still tracked, so the same push isn't re-fetched/re-evaluated next tick.
        self.assertEqual(cursor["owner/repo"]["hotfix/urgent-fix"], "abc123")

    def test_someone_elses_hotfix_push_still_notified_when_username_set(self):
        client = FakeClient()
        client.branches["owner/repo"] = [{"name": "hotfix/urgent-fix", "commit": {"sha": "abc123"}}]
        client.events["owner/repo"] = [push_event("someone-else", "hotfix/urgent-fix", "abc123")]
        check = HotfixPushCheck(client, ["owner/repo"], cadence_seconds=60.0, username="sean")
        notices, _ = run(check.run({}))
        self.assertEqual(len(notices), 1)

    def test_someone_else_pushing_a_commit_sean_authored_still_notifies(self):
        # The exclusion is about who pushed, not who wrote it. Reading commit
        # authorship here would silently swallow a real hotfix by someone else
        # that happened to carry Sean's authorship (a cherry-pick, a rebase).
        client = FakeClient()
        client.branches["owner/repo"] = [{"name": "hotfix/urgent-fix", "commit": {"sha": "abc123"}}]
        client.commits[("owner/repo", "abc123")] = {"author": {"login": "Sean"}}
        client.events["owner/repo"] = [push_event("someone-else", "hotfix/urgent-fix", "abc123")]
        check = HotfixPushCheck(client, ["owner/repo"], cadence_seconds=60.0, username="sean")
        notices, _ = run(check.run({}))
        self.assertEqual(len(notices), 1)

    def test_sean_pushing_someone_elses_commit_is_still_his_own_push(self):
        # The mirror image, and the normal shape of an emergency cherry-pick:
        # commit authored by a colleague, pushed by Sean. He doesn't need a
        # notification about something he just did.
        client = FakeClient()
        client.branches["owner/repo"] = [{"name": "hotfix/urgent-fix", "commit": {"sha": "abc123"}}]
        client.commits[("owner/repo", "abc123")] = {"author": {"login": "someone-else"}}
        client.events["owner/repo"] = [push_event("sean", "hotfix/urgent-fix", "abc123")]
        check = HotfixPushCheck(client, ["owner/repo"], cadence_seconds=60.0, username="sean")
        notices, _ = run(check.run({}))
        self.assertEqual(notices, [])

    def test_push_event_for_a_different_branch_does_not_match(self):
        client = FakeClient()
        client.branches["owner/repo"] = [{"name": "hotfix/urgent-fix", "commit": {"sha": "abc123"}}]
        client.events["owner/repo"] = [push_event("sean", "hotfix/other", "abc123")]
        check = HotfixPushCheck(client, ["owner/repo"], cadence_seconds=60.0, username="sean")
        notices, _ = run(check.run({}))
        self.assertEqual(len(notices), 1)

    def test_non_push_events_are_ignored(self):
        client = FakeClient()
        client.branches["owner/repo"] = [{"name": "hotfix/urgent-fix", "commit": {"sha": "abc123"}}]
        client.events["owner/repo"] = [
            {"type": "WatchEvent", "actor": {"login": "sean"}, "payload": {}},
            push_event("sean", "hotfix/urgent-fix", "abc123"),
        ]
        check = HotfixPushCheck(client, ["owner/repo"], cadence_seconds=60.0, username="sean")
        notices, _ = run(check.run({}))
        self.assertEqual(notices, [])

    def test_push_aged_out_of_the_events_window_defaults_to_notifying(self):
        # GitHub caps /events at ~300 events / 90 days. A push we can't find
        # is unknown, not "not Sean's" — and unknown must page rather than
        # stay silent on a real production hotfix.
        client = FakeClient()
        client.branches["owner/repo"] = [{"name": "hotfix/urgent-fix", "commit": {"sha": "abc123"}}]
        client.events["owner/repo"] = []
        check = HotfixPushCheck(client, ["owner/repo"], cadence_seconds=60.0, username="sean")
        notices, _ = run(check.run({}))
        self.assertEqual(len(notices), 1)


class TestDormantRepoCheck(unittest.TestCase):
    def test_dormant_after_prior_activity_notifies(self):
        client = FakeClient()
        client.participation["owner/repo"] = {"all": [2, 3, 1, 4] + [0, 0, 0, 0]}
        check = DormantRepoCheck(client, ["owner/repo"], cadence_seconds=1800.0)
        notices, cursor = run(check.run({}))
        self.assertEqual(len(notices), 1)
        self.assertEqual(notices[0].severity, "info")
        self.assertTrue(cursor["owner/repo"])

    def test_active_repo_no_notice(self):
        client = FakeClient()
        client.participation["owner/repo"] = {"all": [2, 3, 1, 4] + [1, 0, 2, 1]}
        check = DormantRepoCheck(client, ["owner/repo"], cadence_seconds=1800.0)
        notices, cursor = run(check.run({}))
        self.assertEqual(notices, [])
        self.assertFalse(cursor["owner/repo"])

    def test_already_notified_dormant_not_repeated(self):
        client = FakeClient()
        client.participation["owner/repo"] = {"all": [2, 3, 1, 4] + [0, 0, 0, 0]}
        check = DormantRepoCheck(client, ["owner/repo"], cadence_seconds=1800.0)
        notices, _ = run(check.run({"owner/repo": True}))
        self.assertEqual(notices, [])

    def test_insufficient_history_skipped(self):
        client = FakeClient()
        client.participation["owner/repo"] = {"all": [0, 0]}
        check = DormantRepoCheck(client, ["owner/repo"], cadence_seconds=1800.0)
        notices, cursor = run(check.run({}))
        self.assertEqual(notices, [])
        self.assertEqual(cursor, {})


class TestBuildCodeSentinelChecks(unittest.TestCase):
    def test_empty_when_no_token(self):
        settings = Settings(github_token="", github_watched_repos=["owner/repo"])
        self.assertEqual(build_code_sentinel_checks(settings), [])

    def test_empty_when_no_watched_repos(self):
        settings = Settings(github_token="tok", github_watched_repos=[])
        self.assertEqual(build_code_sentinel_checks(settings), [])

    def test_returns_five_checks_when_configured(self):
        settings = Settings(github_token="tok", github_watched_repos=["owner/repo"])
        checks = build_code_sentinel_checks(settings)
        self.assertEqual(len(checks), 5)
        names = {c.name for c in checks}
        self.assertEqual(
            names,
            {"ci_failure", "stale_pr", "dependency_alert", "hotfix_push", "dormant_repo"},
        )

    def test_github_username_is_threaded_into_stale_pr_and_hotfix_push(self):
        settings = Settings(
            github_token="tok", github_watched_repos=["owner/repo"], github_username="sean"
        )
        checks = {c.name: c for c in build_code_sentinel_checks(settings)}
        self.assertEqual(checks["stale_pr"]._username, "sean")
        self.assertEqual(checks["hotfix_push"]._username, "sean")


if __name__ == "__main__":
    unittest.main()
