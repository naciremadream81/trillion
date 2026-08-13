"""
draft_email — package a composed email as a reviewable draft (Tier 2).

Draft only: this module has no send capability, and none is planned until
sending is built as its own action — send_email is already blocked by name
in agent/safety/risk.py's HARDLINE_TOOLS, ready for whenever that lands. The
model composes the actual subject/body in its own reasoning (matching Sean's
voice, per AGENT.md); this tool just formats it as a draft for Sean to read
and send himself. Trillion never sends anything on its own.
"""

from __future__ import annotations

from ..safety.risk import LOW
from .base import BaseTool


class DraftEmailTool(BaseTool):
    name = "draft_email"
    description = (
        "Compose an email draft for Sean to review and send himself. Does "
        "NOT send anything — there is no send capability here. Provide the "
        "recipient, subject, and the full body text you've written."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "to": {"type": "string", "description": "Recipient email address or name."},
            "subject": {"type": "string", "description": "Email subject line."},
            "body": {"type": "string", "description": "Full email body text."},
        },
        "required": ["to", "subject", "body"],
    }
    # Tier 6: composes text, sends nothing — cheap and reversible, but not
    # zero-effect, so LOW rather than READ_ONLY.
    risk = LOW

    async def run(self, to: str = "", subject: str = "", body: str = "", **_) -> str:
        to, subject, body = to.strip(), subject.strip(), body.strip()
        if not to or not subject or not body:
            return "[draft_email needs a non-empty to, subject, and body]"
        return f"Draft (not sent):\nTo: {to}\nSubject: {subject}\n\n{body}"
