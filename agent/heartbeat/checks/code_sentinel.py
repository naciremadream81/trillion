"""
Code Sentinel — the five GitHub-watching checks from playbooks/the-code-
sentinel.md, built on the Check protocol (base.py) and GitHubClient
(../github_client.py).

Explicitly excluded (per the playbook): Dependabot updates, green CI runs,
Sean's own pushes, and PRs he opened himself. CIFailureCheck, DependencyAlertCheck,
and DormantRepoCheck never need an identity to stay quiet on the excluded
cases — but StalePRCheck and HotfixPushCheck do, since a PR or a hotfix
branch push can belong to anyone, so they compare against
settings.github_username (case-insensitively) before notifying.

Those two ask *different* questions, and the distinction matters: a PR has an
opener (`pr.user`), but a push has a pusher, who is not necessarily the commit's
author. HotfixPushCheck therefore reads PushEvent actors, not commit authorship
— see _is_own_push. Both fail open: an identity we can't establish gets notified.

Every notice is exactly three sentences: what happened, why it matters,
what to do next — the playbook's own format.

build_code_sentinel_checks(settings) self-skips (returns []) when
github_token or github_watched_repos is empty, mirroring
resolve_search_provider()'s None-means-skip convention.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from .base import Notice
from ..github_client import GitHubClient

logger = logging.getLogger(__name__)

STALE_PR_THRESHOLD_SECONDS = 48 * 3600
DORMANT_WEEKS_RECENT = 4  # ~30 days
ALERT_SEVERITIES_THAT_PAGE = {"high", "critical"}
HOTFIX_BRANCH_PREFIXES = ("hotfix/", "rollback/")


def _parse_iso8601(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class CIFailureCheck:
    name = "ci_failure"

    def __init__(self, client: GitHubClient, repos: list[str], cadence_seconds: float) -> None:
        self._client = client
        self._repos = repos
        self.cadence_seconds = cadence_seconds

    async def run(self, cursor: dict) -> tuple[list, dict]:
        notices = []
        new_cursor = dict(cursor)
        for repo in self._repos:
            try:
                default_branch = await self._client.get_default_branch(repo)
                runs = await self._client.list_check_runs(repo, default_branch)
            except Exception:  # noqa: BLE001 — one repo's API error must not block the rest
                logger.exception("ci_failure check failed for %r", repo)
                continue
            failures = [r for r in runs if r.get("status") == "completed" and r.get("conclusion") == "failure"]
            if not failures:
                continue
            latest = max(failures, key=lambda r: r.get("started_at") or "")
            last_seen_id = new_cursor.get(repo)
            if latest.get("id") == last_seen_id:
                continue
            new_cursor[repo] = latest.get("id")
            check_name = latest.get("name", "a check")
            # Name the branch we actually queried. Saying "main" for a repo
            # whose default is "master" or "develop" sends Sean to look at a
            # branch that isn't the one that broke.
            notices.append(
                Notice(
                    severity="critical",
                    message=(
                        f"{check_name!r} failed on {repo}'s {default_branch} branch. "
                        f"A red {default_branch} blocks anyone building on top of it until it's fixed. "
                        f"I'd open the failing run's logs and fix or revert whatever broke it."
                    ),
                )
            )
        return notices, new_cursor


class StalePRCheck:
    name = "stale_pr"

    def __init__(
        self, client: GitHubClient, repos: list[str], cadence_seconds: float, username: str = ""
    ) -> None:
        self._client = client
        self._repos = repos
        self.cadence_seconds = cadence_seconds
        self._username = username

    async def run(self, cursor: dict) -> tuple[list, dict]:
        notices = []
        new_cursor = dict(cursor)
        now = datetime.now(timezone.utc)
        for repo in self._repos:
            try:
                prs = await self._client.list_open_pull_requests(repo)
            except Exception:  # noqa: BLE001
                logger.exception("stale_pr check failed for %r", repo)
                continue
            already_notified = set(new_cursor.get(repo, []))
            still_open = set()
            for pr in prs:
                author = (pr.get("user") or {}).get("login") or ""
                if self._username and author.lower() == self._username.lower():
                    continue  # Sean's own PR — excluded entirely, per the playbook
                number = pr.get("number")
                still_open.add(number)
                updated_at = _parse_iso8601(pr["updated_at"])
                if (now - updated_at).total_seconds() < STALE_PR_THRESHOLD_SECONDS:
                    continue
                if number in already_notified:
                    continue
                already_notified.add(number)
                notices.append(
                    Notice(
                        severity="info",
                        message=(
                            f"{repo}#{number} ({pr.get('title', '')!r}) has had no activity in over 48 hours. "
                            f"A stalled PR either blocks whatever depends on it or is quietly rotting. "
                            f"I'd ping the author or review it yourself to unblock it."
                        ),
                    )
                )
            # Drop PRs that are no longer open so a reopen can re-notify.
            new_cursor[repo] = sorted(already_notified & still_open)
        return notices, new_cursor


class DependencyAlertCheck:
    name = "dependency_alert"

    def __init__(self, client: GitHubClient, repos: list[str], cadence_seconds: float) -> None:
        self._client = client
        self._repos = repos
        self.cadence_seconds = cadence_seconds

    async def run(self, cursor: dict) -> tuple[list, dict]:
        notices = []
        new_cursor = dict(cursor)
        for repo in self._repos:
            try:
                alerts = await self._client.list_dependabot_alerts(repo)
            except Exception:  # noqa: BLE001
                logger.exception("dependency_alert check failed for %r", repo)
                continue
            already_notified = set(new_cursor.get(repo, []))
            for alert in alerts:
                number = alert.get("number")
                severity = (alert.get("security_advisory", {}) or {}).get("severity", "").lower()
                if severity not in ALERT_SEVERITIES_THAT_PAGE:
                    continue
                if number in already_notified:
                    continue
                already_notified.add(number)
                package = (alert.get("dependency", {}) or {}).get("package", {}).get("name", "a dependency")
                notices.append(
                    Notice(
                        severity="critical",
                        message=(
                            f"{repo} has a {severity}-severity vulnerability in {package}. "
                            f"High/critical CVEs in a dependency are an active exploitation risk, not routine upkeep. "
                            f"I'd review the advisory and upgrade the package as soon as you can."
                        ),
                    )
                )
            new_cursor[repo] = sorted(already_notified)
        return notices, new_cursor


class HotfixPushCheck:
    name = "hotfix_push"

    def __init__(
        self, client: GitHubClient, repos: list[str], cadence_seconds: float, username: str = ""
    ) -> None:
        self._client = client
        self._repos = repos
        self.cadence_seconds = cadence_seconds
        self._username = username

    async def run(self, cursor: dict) -> tuple[list, dict]:
        notices = []
        new_cursor = dict(cursor)
        for repo in self._repos:
            try:
                branches = await self._client.list_branches(repo)
            except Exception:  # noqa: BLE001
                logger.exception("hotfix_push check failed for %r", repo)
                continue
            known = dict(new_cursor.get(repo, {}))
            for branch in branches:
                branch_name = branch.get("name", "")
                if not branch_name.startswith(HOTFIX_BRANCH_PREFIXES):
                    continue
                sha = (branch.get("commit", {}) or {}).get("sha")
                if known.get(branch_name) == sha:
                    continue
                known[branch_name] = sha
                if (
                    self._username
                    and sha
                    and await self._is_own_push(repo, branch_name, sha)
                ):
                    continue  # Sean's own push — excluded entirely, per the playbook
                notices.append(
                    Notice(
                        severity="warning",
                        message=(
                            f"{repo} got a new push to {branch_name!r}. "
                            f"Hotfix and rollback branches mean something's being emergency-patched in production. "
                            f"I'd check what changed and confirm it's intentional before it merges."
                        ),
                    )
                )
            new_cursor[repo] = known
        return notices, new_cursor

    async def _is_own_push(self, repo: str, branch_name: str, sha: str) -> bool:
        """Whether the watched account is the one who *pushed* sha to this branch.

        Deliberately not commit authorship. A commit's `author` is whoever
        wrote it, which is a different question and gets both cases wrong:
        someone else pushing a commit Sean authored would be silently
        suppressed, and Sean pushing someone else's commit — the normal shape
        of a cherry-picked emergency fix — would page him about his own push.
        PushEvent's top-level `actor` is the pusher, so that's what this reads.

        Unknown means notify. Events age out of GitHub's window and the API
        can fail, and neither is evidence the push was Sean's; missing one
        exclusion is far cheaper than staying silent on a real hotfix."""
        try:
            events = await self._client.list_repo_events(repo)
        except Exception:  # noqa: BLE001
            logger.exception("hotfix_push check failed to fetch events for %r", repo)
            return False
        ref = f"refs/heads/{branch_name}"
        for event in events:
            if event.get("type") != "PushEvent":
                continue
            payload = event.get("payload") or {}
            if payload.get("head") != sha or payload.get("ref") != ref:
                continue
            actor = (event.get("actor") or {}).get("login") or ""
            return actor.lower() == self._username.lower()
        return False


class DormantRepoCheck:
    name = "dormant_repo"

    def __init__(self, client: GitHubClient, repos: list[str], cadence_seconds: float) -> None:
        self._client = client
        self._repos = repos
        self.cadence_seconds = cadence_seconds

    async def run(self, cursor: dict) -> tuple[list, dict]:
        notices = []
        new_cursor = dict(cursor)
        for repo in self._repos:
            try:
                participation = await self._client.get_participation(repo)
            except Exception:  # noqa: BLE001
                logger.exception("dormant_repo check failed for %r", repo)
                continue
            weeks = participation.get("all", []) or []
            if len(weeks) <= DORMANT_WEEKS_RECENT:
                continue
            recent = weeks[-DORMANT_WEEKS_RECENT:]
            prior = weeks[:-DORMANT_WEEKS_RECENT]
            is_dormant = all(count == 0 for count in recent) and any(count > 0 for count in prior)
            already_notified = new_cursor.get(repo, False)
            if is_dormant and not already_notified:
                new_cursor[repo] = True
                notices.append(
                    Notice(
                        severity="info",
                        message=(
                            f"{repo} has had zero commits in about 30 days after previously seeing weekly activity. "
                            f"A sudden stop after a steady cadence usually means it's stalled, not finished. "
                            f"I'd check in on it or archive it if it's actually done."
                        ),
                    )
                )
            elif not is_dormant:
                new_cursor[repo] = False
        return notices, new_cursor


def build_code_sentinel_checks(settings) -> list:
    """
    The five Code Sentinel checks, or [] when unconfigured.

    Self-skips (no token, no watched repos) rather than registering and
    failing on every tick — same convention as
    agent/tools/web_search.py::resolve_search_provider().
    """
    if not settings.github_token or not settings.github_watched_repos:
        return []
    client = GitHubClient(settings.github_token)
    repos = settings.github_watched_repos
    username = settings.github_username
    return [
        CIFailureCheck(client, repos, cadence_seconds=settings.heartbeat_fast_cadence_seconds),
        HotfixPushCheck(
            client, repos, cadence_seconds=settings.heartbeat_fast_cadence_seconds, username=username
        ),
        StalePRCheck(
            client, repos, cadence_seconds=settings.heartbeat_slow_cadence_seconds, username=username
        ),
        DependencyAlertCheck(client, repos, cadence_seconds=settings.heartbeat_slow_cadence_seconds),
        DormantRepoCheck(client, repos, cadence_seconds=settings.heartbeat_slow_cadence_seconds),
    ]
