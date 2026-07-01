"""
Cost tracking for Trillion.

Turns the `usage` object that every LLM response already carries into a
live picture of what the agent costs — this month, today, per model, and
how much prompt caching is saving.

Built in tiers:
    pricing.py    — rate table + cost function          (Phase 1)
    storage.py    — SQLite usage table + repo            (Phase 2/3)
    recorder.py   — best-effort capture into the client  (Phase 3)
    aggregate.py  — month-to-date payload for the UI     (Phase 4)

Nothing here may ever break a conversation turn. Recording is best-effort:
if it can't record, it logs and moves on.
"""
