"""
Revenue celebration — playbook/money-celebration.md.

Detection (stripe_client.py) → deduplicated records with a separate
"celebrated" fact (storage.py) → a heartbeat poll (agent/heartbeat/checks/
revenue.py) → catch-up-on-connect endpoints in serve.py → the overlay in
index.html.

The catch-up half is the point. Build only the live push and every payment
that lands with the tab closed is silently never celebrated, which feels
random and is the hardest version of this to debug.
"""
