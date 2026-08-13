# Incident runbook

For the first 30 minutes of a real incident. Read this file — don't search docs.

## Universal first moves

```bash
# 1. Stop Trillion from making any more tool calls. Immediate, no restart needed.
#    (In a running CLI session you can also just type /pause.)
echo "TRILLION_PAUSED=true" >> .env
# Persist it for the next restart too — the line above only edits .env;
# if Trillion is already running, also flip it live:
#   /pause                       (CLI)
#   set TRILLION_PAUSED=true and restart serve.py   (web)
# The kill switch stops gated actions, background dispatch, and Software
# Factory builds. Conversation and read-only tools keep working — see
# agent/config.py's Settings.trillion_paused docstring for the exact scope.

# 2. Capture forensic state before anything else changes.
mkdir -p /tmp/trillion-incident
git log --oneline -30 > /tmp/trillion-incident/git-log-$(date +%s).txt
cp safety.db /tmp/trillion-incident/safety-$(date +%s).db 2>/dev/null
cp heartbeat.db /tmp/trillion-incident/heartbeat-$(date +%s).db 2>/dev/null
# If you run Trillion under systemd (not required by this repo — only the
# rclone vault mount ships a unit, see CLAUDE.md), also grab:
#   journalctl -u <your-unit> --since "2 hours ago" > /tmp/trillion-incident/journal.txt
# Otherwise capture whatever you redirected serve.py's stdout/stderr to.

# 3. Check what was actually gated/executed recently.
#    CLI:  /audit              (recent audit log: pause/resume, approvals, denials)
#          /pending-actions    (anything still parked awaiting a yes)
```

## "Trillion is doing something I didn't ask for"

1. **Kill switch first** — run step 1 above. This is the single fastest way
   to stop further action; it does not require finding *why* yet.
2. **Disable any user-defined scheduled/autonomous work:**
   - Software Factory autonomous builds: set `TRILLION_FACTORY_PAUSED=true`
     and/or clear `TRILLION_FACTORY_AUTONOMOUS_THEMES` in `.env`, then
     restart. `factory_paused` is a child scope of the main kill switch —
     see `agent/config.py:174`.
   - Heartbeat checks (Code Sentinel, CVE scan) keep running read-only
     checks even while paused by design; if you need them off too, unset
     `TRILLION_GITHUB_WATCHED_REPOS` / remove `GITHUB_TOKEN`.
3. **Find the trigger.** There is no persisted chat log (`agent/core.py`
   keeps `self.history` in-memory only, cleared on restart), so:
   - `/audit` (CLI) for anything that went through the confirmation gate.
   - `/pending-actions` for anything still parked — read its `summary`
     field, it's the frozen intent recorded at gate time.
   - If a scheduled build or heartbeat check kicked things off, `/builds`
     (Software Factory) or the heartbeat notices panel in the web UI
     (`GET /api/heartbeat/notices`) will show what ran and when.
   - Check the security shield (`GET /api/security/status`, or the shield
     pill in the UI) for `gate-coverage` and `approval-mode` — a mode that
     silently dropped to `off` explains a lot.
4. Once you understand the trigger, `/resume` (or unset `TRILLION_PAUSED`)
   only after you've fixed the root cause, not before.

## Credentials

### ANTHROPIC_API_KEY leaked

**Blast radius:** unlimited Claude API usage billed to your account. No
access to your other services — this key only talks to Anthropic's API.

1. https://console.anthropic.com/settings/keys → revoke the key, generate a
   new one.
2. Update `ANTHROPIC_API_KEY` in `.env` (or your secrets manager) in dev and
   prod.
3. Restart Trillion (`trillion serve` / `python serve.py` / `python main.py`).
4. Send a normal chat message to confirm the new key works.
5. Audit usage at https://console.anthropic.com/settings/usage. Any spend or
   volume you don't recognize = the abuse window.

### OPENAI_API_KEY leaked

**Blast radius:** unlimited OpenAI (or OpenRouter, if that's how the key is
scoped) API usage billed to your account. Only relevant if
`TRILLION_PROVIDER=openai`.

1. https://platform.openai.com/api-keys (or your OpenRouter dashboard) →
   revoke, generate a new one.
2. Update `OPENAI_API_KEY` in `.env` in dev and prod.
3. Restart Trillion.
4. Send a normal chat message to confirm the new key works.
5. Audit usage at https://platform.openai.com/usage. Anything unexpected =
   abuse window.

### DEEPGRAM_API_KEY leaked

**Blast radius:** Deepgram transcription quota/billing abuse. No access to
anything else — voice input just stops working until replaced.

