# What Trillion knows about itself

This file is generated from agent/tools/registry.py and agent/config.py by
agent/selfknowledge — it answers "what tools exist, and what turns them on"
from the source, not from memory.

**It describes the BASELINE, not your deployment.** It is generated with no
environment read at all, so it is identical on every machine and can be
committed and checked in CI. The capabilities table therefore lists only the
unconditional tools; the config-gating section below is the interesting half,
because it lists every switch that adds more.

To see what *this* machine actually offers:

    python -m agent.selfknowledge --live

Nothing depends on this file being live-accurate. agent/system_prompt.py's
_load_self_knowledge() computes the summary from the calling Agent's own
registry every turn; the SLIM block below is only a fallback for a bare Agent
that has no registry — which has no tools, which is what the baseline says.

Everything between a block's `START`/`END` markers is rewritten by
`python -m agent.selfknowledge --refresh`; hand-written notes are safe
anywhere outside those markers.
## Capabilities

<!-- AUTO-START: capabilities -->
| Tool | Risk tier | Description |
| --- | --- | --- |
| `confirm_action` | read_only | Execute an action that was parked for Sean's confirmation, after he has explicitly agreed to it in his own message. Pass the action_id from the [CONFIRMATION REQUIRED] notice. The action runs with exactly the arguments that were shown to Sean — you cannot change them here. Only call this once he has actually said yes; calling it without his agreement is refused and logged. |
| `draft_email` | low | Compose an email draft for Sean to review and send himself. Does NOT send anything — there is no send capability here. Provide the recipient, subject, and the full body text you've written. |
| `forget_fact` | hardline | Remove a previously remembered fact from memory. Pass the fact exactly as it's stored — this is destructive and requires an exact match, no partial or fuzzy matching. |
| `remember_fact` | low | Save a durable fact about Sean or the project to memory, so future conversations start already knowing it. One plain statement per call, e.g. 'Sean prefers dry, direct answers over hedging.' Calling this again with a fact that's already saved is a no-op. |
| `search_notes` | read_only | Search Sean's notes vault by keyword. Returns matching note titles, paths, and short snippets. Read-only — there is no way to write or delete notes through this tool. |
<!-- AUTO-END: capabilities -->

## Config gating

<!-- AUTO-START: config-gating -->
- `supabase_analytics_url` — enables `query_analytics`
- `brave_search_api_key` — enables `web_search`
- `firecrawl_api_key` — enables `web_search`
- `design_agent_enabled` — enables `generate_mockup`, `list_design_projects`
- `mining_wallet` — enables `query_mining`
<!-- AUTO-END: config-gating -->

## Summary (injected into every system prompt)

<!-- SLIM-START -->
Tools currently available: `confirm_action`, `draft_email`, `forget_fact`, `remember_fact`, `search_notes`.
Unset config that would add more: `supabase_analytics_url`→query_analytics; `brave_search_api_key`→web_search; `firecrawl_api_key`→web_search; `design_agent_enabled`→generate_mockup, list_design_projects; `mining_wallet`→query_mining.
Full detail: context/self/trillion.md.
<!-- SLIM-END -->
