# Build a Scout Sub-Agent With Operator-Tunable Doctrine

You are going to add a **scout** to an existing agent system: a sub-agent that
wakes on a schedule, hunts for something its operator cares about, scores what
it finds against a rubric, and hands the results to a human who decides.

The scouting loop is the easy half. The half that decides whether this is still
running in three months is **where the instructions live**. If the thing that
tells your scout what to hunt today is a string constant in your source tree,
then every retune is a code change, a deploy, and a context switch — so it
doesn't happen, and the scout slowly drifts into producing output nobody reads.
This build treats the instruction set as data from the start.

---

## Rules of engagement (read before doing anything)

1. **Interview first.** Do not write code, do not create files, do not propose
   an architecture until you have asked the questions in Phase 0 and received
   answers. Ask them in small batches, not all at once.
2. **One tier at a time.** After the interview, propose a tier plan and get
   explicit approval. Then build one tier, verify it, show the result, and
   stop. Wait to be told to continue.
3. **Verify, don't assert.** "Should work" is not a result. Each tier below has
   a verification step that produces evidence. Run it and show the output.
4. **Match the host codebase.** Every example here is illustrative pseudocode.
   Read the surrounding code first and write in its idiom — its naming, its
   error handling, its test style, its comment density.
5. **Never invent the operator's domain knowledge.** The rubric, the hard kills,
   and the lanes are theirs. Ask. Do not write a plausible-sounding rubric and
   hope they correct it.
6. **Say what you skipped.** If a tier is blocked or you deliberately left
   something out, name it. Silent scope reduction is worse than an open gap.

---

## Phase 0 — The interview

Ask these before building. Group them; don't dump all thirty at once. Where an
answer has an obvious default, say what you'd assume and let them correct you
rather than making them type an essay.

### A. The host system

1. What language and runtime is the agent system written in? What web framework
   and database, if any?
2. How are sub-agents defined today — a class, a config file, a registry, a
   manifest? Show me one existing sub-agent so I can match the pattern.
3. Is there a scheduler already (cron, a routines table, a task queue)? If yes,
   how does a scheduled job get its instructions?
4. Is there an existing settings or admin UI where an operator can see and edit
   agent configuration? If not, is adding one in scope, or should editable
   instructions stop at "a file in the repo"?
5. Where do sub-agent runs execute — one always-on process, a worker pool, a
   laptop that sleeps? (This decides whether a missed schedule is possible.)
6. Which model provider and SDK? Do you already have a tool-calling loop I
   should reuse rather than writing a second one?

### B. What it hunts

7. In one sentence: what is this scout looking for?
8. What counts as a **find** worth surfacing? Give me one real example of a good
   one and one that looks good but isn't.
9. What are the data sources, and which are free versus paid? Be specific — I'd
   rather work within a free tier than assume you'll buy an API.
10. Are there sources you've deliberately ruled out, and why? (Rate limits,
    terms of service, cost, unreliability — I need the reason so I don't
    helpfully "upgrade" you back onto them later.)
11. What must this scout **never** propose? These become hard kills in the
    doctrine, and they're the highest-value thing you'll tell me.
12. Does it need to avoid repeating itself across runs? If so, what makes two
    finds "the same one"?

### C. Rotation

13. Does the scout look for the same thing every run, or should it rotate
    between different strategies?
14. If it rotates: how many strategies, and what's the cadence — one per run on
    fixed days, round-robin, or weighted?
15. Should the operator be able to add, remove, or reword a strategy without a
    code change? (If yes, Tier 3 is not optional.)

### D. Scoring

16. What are the scoring axes? How many, and what scale?
17. For each axis, what does the top score actually look like versus a 1? I want
    your words, not mine.
18. Should every find carry a kill criterion — a pre-committed bar that says
    when to abandon it later? What shape does that take in your world?
19. Is there a threshold below which a find shouldn't be surfaced at all, or do
    you want to see everything with its score attached?

