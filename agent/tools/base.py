"""
BaseTool — the contract every tool implements.

Keep `description` written for the model, not the compiler: it's how the
model decides whether to call the tool. `input_schema` is JSON Schema.
`definition()` emits the Anthropic tool-schema shape (name / description /
input_schema), which is what the Claude provider passes straight through.
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class BaseTool(ABC):
    name: str = ""
    description: str = ""
    input_schema: dict = {}

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
