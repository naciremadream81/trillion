"""
The cloud proxy tool — playbook/cloud-to-local.md Tier 3.

**A doppelgänger, not a new tool.** The proxy carries the SAME name and the
SAME input schema as the real local tool. The cloud model already knows how
to call `generate_mockup`; if the proxy matches, zero prompts change and the
model cannot tell it is talking to a proxy. Drift the name or the schema and
you will be editing system prompts forever.

**No double-fire, by construction.** register_proxies() only registers a
proxy for a tool that is NOT already in the registry. On the machine with the
real capability the real tool is present, so the proxy is never built; on the
machine without it, only the proxy exists. The guarantee is which process
holds which tool — not a flag anyone can get wrong.

**Honesty over optimism.** If the worker is offline, the acknowledgement says
queued, not started. A cheerful "done!" that hides a never-started job is
worse than a blunt "it'll run when your computer is online" — and the work is
enqueued either way, so presence only ever changes the sentence.
"""

from __future__ import annotations

import json
import logging

from ..safety.risk import CONSEQUENTIAL
from ..tools.base import BaseTool
from .storage import KIND_REMOTE_DISPATCH, RemoteQueue
from .worker import DEFAULT_WORKER_ROLE

logger = logging.getLogger(__name__)

# Which local tools get a cloud proxy, and which runner name the worker
# routes them to. Explicit rather than automatic: a tool becomes remotely
# dispatchable because someone decided it should be, not because it happened
# to be missing from a registry.
PROXIED_TOOLS = {
    # The design agent needs the `claude` CLI and the project filesystem, so
    # it can only ever run on the machine that has them.
    "generate_mockup": "head-of-design",
}


class RemoteDispatchProxy(BaseTool):
    """Enqueues instead of executing. Same name and schema as the real tool."""

    factory_allowed = False   # a spawned agent must not queue work across machines
    risk = CONSEQUENTIAL
    trusted_output = True     # this tool's output is entirely its own words

    def __init__(self, agent: str, definition: dict, queue: RemoteQueue,
                 worker_role: str = DEFAULT_WORKER_ROLE):
        self._agent = agent
        self._definition = dict(definition)
        self._queue = queue
        self._worker_role = worker_role
        # Mirrored exactly — see the module docstring.
        self.name = definition["name"]
        self.description = definition.get("description", "")
        self.input_schema = definition.get("input_schema", {})

    def definition(self) -> dict:
        return self._definition

    async def run(self, **kwargs) -> str:
        # Presence is read first but is never allowed to block the enqueue:
        # is_online() swallows its own failures and answers False, so the
        # worst case is pessimistic wording on work that queued fine.
        try:
            online = self._queue.is_online(self._worker_role)
        except Exception:  # noqa: BLE001
            online = False

        try:
            task_id = self._queue.enqueue(
                self._worker_role, KIND_REMOTE_DISPATCH,
                {"agent": self._agent, "args": kwargs},
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("remote proxy: could not enqueue for %s", self._agent)
            return json.dumps({
                "spoken": "I couldn't queue that — the task queue is unreachable, "
                          "so nothing is going to run.",
                "error": f"{type(e).__name__}: {e}",
            })

        spoken = (
            "On it — starting on your computer."
            if online else
            "Queued — it'll start when your computer's online."
        )
        return json.dumps({"spoken": spoken, "task_id": task_id, "queued": True})


def register_proxies(registry, queue: RemoteQueue, worker_role: str = DEFAULT_WORKER_ROLE,
                     proxied=None) -> list[str]:
    """
    Register a proxy for every proxied tool the registry does NOT already
    have. Returns the names registered.

    The absence check is the no-double-fire guarantee. Call this AFTER
    build_registry() has run: on the machine with the real capability every
    name is already taken and this registers nothing.

    A proxy needs the real tool's definition to mirror it, and the definition
    lives with the real tool's class — so it is imported here rather than
    hand-copied. A hand-copied schema drifts, and the drift shows up as the
    cloud model calling the tool with arguments the local one rejects.
    """
    proxied = PROXIED_TOOLS if proxied is None else proxied
    registered = []
    existing = set(registry.names())
    for tool_name, agent in proxied.items():
        if tool_name in existing:
            continue  # the real thing is here; never shadow it
        definition = _definition_for(tool_name)
        if definition is None:
            logger.warning("remote proxy: no definition available for %s; skipping", tool_name)
            continue
        registry.register(RemoteDispatchProxy(agent, definition, queue, worker_role))
        registered.append(tool_name)
    return registered


def _definition_for(tool_name: str) -> dict | None:
    """The real tool's name/description/input_schema, read off its class."""
    if tool_name == "generate_mockup":
        try:
            from ..tools.design import GenerateMockupTool

            return {
                "name": GenerateMockupTool.name,
                "description": GenerateMockupTool.description,
                "input_schema": GenerateMockupTool.input_schema,
            }
        except Exception:  # noqa: BLE001
            return None
    return None
