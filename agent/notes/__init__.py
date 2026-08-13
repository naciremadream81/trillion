"""
Notes search (Tier 2 — search_notes).

The Aires Ai Brain vault lives behind an rclone FUSE mount that can report
healthy (systemd active, mounted) while every read fails — this happened
live during development. Two design decisions follow directly from that:

  index.py   a local SQLite FTS5 index, built/refreshed while the mount is
             reachable, so queries never depend on the mount being alive.
             health_check() does a bounded real read, never a systemd or
             mount-table check.
"""
