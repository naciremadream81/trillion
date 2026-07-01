"""
Phase 3 verification — the best-effort recorder.

Run from the project root:
    python -m unittest tests.test_recorder
"""

import types
import unittest

from agent.cost import recorder
from agent.cost.pricing import compute_cost
from agent.cost.recorder import record_usage, set_usage_repo
from agent.providers.base import TokenUsage


class FakeRepo:
    """Captures record() calls; can be told to raise."""

    def __init__(self, raise_on_record: bool = False):
        self.calls: list[dict] = []
        self.raise_on_record = raise_on_record

    def record(self, **kwargs):
        if self.raise_on_record:
            raise RuntimeError("simulated DB failure")
        self.calls.append(kwargs)
        return len(self.calls)


class TestRecordUsage(unittest.TestCase):
    def tearDown(self):
        set_usage_repo(None)  # reset global between tests

    def test_computes_cost_and_records(self):
        repo = FakeRepo()
        set_usage_repo(repo)
        usage = TokenUsage(input_tokens=1000, output_tokens=1000)

        record_usage("claude-sonnet-4-6", usage, source="conversation")

        self.assertEqual(len(repo.calls), 1)
        call = repo.calls[0]
        self.assertEqual(call["model"], "claude-sonnet-4-6")
        self.assertEqual(call["input_tokens"], 1000)
        self.assertEqual(call["output_tokens"], 1000)
        self.assertEqual(call["source"], "conversation")
        expected = compute_cost("claude-sonnet-4-6", input_tokens=1000, output_tokens=1000)
        self.assertAlmostEqual(call["cost_usd"], expected)

    def test_none_cache_fields_are_handled(self):
        repo = FakeRepo()
        set_usage_repo(repo)
        # A usage-like object where cache fields are None (not just 0).
        usage = types.SimpleNamespace(
            input_tokens=500,
            output_tokens=200,
            cache_write_tokens=None,
            cache_read_tokens=None,
        )

        record_usage("claude-sonnet-4-6", usage)

        self.assertEqual(len(repo.calls), 1)
        call = repo.calls[0]
        self.assertEqual(call["cache_write_tokens"], 0)
        self.assertEqual(call["cache_read_tokens"], 0)

    def test_repo_failure_does_not_propagate(self):
        set_usage_repo(FakeRepo(raise_on_record=True))
        # Must not raise — best-effort swallows everything.
        try:
            record_usage("claude-sonnet-4-6", TokenUsage(input_tokens=10))
        except Exception as e:  # noqa: BLE001
            self.fail(f"record_usage propagated an exception: {e}")

    def test_no_repo_is_a_noop(self):
        set_usage_repo(None)
        # Should simply do nothing, no error.
        record_usage("claude-sonnet-4-6", TokenUsage(input_tokens=10))

    def test_none_usage_is_a_noop(self):
        repo = FakeRepo()
        set_usage_repo(repo)
        record_usage("claude-sonnet-4-6", None)
        self.assertEqual(len(repo.calls), 0)


if __name__ == "__main__":
    unittest.main()
