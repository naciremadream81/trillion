"""
Phase 4 verification — the /api/usage endpoint returns the payload over HTTP.

Requires aiohttp (a project dependency). Run from the project root:
    python -m unittest tests.test_endpoint
"""

import os
import shutil
import tempfile

from aiohttp.test_utils import AioHTTPTestCase

from agent.cost.aggregate import UsageDashboard
from agent.cost.storage import UsageRepo
from serve import build_app


class TestUsageEndpoint(AioHTTPTestCase):
    async def get_application(self):
        self.tmp = tempfile.mkdtemp()
        db_path = os.path.join(self.tmp, "usage.db")
        repo = UsageRepo(db_path=db_path)
        repo.record(
            model="claude-sonnet-4-6",
            input_tokens=1_000,
            output_tokens=500,
            cost_usd=0.0105,
        )
        return build_app(dashboard=UsageDashboard(repo))

    def tearDown(self):
        super().tearDown()
        shutil.rmtree(self.tmp, ignore_errors=True)

    async def test_usage_endpoint_returns_payload(self):
        resp = await self.client.request("GET", "/api/usage")
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.content_type, "application/json")
        data = await resp.json()
        # Shape check — the fields the UI depends on.
        for key in ("total_cost_usd", "today_cost_usd", "cache_savings_usd",
                    "by_model", "by_day", "delta", "month"):
            self.assertIn(key, data)
        self.assertEqual(data["calls"], 1)
        self.assertEqual(data["by_model"][0]["model"], "claude-sonnet-4-6")


if __name__ == "__main__":
    import unittest
    unittest.main()
