"""
Tests for the query_analytics read-only Supabase tool.

Offline tests (validator, schema, row cap, JSON safety) run always.
The live test runs only when SUPABASE_ANALYTICS_URL is set in the environment
(so a plain `python -m unittest` stays offline).

Run offline:            python -m unittest tests.test_analytics_tool
Run incl. live check:   (load .env first, e.g. via a wrapper that sets the env)
"""

import asyncio
import datetime
import json
import os
import unittest

from agent.tools.analytics_tool import MAX_ROWS, QueryAnalyticsTool, validate_sql


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class TestValidator(unittest.TestCase):
    def test_accepts_select(self):
        self.assertEqual(validate_sql("SELECT 1"), "SELECT 1")
        self.assertEqual(validate_sql("  select * from contacts ;  "), "select * from contacts")

    def test_accepts_with(self):
        sql = "WITH x AS (SELECT 1 AS n) SELECT n FROM x"
        self.assertEqual(validate_sql(sql), sql)

    def test_rejects_writes(self):
        for bad in ["INSERT INTO t VALUES (1)", "update t set a=1", "delete from t",
                    "drop table t", "truncate t", "alter table t add column c int"]:
            with self.assertRaises(ValueError):
                validate_sql(bad)

    def test_rejects_chaining(self):
        with self.assertRaises(ValueError):
            validate_sql("SELECT 1; DROP TABLE contacts")

    def test_rejects_empty(self):
        with self.assertRaises(ValueError):
            validate_sql("   ")

    def test_created_at_column_is_not_a_false_positive(self):
        # 'created_at' contains 'create' but must not trip the write-keyword guard.
        self.assertTrue(validate_sql("SELECT created_at FROM contacts"))


class FakeTool(QueryAnalyticsTool):
    """Overrides _fetch so run() can be tested without a database."""
    def __init__(self, rows):
        super().__init__("postgresql://unused")
        self._rows = rows

    async def _fetch(self, sql, *args):
        return self._rows


class TestRun(unittest.TestCase):
    def test_definition_shape(self):
        t = QueryAnalyticsTool("postgresql://unused")
        d = t.definition()
        self.assertEqual(d["name"], "query_analytics")
        self.assertIn("input_schema", d)
        self.assertEqual(
            set(d["input_schema"]["properties"]["action"]["enum"]),
            {"query", "list_tables", "describe_table"},
        )

    def test_query_requires_sql(self):
        t = QueryAnalyticsTool("postgresql://unused")
        self.assertIn("required", run(t.run(action="query")))

    def test_describe_requires_valid_table(self):
        t = QueryAnalyticsTool("postgresql://unused")
        self.assertIn("valid 'table'", run(t.run(action="describe_table", table="bad; drop")))

    def test_query_json_safe(self):
        ts = datetime.datetime(2026, 7, 1, 12, 0, tzinfo=datetime.timezone.utc)
        t = FakeTool([{"id": 1, "full_name": "Ada", "created_at": ts}])
        out = json.loads(run(t.run(action="query", sql="SELECT * FROM contacts")))
        self.assertEqual(out["row_count"], 1)
        self.assertEqual(out["rows"][0]["created_at"], ts.isoformat())  # datetime -> ISO string

    def test_row_cap(self):
        t = FakeTool([{"id": i} for i in range(MAX_ROWS + 50)])
        out = json.loads(run(t.run(action="query", sql="SELECT * FROM contacts")))
        self.assertEqual(out["row_count"], MAX_ROWS)
        self.assertTrue(out["truncated"])

    def test_bad_sql_is_reported_not_raised(self):
        t = QueryAnalyticsTool("postgresql://unused")
        self.assertIn("rejected", run(t.run(action="query", sql="DELETE FROM contacts")))


@unittest.skipUnless(os.getenv("SUPABASE_ANALYTICS_URL"), "SUPABASE_ANALYTICS_URL not set")
class TestLive(unittest.TestCase):
    def test_count_contacts(self):
        t = QueryAnalyticsTool(os.environ["SUPABASE_ANALYTICS_URL"])
        out = json.loads(run(t.run(action="query", sql="SELECT count(*) AS n FROM contacts")))
        self.assertEqual(out["row_count"], 1)
        self.assertIn("n", out["rows"][0])


if __name__ == "__main__":
    unittest.main()
