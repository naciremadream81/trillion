"""
Tests for GitHubClient (agent/heartbeat/github_client.py).

Run from the project root:
    python -m unittest tests.test_github_client
"""

import asyncio
import unittest

from agent.heartbeat.github_client import GitHubClient


def run(coro):
    return asyncio.run(coro)


class FakeGitHubClient(GitHubClient):
    """Overrides _get so each list_*/get_* method can be tested without a
    network call, mirroring FakeWebSearchTool's approach to _search."""

    def __init__(self, response=None, error=None):
        super().__init__(token="fake-token")
        self._response = response
        self._error = error
        self.calls = []

    async def _get(self, path, params=None):
        self.calls.append((path, params))
        if self._error is not None:
            raise self._error
        return self._response


class TestGitHubClient(unittest.TestCase):
    def test_list_check_runs_returns_check_runs_list(self):
        client = FakeGitHubClient(response={"check_runs": [{"id": 1}]})
        result = run(client.list_check_runs("owner/repo", "main"))
        self.assertEqual(result, [{"id": 1}])
        self.assertEqual(client.calls[0][0], "/repos/owner/repo/commits/main/check-runs")

    def test_list_check_runs_defaults_to_empty_list(self):
        client = FakeGitHubClient(response={})
        result = run(client.list_check_runs("owner/repo", "main"))
        self.assertEqual(result, [])

    def test_list_open_pull_requests(self):
        client = FakeGitHubClient(response=[{"number": 1}])
        result = run(client.list_open_pull_requests("owner/repo"))
        self.assertEqual(result, [{"number": 1}])
        path, params = client.calls[0]
        self.assertEqual(path, "/repos/owner/repo/pulls")
        self.assertEqual(params["state"], "open")

    def test_list_dependabot_alerts(self):
        client = FakeGitHubClient(response=[{"number": 5}])
        result = run(client.list_dependabot_alerts("owner/repo"))
        self.assertEqual(result, [{"number": 5}])
        self.assertEqual(client.calls[0][0], "/repos/owner/repo/dependabot/alerts")

    def test_list_branches(self):
        client = FakeGitHubClient(response=[{"name": "hotfix/urgent"}])
        result = run(client.list_branches("owner/repo"))
        self.assertEqual(result, [{"name": "hotfix/urgent"}])
        self.assertEqual(client.calls[0][0], "/repos/owner/repo/branches")

    def test_get_default_branch_returns_default_branch(self):
        client = FakeGitHubClient(response={"default_branch": "master"})
        result = run(client.get_default_branch("owner/repo"))
        self.assertEqual(result, "master")
        self.assertEqual(client.calls[0][0], "/repos/owner/repo")

    def test_get_default_branch_falls_back_to_main(self):
        client = FakeGitHubClient(response={})
        result = run(client.get_default_branch("owner/repo"))
        self.assertEqual(result, "main")

    def test_get_commit_returns_commit(self):
        client = FakeGitHubClient(response={"author": {"login": "sean"}})
        result = run(client.get_commit("owner/repo", "abc123"))
        self.assertEqual(result, {"author": {"login": "sean"}})
        self.assertEqual(client.calls[0][0], "/repos/owner/repo/commits/abc123")

    def test_get_commit_defaults_to_empty_dict(self):
        client = FakeGitHubClient(response=None)
        result = run(client.get_commit("owner/repo", "abc123"))
        self.assertEqual(result, {})

    def test_list_repo_events(self):
        client = FakeGitHubClient(response=[{"type": "PushEvent"}])
        result = run(client.list_repo_events("owner/repo"))
        self.assertEqual(result, [{"type": "PushEvent"}])
        self.assertEqual(client.calls[0][0], "/repos/owner/repo/events")

    def test_list_repo_events_defaults_to_empty_list(self):
        client = FakeGitHubClient(response=None)
        result = run(client.list_repo_events("owner/repo"))
        self.assertEqual(result, [])

    def test_get_participation_returns_dict(self):
        client = FakeGitHubClient(response={"all": [1, 2, 3]})
        result = run(client.get_participation("owner/repo"))
        self.assertEqual(result, {"all": [1, 2, 3]})
        self.assertEqual(client.calls[0][0], "/repos/owner/repo/stats/participation")

    def test_get_participation_defaults_to_empty_dict(self):
        client = FakeGitHubClient(response=None)
        result = run(client.get_participation("owner/repo"))
        self.assertEqual(result, {})

    def test_headers_include_bearer_token(self):
        client = GitHubClient(token="secret-token")
        headers = client._headers()
        self.assertEqual(headers["Authorization"], "Bearer secret-token")
        self.assertEqual(headers["Accept"], "application/vnd.github+json")

    def test_api_error_propagates(self):
        client = FakeGitHubClient(error=RuntimeError("GitHub API error 401: bad credentials"))
        with self.assertRaises(RuntimeError):
            run(client.list_branches("owner/repo"))


if __name__ == "__main__":
    unittest.main()
