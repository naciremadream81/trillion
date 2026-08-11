# Software Factory: orchestrated per-task Dev↔QA pipeline

*Design doc — 2026-08-11*

## Motivation

`/home/archie/codebase/agency-agents/specialized/agents-orchestrator.md` describes a
pipeline pattern (PM → Architect → per-task Dev↔QA loop → final integration
review) for running autonomous multi-agent development end to end. Sean asked
for the Software Factory (`agent/factory/software/`) to be rebuilt around this
pattern.

Today's Software Factory builds an entire project in one blind pass: a single
`PLANNING` call produces a file list, `CODING` writes every file in one loop
(up to 20 iterations), and `TESTING` runs the whole test suite exactly once,
with one whole-project corrective retry if it fails. The only quality signal
is that one binary pass/fail at the very end. This redesign replaces the
single CODING pass with a task-by-task implement→review→retry loop, adds an
architecture stage before coding starts, and adds a final independent review
after testing — while preserving the Software Factory's existing autonomy
model: no approval gate, `BUILT` is unconditionally terminal.

## Scope

In scope: `agent/factory/software/{pipeline,planning,storage,readme_md}.py`,
a new `agent/factory/software/architecture.py`, `main.py`'s `/builds` output,
and the corresponding tests.

Out of scope: `agent/factory/software/scheduler.py` (autonomous builds; it
only calls the public `start_build()` and needs no changes), any change to
the Agent Factory (`agent/factory/` proper, for spawned chat specialists —
unrelated system), and anything resembling a browser/screenshot-based QA
(Trillion's Software Factory has no such tooling; QA here is LLM code review
against acceptance criteria, not visual evidence).

## Decisions (resolved during brainstorming)

1. **Loop granularity: fully per-task.** `PLANNING` produces a task list;
   each task gets its own implement → QA → retry (≤3) cycle before the next
   task starts. Most faithful to the source spec; accepted higher LLM-call
   cost per build in exchange for a much better quality signal than today's
   single all-or-nothing pass.
