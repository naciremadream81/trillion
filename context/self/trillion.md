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
| `draft_email` | low | Compose an email draft for Sean to review and send himself. Does NOT send anything — there is no send capability here. Provide the recipient, subject, and the full body text you've written. |
| `search_notes` | read_only | Search Sean's notes vault by keyword. Returns matching note titles, paths, and short snippets. Read-only — there is no way to write or delete notes through this tool. |
<!-- AUTO-END: capabilities -->

## Config gating

<!-- AUTO-START: config-gating -->
- `supabase_analytics_url` — enables `query_analytics`
- `brave_search_api_key` — enables `web_search`
- `firecrawl_api_key` — enables `web_search`
<!-- AUTO-END: config-gating -->

## Summary (injected into every system prompt)

<!-- SLIM-START -->
Tools currently available: `draft_email`, `search_notes`.
Unset config that would add more: `supabase_analytics_url`→query_analytics; `brave_search_api_key`→web_search; `firecrawl_api_key`→web_search.
Full detail: context/self/trillion.md.
<!-- SLIM-END -->
