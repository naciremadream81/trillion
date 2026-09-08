"""
CLI for agent/selfknowledge.

    python -m agent.selfknowledge --refresh   regenerate context/self/trillion.md in place
    python -m agent.selfknowledge --check     exit 1 if the file is stale, without writing
    python -m agent.selfknowledge --live      print what THIS deployment actually offers

`--refresh` and `--check` work against the canonical baseline — the dataclass
defaults, no environment read — because the document is committed to a shared
repo and must therefore be identical on every machine. Generating it from
`.env` made it a per-machine artifact: whoever refreshed it last stamped their
own configuration into it and the check then failed for everybody else, CI
included. See render.baseline_settings() for the full reasoning.

`--live` exists because the deployment-specific view is still worth having —
it just isn't the thing that gets committed. It prints; it never writes.

Nothing is lost by the default being deterministic: agent/system_prompt.py's
_load_self_knowledge() computes the summary live from the calling Agent's own
registry, so the committed file is only a fallback for a bare Agent with no
registry at all.
"""

from __future__ import annotations

import argparse
import sys

from dotenv import load_dotenv

from . import drift, render


def main(argv: list[str] | None = None) -> int:
    arg_parser = argparse.ArgumentParser(prog="python -m agent.selfknowledge")
    group = arg_parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--refresh", action="store_true", help="regenerate context/self/trillion.md in place"
    )
    group.add_argument(
        "--check", action="store_true", help="exit 1 if the file is stale, without writing"
    )
    group.add_argument(
        "--live", action="store_true",
        help="print what this deployment offers, from .env (does not write)",
    )
    args = arg_parser.parse_args(argv)

    if args.live:
        # Only this path reads .env. The other two must not: an environment
        # loaded here would leak straight back into the committed file.
        from ..config import get_settings

        load_dotenv()
        blocks = render.render_blocks(get_settings())
        print("What THIS deployment offers (not what is committed):\n")
        print(blocks["capabilities"])
        print()
        print(blocks["config-gating"])
        print()
        print(blocks["slim"])
        return 0

    if args.refresh:
        changed = render.refresh_file()
        print("updated" if changed else "already up to date")
        return 0

    try:
        drift.check_no_drift()
    except drift.DriftError as exc:
        print(f"context/self/trillion.md is stale: {exc}", file=sys.stderr)
        return 1
    print("up to date")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
