"""
convene_board — put a decision to Sean's board of advisors.

The one-phrase command the playbook asks for at the end: "ask the board
whether I should cut this product loose" reaches this tool, and a meeting
happens.

Consequential rather than read_only, and NOT factory_allowed. A meeting is
one router call plus one call per seat plus a chair call — real money, spent
on Sean's behalf — so it goes through the Tier 6 confirmation gate like any
other spend, and a spawned specialist never gets it. A sub-agent that could
convene the board could spend the ceiling in a loop.
"""

from __future__ import annotations

import logging

from ..board.convene import BoardUnavailable, Declined, hold_meeting
from ..safety.risk import CONSEQUENTIAL
from .base import BaseTool

logger = logging.getLogger(__name__)


class ConveneBoardTool(BaseTool):
    name = "convene_board"
    description = (
        "Put a real business decision to Sean's board of advisors. Each seat is "
        "a researched dossier of one advisor's published thinking; they answer "
        "in isolation and you synthesize. Use this when Sean asks what the "
        "board thinks, names an advisor, or faces a genuine judgement call "
        "about his business — pricing, positioning, whether to kill something, "
        "when to hire. Not for factual lookups (a tool answers those), code, "
        "medical, legal, or family questions. Costs real money: one model call "
        "per seat."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": (
                    "The decision, in Sean's own framing. Keep it as he put it — "
                    "the board is more useful on a real question than a tidied one."
                ),
            },
        },
        "required": ["question"],
    }

    # Spends money on Sean's behalf; never handed to a spawned agent.
    risk = CONSEQUENTIAL
    factory_allowed = False
    # The advisors' words come from dossiers researched off the open web, and
    # the chair's synthesis quotes them. Untrusted, like any other web-derived
    # content — see agent/safety/untrusted.py.
    trusted_output = False

    def __init__(self, ask_model, *, brief_provider=None, repo=None, ceiling_usd: float = 0.75):
        """
        `ask_model(system, prompt, max_tokens) -> str` does every model call.
        `brief_provider()` returns the live business figures as text — the
        chair reads them and the seats get a short version. Passed in rather
        than imported so this tool doesn't decide what "the numbers" are.
        """
        self._ask_model = ask_model
        self._brief_provider = brief_provider
        self._repo = repo
        self._ceiling_usd = ceiling_usd

    def _brief(self) -> str:
        if self._brief_provider is None:
            return ""
        try:
            return self._brief_provider() or ""
        except Exception:  # noqa: BLE001 — a blind board still beats no board
            logger.warning("board: could not read the live brief; the chair is flying blind")
            return ""

    async def run(self, question: str = "") -> str:
        question = (question or "").strip()
        if not question:
            return "I need the actual question before I can convene anyone."

        try:
            meeting = await hold_meeting(
                question,
                self._ask_model,
                brief=self._brief(),
                repo=self._repo,
                ceiling_usd=self._ceiling_usd,
            )
        except Declined as e:
            return f"The board declined this one: {e}"
        except BoardUnavailable as e:
            return f"There's no board to convene — {e}"
        except Exception as e:  # noqa: BLE001
            logger.exception("board: the meeting failed")
            return f"The meeting failed ({type(e).__name__}). Nothing was decided."

        lines = [
            meeting["spoken"],
            "",
            f"Seats: {', '.join(s['name'] for s in meeting['seats'])}",
        ]
        if meeting["abstained"]:
            # Rendered differently from a failure on purpose: an advisor with
            # no doctrine on something is a real answer, not an error.
            lines.append(f"Abstained (no doctrine on this): {', '.join(meeting['abstained'])}")
        if meeting["unsourced"]:
            lines.append(f"Spoke without citing doctrine: {', '.join(meeting['unsourced'])}")
        if meeting["failed"]:
            lines.append(f"Did not respond: {', '.join(meeting['failed'])}")
        if meeting["recommendation"]:
            lines += ["", f"Recommendation: {meeting['recommendation']}"]
        if meeting["detail"]:
            lines += ["", meeting["detail"]]
        return "\n".join(lines)
