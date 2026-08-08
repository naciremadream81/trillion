"""
Tests for the Agent Factory draft generator (agent/factory/draft.py).

Uses a FakeProvider that returns canned replies — no live API calls.

Run from the project root:
    python -m unittest tests.test_factory_draft
"""

import asyncio
import unittest

from agent.factory.draft import DraftError, compute_tool_allowlist, draft_agent_spec, slugify, unique_slug
from agent.providers.base import BaseProvider, ProviderResponse, TextChunk, TokenUsage


def run(coro):
    return asyncio.run(coro)


REPORT = {
    "domain": "SQL migration review",
    "competencies": ["schema review"],
    "tools_available": ["web_search", "query_analytics"],
    "tools_wishlist": ["query_analytics"],
    "design_patterns": ["idempotent migrations"],
    "sources": [],
}


class FakeProvider(BaseProvider):
    def __init__(self, replies):
        self._replies = list(replies)

    @property
    def model_name(self):
        return "fake-model"

    async def stream(self, messages, system, tools=None):
        text = self._replies.pop(0) if self._replies else ""
        yield TextChunk(text=text)
        yield ProviderResponse(text=text, tool_calls=[], usage=TokenUsage(), model=self.model_name)


class TestSlugHelpers(unittest.TestCase):
    def test_slugify_basic(self):
        self.assertEqual(slugify("SQL Migration Review"), "sql-migration-review")

    def test_slugify_strips_punctuation(self):
        self.assertEqual(slugify("C++ / Rust!!"), "c-rust")

    def test_slugify_empty_falls_back(self):
        self.assertEqual(slugify("###"), "agent")

    def test_unique_slug_appends_suffix_on_collision(self):
        taken = {"sql-reviewer"}
        result = unique_slug("sql-reviewer", lambda s: s in taken)
        self.assertEqual(result, "sql-reviewer-2")

    def test_slugify_caps_length(self):
        # A verbose model-generated `domain` sentence, unbounded, previously
        # slugified past ext4's 255-byte filename limit and crashed
        # write_spec_markdown() with ENAMETOOLONG.
        long_domain = (
            "Database DevOps SQL Migration Safety Engineering: the discipline of "
            "statically and dynamically analyzing SQL migration scripts before "
            "they are applied to production databases, covering correctness, "
            "safety, reversibility, and performance impact"
        )
        slug = slugify(long_domain)
        self.assertLessEqual(len(slug), 60)
        self.assertFalse(slug.endswith("-"))

    def test_slugify_truncation_does_not_leave_trailing_hyphen(self):
        # Truncating mid-word can land exactly on a "-" boundary; make sure
        # that gets stripped rather than producing e.g. "foo-bar-".
        text = "a" * 59 + " b"  # 60th char lands on the separating hyphen
        slug = slugify(text)
        self.assertFalse(slug.endswith("-"))

    def test_unique_slug_no_collision(self):
        result = unique_slug("sql-reviewer", lambda s: False)
        self.assertEqual(result, "sql-reviewer")


class TestComputeToolAllowlist(unittest.TestCase):
    def test_intersects_wishlist_registry_and_factory_allowed(self):
        result = compute_tool_allowlist(
            wishlist=["web_search", "query_analytics", "made_up_tool"],
            registry_tool_names=["web_search", "query_analytics"],
            factory_allowed_names={"web_search"},  # query_analytics NOT factory-allowed
        )
        self.assertEqual(result, ["web_search"])

    def test_empty_when_nothing_overlaps(self):
        result = compute_tool_allowlist([], ["web_search"], {"web_search"})
        self.assertEqual(result, [])


class TestDraftAgentSpec(unittest.TestCase):
    def test_success_paraphrased(self):
        provider = FakeProvider(['{"system_prompt": "You are a specialist in reviewing SQL migrations for safety."}'])
        spec = run(
            draft_agent_spec(
                "a specialist that reviews SQL migrations for safety before they run",
                REPORT,
                provider,
                registry_tool_names=["web_search", "query_analytics"],
                factory_allowed_names={"web_search", "query_analytics"},
                slug_taken=lambda s: False,
            )
        )
        self.assertEqual(spec["slug"], "sql-migration-review")
        self.assertIn("SQL migrations", spec["system_prompt"])
        self.assertEqual(spec["tool_allowlist"], ["query_analytics", "web_search"])

    def test_verbatim_quote_triggers_retry(self):
        role = "a specialist that reviews SQL migrations for safety before they run"
        verbatim_reply = f'{{"system_prompt": "{role}"}}'
        paraphrased_reply = '{"system_prompt": "You review database schema changes before deployment."}'
        provider = FakeProvider([verbatim_reply, paraphrased_reply])
        spec = run(
            draft_agent_spec(
                role, REPORT, provider,
                registry_tool_names=["web_search"], factory_allowed_names={"web_search"},
                slug_taken=lambda s: False,
            )
        )
        self.assertIn("database schema", spec["system_prompt"])

    def test_fails_after_exhausting_retry_on_bad_json(self):
        provider = FakeProvider(["not json", "still not json"])
        with self.assertRaises(DraftError):
            run(
                draft_agent_spec(
                    "role", REPORT, provider,
                    registry_tool_names=[], factory_allowed_names=set(),
                    slug_taken=lambda s: False,
                )
            )

    def test_slug_collision_gets_suffix(self):
        provider = FakeProvider(['{"system_prompt": "You review migrations."}'])
        taken = {"sql-migration-review"}
        spec = run(
            draft_agent_spec(
                "role", REPORT, provider,
                registry_tool_names=[], factory_allowed_names=set(),
                slug_taken=lambda s: s in taken,
            )
        )
        self.assertEqual(spec["slug"], "sql-migration-review-2")

    def test_tool_allowlist_never_exceeds_factory_allowed(self):
        provider = FakeProvider(['{"system_prompt": "You review migrations carefully."}'])
        spec = run(
            draft_agent_spec(
                "role", REPORT, provider,
                registry_tool_names=["web_search", "query_analytics"],
                factory_allowed_names={"web_search"},  # query_analytics excluded
                slug_taken=lambda s: False,
            )
        )
        self.assertNotIn("query_analytics", spec["tool_allowlist"])


if __name__ == "__main__":
    unittest.main()
