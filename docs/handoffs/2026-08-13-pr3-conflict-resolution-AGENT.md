# AGENT HANDOFF — resolve PR #3 merge conflicts

*Written 2026-08-13. Assume zero memory of any prior conversation. Read this file and the repo — nothing else.*

## 1. Mission

`naciremadream81/trillion` PR #3 ("Harden run_project_tests, close SELECT
INTO gap, misc review fixes", branch `fix/code-review-followups` → `main`)
has merge conflicts and needs to land. It's old — 4 commits, based off a
main that's since moved 23 commits ahead (including a ~9,500-line
safety-rails effort that touched two of the same files PR #3 touches).
Resolve the conflicts, verify nothing regresses, and get it mergeable.

## 2. Current State

- PR #3 is **open**, `mergeable: CONFLICTING` (confirmed via
  `gh pr view 3 --json mergeable`).
- Nobody has started resolution yet. A prior session only did read-only
  investigation (`git fetch`, `git log`, `git merge-tree`, `gh pr diff
  --name-only`) — no branch was checked out, no merge attempted, no commits
  made toward a fix.
- Exact SHAs at investigation time:
  ```
  origin/main (base)                              = d4ecf73ddb922cf04c791d1798809e9923e8484c
  origin/fix/code-review-followups (PR #3 head)   = e11388c516f60330a557d2974f188db29318de1e
  merge-base(main, PR#3)                          = 0c737c374be96a155134533074b763d0eb84a666
  ```
  **Re-fetch and re-check these before trusting them** — main may have moved
  again since this was written.
- Files GitHub reports as changed in PR #3:
  ```
  .env.example
  .gitignore
  AGENT.md
  README.md
  agent/config.py
  agent/tools/analytics_tool.py
  agent/tools/project_fs.py
  docs/superpowers/specs/2026-08-11-software-factory-orchestrator-design.md
  serve.py
  tests/test_analytics_tool.py
  tests/test_project_fs.py
  tests/test_serve_chat_sessions.py
  tests/test_serve_factory_wiring.py
  tests/test_serve_software_factory_wiring.py
  ```
- **Exact next action:** fetch, check out the PR branch, run the merge, and
  work through conflicts file by file — see the Resume Command at the
  bottom for the literal commands.

## 3. Decisions Made (and Why)

- **Decision:** investigate read-only first (no checkout, no merge attempt)
  before touching anything.
  **Alternatives considered:** just start merging immediately.
  **Reason:** the user asked for handoff docs mid-investigation rather than
  letting the investigating session continue into resolution — better to
  leave the repo in a known-clean state for whoever picks this up next.
  **Reversibility:** N/A, this was a one-time choice already made; nothing
  to undo.
- **No merge-vs-rebase decision has been made yet.** That's yours to make.
  Given PR #3 is 4 commits with a clean individual narrative (bubblewrap
  sandboxing, SELECT-INTO fix, autonomous-scheduler wiring, a design doc),
  a `git rebase origin/main` preserving that commit structure is probably
  nicer than a merge commit — but if the per-file conflicts turn out to be
  large/tangled, a single merge commit with one consolidated resolution is
  easier to reason about and review. Pick based on how bad the conflicts
  actually are once you're in them; don't over-plan this up front.

## 4. Architecture & Key Files

Files PR #3 touches, and what's happened to each on `main` since PR #3's
base (`0c737c37`) — this is the part that makes this merge non-trivial:

| File | Main's diff since base | Why it moved | Conflict risk |
|---|---|---|---|
| `serve.py` | +198/-5 | P1 (confirmation-gate wiring), P5 (notes/email tool wiring), P6 (heartbeat endpoints), P7 (security-shield endpoint, CSP/security-headers middleware) all landed in `build_app()` | **High** — this is almost certainly the real conflict |
| `agent/config.py` | +125/-0 | New `Settings` fields for safety rails, notes, heartbeat, security — but purely additive (all `+`, no `-`), so likely auto-mergeable or a trivial conflict | Low-medium |
| `agent/tools/project_fs.py` | +11/-0 | P1 added `risk` / `requires_confirmation` fields at the `BaseTool` level (`agent/tools/base.py`), which `project_fs.py`'s tools inherit | Low — small diff, but see Gotcha below |
| `agent/tools/analytics_tool.py` | (not measured — check `git diff <base> origin/main -- agent/tools/analytics_tool.py`) | Unknown — re-check before assuming | Unknown |
| `.env.example`, `AGENT.md`, `README.md` | doc-only additions on both sides (Software Factory env vars, staleness note, bubblewrap prerequisite) | Both sides added different sections | Low — should merge cleanly or need trivial reconciliation |
| `.gitignore`, `tests/test_analytics_tool.py` | changed both sides | `git merge-tree` showed these auto-merge cleanly already | None expected |
| `docs/.../2026-08-11-...design.md`, `tests/test_serve_chat_sessions.py`, `tests/test_serve_software_factory_wiring.py` | added only by PR #3 | New files, main never touched them | None expected |

