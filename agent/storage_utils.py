"""
Shared SQLite connection handling.

Exists because of a leak found in production, not as tidying. Every storage
module here was written as:

    def _connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    with self._connect() as conn:
        ...

and **that `with` block does not close the connection.** Python's
sqlite3.Connection context manager commits or rolls back the transaction and
leaves the connection open — a genuinely surprising API, and the source of
the `ResourceWarning: unclosed database` lines that had been showing up in
test output all along.

The cost, measured on the real deployment: `trillion-orb.service` reached
716MB RSS over three days against 78MB for a fresh process, holding 41 open
database file descriptors against 9. Profiling per endpoint,
`GET /api/security/status` leaked ~10MB per 60 calls and
`GET /api/heartbeat/notices` ~5MB per 60 — both of which the browser polls
continuously, so a dashboard left open all day is the whole problem. The
memory pressure pushed the process into swap, and pages faulting back in on
every Piper synthesis made voice 2.7x slower.

`connect()` below is a context manager, which is what lets every one of the
71 existing `with self._connect() as conn:` call sites keep working
untouched: the generator yields the connection, so `as conn` still binds the
connection, `with conn` inside still commits, and the `finally` adds the
close that was missing.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager


@contextmanager
def connect(db_path: str, row_factory=sqlite3.Row):
    """
    Open a SQLite connection, commit-or-rollback on exit, and CLOSE it.

    The close is the entire point — see the module docstring. Written as a
    generator rather than a subclass so that call sites written against the
    old raw-connection shape need no change at all.
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = row_factory
    try:
        with conn:          # commit on success, rollback on exception
            yield conn
    finally:
        conn.close()        # the half sqlite3's own context manager omits
