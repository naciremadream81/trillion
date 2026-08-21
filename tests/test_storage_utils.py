"""
Tests for agent/storage_utils.py — the leaked-connection fix.

Found in production: trillion-orb.service reached 716MB RSS over three days
against 78MB fresh, holding 41 open database file descriptors against 9. The
cause was that `with sqlite3.Connection` commits but does NOT close, so every
request that touched storage leaked a connection. `GET /api/security/status`
leaked ~10MB per 60 calls; the browser polls it continuously.

Run from the project root:
    python -m unittest tests.test_storage_utils
"""

import gc
import os
import shutil
import sqlite3
import tempfile
import unittest
import warnings

from agent.storage_utils import connect


class TestConnectClosesTheConnection(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "t.db")
        with connect(self.db) as conn:
            conn.execute("CREATE TABLE t (a INTEGER)")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_the_connection_is_closed_on_exit(self):
        # The whole point. sqlite3's own context manager leaves it open.
        with connect(self.db) as conn:
            captured = conn
        with self.assertRaises(sqlite3.ProgrammingError):
            captured.execute("SELECT 1")

    def test_sqlite_own_context_manager_would_not_have(self):
        # Pinning the surprising behaviour this module exists to work around,
        # so nobody "simplifies" it back.
        raw = sqlite3.connect(self.db)
        try:
            with raw:
                raw.execute("SELECT 1")
            raw.execute("SELECT 1")  # still open — no exception
        finally:
            raw.close()

    def test_writes_commit(self):
        with connect(self.db) as conn:
            conn.execute("INSERT INTO t (a) VALUES (1)")
        with connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM t").fetchone()[0], 1)

    def test_an_exception_rolls_back_and_still_closes(self):
        captured = []
        with self.assertRaises(ValueError):
            with connect(self.db) as conn:
                captured.append(conn)
                conn.execute("INSERT INTO t (a) VALUES (99)")
                raise ValueError("boom")
        with connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM t WHERE a = 99").fetchone()[0], 0)
        with self.assertRaises(sqlite3.ProgrammingError):
            captured[0].execute("SELECT 1")

    def test_rows_come_back_as_sqlite_row(self):
        with connect(self.db) as conn:
            conn.execute("INSERT INTO t (a) VALUES (7)")
            row = conn.execute("SELECT a FROM t").fetchone()
        self.assertEqual(row["a"], 7)

    def test_repeated_use_leaks_no_resource_warnings(self):
        # The warning that had been in test output all along.
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", ResourceWarning)
            for _ in range(50):
                with connect(self.db) as conn:
                    conn.execute("SELECT COUNT(*) FROM t").fetchone()
            gc.collect()
        unclosed = [w for w in caught if "unclosed database" in str(w.message)]
        self.assertEqual(unclosed, [])


class TestEveryStorageModuleUsesIt(unittest.TestCase):
    def test_no_module_returns_a_raw_connection_from_connect(self):
        # A new storage module written against the old shape would reintroduce
        # the leak silently, so this checks the pattern rather than trusting it.
        import pathlib

        offenders = []
        for path in pathlib.Path("agent").rglob("*.py"):
            # storage_utils itself quotes the old pattern in its docstring as
            # the thing it exists to replace.
            if path.name == "storage_utils.py":
                continue
            text = path.read_text()
            if "def _connect" in text and "storage_utils.connect" not in text:
                offenders.append(str(path))
        self.assertEqual(offenders, [], f"these still return a raw connection: {offenders}")


if __name__ == "__main__":
    unittest.main()
