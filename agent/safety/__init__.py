"""
Tier 6 — the safety rails.

AGENT.md and the system prompt both promise Sean two things: no consequential
action without his explicit per-action confirmation, and outside content
treated as data rather than instructions. Before this package existed, both
were prose the model was asked to honor. This is where they become code the
model can't route around.

  risk.py      what counts as consequential, and the list that can't be
               configured away
  approval.py  the Gate — intercepts a tool call, parks it, waits for Sean
  storage.py   SafetyRepo — pending actions and the append-only audit log
"""
