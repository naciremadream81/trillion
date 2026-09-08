"""
The standing review — playbook/the-board.md Tier 7.

Everything else about the board waits for Sean to ask. The most valuable
thing a real board does is show up when he *didn't* call the meeting.

Once a month, convene with no question on the table but the business itself.
The prompt lives in agent/board/convene.py (STANDING_REVIEW_QUESTION) and its
last line — "do not ask what they want to discuss, this is your agenda, not
theirs" — is load-bearing: without it the likeliest failure is a board that
politely hands the question back, which defeats the whole routine.

WHICH PROCESS OWNS THIS. Trillion runs in two places — main.py's terminal
chat and serve.py's always-on web server. A monthly job needs the surface
that is actually awake, so this Check is registered by serve.py's scheduler
only. Registering it in both would double-convene (two full fan-outs, twice
the money) on any day both happen to be running.

It is written as a Check so quiet hours, the dedup cursor, notice storage and
schedule persistence all come from the existing scheduler rather than being
rebuilt. Severity is deliberately not critical: a monthly agenda item is
never worth a 3am interruption, and quiet hours holding it until morning is
the correct behaviour.
"""

from __future__ import annotations

import logging
import time

from ...board.convene import STANDING_REVIEW_QUESTION, BoardUnavailable, Declined, hold_meeting
from .base import Notice

logger = logging.getLogger(__name__)

MONTH_SECONDS = 30 * 24 * 3600


class BoardStandingReviewCheck:
    """Convenes the board once a month with no question but the business."""

    name = "board_standing_review"
    # The scheduler decides when this is due; the cursor below is a second,
    # independent guard so a reset schedule can't buy two fan-outs in a week.
    cadence_seconds = MONTH_SECONDS

    def __init__(self, ask_model, *, brief_provider=None, repo=None,
                 ceiling_usd: float = 0.75, clock=time.time):
        self._ask_model = ask_model
        self._brief_provider = brief_provider
        self._repo = repo
        self._ceiling_usd = ceiling_usd
        self._clock = clock

    def _brief(self) -> str:
        if self._brief_provider is None:
            return ""
        try:
            return self._brief_provider() or ""
        except Exception:  # noqa: BLE001
            logger.warning("board review: could not read the live brief")
            return ""

    async def run(self, cursor: dict) -> tuple[list[Notice], dict]:
        now = self._clock()
        last = cursor.get("last_review_at")
        if isinstance(last, (int, float)) and (now - last) < MONTH_SECONDS * 0.9:
            # Belt and braces. A meeting is four paid calls; a schedule that
            # resets (a rename, a wiped state row, a redeploy) must not be
            # able to spend them twice in a month.
            return [], cursor

        brief = self._brief()
        if not brief:
            # The whole point of the standing review is that the board reads
            # the numbers. Without them it is four advisors free-associating,
            # which is worth less than nothing at four calls a month.
            logger.info("board review: skipped — no live business brief available")
            return [], cursor

        try:
            meeting = await hold_meeting(
                STANDING_REVIEW_QUESTION,
                self._ask_model,
                brief=brief,
                repo=self._repo,
                ceiling_usd=self._ceiling_usd,
                unprompted=True,
            )
        except (BoardUnavailable, Declined) as e:
            logger.info("board review: no meeting held (%s)", e)
            return [], {**cursor, "last_review_at": now}
        except Exception:  # noqa: BLE001 — a failed review must not kill the heartbeat
            logger.exception("board review: the meeting failed")
            return [], cursor

        # Marked as unprompted so Sean can tell it apart from an answer he
        # asked for — a board that shows up uninvited reads very differently
        # from one that replied.
        message = "The board met without being asked. " + (meeting["spoken"] or "")
        return [Notice(severity="info", message=message.strip())], {
            **cursor,
            "last_review_at": now,
            "last_meeting_id": meeting["id"],
        }
