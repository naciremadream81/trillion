"""Software Factory: builds whole standalone software projects autonomously.

Forks the Agent Factory's architecture (agent/factory/) at one point: there
is no AWAITING_APPROVAL state. A build that reaches BUILT is terminal and
immediately real — see the plan doc for why that's safe (the autonomy
boundary is drawn at the filesystem, not the action).
"""
