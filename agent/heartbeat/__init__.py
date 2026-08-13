"""
Tier 5 — the heartbeat.

Trillion can reach out first, but stays quiet by default: most ticks produce
nothing, and a true interruption is reserved for things that genuinely
warrant Sean's attention. Everything else accumulates in a calm log Sean can
check on his own time.

  storage.py     HeartbeatRepo — notices (quiet-hours-aware), per-check
                 scheduling, and per-check dedup state
  checks/        the Check abstraction and the Code Sentinel checks that
                 implement it
  scheduler.py   HeartbeatScheduler — the tick loop, mirrors
                 agent/factory/software/scheduler.py::AutonomousScheduler
  github_client.py  thin aiohttp wrapper the Code Sentinel checks poll
"""
