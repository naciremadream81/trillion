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

Agent Factory commands (spawn new specialist sub-agents, gated by approval):
    /spawn <role description>   — research and draft a new specialist agent
    /pending                    — list spawn tasks awaiting your approval
    /approve <id>                — approve a drafted spec, minting the agent
    /reject <id> <feedback>      — reject with feedback (up to 3 revisions)

Software Factory commands (build whole standalone projects, fully autonomous):
    /build <description>   — build a whole software project in the background
    /builds                — list recent builds and their status
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
    "/spawn":   "Research and draft a new specialist agent: /spawn <role description>",
    "/pending": "List spawn tasks awaiting approval",
    "/approve": "Approve a drafted spec: /approve <id>",
    "/reject":  "Reject with feedback: /reject <id> <feedback>",
    "/build":   "Build a whole software project: /build <description>",
    "/builds":  "List recent Software Factory builds and their status",
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


def handle_slash(
    command: str,
    agent: Agent,
    provider_name: str,
    factory: "FactoryContext | None" = None,
    software_factory: "SoftwareFactoryContext | None" = None,
) -> bool:
    """
    Handle a slash command. Returns True if we should continue the loop,
    False if we should exit.

    `factory` carries the Agent Factory's dependencies (repo, provider,
    registry, background_tasks) — None when the factory isn't wired up
    (e.g. tests exercising the non-factory commands only). `software_factory`
    is the analogous bundle for the Software Factory's /build and /builds.
    """
    parts = command.strip().split(maxsplit=1)
    cmd = parts[0].lower()
    rest = parts[1] if len(parts) > 1 else ""

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

    elif cmd in ("/spawn", "/pending", "/approve", "/reject"):
        if factory is None:
            console.print("[yellow]Agent Factory isn't available in this session.[/yellow]\n")
        else:
            _handle_factory_command(cmd, rest, factory)

    elif cmd in ("/build", "/builds"):
        if software_factory is None:
            console.print("[yellow]Software Factory isn't available in this session.[/yellow]\n")
        else:
            _handle_software_factory_command(cmd, rest, software_factory)

    else:
        console.print(f"[yellow]Unknown command: {command}. Type /help.[/yellow]\n")

    return True


class FactoryContext:
    """Dependencies the Agent Factory slash commands need. Built once in
    main() and threaded through to handle_slash()."""

    def __init__(self, repo, provider, registry, background_tasks: set, watcher=None) -> None:
        self.repo = repo
        self.provider = provider
        self.registry = registry
        self.background_tasks = background_tasks
        self.watcher = watcher


def _handle_factory_command(cmd: str, rest: str, factory: "FactoryContext") -> None:
    from agent.factory.pipeline import SpawnCapExceeded, resume_spawn, start_spawn
    from agent.factory.storage import AWAITING_APPROVAL

    if cmd == "/spawn":
        role = rest.strip()
        if not role:
            console.print("[yellow]Usage: /spawn <role description>[/yellow]\n")
            return
        try:
            task_id = start_spawn(
                role,
                factory.repo,
                factory.provider,
                factory.registry,
                factory.registry.names(),
                factory.registry.factory_allowed_names(),
                background_tasks=factory.background_tasks,
            )
        except SpawnCapExceeded as e:
            console.print(f"[yellow]{e}[/yellow]\n")
        else:
            console.print(
                f"[dim]Spawn task #{task_id} started — researching in the background. "
                f"Check /pending shortly.[/dim]\n"
            )

    elif cmd == "/pending":
        tasks = factory.repo.list_pending_approval()
        if not tasks:
            console.print("[dim]No spawn tasks awaiting approval.[/dim]\n")
        else:
            console.print("\n[dim]── Awaiting approval ──[/dim]")
            for t in tasks:
                console.print(
                    f"  [bold]#{t['id']}[/bold]  slug=[bold]{t['slug']}[/bold]  "
                    f"tools={t['tool_allowlist']}  revisions={t['revision_count']}"
                )
                console.print(f"    role: {t['role_description'][:150]}")
                console.print(f"    prompt: {t['system_prompt'][:200]}")
            console.print("[dim]───────────────────────[/dim]\n")

    elif cmd == "/approve":
        task_id_str = rest.strip()
        if not task_id_str.isdigit():
            console.print("[yellow]Usage: /approve <id>[/yellow]\n")
            return
        try:
            agent_id = factory.repo.approve(int(task_id_str))
        except ValueError as e:
            console.print(f"[red]{e}[/red]\n")
        else:
            if factory.watcher is not None:
                factory.watcher.sync_once()
            console.print(f"[green]Approved — spawned agent #{agent_id} is now live.[/green]\n")

    elif cmd == "/reject":
        reject_parts = rest.split(maxsplit=1)
        if len(reject_parts) < 2 or not reject_parts[0].isdigit():
            console.print("[yellow]Usage: /reject <id> <feedback>[/yellow]\n")
            return
        task_id = int(reject_parts[0])
        feedback = reject_parts[1]
        task = factory.repo.get_spawn_task(task_id)
        if task is None or task["status"] != AWAITING_APPROVAL:
            console.print(f"[red]Spawn task {task_id} isn't awaiting approval.[/red]\n")
            return
        status = factory.repo.reject(task_id, feedback)
        if status == "PENDING":
            resume_spawn(
                task_id,
                factory.repo,
                factory.provider,
                factory.registry,
                factory.registry.names(),
                factory.registry.factory_allowed_names(),
                background_tasks=factory.background_tasks,
            )
            console.print(f"[dim]Rejected — revising spawn task #{task_id} in the background.[/dim]\n")
        else:  # FAILED — revision cap hit
            console.print(f"[red]Spawn task #{task_id} failed: max revision rounds exceeded.[/red]\n")


