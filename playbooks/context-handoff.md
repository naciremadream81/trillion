# Context Handoff Prompt

> **How to use:** Paste everything below the `---` into Claude Code when your context window is getting heavy. It will produce a `HANDOFF.md` file in the repo. Then run `/clear` and start your next session by saying: *"Read HANDOFF.md and confirm you're ready to continue."*

---

# Create a Context Handoff Document

We're approaching the limits of useful context in this conversation. Before I clear it, I need you to write a handoff document that lets a fresh Claude Code session pick up exactly where we left off, with no loss of momentum, no re-litigation of decisions we already made, and no repeated mistakes.

This is **not** a conversation summary. It's an engineering handoff. Write it the way a senior engineer would brief their replacement on day one of a project, focused on what matters for shipping, not what was said.

## Output

Create a file named `HANDOFF.md` in the repo root. If a `HANDOFF.md` already exists, read it first, then archive it as `HANDOFF-archive-YYYY-MM-DD-HHMM.md` before writing the new one. The new file should stand entirely on its own; assume the next session has zero memory of this conversation.

## Required Sections

Use these exact headings, in this order. Skip a section only if it genuinely doesn't apply (and say so briefly rather than omitting it).

### 1. Mission
Two or three sentences. What are we actually building or fixing, and why does it matter? Skip the corporate framing; write it like you'd describe it to another engineer over coffee.

### 2. Current State
Where things stand **right now**, in concrete terms:
- What's working and verified
- What's half-built and in what state
- What's broken or blocked, and on what
- The exact next action a fresh session should take

Be specific. "Auth is working" is useless. "Clerk sign-in works end-to-end; the `useUser()` hook returns the expected shape; `/api/protected` correctly 401s when unauthenticated, verified manually at commit `abc1234`" is useful.

### 3. Decisions Made (and Why)
Every non-obvious technical or product decision made this session. Format each as:
- **Decision:** what we chose
- **Alternatives considered:** what we rejected
- **Reason:** why we chose it
- **Reversibility:** is this load-bearing, or easy to change later?

This section is the single biggest win over `/compact`. The next session will be tempted to re-debate these. Document them well enough that it doesn't have to.

### 4. Architecture & Key Files
A map of the relevant code. For each important file or module, one line on what it does and why it exists. Call out:
- Files we created this session
- Files we modified significantly (and what changed conceptually, not line-by-line)
- Files that look like they should be touched but **shouldn't**, and why

### 5. Gotchas & Hard-Won Knowledge
The stuff that cost us time. Every dead end, every "oh that's why" moment, every footgun we tripped over. Examples:
- "Supabase RLS policies don't apply to the service role key, we learned this the hard way"
- "Next.js App Router caches `fetch` aggressively; use `{ cache: 'no-store' }` for the dashboard queries"
- "The Stripe webhook signature check fails silently if the body is parsed before verification"

If a fresh session is going to repeat a mistake without this, write it down.

### 6. Conventions In Play
Anything about *how* we're writing code in this project that isn't obvious from looking at it: naming patterns, file organization rules, commit style, testing approach, what we're deliberately *not* doing (e.g., "no unit tests yet, we're prototyping"). Reference any existing `design-principles.md`, `CLAUDE.md`, or similar files.

### 7. Open Questions
Things we deferred, didn't decide, or need user input on. Phrase each as a clear question, not a vague topic. The next session should know exactly what to ask the user about before proceeding.

### 8. Do Not Touch
Files, systems, or decisions that are settled and should not be revisited unless the user explicitly asks. This prevents the next session from "helpfully" refactoring something we deliberately left alone.

### 9. Resume Command
End the file with a short, copy-pasteable instruction the user can give the next Claude Code session to get rolling immediately. Something like:

> "Read HANDOFF.md. Then [specific next action]. Do not [specific thing to avoid]. Confirm before making changes outside [scope]."

## Quality Bar

Before you write this, do the work to make it actually useful:

1. **Re-read this conversation** with fresh eyes. What would a stranger need to know?
2. **Inspect the actual repo state.** Run `git status`, `git log --oneline -20`, check the files we discussed. Don't write from memory; write from ground truth. If memory and the filesystem disagree, the filesystem wins and you note the discrepancy.
3. **Cut ruthlessly.** Every sentence should earn its place. If removing it wouldn't hurt the next session, remove it.
4. **Be honest about uncertainty.** If you're not sure something works, say so. If we made a choice you have reservations about, note them in Decisions Made.
5. **No throat-clearing.** No "In this session we explored…" preambles. Get to the substance.

The test: if the next Claude Code session reads only `HANDOFF.md` and the repo, it should be able to make the next meaningful commit within five minutes without asking the user anything that was already settled.

When the file is written, show me the path and a one-paragraph summary of what's in it so I can sanity-check before clearing.