2. **QA method: LLM code review only, no execution per task.** A read-only
   reviewer `Agent` (only `read_project_file`, no write access) checks a
   task's files against its own acceptance criteria and returns
   PASS/FAIL + feedback. The whole-project test suite still runs exactly
   once, after all tasks are done — unchanged from today. Running the test
   suite after *every* task was rejected: most early tasks (e.g. "define
   data models") aren't independently runnable, so per-task test runs would
   mostly just fail or no-op, adding noise and subprocess overhead for no
   signal.
3. **Final verdict is informational only, never gates `BUILT`.** Matches the
   Software Factory's original, explicit design principle: "the autonomy
   boundary is drawn at the filesystem, not the action" — no approval gate,
   `BUILT` is terminal and immediately real. A `NEEDS_WORK`-blocks-`BUILT`
   state was explicitly rejected as a reversal of that decision. The verdict
   (`READY` / `NEEDS_WORK` + notes) is recorded in the README and the build
   task, the same way test pass/fail is already recorded today without
   blocking anything.
4. **A distinct `ARCHITECTURE` stage is added**, between `PLANNING` and
   `SCAFFOLDING`. One LLM call turns the plan's task list into a short
   technical foundation doc (module layout, data flow, key interfaces),
   written to `ARCHITECTURE.md`. Every per-task dev turn gets this as shared
   context, so independently-run task turns build toward one coherent
   structure instead of each guessing independently. Rejected the
   alternative (skip it, let `PLANNING`'s plan alone be the shared context)
   because per-task turns are now genuinely independent LLM calls, which
   raises the risk of structural drift without a written anchor.
5. **Per-task results live in the existing `plan` JSON blob, not a new
   table.** `plan` is already a JSON TEXT column (see `storage.py`'s
   existing pattern); adding a `tasks` list (from `PLANNING`) and a
   `task_results` list (written once after the per-task loop, via a new
   `BuildRepo.set_task_results()`) needs no schema migration. A new
   relational `build_task_items` table was considered and rejected — it's
   schema complexity disproportionate to telemetry nothing else needs to
   query relationally. The existing `retry_count` column keeps its current
   meaning (the whole-project `TESTING → CODING` retry, unchanged); per-task
   retry counts live inside `task_results` so the two retry concepts don't
   conflate.

## State machine

```
PENDING → PLANNING → ARCHITECTURE → SCAFFOLDING → CODING → TESTING → INTEGRATION → DOCS → BUILT
                                                       ↑________|
                                              (one corrective retry, unchanged from today)
                                          any state → FAILED (planning error, budget overage,
                                                       unexpected exception — unchanged)
```

`_VALID_TRANSITIONS` (extends the current table in `storage.py`):

```python
PENDING:      {PLANNING, FAILED}
PLANNING:     {ARCHITECTURE, FAILED}
ARCHITECTURE: {SCAFFOLDING, FAILED}
SCAFFOLDING:  {CODING, FAILED}
CODING:       {TESTING, FAILED}
TESTING:      {CODING, INTEGRATION, FAILED}   # was {CODING, DOCS, FAILED}
INTEGRATION:  {DOCS, FAILED}
DOCS:         {BUILT, FAILED}
```

`CODING` remains a single DB status even though it now runs a multi-task
internal loop — consistent with how it's already a multi-iteration loop
under one status today. No per-task DB status transitions; per-task progress
is observable via `task_results` in the plan JSON (surfaced through
`get_build_task()` / `list_recent_builds()`, which already `json.loads` the
`plan` column).

## Data model

`plan` (JSON, unchanged column) gains two new keys:

```json
{
  "project_name": "...", "tech_stack": "...", "files": ["..."],
  "entry_point": "...", "test_command": "...", "summary": "...",
  "tasks": [
    {"id": 1, "title": "...", "description": "...", "acceptance_criteria": "..."}
  ],
  "task_results": [
    {"task_id": 1, "status": "PASSED|BLOCKED", "attempts": 1, "last_feedback": "..."}
  ]
}
```

`tasks` is written by `PLANNING` (via `set_plan()`, unchanged call site).
`task_results` is written once, after the per-task loop finishes, via a new
`BuildRepo.set_task_results(task_id, results)` — a plain `UPDATE` of the
`plan` column's JSON, no status transition (the DB status is already
`CODING` for the whole duration of the loop).

Plan validation (`planning.py`) caps `len(tasks)` at **20** — a hard ceiling
on worst-case per-build cost, same "reject rather than silently degrade"
posture as the existing non-empty-`files` check.

## Module changes

### `SCAFFOLDING` (unchanged)
Still creates an empty stub file for every path in `plan["files"]`, exactly
as today, before the per-task loop starts. `files` and `tasks` are both
required and serve different purposes: `files` is the flat inventory
`SCAFFOLDING` stubs out up front; `tasks` is the unit of work the per-task
loop iterates. A task's dev turn isn't restricted to the files named in
`plan["files"]` — same as today, `write_project_file` only enforces the
project-directory sandbox, not a match against the planned file list.

### `planning.py`
- `REQUIRED_FIELDS` gains `"tasks"`.
- System prompt and `_final_ask()`'s JSON shape updated to request the task
  list: each task a `{title, description, acceptance_criteria}` object.
- `_validate_plan()` validates `tasks` is a non-empty list of objects with
  those three string fields, and rejects `len(tasks) > 20`.

### `architecture.py` (new)
- `run_architecture(description, plan, provider) -> str`: one LLM call, no
  tools (read-only), returns architecture markdown. Same two-shot shape as
  `planning.py`/`research.py` is unnecessary here since there's no strict
  JSON contract to validate — free-form markdown, one shot.
- Pipeline helper `_run_architecture()` in `pipeline.py` calls it and writes
  `ARCHITECTURE.md` via `WriteProjectFileTool`.

### `pipeline.py`
- `_run_architecture(description, plan, project_dir, provider) -> str` —
  writes `ARCHITECTURE.md`, returns its content for reuse as dev-turn context.
- `_run_task_loop(description, plan, architecture_doc, project_dir, provider, settings, usage_repo) -> list[dict]`:
  for each task in `plan["tasks"]`, in order:
  1. **Dev turn**: `Agent` with `write_project_file` + `read_project_file`,
     system prompt scoped to *this task only*, given the architecture doc
     and a short summary of prior tasks' outcomes as context. Reuses the
     existing "loop until done-sentinel" shape from today's `_run_coding`,
     capped at a new `TASK_CODING_MAX_ITERATIONS = 8` (down from the
     whole-project `CODING_MAX_ITERATIONS = 20`, since one task needs far
     fewer turns).
  2. **QA turn**: a separate `Agent`, `read_project_file` only (no write
     access — a reviewer can't fix what it's reviewing), system prompt asks
     it to check the task's acceptance criteria against the files it
     touched, reply `{"result": "PASS"|"FAIL", "feedback": "..."}` (same
     bare-JSON-reply pattern as `planning.py`).
  3. **Retry**: on FAIL, feed the QA feedback into the next dev turn's
     prompt, up to 3 attempts total. After 3 fails, record `status: BLOCKED`
     for that task and move to the next task — never aborts the build.
  4. `_check_budget()` runs before each task's dev turn, not just between
     pipeline stages, so a long multi-task build still respects the daily
     budget cap mid-loop.
  Returns the `task_results` list; the pipeline writes it via
  `repo.set_task_results()` once the loop completes.
- `_run_integration(description, plan, task_results, project_dir, test_passed, test_output, provider) -> dict`:
  final read-only reviewer call (whole project context + task results + test
  output), returns `{"verdict": "READY"|"NEEDS_WORK", "notes": "..."}`.
  Never raises on a `NEEDS_WORK` verdict — it's data, not a failure.
- `run_build_pipeline()`: re-sequenced —
  `PLANNING → ARCHITECTURE → SCAFFOLDING → CODING (task loop) → TESTING
  (existing whole-project run + existing one-shot corrective retry,
  unchanged) → INTEGRATION → DOCS → BUILT`.

### `readme_md.py`
- `readme_markdown()` gains: an "Architecture" section (the `ARCHITECTURE.md`
  content, or a pointer to it), a "Tasks" table (title, status, attempts),
  and an "Integration review" section (verdict + notes). Existing "Tests"
  section unchanged.

### `storage.py`
- `ARCHITECTURE`, `INTEGRATION` added as new status constants.
- `_VALID_TRANSITIONS` updated per the table above.
- New `set_task_results(task_id, results: list[dict]) -> None`: `UPDATE`s
  the `plan` JSON column's `task_results` key. Requires status still be
  `CODING` (asserted, not a formal transition — no status change happens).

### `main.py`
- `/builds` output: when `plan.get("task_results")` is present, append a
  one-line summary, e.g. `tasks: 6/8 passed, 2 blocked`. Additive only, no
  new command.

## Cost containment (summary)

- 3 retries per task (matches the source spec).
- `TASK_CODING_MAX_ITERATIONS = 8` per task (vs. today's whole-project 20).
- Budget checked before every task's dev turn, not just between stages.
- Plan-level task-count cap of 20.

## Testing strategy

- `test_software_planning.py`: extend for `tasks` field validation
  (required, non-empty, capped at 20, correct shape per item).
- New `test_software_architecture.py`: `run_architecture()` against a
  `FakeProvider`, asserting the returned markdown is written correctly by
  the pipeline helper.
- `test_software_pipeline.py` (substantial rewrite, `FakeProvider` reply
  sequences get longer to match the new stage count): multi-task success;
  a task that fails QA once then passes on retry; a task that gets
  `BLOCKED` after 3 fails but the build still reaches `BUILT`; the
  integration verdict is recorded but never blocks; existing
  budget-cap/build-cap/paused/planning-failure tests carried forward
  against the new stage sequence.
- `test_software_storage.py`: extend `_VALID_TRANSITIONS` coverage for
  `ARCHITECTURE`/`INTEGRATION`; new tests for `set_task_results()`.
- `test_main_software_commands.py`: extend `/builds` output test for the
  task-summary line.
- No changes needed to `test_software_caps.py`, `test_software_scheduler.py`,
  or `test_serve_software_factory_wiring.py` — none touch pipeline internals.

## Non-goals

- No screenshot/visual QA (no browser tooling in this codebase).
- No approval gate / no `NEEDS_WORK`-blocks-`BUILT` state.
- No change to the Agent Factory (`agent/factory/` for chat specialists).
- No change to `scheduler.py`'s public interface.
