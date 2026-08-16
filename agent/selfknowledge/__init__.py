"""
Self-knowledge: Trillion's generated understanding of its own capabilities.

context/self/trillion.md answers "what tools do you have, and what turns
them on" from the model's own source rather than a hand-maintained doc that
drifts. See generators.py for how each section is derived, render.py for how
it's assembled, and drift.py for the check that catches a stale file.
"""