### E. Who decides

20. Does the scout ever act on a find, or only recommend? (Strongly recommend
    "only recommend" for v1.)
21. Where does the human see the results — a briefing, an inbox, a queue, a
    dashboard panel?
22. What are the human's verbs? Promote/pass? Accept/reject/snooze? Something
    else?
23. Should a run that finds nothing still report? (Answering "yes" is usually
    right: silence is ambiguous between "nothing found" and "it broke.")

### F. Editability and guardrails

24. Who will be retuning the doctrine — you, a teammate, or nobody? How often do
    you expect to change it in the first month?
25. Do you need version history and one-click revert on the instructions, or is
    git history enough?
26. What's the per-run cost ceiling, iteration cap, and wall-clock timeout?
27. What should happen when a run fails partway — throw the whole thing away,
    salvage what parsed, or retry?
28. Is there anything in this domain that is confidential and must never appear
    in logs, alerts, or error messages?

### G. Scope

29. Which tiers below do you actually want in this session? It is completely
    reasonable to stop at Tier 4 and never build a UI.
30. Anything already half-built that I should extend rather than replace?

**After the interview:** summarize what you heard in under twenty lines, state
your assumptions explicitly, propose the tier plan, and wait for approval.

---

## The architecture in one picture

```
  schedule ──▶ routine row (stores a TAG, not the instructions)
                    │
                    ▼
            resolve the tag at RUN TIME
                    │
                    ├── doctrine document  ──┐
                    └── lane document ───────┤   effective = override or file
                                             │   (override lives in the DB,
                                             ▼    edited from the UI)
                        ┌────────────────────────────┐
                        │  research loop             │
                        │  tools → terminal emit tool│
                        │  cost cap / iter cap / TTL │
                        └────────────┬───────────────┘
                                     ▼
                        validated structured report
                                     │
                        ┌────────────┴───────────────┐
                        ▼                            ▼
                  review pipeline               briefing/digest
                  (human promotes                (always delivered,
                   or passes)                     even when empty)
```

Two ideas carry most of the weight:

**The routine stores a tag, not a prompt.** If the schedule row holds the actual
instructions, then rotating strategies requires a database write on every run,
and an operator editing that field can accidentally freeze the scout onto one
strategy forever. Store something inert like `[scout:rotate]` and expand it at
run time.

**Instructions are documents, not constants.** A file in the repo is the
default; an optional row in a database shadows it; the operator edits the row
from the UI. The code reads `override or file` and never knows which it got.

---

## Tier 1 — A research loop that ends in a validated report

**Goal:** one function you can call by hand that hunts and returns a typed
object.

Give the model real tools plus one **terminal tool** — a tool whose call ends
the loop and whose input *is* the result. This beats asking for JSON in prose:
the schema is enforced at the tool-call layer, so the model retries on a
mismatch instead of handing you unparseable text.

```
tools = [ ...your research tools..., {
    "name": "emit_report",
    "description": "Emit the final scored report. Call once, at the end.",
    "input_schema": ReportSchema.json_schema(),
}]

result = run_tool_loop(
    tools=tools,
    terminal_tool_name="emit_report",
    max_iterations=<enough for tools to chain>,
    max_cost_usd=<hard ceiling>,
    timeout_s=<wall clock>,
    max_tokens=<sized for a FULL report, not the default>,
)
```

Four things that bite here, all worth writing down in comments:

- **`max_tokens` is a silent killer.** A full scorecard with evidence and
  arithmetic does not fit a small default. The first truncated run validates to
  nothing and looks like "the model found nothing." Size it deliberately and
  say in a comment that the cost cap — not the token limit — is the real
  guardrail.
- **Salvage, don't discard.** A truncated response usually contains the summary
  and the first item or two. Hand the partial payload to a coercion function
  that keeps what parses and drops what doesn't, rather than throwing away a run
  the operator just paid for.
