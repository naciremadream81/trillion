"""
GitHubClient — thin async wrapper over the GitHub REST API.

Mirrors agent/tools/web_search.py's design: one HTTP call per method, so
each is trivially mockable in tests without a live API call. Used
exclusively by the Code Sentinel checks (agent/heartbeat/checks/
code_sentinel.py); nothing else in Trillion talks to GitHub.
"""

from __future__ import annotations

import aiohttp

GITHUB_API_URL = "https://api.github.com"
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=15, connect=5)


class GitHubClient:
    def __init__(self, token: str, base_url: str = GITHUB_API_URL) -> None:
        self._token = token
        self._base_url = base_url.rstrip("/")

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    async def _get(self, path: str, params: dict | None = None) -> dict | list:
        url = f"{self._base_url}{path}"
        async with aiohttp.ClientSession(timeout=REQUEST_TIMEOUT) as session:
            async with session.get(url, headers=self._headers(), params=params) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    raise RuntimeError(f"GitHub API error {resp.status} for {path}: {body[:200]}")
                return await resp.json()

    async def list_check_runs(self, repo: str, ref: str) -> list[dict]:
        """Check runs for the given ref (e.g. the default branch's HEAD)."""
        data = await self._get(f"/repos/{repo}/commits/{ref}/check-runs")
        return data.get("check_runs", []) or []

    async def list_open_pull_requests(self, repo: str) -> list[dict]:
        data = await self._get(f"/repos/{repo}/pulls", params={"state": "open", "per_page": "100"})
        return data or []

    async def list_dependabot_alerts(self, repo: str) -> list[dict]:
        data = await self._get(f"/repos/{repo}/dependabot/alerts", params={"state": "open", "per_page": "100"})
        return data or []

    async def list_branches(self, repo: str) -> list[dict]:
        data = await self._get(f"/repos/{repo}/branches", params={"per_page": "100"})
        return data or []

    async def get_default_branch(self, repo: str) -> str:
        """The repo's default branch. Never assume "main" — watched repos
        are not required to use that name (e.g. "master", "develop")."""
        data = await self._get(f"/repos/{repo}")
        return (data or {}).get("default_branch") or "main"

    async def get_commit(self, repo: str, sha: str) -> dict:
        """A single commit, including its GitHub author/committer identity.

        Note for future callers: commit `author` is who *wrote* the commit,
        not who pushed it. To attribute a push to an account, use
        list_repo_events() — see HotfixPushCheck._is_own_push."""
        data = await self._get(f"/repos/{repo}/commits/{sha}")
        return data or {}

    async def list_repo_events(self, repo: str) -> list[dict]:
        """Recent repository events, newest first.

        The only GitHub surface that reports who *pushed* a SHA (PushEvent's
        top-level `actor`) as opposed to who authored the commit. Capped by
        GitHub at ~300 events / 90 days and served from a ~60s cache, so a
        push can age out of this window — callers must treat "not found" as
        unknown rather than as a negative answer."""
        data = await self._get(f"/repos/{repo}/events", params={"per_page": "100"})
        return data or []

    async def get_participation(self, repo: str) -> dict:
        """Weekly commit counts (all branches) for the last 52 weeks, oldest first."""
        data = await self._get(f"/repos/{repo}/stats/participation")
        return data or {}
