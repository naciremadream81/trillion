"""
The per-agent runner registry — playbook/cloud-to-local.md Tier 2.

Each runner invokes the REAL local agent to completion and returns a summary
dict for the task's result row. "The real agent, not a reimplementation" is
the whole point: forking the logic into the worker means the remote path and
the local path drift, and the remote one is the one nobody is watching.

RUN TO COMPLETION. Every runner here must await the actual end of the run,
not a fire-and-forget wrapper that returns a "started" ack. Trillion has both
shapes:

  generate_mockup   GenerateMockupTool.run() awaits its own subprocess and
                    returns the finished result — awaiting it IS completion.
  start_build       hands work to a background task and returns immediately.
                    A runner for it would have to await the pipeline
                    coroutine, NOT start_build(), or the task would complete
                    before the build had done anything and the ping would be
                    a lie. Not registered here yet, and that is why.

Dependencies arrive in `deps`, built once by the local process that already
wires them up. A runner whose dependency is missing returns an error summary
rather than raising — the cloud asked for something this machine cannot do,
which is an answer, not an outage.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


async def head_of_design_runner(args: dict, deps: dict) -> dict:
    """
    Compose one screen with the real design agent.

    `deps["design_tool"]` is the actual GenerateMockupTool the local registry
    built — same instance, same budget ledger, same settings. Passing the
    tool rather than reconstructing it is what keeps the remote path honest:
    a rebuilt tool with its own budget object would let a remote dispatch
    spend past a ceiling the local one thought it was enforcing.
    """
    tool = deps.get("design_tool")
    if tool is None:
        return {
            "status": "error",
            "error": "the design agent is not configured on this machine "
                     "(TRILLION_DESIGN_AGENT off, or the claude CLI is not on PATH)",
        }

    try:
        # Awaited to its end — this tool composes synchronously inside run(),
        # so this really is completion rather than a start acknowledgement.
        result = await tool.run(**args)
    except Exception as e:  # noqa: BLE001
        logger.exception("remote runner: the design agent failed")
        return {"status": "error", "error": f"{type(e).__name__}: {e}"}

    text = str(result or "")
    # The tool reports its own refusals and failures in the RESULT STRING and
    # returns normally. Treating only exceptions as failure is exactly how a
    # failure gets reported to Sean as a success, so the terminal-failure
    # shapes are mapped here too.
    lowered = text.lower()
    if text.startswith("[generate_mockup") or "refused" in lowered[:80]:
        return {"status": "failed", "error": text[:500], "summary": text[:2000]}

    return {"status": "ok", "summary": text[:2000], "screen": args.get("screen_name", "")}


def build_runners(deps: dict | None = None) -> dict:
    """The runner map for RemoteWorker. One entry per remotely-dispatchable
    agent; adding another is one line plus its entry in proxy.PROXIED_TOOLS."""
    return {"head-of-design": head_of_design_runner}


def build_deps(settings, registry) -> dict:
    """
    The bundle the runners need, taken from what the local process ALREADY
    built rather than constructed fresh.

    `registry.get("generate_mockup")` returns None on a machine where the
    design agent isn't configured, and the runner degrades to an error
    summary — which is the correct behaviour, not a bug to guard against
    here.
    """
    return {
        "settings": settings,
        "design_tool": registry.get("generate_mockup") if registry is not None else None,
    }
