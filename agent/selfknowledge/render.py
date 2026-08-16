"""
Assembles/refreshes context/self/trillion.md.

INITIAL_DOCUMENT is the scaffold used the first time the file is created —
hand-written framing prose plus the marker blocks refresh_text() knows how
to fill in. After that, refresh_file() only ever rewrites what's between the
markers; everything else in the file is free for a human to edit directly
and survives the next refresh untouched.
"""

from __future__ import annotations

import os

from ..config import Settings, get_settings
from ..memory import DEFAULT_MEMORY_PATH
from ..tools.confirm import ConfirmActionTool
from ..tools.memory import ForgetFactTool, RememberFactTool
from ..tools.registry import ToolRegistry, build_registry
from . import generators, parser

DOC_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "context",
    "self",
    "trillion.md",
)

INITIAL_DOCUMENT = """\
# What Trillion knows about itself

This file is generated from agent/tools/registry.py and agent/config.py by
agent/selfknowledge — it answers "what tools do you have, and what turns
them on" from the source, not from memory. Everything between a block's
`START`/`END` markers below is rewritten by `python -m agent.selfknowledge
--refresh`; hand-written notes are safe anywhere outside those markers.

## Capabilities

<!-- AUTO-START: capabilities -->
<!-- AUTO-END: capabilities -->

## Config gating

<!-- AUTO-START: config-gating -->
<!-- AUTO-END: config-gating -->

## Summary (injected into every system prompt)

<!-- SLIM-START -->
<!-- SLIM-END -->
"""


def _with_always_on_tools(registry: ToolRegistry) -> ToolRegistry:
    """
    confirm_action, remember_fact, and forget_fact aren't in build_registry()
    — agent/core.py's Agent.__init__ registers them itself, bound to that
    instance's history/update_memory (see its docstring for why). Both real
    entry points (main.py, serve.py) always construct a gate, so in every
    real deployment these three are unconditionally present. Registered here
    with inert bindings purely so this document's capabilities table isn't
    missing exactly the tools Trillion is most likely to reach for.

    dispatch_to_<slug> tools are deliberately NOT added here: which ones
    exist depends on live spawned_agents rows, not Settings, so no static
    probe run at doc-refresh time can predict them.
    """
    registry.register(ConfirmActionTool(gate=None, history_provider=lambda: []))
    registry.register(RememberFactTool(DEFAULT_MEMORY_PATH, on_change=lambda facts: None))
    registry.register(ForgetFactTool(DEFAULT_MEMORY_PATH, on_change=lambda facts: None))
    return registry


def render_blocks(settings: Settings | None = None) -> dict[str, str]:
    """
    Fresh content for each named block, computed from live settings
    (agent.config.get_settings() when none is given — the SLIM block that
    rides on every turn should describe what's actually deployed, not a
    hypothetical default configuration).

    Gating is probed from this same settings baseline (not a blank
    Settings()), so conditional interactions like resolve_search_provider()'s
    fail-closed explicit-provider selection are reflected correctly, and a
    key that's already set doesn't get listed as "unset config that would
    add more" for a tool the capabilities table already shows as available.
    """
    if settings is None:
        settings = get_settings()
    registry = _with_always_on_tools(build_registry(settings))
    gates = generators.probe_config_gating(settings)
    return {
        "capabilities": generators.generate_capabilities(registry),
        "config-gating": generators.generate_config_gating(gates),
        "slim": generators.generate_slim_summary(registry, gates),
    }


def refresh_text(existing: str, settings: Settings | None = None) -> str:
    """Given current file text, return it with every AUTO/SLIM block's
    content replaced by freshly generated content."""
    blocks = render_blocks(settings)
    text = parser.replace_auto_block(existing, "capabilities", blocks["capabilities"])
    text = parser.replace_auto_block(text, "config-gating", blocks["config-gating"])
    text = parser.replace_slim_block(text, blocks["slim"])
    return text


def refresh_file(path: str = DOC_PATH, settings: Settings | None = None) -> bool:
    """Refresh path in place, creating it from INITIAL_DOCUMENT if missing.
    Returns True if the file's content changed."""
    existed = os.path.isfile(path)
    if existed:
        with open(path, encoding="utf-8") as f:
            existing = f.read()
    else:
        existing = INITIAL_DOCUMENT

    new_text = refresh_text(existing, settings)
    if existed and new_text == existing:
        return False

    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(new_text)
    return True
