"""
CLI for agent/selfknowledge.

    python -m agent.selfknowledge --refresh   regenerate context/self/trillion.md in place
    python -m agent.selfknowledge --check     exit 1 if the file is stale, without writing

Loads .env the same way main.py/serve.py do, before anything reads
agent.config.get_settings(): without this, a deployment configured entirely
through .env (as opposed to real exported env vars) would refresh/check
against a blank Settings(), silently disagreeing with what's actually
running.
"""

from __future__ import annotations

import argparse
import sys

from dotenv import load_dotenv

from . import drift, render

load_dotenv()


def main(argv: list[str] | None = None) -> int:
    arg_parser = argparse.ArgumentParser(prog="python -m agent.selfknowledge")
    group = arg_parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--refresh", action="store_true", help="regenerate context/self/trillion.md in place"
    )
    group.add_argument(
        "--check", action="store_true", help="exit 1 if the file is stale, without writing"
    )
    args = arg_parser.parse_args(argv)

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
