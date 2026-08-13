"""
trillion — unified CLI entry point.

Installed as a console script (see pyproject.toml's [project.scripts]).
Dispatches to the two existing entry points without duplicating their
logic: bare `trillion` runs main.py's terminal chat, `trillion serve` runs
serve.py's web server. Both keep parsing sys.argv themselves (argparse in
main.py, TRILLION_WEB_PORT in serve.py) — this just decides which one gets
control, stripping the `serve` subcommand token first so it isn't mistaken
for one of main.py's own arguments (e.g. `trillion --provider openai`
still reaches main.py's argparse untouched).
"""

from __future__ import annotations

import asyncio
import sys


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "serve":
        del sys.argv[1]
        import serve

        serve.main()
        return

    import main as trillion_main

    asyncio.run(trillion_main.main())


if __name__ == "__main__":
    main()