1. https://console.deepgram.com/ → project settings → revoke the key,
   generate a new one.
2. Update `DEEPGRAM_API_KEY` in `.env` in dev and prod.
3. Restart Trillion.
4. Test a voice turn (tap-to-speak) to confirm STT still works.
5. Audit usage/logs in the Deepgram console. Anything unexpected = abuse
   window.

### BRAVE_SEARCH_API_KEY leaked

**Blast radius:** Brave Search API quota abuse (free tier is 2,000
queries/month — an attacker can burn that fast). No other access.

1. https://api-dashboard.search.brave.com/ → revoke, generate a new key.
2. Update `BRAVE_SEARCH_API_KEY` in `.env` in dev and prod.
3. Restart Trillion.
4. Trigger a `web_search` tool call (or wait for the opportunity scout) to
   confirm the new key works.
5. Audit usage in the Brave dashboard. Anything unexpected = abuse window.

### FIRECRAWL_API_KEY leaked

**Blast radius:** Firecrawl crawl/scrape quota and billing abuse. No other
access. (If you run a self-hosted Firecrawl via `FIRECRAWL_BASE_URL`, this
key may not even be required — check your instance's auth config instead.)

1. https://www.firecrawl.dev/ → dashboard → revoke, generate a new key.
2. Update `FIRECRAWL_API_KEY` in `.env` in dev and prod.
3. Restart Trillion.
4. Trigger a `web_search` tool call to confirm the new key works.
5. Audit usage in the Firecrawl dashboard. Anything unexpected = abuse
   window.

### GITHUB_TOKEN leaked

**Blast radius:** depends entirely on the scope you granted. Trillion's
Code Sentinel (`agent/heartbeat/github_client.py`) only ever makes read
calls — check-runs, pull requests, Dependabot alerts, branches,
participation stats — so a correctly-scoped fine-grained read-only token
leaking exposes repo metadata, nothing you can mutate. If you granted a
broader classic PAT, the blast radius is whatever that scope allows —
treat it as compromised for everything the scope covers, not just what
Trillion happens to use.

1. https://github.com/settings/tokens (or Fine-grained tokens) → revoke the
   token, generate a new one scoped read-only to exactly the repos in
   `TRILLION_GITHUB_WATCHED_REPOS`.
2. Update `GITHUB_TOKEN` in `.env` in dev and prod.
3. Restart Trillion.
4. Wait for (or manually trigger) the next heartbeat tick and check
   `/api/heartbeat/notices` for a fresh Code Sentinel result.
5. Audit token usage at https://github.com/settings/security-log. Anything
   unexpected = abuse window.

### TRILLION_WEB_AUTH_TOKEN leaked

**Blast radius: read this one carefully — it's not what you'd expect.**
`TRILLION_WEB_AUTH_TOKEN` is currently only checked at *startup*, by
`check_bind_safety()` (`agent/security/startup_guard.py`), to decide whether
Trillion is allowed to bind a non-loopback host at all. **There is no
request-time auth middleware yet** — no code path on `/api/chat` or any
other route actually validates an incoming `Authorization: Bearer <token>`
header. Rotating this token does not revoke anyone's access, because
nothing was checking it per-request in the first place.

Practically: if this leaked *and* `web_host` is bound non-loopback, treat it
the same as "the whole API is unauthenticated on the open network" — because
right now, it is. The token's presence only unlocked the *decision* to bind
publicly; it isn't the thing standing between a request and `/api/chat`.

1. **Rebind to loopback immediately** — set `TRILLION_WEB_HOST=127.0.0.1`
   (or remove the override) and restart. This is the only step that
   actually closes the door today.
2. Generate a new token anyway (`python -c "import secrets;
   print(secrets.token_urlsafe(32))"`) and update `.env`, so that whenever
   request-time auth middleware ships, the old value is already dead.
3. Review `/audit` and `/pending-actions` for anything gated that ran or
   was approved during the suspected leak window — that's the only signal
   available today, since there's no per-request access log to check.
4. This gap is real and worth fixing properly (an origin/bearer-check
   middleware), not just working around during an incident — see the
   security shield's `csrf-origin-gate` signal, which already reports
   `absent` for the same reason.

## After

Once the leak is closed and the trigger understood:

- `/resume` (or unset `TRILLION_PAUSED`) to lift the kill switch.
- Re-enable any autonomous work you disabled in step 2 of the "misbehaving
  agent" section, if it wasn't the cause.
- Note what happened somewhere durable (a dated entry, a memory fact via
  Tier 4) so the next incident isn't a cold start either.