- **Distinguish failure modes in the logs.** `truncated`, `loop_detected`,
  `exhausted`, and `completed` should not all produce the same message. When
  this thing quietly stops working months from now, those log lines are the
  entire investigation.
- **A salvage message must not read like a finding.** If your coercion path
  returns something like "partial result — nothing cleared the bar," an operator
  glancing at a briefing cannot tell a broken run from an honest empty one. Make
  the fallback text unmistakably a failure, or carry a status field alongside it.

**Verify:** call it once by hand with a realistic query. Show the operator the
raw report object and the wall-clock and cost of the run. Confirm the numbers
sit inside the caps from question 26.

---

## Tier 2 — Move the doctrine out of code

**Goal:** the scout's system prompt lives in a markdown file next to its code,
loaded at run time with mtime-based caching.

```
DOCTRINE = Path(__file__).parent / "DOCTRINE.md"

def system_prompt() -> str:
    return load_document(DOCTRINE)   # caches on mtime
```

The doctrine should contain, in the operator's own words from questions 7–19:
the thesis, the hard kills as an explicit list, the order of authority for its
tools, the rubric with what a top score versus a bottom score looks like, and a
discipline section covering what to do when evidence is thin.

Two rules for the doctrine's content:

- **Hard kills are a list, not a paragraph.** They get skimmed. Make each one a
  bullet with the reason attached, because a rule without a reason gets
  rationalized around.
- **"You recommend, the human decides" belongs in the prompt itself**, not only
  in your architecture. A scoring model that believes it is deciding writes
  differently — more confident, less hedged, less useful.

**Verify:** edit one line of the file, re-run, and show the change reaching the
model's system prompt. No restart should be required if your loader caches on
mtime.

---

## Tier 3 — Rotating lanes as a parsed document

Skip this tier if the answer to question 13 was "same thing every run."

**Goal:** the strategies the scout rotates between are a document, parsed into an
ordered list, and the rotation is positional.

```markdown
# Hunting lanes

One lane fires per run. Heading order is the rotation order.
Each `## Heading` starts a lane; the heading becomes the label stored
on everything that lane finds. This text above the first heading is
for you — the parser ignores it.

## First-strategy-name

The brief for this strategy, written as instructions to the scout.

## Second-strategy-name

...
```

Parse `## Heading` → `(label, brief)` pairs in file order. Slugify the heading
into the label so that reformatting a heading — adding a hyphen, changing case —
doesn't silently fork the historical record of which lane found what.

Now the part that matters. **The moment a human can edit this, it can be edited
badly.** Nothing in the lane module may raise:

| Failure | Behavior |
|---|---|
| Override parses to zero lanes | Fall back to the shipped file, log *why* and what the format is |
| File missing or unreadable | Fall back to one generic brief, log at error level |
| Fewer lanes than the schedule expects | Take the index **modulo the live count** |

That last row is the subtle one. If Friday maps to index 2 and the operator
deletes a lane, Friday's run becomes an `IndexError` — a scheduled job that dies
silently at 8:30am on a day nobody is watching. Any positional index into a
user-editable list needs a modulo.

```
def lane_index(day, count):
    return WEEKDAY_TO_LANE.get(day.weekday(), day.weekday()) % count
```

**Verify:** write tests, not assertions in a comment. One per degradation path:
junk input parses to empty rather than raising; an unparseable override falls
back to the file; a missing file yields the emergency lane; a single-lane
document still serves every scheduled day. Show them passing.

---

## Tier 4 — Overrides: retune without a deploy

**Goal:** an operator-set value in the database shadows the file default, and
takes effect on the next run without a restart.

You need three pieces:

1. **A table** keyed by an arbitrary string, not a foreign key to your agent
   list. Extra documents belong to an agent but are not the agent, so the key is
   `"<agent>_lanes"`, not `"<agent>"`. One row, one document.