**Files that look related but aren't part of this PR — don't go touch
them "while you're in there":**
- `agent/safety/*`, `agent/security/*`, `agent/heartbeat/*`, `agent/notes/*`
  — all new packages from the just-merged P0–P7 work (PR #4). They're the
  *reason* `serve.py` and `agent/config.py` moved, but PR #3 doesn't touch
  them directly and shouldn't need to.
- `docs/incident-runbook.md` — unrelated, also just landed via PR #4.

## 5. Gotchas & Hard-Won Knowledge

- **`agent/tools/analytics_tool.py`'s forbidden-keyword regex.** PR #3 adds
  `into` to the blocklist regex (`_FORBIDDEN`) to close a `SELECT ... INTO`
  gap, *and* adds literal/comment-blanking logic so the word "into" inside
  a string literal doesn't false-positive the guard. Read that hunk
  carefully during the merge — it's exactly the kind of thing that's easy
  to half-apply and end up with either the vulnerability still open or the
  literal-exclusion regex missing.
- **`serve.py`'s conflict is structural, not textual-random.** Main's
  additions are all new route registrations inside `build_app()` (new
  `app.router.add_*` calls) plus new middleware wiring. PR #3's changes are
  (per commit titles) chat-session and Software-Factory wiring review
  fixes — likely also inside `build_app()` or adjacent handler functions.
  These are probably **both correct and both wanted** — the fix is almost
  certainly "keep both," not "pick a side." Don't resolve this by discarding
  either side wholesale; read what each hunk actually does first.
- **`project_fs.py` / `analytics_tool.py` both-sides-touched is not a real
  disagreement.** Main's side added `risk`/`requires_confirmation` at the
  `BaseTool` ABC level (`agent/tools/base.py`) — individual tool files like
  these just inherit/declare those fields. PR #3's side is an independent,
  unrelated hardening pass (bubblewrap sandboxing for `project_fs.py`'s
  `run_project_tests`, the SELECT-INTO fix for `analytics_tool.py`). They
  should compose without contradiction — but verify by reading both, don't
  assume.
- **`git merge-tree` in this repo's git version doesn't print `CONFLICT`
  markers directly** — it shows `changed in both` with a unified diff
  against the merge-base, which tells you a file was touched on both sides
  but not definitively whether the specific lines collide. Treat GitHub's
  `mergeable: CONFLICTING` status as authoritative for "yes there's a real
  conflict somewhere"; use `git merge-tree` output only to scope *which*
  files are worth reading closely first.

## 6. Conventions In Play

- Tests: stdlib `unittest` only, no pytest. One `tests/test_<thing>.py` per
  module. Full regression: `.venv/bin/python -m unittest discover -s
  tests` — **use the venv python**, bare `python3` is missing dependencies
  in this environment and will misreport a working change as broken.
- Commit style (see recent `git log --oneline` on `main`): imperative,
  descriptive first line, body explains *why* not *what*, `Co-Authored-By:
  Claude Fable 5 <noreply@anthropic.com>` trailer on Claude-authored
  commits.
- This repo's git safety rules (from the standing system prompt, not
  optional): never `--force` push without explicit user ask, never skip
  hooks, prefer new commits over amends, stage files by name not `-A`/`.`
  unless you've reviewed `git status` first, never commit unless asked (for
  this task, resolving the PR *is* the ask — commits are expected).
- If you open a PR of your own for anything unrelated found along the way,
  follow the same pattern as PR #4: feature branch off `main`, full test
  suite passes before pushing, PR body's "Test plan" only claims what was
  actually verified (a permission-classifier in this environment will
  reject a PR body that claims interaction testing that didn't happen —
  don't write "manually verified" unless you actually clicked/ran it).

## 7. Open Questions

1. **Is PR #3 still wanted at all?** It's old — based on the repo's state
   from before the entire P0–P7 safety-rails effort. Worth a quick sanity
   check with the user: is the SELECT-INTO fix and bubblewrap sandboxing
   still the desired approach, or has anything in the since-landed
   safety-rails work (e.g. the confirmation gate) made part of this PR
   redundant? Don't assume — ask if genuinely unsure after reading the
   diff, but resolving the mechanical conflict doesn't require an answer to
   this, so don't block on it unless something looks actually superseded.
2. **Merge vs. rebase** — see Decisions Made above. Left for you to decide
   once you're looking at the actual conflict hunks.
3. **`agent/tools/analytics_tool.py`'s exact diff on main** was not measured
   during investigation (unlike the other two high-risk files) — check it
   first thing, don't assume it's low-risk just because it wasn't sized.

## 8. Do Not Touch

- Don't refactor `agent/safety/`, `agent/security/`, `agent/heartbeat/`, or
  `agent/notes/` while resolving this — they're unrelated, freshly landed
  (PR #4), and out of scope. If the merge naturally touches a line in one of
  them (unlikely, they're not in PR #3's file list), stop and re-read this
  doc before proceeding.
- Don't discard either side of the `serve.py` conflict wholesale — see
  Gotchas. Both sides are almost certainly wanted.
- Don't force-push to `main` or to `fix/code-review-followups` without
  explicit user confirmation, even after conflicts are resolved locally.

## 9. Resume Command

> Read `docs/handoffs/2026-08-13-pr3-conflict-resolution-AGENT.md`, then:
> 1. `git fetch origin` and re-check `gh pr view 3 --json mergeable` — confirm it's still `CONFLICTING` and note the current head SHA (may have moved past `e11388c`).
> 2. Check out `fix/code-review-followups` (fresh local branch tracking `origin/fix/code-review-followups`), then merge (or rebase onto) `origin/main`.
> 3. Resolve conflicts file by file, using the risk table in section 4 to prioritize — read both sides before picking, especially for `serve.py` and `agent/tools/analytics_tool.py`.
> 4. Run `.venv/bin/python -m unittest discover -s tests` (venv python, not bare `python3`) — must pass clean before pushing.
> 5. Push and confirm `gh pr view 3 --json mergeable` now reports `MERGEABLE`.
> Do not touch `agent/safety/`, `agent/security/`, `agent/heartbeat/`, or `agent/notes/`. Confirm with the user before force-pushing anything or before merging PR #3 itself.
