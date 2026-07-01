"""
Phase 2 verification — storage schema + repo round-trip.

Run from the project root:
    python -m unittest tests.test_storage
"""

import os
import sqlite3
import tempfile
import unittest

from agent.cost.storage import UsageRepo

EXPECTED_COLUMNS = {
    "id",
    "model",
    "input_tokens",
    "output_tokens",
    "cache_write_tokens",
    "cache_read_tokens",
    "cost_usd",
    "source",
    "created_at",
}


class TestStorage(unittest.TestCase):
    def setUp(self):
        # A real file (not :memory:) because the repo opens a fresh connection
        # per call — a :memory: db would be a different database each time.
        self.tmp = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmp, "usage.db")
        self.repo = UsageRepo(db_path=self.db_path)

    def tearDown(self):
        try:
            os.remove(self.db_path)
        except FileNotFoundError:
            pass
        os.rmdir(self.tmp)

    def test_table_exists_with_expected_columns(self):
        conn = sqlite3.connect(self.db_path)
        try:
            cols = {row[1] for row in conn.execute("PRAGMA table_info(usage)")}
        finally:
            conn.close()
        self.assertEqual(cols, EXPECTED_COLUMNS)

    def test_timestamp_is_indexed(self):
        conn = sqlite3.connect(self.db_path)
        try:
            indexes = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='index'"
                )
            }
        finally:
            conn.close()
        self.assertIn("idx_usage_created_at", indexes)

    def test_record_round_trip(self):
        row_id = self.repo.record(
            model="claude-sonnet-4-6",
            input_tokens=1200,
            output_tokens=340,
            cache_write_tokens=800,
            cache_read_tokens=5000,
            cost_usd=0.0123,
            source="conversation",
            created_at="2026-07-01T12:00:00+00:00",
        )
        self.assertIsInstance(row_id, int)

        rows = self.repo.all()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["id"], row_id)
        self.assertEqual(row["model"], "claude-sonnet-4-6")
        self.assertEqual(row["input_tokens"], 1200)
        self.assertEqual(row["output_tokens"], 340)
        self.assertEqual(row["cache_write_tokens"], 800)
        self.assertEqual(row["cache_read_tokens"], 5000)
        self.assertAlmostEqual(row["cost_usd"], 0.0123)
        self.assertEqual(row["source"], "conversation")
        self.assertEqual(row["created_at"], "2026-07-01T12:00:00+00:00")

    def test_defaults_are_applied(self):
        # Minimal insert: only a model. Everything else should default cleanly.
        self.repo.record(model="gpt-4o")
        row = self.repo.all()[0]
        self.assertEqual(row["input_tokens"], 0)
        self.assertEqual(row["output_tokens"], 0)
        self.assertEqual(row["cache_write_tokens"], 0)
        self.assertEqual(row["cache_read_tokens"], 0)
        self.assertEqual(row["cost_usd"], 0.0)
        self.assertEqual(row["source"], "conversation")
        self.assertTrue(row["created_at"])  # auto-stamped, non-empty


if __name__ == "__main__":
    unittest.main()