2. **An in-process cache** so prompt loaders stay synchronous and fast. Loaded
   at startup, refreshed on a pub/sub notification.
3. **A read that doesn't care.** `override or load_document(file)`. Callers never
   branch on which one they got.

```
def effective_document(key, path):
    return get_override(key) or load_document(path)
```

When an override is written, publish a notification and have every process
refresh its whole cache. Full refresh over per-key invalidation: caches this
small aren't worth the bug surface of partial updates.

**Verify:** write an override directly, confirm the next run uses it, and — this
is the claim you're actually making — confirm it **without restarting the
process**. Then delete the override and confirm the file default returns.

---

## Tier 5 — Surface it in the UI

Skip if question 4 said there's no UI and none is wanted. Read this tier anyway:
the trap in it is the most expensive one in this whole document.

**Goal:** the operator opens the agent's settings and sees the real doctrine, the
real lanes, and the real tool list — all editable, each revertable on its own.

### The trap

Panels like this usually gate on lookup tables — one mapping a slug to its code
default, one listing which agents may be edited, one listing each agent's tools.
Miss an entry in any of them and **the panel doesn't error, it lies**: it shows
an empty textarea and a message like "no default available" for an agent whose
prompt is loaded on every single run. The operator concludes the feature doesn't
exist and stops looking.

This is worth a specific test:

```
def test_everything_editable_has_a_real_default():
    keys = editable_agents | {d.key for docs in extra_documents.values()
                                     for d in docs}
    missing = [k for k in keys if not has_default_branch(k)]
    assert not missing
```

And a warning that generalizes past this feature: **if your test file
hand-copies the lookup it's testing, that copy is not coverage.** It drifts from
the original, both stay green, and the bug ships. Assert against the real source.

### Extra documents

Register additional editable documents explicitly rather than discovering them
by naming convention — a convention lets an agent expose an internal prompt key
by accident. Each document reports `effective`, `default`, `is_overridden`, and
its own editability, and each gets its own revert.

### Two distinctions that look like one

- **"This agent has an override" is not "this agent's main prompt has an
  override."** Use the broad one for the header badge and the narrow one to gate
  the main prompt's Revert button, or reverting the doctrine becomes a no-op
  delete that looks like it worked.
- **A version restore must route by the version's own key, not the key in the
  URL.** Once extra documents write version rows into the same history table, a
  restore that trusts the URL will happily paste a lane document over the
  doctrine, with no error and no way to notice.

### Validate before you write

If a save carries several documents, validate all of them before writing any.
Otherwise a rejected second textarea leaves the first one half-saved and the
operator has no idea which state they're in.

**Verify:** a full round trip through the real endpoints — read, edit, confirm it
reads back as overridden, revert, confirm it's clean and nothing was left
behind. Then open the actual UI and confirm the textareas render populated and
enabled. Screenshot it.

---

## Tier 6 — Schedule, pipeline, and the human's decision

**Goal:** it runs on its own, results land somewhere a human acts on them, and
nothing is ever decided without them.

**The routine row.** Seed it idempotently by name so a restart doesn't create
duplicates, and refresh its stored prompt on boot so it tracks the current
wording. Only rewrite the schedule when it actually differs — a naive
"always update" pushes a due run into the future on every restart, and a job
that reboots often may never fire at all.

**The tag expands at run time.** The row stores `[scout:rotate]`. At dispatch,
that resolves into today's lane. Rotation needs no writes and an operator
editing the field can't pin the scout to one strategy.

**Persist per item, not per report.** Wrap each item's write in its own
try/except. One malformed row must not discard the four good ones beside it.
Count what actually persisted and report *that* — telling the operator "3 new
finds" when zero were saved sends them looking for rows that aren't there.

**Repetition memory.** If question 12 said finds can repeat, load recent
identifiers and pass them into the query as an explicit "already seen, find
something new" instruction. Cheap, and it compounds.

