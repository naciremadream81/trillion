"""
Spawn pipeline: drives a spawn_task through the Factory's state machine —
PENDING -> RESEARCHING -> DRAFTING_SPEC -> WRITING_PROMPT -> AWAITING_APPROVAL,
or FAILED on any step's error.

start_spawn() is the fire-and-forget entry point the CLI calls: it creates
the row and schedules run_pipeline() in the background so the CLI isn't
blocked on research + drafting. The caller must pass a `background_tasks`
set it owns and keeps alive for the process lifetime — asyncio only holds a
weak reference to a bare create_task() result, so without a strong reference
the task can be garbage-collected mid-flight (a documented asyncio footgun).
"""

from __future__ import annotations

import asyncio
import logging

from .draft import DraftError, draft_agent_spec
from .research import ResearchError, run_research
from .spec_md import write_spec_markdown
from .storage import AWAITING_APPROVAL, DRAFTING_SPEC, FAILED, RESEARCHING, WRITING_PROMPT, FactoryRepo

logger = logging.getLogger(__name__)

DAILY_SPAWN_CAP = 5


class SpawnCapExceeded(Exception):
    """Raised when starting a new spawn would exceed the daily cap."""


async def run_pipeline(
    task_id: int,
    role_description: str,
    repo: FactoryRepo,
    provider,
    research_tool_registry,
    registry_tool_names: list[str],
    factory_allowed_names: set[str],
) -> None:
    """
    Drive one spawn_task from RESEARCHING through AWAITING_APPROVAL (or
    FAILED). Never raises — every failure is recorded on the task row
    instead, matching Trillion's existing "exceptions become data the caller
    can see" convention (agent/core.py's _run_tool catches and stringifies
    rather than propagating).
    """
    try:
        repo.update_status(task_id, RESEARCHING)
        report = await run_research(role_description, provider, research_tool_registry, repo=repo)
        repo.save_research_report(
            task_id,
            domain=report["domain"],
            competencies=report["competencies"],
            tools_available=report["tools_available"],
            tools_wishlist=report["tools_wishlist"],
            design_patterns=report["design_patterns"],
            sources=report["sources"],
        )

        repo.update_status(task_id, DRAFTING_SPEC)
        repo.update_status(task_id, WRITING_PROMPT)
        spec = await draft_agent_spec(
            role_description,
            report,
            provider,
            registry_tool_names,
            factory_allowed_names,
            slug_taken=repo.slug_taken,
        )
        write_spec_markdown(role_description, report, spec)

        repo.set_draft(
            task_id,
            slug=spec["slug"],
            system_prompt=spec["system_prompt"],
            tool_allowlist=spec["tool_allowlist"],
        )
    except (ResearchError, DraftError) as e:
        repo.update_status(task_id, FAILED, failure_reason=str(e))
    except Exception as e:  # noqa: BLE001 — a background pipeline must never crash the process
        logger.exception("spawn pipeline %s failed unexpectedly", task_id)
        repo.update_status(task_id, FAILED, failure_reason=f"unexpected error: {e}")


def start_spawn(
    role_description: str,
    repo: FactoryRepo,
    provider,
    research_tool_registry,
    registry_tool_names: list[str],
    factory_allowed_names: set[str],
    *,
    background_tasks: set,
) -> int:
    """
    Create the spawn_task row and schedule run_pipeline() in the background.
    Returns the new task id immediately (the CLI can tell the user "spawn
    #7 started, check /pending later" without blocking on research).

    Raises SpawnCapExceeded before creating anything if today's count is
    already at DAILY_SPAWN_CAP.
    """
    if repo.count_spawns_today() >= DAILY_SPAWN_CAP:
        raise SpawnCapExceeded(f"daily spawn cap of {DAILY_SPAWN_CAP} reached")

    task_id = repo.create_spawn_task(role_description)
    coro = run_pipeline(
        task_id,
        role_description,
        repo,
        provider,
        research_tool_registry,
        registry_tool_names,
        factory_allowed_names,
    )
    bg_task = asyncio.create_task(coro)
    background_tasks.add(bg_task)
    bg_task.add_done_callback(background_tasks.discard)
    return task_id


def resume_spawn(
    task_id: int,
    repo: FactoryRepo,
    provider,
    research_tool_registry,
    registry_tool_names: list[str],
    factory_allowed_names: set[str],
    *,
    background_tasks: set,
) -> None:
    """
    Re-run the pipeline for a task that was rejected with feedback (repo.reject()
    returned PENDING). Reuses the existing row rather than creating a new one —
    a revision isn't a new spawn, so it doesn't count against the daily cap.
    """
    task = repo.get_spawn_task(task_id)
    if task is None:
        raise ValueError(f"spawn task {task_id} not found")

    role_description = task["role_description"]
    if task.get("feedback"):
        role_description = (
            f"{role_description}\n\n"
            f"Revision feedback from a previously rejected draft (address this): "
            f"{task['feedback']}"
        )

    coro = run_pipeline(
        task_id,
        role_description,
        repo,
        provider,
        research_tool_registry,
        registry_tool_names,
        factory_allowed_names,
    )
    bg_task = asyncio.create_task(coro)
    background_tasks.add(bg_task)
    bg_task.add_done_callback(background_tasks.discard)
