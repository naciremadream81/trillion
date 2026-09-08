"""
Running laptop-only agents from the cloud — playbook/cloud-to-local.md.

A durable SQLite queue both processes reach (storage.py), a worker that
drains it on startup and runs the REAL local agent to completion (worker.py),
and a cloud-side proxy tool that enqueues instead of executing (proxy.py).

The no-double-fire guarantee is structural: the proxy is registered only in a
process where the real tool is absent, so the two can never both run.
"""