class SoftwareFactoryContext:
    """Dependencies the Software Factory slash commands need. Built once in
    main() and threaded through to handle_slash()."""

    def __init__(self, repo, provider, settings, background_tasks: set, usage_repo=None) -> None:
        self.repo = repo
        self.provider = provider
        self.settings = settings
        self.background_tasks = background_tasks
        self.usage_repo = usage_repo


def _handle_software_factory_command(cmd: str, rest: str, sf: "SoftwareFactoryContext") -> None:
    from agent.factory.software.pipeline import BudgetCapExceeded, BuildCapExceeded, FactoryPaused, start_build

    if cmd == "/build":
        description = rest.strip()
        if not description:
            console.print("[yellow]Usage: /build <description>[/yellow]\n")
            return
        try:
            task_id = start_build(
                description, sf.repo, sf.provider, sf.settings,
                background_tasks=sf.background_tasks, usage_repo=sf.usage_repo,
            )
        except (FactoryPaused, BuildCapExceeded, BudgetCapExceeded) as e:
            console.print(f"[yellow]{e}[/yellow]\n")
        else:
            console.print(
                f"[dim]Build task #{task_id} started — building in the background. "
                f"Check /builds shortly.[/dim]\n"
            )

    elif cmd == "/builds":
        tasks = sf.repo.list_recent_builds()
        if not tasks:
            console.print("[dim]No builds yet.[/dim]\n")
        else:
            console.print("\n[dim]── Recent builds ──[/dim]")
            for t in tasks:
                console.print(
                    f"  [bold]#{t['id']}[/bold]  status=[bold]{t['status']}[/bold]  "
                    f"slug={t['slug']}  retries={t['retry_count']}"
                )
                console.print(f"    brief: {t['description'][:150]}")
                if t["status"] == "FAILED" and t["failure_reason"]:
                    console.print(f"    [red]reason: {t['failure_reason'][:200]}[/red]")
            console.print("[dim]───────────────────[/dim]\n")


async def chat_loop(
    agent: Agent,
    provider_name: str,
    factory: "FactoryContext | None" = None,
    software_factory: "SoftwareFactoryContext | None" = None,
) -> None:
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
                should_continue = handle_slash(user_input, agent, provider_name, factory, software_factory)
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
    usage_repo = None
    try:
        from agent.cost.recorder import set_usage_repo
        from agent.cost.storage import UsageRepo

        usage_repo = UsageRepo()
        set_usage_repo(usage_repo)
    except Exception as e:  # noqa: BLE001
        console.print(f"[dim]Cost tracking unavailable ({e}); continuing.[/dim]")

    # Build the tool registry from settings (tools self-skip when unconfigured).
    from agent.config import get_settings
    from agent.tools.registry import build_registry

    settings = get_settings()
    registry = build_registry(settings)
    if registry.names():
        console.print(f"[dim]Registered {len(registry.names())} tools: {', '.join(registry.names())}[/dim]")

    # Shared strong-ref set for every background task spawned below — a bare
    # asyncio.create_task() result is only weakly referenced, so without this
    # the task can be garbage-collected mid-flight.
    background_tasks: set = set()

    # Agent Factory: best-effort, like cost tracking — a broken factory.db
    # shouldn't stop Sean from having a normal conversation.
    factory = None
    try:
        from agent.factory.dispatch import RegistryWatcher
        from agent.factory.storage import FactoryRepo

        repo = FactoryRepo()
        watcher = RegistryWatcher(repo, provider, registry)
        watcher.sync_once()  # agents approved in a prior session are live immediately
        watcher_task = asyncio.create_task(watcher.run_forever())
        background_tasks.add(watcher_task)
        watcher_task.add_done_callback(background_tasks.discard)

        factory = FactoryContext(
            repo=repo, provider=provider, registry=registry,
            background_tasks=background_tasks, watcher=watcher,
        )
    except Exception as e:  # noqa: BLE001
        console.print(f"[dim]Agent Factory unavailable ({e}); continuing.[/dim]")

    # Software Factory: same best-effort posture — a broken software_factory.db
    # shouldn't stop a normal conversation either. The autonomous scheduler
    # only starts ticking if TRILLION_FACTORY_AUTONOMOUS_THEMES is set.
    software_factory = None
    try:
        from agent.factory.software.scheduler import AutonomousScheduler
        from agent.factory.software.storage import BuildRepo

        sf_repo = BuildRepo()
        software_factory = SoftwareFactoryContext(
            repo=sf_repo, provider=provider, settings=settings,
            background_tasks=background_tasks, usage_repo=usage_repo,
        )
        if settings.factory_autonomous_themes:
            scheduler = AutonomousScheduler(
                sf_repo, provider, settings,
                background_tasks=background_tasks, usage_repo=usage_repo,
            )
            scheduler_task = asyncio.create_task(scheduler.run_forever())
            background_tasks.add(scheduler_task)
            scheduler_task.add_done_callback(background_tasks.discard)
    except Exception as e:  # noqa: BLE001
        console.print(f"[dim]Software Factory unavailable ({e}); continuing.[/dim]")

    agent = Agent(provider=provider, tool_registry=registry)
    await chat_loop(agent, provider_name=args.provider, factory=factory, software_factory=software_factory)


if __name__ == "__main__":
    asyncio.run(main())
