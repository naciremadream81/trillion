"""
Trillion — Tier 1 entry point.

Run this to start a text conversation with Trillion.
The typed interface stays alive through every future tier — it's your
debugging path and fallback when voice misbehaves.

Usage:
    python main.py
    python main.py --provider openai
    python main.py --provider ollama
    python main.py --reset          # clear history and start fresh

Special commands (type these during a session):
    /reset      — clear conversation history
    /history    — print the current session history
    /model      — show which model is active
    /quit       — exit (or just Ctrl+C)
"""

import argparse
import asyncio
import os
import sys

from dotenv import load_dotenv
from rich.console import Console
from rich.text import Text

from agent.core import Agent
from agent.providers import get_provider

load_dotenv()

console = Console()

BANNER = """
╔══════════════════════════════════════════╗
║  Trillion — your AI co-founder           ║
║  Type a message. /quit or Ctrl+C to exit ║
╚══════════════════════════════════════════╝
"""

SLASH_COMMANDS = {
    "/reset":   "Clear conversation history",
    "/history": "Print session history",
    "/model":   "Show active model",
    "/quit":    "Exit",
    "/help":    "Show this list",
}


def print_banner(provider_name: str, model_name: str) -> None:
    console.print(BANNER, style="bold cyan")
    console.print(
        f"  Provider: [bold]{provider_name}[/bold]  |  "
        f"Model: [bold]{model_name}[/bold]\n",
        style="dim",
    )


def handle_slash(command: str, agent: Agent, provider_name: str) -> bool:
    """
    Handle a slash command. Returns True if we should continue the loop,
    False if we should exit.
    """
    cmd = command.strip().lower()

    if cmd == "/quit":
        console.print("\n[dim]Trillion signing off.[/dim]")
        return False

    elif cmd == "/reset":
        agent.reset()
        console.print("[dim]History cleared. Starting fresh.[/dim]\n")

    elif cmd == "/history":
        if not agent.history:
            console.print("[dim]No history yet.[/dim]\n")
        else:
            console.print("\n[dim]── Session history ──[/dim]")
            for i, msg in enumerate(agent.history):
                role = msg["role"].upper()
                content = msg.get("content", "")
                console.print(f"[bold]{role}[/bold]: {content[:200]}")
            console.print("[dim]────────────────────[/dim]\n")

    elif cmd == "/model":
        console.print(
            f"[dim]Provider: {provider_name}  |  "
            f"Model: {agent.provider.model_name}[/dim]\n"
        )

    elif cmd == "/help":
        console.print("\n[dim]Slash commands:[/dim]")
        for c, desc in SLASH_COMMANDS.items():
            console.print(f"  [bold]{c}[/bold]  {desc}")
        console.print()

    else:
        console.print(f"[yellow]Unknown command: {command}. Type /help.[/yellow]\n")

    return True


async def chat_loop(agent: Agent, provider_name: str) -> None:
    """The main REPL. Runs until the user quits."""
    print_banner(provider_name, agent.provider.model_name)

    while True:
        try:
            # ── Get input ─────────────────────────────────────────────────
            try:
                user_input = input("You: ").strip()
            except EOFError:
                # Piped input ended
                break

            if not user_input:
                continue

            # ── Slash commands ────────────────────────────────────────────
            if user_input.startswith("/"):
                should_continue = handle_slash(user_input, agent, provider_name)
                if not should_continue:
                    break
                continue

            # ── Agent turn ────────────────────────────────────────────────
            # Only print the "Trillion:" prefix once a reply actually starts, so
            # a sign-off (Tier 5) produces clean silence, not an empty prompt.
            got_reply = False
            async for chunk in agent.turn(user_input):
                if not got_reply:
                    print("Trillion: ", end="", flush=True)
                    got_reply = True
                print(chunk, end="", flush=True)

            if got_reply:
                print()  # newline after streamed reply
                print()  # breathing room
            else:
                # A sign-off — Trillion lets the conversation rest.
                print()

        except KeyboardInterrupt:
            console.print("\n\n[dim]Trillion signing off.[/dim]")
            break

        except Exception as e:  # noqa: BLE001
            console.print(f"\n[red]Something went wrong: {e}[/red]")
            console.print("[dim]Your history is intact — try again.[/dim]\n")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Trillion — your AI co-founder",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--provider",
        default=os.getenv("TRILLION_PROVIDER", "claude"),
        choices=["claude", "openai", "ollama"],
        help="Model provider (default: $TRILLION_PROVIDER or claude)",
    )
    return parser.parse_args()


async def main() -> None:
    args = parse_args()

    try:
        provider = get_provider(args.provider)
    except EnvironmentError as e:
        console.print(f"\n[bold red]Setup error:[/bold red] {e}")
        console.print(
            "\n[dim]Copy .env.example to .env and fill in your API key, then try again.[/dim]"
        )
        sys.exit(1)
    except ValueError as e:
        console.print(f"\n[bold red]Config error:[/bold red] {e}")
        sys.exit(1)

    # Wire cost tracking (best-effort: never block startup on it).
    try:
        from agent.cost.recorder import set_usage_repo
        from agent.cost.storage import UsageRepo

        set_usage_repo(UsageRepo())
    except Exception as e:  # noqa: BLE001
        console.print(f"[dim]Cost tracking unavailable ({e}); continuing.[/dim]")

    # Build the tool registry from settings (tools self-skip when unconfigured).
    from agent.config import get_settings
    from agent.tools.registry import build_registry

    registry = build_registry(get_settings())
    if registry.names():
        console.print(f"[dim]Registered {len(registry.names())} tools: {', '.join(registry.names())}[/dim]")

    agent = Agent(provider=provider, tool_registry=registry)
    await chat_loop(agent, provider_name=args.provider)


if __name__ == "__main__":
    asyncio.run(main())