**Deliver even when empty.** An empty run is information. A briefing that says
"nothing cleared the bar this week" tells the operator the scout ran; silence
tells them nothing, and is indistinguishable from a crash.

**Verify:** confirm the routine exists with the right schedule and next-run time,
trigger one run manually end to end, and show the operator both the briefing and
the rows in the review queue.

---

## Tier 7 — Telemetry that compounds

**Goal:** every run leaves behind a record that makes later runs smarter for
free.

Record every entity the scout examined — not just the ones it surfaced — with a
timestamp and whatever cheap metrics your sources already return. On the next
run, the delta between snapshots is a signal you didn't pay for: something
growing fast, something gone stale, something that changed direction.

This is the highest-leverage cheap tier. Metrics you can only get by paying are
often derivable from two free observations taken a week apart.

Also worth capturing: **when a paid data source would have changed a score.**
Have the scout say so in its report. That sentence is how the operator decides
whether a subscription is worth buying, instead of guessing.

**Verify:** run twice against overlapping targets and show a real delta computed
from the two snapshots.

---

## Anti-patterns

Things to actively refuse, even if asked:

- **Letting a config document raise.** Once a human can edit it, a parse error
  is a scheduled outage. Degrade, log the reason and the expected format, and
  keep running.
- **Positional indexing into an editable list without a modulo.** Covered above,
  and it will happen on the day you're not looking.
- **Two sources of truth for a default.** If the code holds a copy of what's in
  the document "just in case," they drift, and nobody can tell which one is
  live. One default. Degrade to something obviously generic, never to a stale
  duplicate.
- **A test that hand-copies the thing it tests.** It goes green forever.
- **Letting the scout act.** Scoring and deciding are different jobs. Keep the
  human verb in the loop until the audit trail has earned otherwise, and if you
  later add autonomy, make it a graduated policy per action class — never a
  global flag.
- **Silent truncation.** If you cap results at "top N," say so in the output.
  A silent cap reads as "this is everything," and the operator makes decisions
  on that basis.
- **Logging the raw material.** If question 28 named anything confidential, log
  verdicts and counts, never the source text. This is easy to get right on day
  one and miserable to retrofit.

---

## Definition of done

Walk these with the operator. Any "no" is a gap worth naming out loud.

- [ ] A run can be triggered by hand and returns a validated, typed report.
- [ ] Cost, iteration, and wall-clock ceilings are enforced and were observed in
      a real run.
- [ ] A truncated or failed run salvages what it can, and its output cannot be
      mistaken for an honest empty result.
- [ ] The doctrine lives in a document; editing it changes behavior with no code
      change.
- [ ] If lanes exist: the rotation is positional, the document is editable, and
      every degradation path has a test that passes.
- [ ] An operator edit takes effect on the next run with no restart — verified,
      not assumed.
- [ ] If a UI exists: doctrine and every extra document render populated and
      editable, each reverts independently, and a full edit → revert round trip
      leaves no residue.
- [ ] The schedule is seeded idempotently and a reboot does not postpone a due
      run.
- [ ] Results reach a human surface, and an empty run still reports.
- [ ] Nothing is acted on without a human verb.
- [ ] The tests you'd need to catch a regression exist, and none of them
      hand-copy the code under test.

---

## Working notes for whoever is driving this

Build in the order above. Tiers 1 and 2 are useful alone — a scout you invoke by
hand with a file-backed prompt is already worth having, and it's the honest
place to stop if the domain turns out to be harder than expected.

The tier most people skip is 4, and it's the one that determines whether this is
alive in three months. A scout whose instructions require a deploy to change is
a scout that gets tuned twice and then ignored.

And when the scout eventually stops producing — it will, at some point — resist
the urge to guess. Check whether the run actually executed, how long it took,
and what its stop reason was. A run that "found nothing" in under a second never
ran at all, and the fix is nowhere near the rubric you were about to rewrite.
