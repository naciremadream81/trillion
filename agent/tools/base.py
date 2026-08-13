"""
BaseTool — the contract every tool implements.

Keep `description` written for the model, not the compiler: it's how the
model decides whether to call the tool. `input_schema` is JSON Schema.
`definition()` emits the Anthropic tool-schema shape (name / description /
input_schema), which is what the Claude provider passes straight through.

`factory_allowed` gates whether the Agent Factory (agent/factory/) may hand
this tool to a spawned specialist agent. Defaults to True; set False on any
tool a spawned agent shouldn't get by default (e.g. one that sends messages,
spends money, or deletes data) once such tools exist.

`risk` and `requires_confirmation` feed Tier 6's confirmation gate — see
agent/safety/risk.py for the tiers and how the two combine. **`risk` defaults
to CONSEQUENTIAL on purpose:** a tool author who forgets to declare a tier
gets an unnecessary confirmation prompt, not a silent hole in the gate.
Declare it explicitly on every tool.

`trusted_output` tells ToolRegistry.run() (agent/tools/registry.py) whether
this tool's result needs the untrusted-content scrub (agent/safety/untrusted.py)
before it reaches the model. **Defaults to False on purpose**, same reasoning
as `risk`: a tool author who forgets to declare it gets unnecessary scrubbing,
not a silent prompt-injection channel. Set True only for output the tool
itself fully controls (no external/user-supplied content mixed in).
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from ..safety.risk import CONSEQUENTIAL


class BaseTool(ABC):
    name: str = ""
    description: str = ""
    input_schema: dict = {}
    factory_allowed: bool = True

    # Tier 6. `risk` is one of agent/safety/risk.py's RISK_TIERS.
    # `requires_confirmation` overrides the tier→mode table when set: True
    # forces a gate for a tool whose tier doesn't capture why it's risky,
    # False asserts there's nothing here worth confirming. None means "use
    # the tier", which is what almost every tool should do.
    risk: str = CONSEQUENTIAL
    requires_confirmation: bool | None = None

    # Tier 6 (P2). See module docstring — defaults to untrusted (False).
    trusted_output: bool = False

    def definition(self) -> dict:
        """Anthropic tool-schema shape."""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }

    @abstractmethod
    async def run(self, **kwargs) -> str:
        """Execute the tool and return a string result for the model."""
        ...
