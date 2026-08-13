"""
Risk tiers, and the decision that turns a tier into a confirmation gate.

Three independent inputs decide whether a tool call gets parked for Sean:

  risk      what the tool declares about itself (BaseTool.risk)
  declared  an explicit per-tool override (BaseTool.requires_confirmation)
  mode      what Sean configured (TRILLION_CONFIRMATION_MODE)

...plus one input that none of the three can override: HARDLINE_TOOLS. A
hardline tool is always gated, in every mode, however it declares itself.
That's the point of it — a mode named `off` should be able to make Trillion
less chatty about reading a file, and should not be able to make it send mail
on its own initiative.

**The default risk tier is CONSEQUENTIAL, deliberately.** A tool that says
nothing about itself is treated as dangerous, so the failure mode for a tool
author who forgets to declare a tier is an unnecessary confirmation prompt,
not a silent hole in the gate. Every tool that ships today declares its tier
explicitly; see `agent/tools/`.
"""

from __future__ import annotations

# ── Risk tiers ───────────────────────────────────────────────────────────────
# READ_ONLY      observes the world and changes nothing: a SELECT, a web
#                fetch, reading a file. Never gated, and still allowed while
#                Trillion is paused.
# LOW            has an effect, but a cheap and reversible one — costs tokens,
#                writes a scratch file. Gated only in `manual` mode.
# CONSEQUENTIAL  the AGENT.md list: sends a message, spends money, deletes
#                data, changes settings. Gated in `smart` and `manual`.
# HARDLINE       consequential AND irreversible enough that Sean should never
#                be able to configure the prompt away by accident. Gated in
#                every mode, including `off`.
READ_ONLY = "read_only"
LOW = "low"
CONSEQUENTIAL = "consequential"
HARDLINE = "hardline"

RISK_TIERS = (READ_ONLY, LOW, CONSEQUENTIAL, HARDLINE)

# ── Confirmation modes ───────────────────────────────────────────────────────
OFF = "off"
SMART = "smart"
MANUAL = "manual"

MODES = (OFF, SMART, MANUAL)
DEFAULT_MODE = SMART

# Tools that are gated no matter what the tool class or the config says.
#
# This is intentionally a list of NAMES rather than a rule, and intentionally
# includes tools that don't exist yet: the whole value of a blocklist is that
# it's already in place when the tool lands. If you add a tool whose name is
# in here, it is gated on day one whether or not you remembered to set
# `risk = HARDLINE` on it.
#
# Do not add a mechanism for removing entries at runtime. "Immutable" is the
# feature.
HARDLINE_TOOLS: frozenset[str] = frozenset(
    {
        # Sends something to another human — unsendable once sent.
        "send_email",
        "send_message",
        "send_sms",
        "post_to_social",
        # Destroys data.
        "delete_file",
        "delete_note",
        "forget_fact",
        # Spends money outside the model API.
        "make_payment",
        "transfer_funds",
        "place_order",
        # Runs code Trillion wrote, on Sean's machine.
        "run_shell",
        "run_command",
        "run_project_tests",
        "claude_code",
        # Changes how Trillion itself behaves.
        "update_settings",
        "rotate_credentials",
    }
)

# The confirmation tool itself can never be gated: gating it would make every
# pending action permanently unapprovable.
NEVER_GATED: frozenset[str] = frozenset({"confirm_action"})


def normalize_mode(mode: str | None) -> str:
    """
    Coerce a configured mode string to one of MODES.

    An unrecognized value falls back to the documented default (`smart`)
    rather than raising: a typo in .env should not stop Trillion from
    starting, and it should not silently become `off` either.
    """
    candidate = (mode or "").strip().lower()
    return candidate if candidate in MODES else DEFAULT_MODE


def normalize_risk(risk: str | None) -> str:
    """
    Coerce a tool's declared tier to one of RISK_TIERS.

    Unrecognized values become CONSEQUENTIAL, matching the fail-closed
    default described in this module's docstring.
    """
    candidate = (risk or "").strip().lower()
    return candidate if candidate in RISK_TIERS else CONSEQUENTIAL


def is_hardline(tool_name: str, risk: str | None = None) -> bool:
    """True if this tool is gated in every mode, whatever else it declares."""
    return tool_name in HARDLINE_TOOLS or normalize_risk(risk) == HARDLINE


def needs_confirmation(
    *,
    tool_name: str,
    risk: str | None,
    declared: bool | None,
    mode: str,
) -> bool:
    """
    Should this tool call be parked for Sean's explicit yes?

    Precedence, highest first:

      1. NEVER_GATED — gating confirm_action would deadlock the gate.
      2. HARDLINE — always gated, ignores `declared` and `mode` both.
      3. `declared` (BaseTool.requires_confirmation), when not None. True
         forces a gate for a tool whose tier doesn't capture why it's risky;
         False is the tool asserting it has no side effects worth confirming.
      4. The mode/tier table:
           off     nothing beyond hardline
           smart   CONSEQUENTIAL and above
           manual  anything that isn't READ_ONLY
    """
    if tool_name in NEVER_GATED:
        return False
    if is_hardline(tool_name, risk):
        return True
    if declared is not None:
        return bool(declared)

    tier = normalize_risk(risk)
    mode = normalize_mode(mode)
    if mode == OFF:
        return False
    if mode == MANUAL:
        return tier != READ_ONLY
    return tier == CONSEQUENTIAL


def blocked_while_paused(tool_name: str, risk: str | None) -> bool:
    """
    Should the kill switch refuse this call outright?

    Pausing Trillion stops it from *acting*, not from talking or looking
    things up. A kill switch that also mutes the conversation is one Sean
    won't reach for, which makes it worse than no kill switch at all — so
    READ_ONLY tools keep working while paused, and everything else doesn't.

    confirm_action is READ_ONLY and so passes here; approving an action while
    paused is refused by Gate.confirm() instead, which is the only place that
    knows a confirmation would go on to *execute* something.
    """
    return normalize_risk(risk) != READ_ONLY
