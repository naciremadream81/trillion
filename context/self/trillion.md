# What Trillion knows about itself

This file is generated from agent/tools/registry.py and agent/config.py by
agent/selfknowledge — it answers "what tools do you have, and what turns
them on" from the source, not from memory. Everything between a block's
`START`/`END` markers below is rewritten by `python -m agent.selfknowledge
--refresh`; hand-written notes are safe anywhere outside those markers.

## Capabilities

<!-- AUTO-START: capabilities -->
| Tool | Risk tier | Description |
| --- | --- | --- |
| `confirm_action` | read_only | Execute an action that was parked for Sean's confirmation, after he has explicitly agreed to it in his own message. Pass the action_id from the [CONFIRMATION REQUIRED] notice. The action runs with exactly the arguments that were shown to Sean — you cannot change them here. Only call this once he has actually said yes; calling it without his agreement is refused and logged. |
| `draft_email` | low | Compose an email draft for Sean to review and send himself. Does NOT send anything — there is no send capability here. Provide the recipient, subject, and the full body text you've written. |
| `forget_fact` | hardline | Remove a previously remembered fact from memory. Pass the fact exactly as it's stored — this is destructive and requires an exact match, no partial or fuzzy matching. |
| `generate_mockup` | consequential | Compose one high-fidelity screen for a project as a real Next.js page, built against its design system. Use when Sean asks for a mockup, a screen, a landing page, or a redesign of something in generated-projects/. This spawns Claude Code to install components, write the page, and run a build — it costs real money and takes minutes, so compose one screen per call and describe it properly rather than guessing. |
| `list_design_projects` | read_only | List the projects the design agent can work on, and whether each has a design system set up yet. Use before generate_mockup when Sean hasn't named a project, or when he asks what you could design. |
| `remember_fact` | low | Save a durable fact about Sean or the project to memory, so future conversations start already knowing it. One plain statement per call, e.g. 'Sean prefers dry, direct answers over hedging.' Calling this again with a fact that's already saved is a no-op. |
| `search_notes` | read_only | Search Sean's notes vault by keyword. Returns matching note titles, paths, and short snippets. Read-only — there is no way to write or delete notes through this tool. |
<!-- AUTO-END: capabilities -->

## Config gating

<!-- AUTO-START: config-gating -->
- `supabase_analytics_url` — enables `query_analytics`
- `firecrawl_api_key` — enables `web_search`
- `mining_wallet` — enables `query_mining`
<!-- AUTO-END: config-gating -->

## Summary (injected into every system prompt)

<!-- SLIM-START -->
Tools currently available: `confirm_action`, `draft_email`, `forget_fact`, `generate_mockup`, `list_design_projects`, `remember_fact`, `search_notes`.
Unset config that would add more: `supabase_analytics_url`→query_analytics; `firecrawl_api_key`→web_search; `mining_wallet`→query_mining.
Full detail: context/self/trillion.md.
<!-- SLIM-END -->
