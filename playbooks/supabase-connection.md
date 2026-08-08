# Adding a Supabase Project to Trillion — AI Playbook

**Share this file with your AI coding assistant (Claude Code, Cursor,
Copilot, etc.) when you want to wire a new Supabase project into your
Trillion instance.** It bakes in the lessons from the first time this
was done, so the next time doesn't need the same painful detours.

The file addresses the AI directly. Paste it whole at the start of
the session, then tell the AI which Supabase project you're hooking up.

---

## You are helping the user add a new read-only Supabase project to Trillion

Your job is to walk the user through the end-to-end process of giving
Trillion query access to a new Supabase-backed Postgres database, so
she can answer questions about it by voice. You will not finish until
Trillion can actually run a query against the DB and return a row.

The user's repo is the Hello-Trillion codebase. Use any existing
`src/trillion/tools/*_tool.py` that follows the read-only Supabase
pattern as the canonical template to mirror — the shape is the
same across integrations (three actions: `query`, `list_tables`,
`describe_table`; SQL validator; row cap; JSON-safe row conversion).

## Core principles (read before acting)

1. **Read-only is non-negotiable.** Trillion must connect as a
   dedicated `trillion_analytics` Postgres role with SELECT-only
   grants and a short `statement_timeout`. Never use the default
   `postgres` superuser in production.
2. **Defense in depth.** The tool layer also validates SQL (SELECT/
   WITH only, no statement chaining) and caps rows. Both layers matter.
3. **Never ask the user to paste DB passwords in chat.** They belong
   in Doppler / `.env`, not in the transcript.
4. **Never ship without a live verification step.** You must run an
   actual connection + SELECT against the new DB before telling the
   user it works.
5. **Fail loudly with the real error.** If something breaks, paste
   the actual Python traceback / log line, don't paraphrase it.

## The playbook (execute in this order)

### Step 0 — confirm scope

Ask the user:

- What should the Supabase project be called inside Trillion? (a
  short lowercase slug with no spaces — this becomes part of the
  tool name, env var, and schema doc path)
- What is the Supabase project reference? (the 20-character ID
  visible in the Supabase dashboard URL, between `/project/` and
  the next slash)
- What tool name should Claude call? Default to `query_<slug>` if
  the user has no preference.

Get these three before writing any code.

### Step 1 — create the read-only role in Supabase

Have the user go to
`https://supabase.com/dashboard/project/<REF>/sql/new` and run the
following SQL. They pick a strong password and **must remember it**
— it is separate from their Supabase project password.

```sql
CREATE ROLE trillion_analytics LOGIN PASSWORD 'STRONG_PASSWORD_HERE';

GRANT USAGE ON SCHEMA public TO trillion_analytics;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO trillion_analytics;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT SELECT ON TABLES TO trillion_analytics;

ALTER ROLE trillion_analytics BYPASSRLS;
ALTER ROLE trillion_analytics SET statement_timeout = '5s';
```

If the role already exists and they've forgotten the password:

```sql
ALTER ROLE trillion_analytics WITH PASSWORD 'new_password_here';
```

### Step 2 — get the RIGHT connection string (the part that bites)

Supabase has three connection modes and the UI nudges you toward the
wrong one. Here's the decision tree:

- **Direct connection** (`db.<ref>.supabase.co:5432`) — on most
  modern projects this host is **IPv6-only**. Most home / office
  networks are IPv4. This fails with
  `socket.gaierror: nodename nor servname provided`. DO NOT USE.
- **Transaction pooler, dedicated** — same `db.<ref>.supabase.co`
  hostname, port `6543`, still IPv6-only on free/standard tiers.
  DO NOT USE unless the user has the paid IPv4 add-on.
- **Transaction pooler, shared** (the "IPv4 connection" toggle) —
  host is `aws-<N>-<region>.pooler.supabase.com:6543`. IPv4-routable.
  **THIS IS WHAT YOU WANT.**

Walk the user through:

1. Open
   `https://supabase.com/dashboard/project/<REF>` and click the
   **Connect** button in the top bar (not Settings).
2. In the modal, choose **Direct → Connection string** tab.
3. Under "Connection Method," select **Transaction pooler**.
4. **Flip the toggle labeled "Use IPv4 connection (Shared Pooler)"
   to ON.** This is the single most important step.
5. The URI shown will now look like
   `postgresql://postgres.<REF>:[YOUR-PASSWORD]@aws-<N>-<region>.pooler.supabase.com:6543/postgres`.
6. Two edits before saving:
   - Change `postgres.<REF>` → `trillion_analytics.<REF>`
   - Replace `[YOUR-PASSWORD]` with the password from Step 1

The final URL must have this anatomy:

```
postgresql://USER:PASSWORD@HOST:PORT/DATABASE
             │     │        │    │     │
             │     │        │    │     └── postgres
             │     │        │    └──────── 6543
             │     │        └───────────── ends in pooler.supabase.com (NOT .supabase.co)
             │     └────────────────────── the trillion_analytics password (NOT the project password)
             └──────────────────────────── trillion_analytics.<REF> (with the dot + ref suffix)
```

### Step 3 — save to Doppler

Env var name convention: `SUPABASE_<SLUG>_URL` (slug uppercased).

Do not commit the URL. Do not echo it back to the user in chat.

### Step 4 — verify the connection BEFORE writing code

This is the single step that would have saved the most time on the
first go. Run this from the repo root with Doppler injecting:

```bash
doppler run -p trillion -c dev -- uv run python -c "
import asyncio, asyncpg, os
async def main():
    url = os.environ['SUPABASE_<SLUG>_URL']
    try:
        c = await asyncio.wait_for(asyncpg.connect(url, statement_cache_size=0), timeout=10)
        row = await c.fetchrow('SELECT current_user AS user, now() AS ts')
        print('OK:', dict(row))
        tables = await c.fetch(\"SELECT table_name FROM information_schema.tables WHERE table_schema='public' ORDER BY table_name\")
        print('Tables:', [t['table_name'] for t in tables])
        await c.close()
    except Exception as e:
        print(f'FAIL ({type(e).__name__}): {str(e)[:250]}')
asyncio.run(main())
"
```

Interpret the output:

- `OK: {...}` + table list → you're good. Record the table names for
  Step 6. Proceed.
- `socket.gaierror: nodename nor servname provided` → the host is
  IPv6-only. User didn't flip the IPv4 toggle. Go back to Step 2.
- `InvalidPasswordError` → password wrong. Check they set it
  correctly in the CREATE ROLE and that Doppler has the same one.
- `InvalidAuthorizationSpecificationError` — username wrong. They
  probably left it as `postgres.<ref>` instead of
  `trillion_analytics.<ref>`.
- `permission denied for table X` → they forgot to grant SELECT.
  Re-run the GRANT statements from Step 1.

Do not proceed to Step 5 until this step returns `OK:` with a table list.

### Step 5 — create the tool (mirror the existing pattern)

Pick any existing `src/trillion/tools/*_tool.py` that already follows
the read-only Supabase pattern and copy it to a new file
`src/trillion/tools/<slug>_tool.py`. Then:

1. Rename the class to `Query<Slug>Tool` (CamelCase slug).
2. Rename the tool name in `definition()` to `query_<slug>`.
3. Rewrite the description for the project's domain, pointing at
   the schema doc you'll write in Step 6.
4. Inside `_fetch`, leave `statement_cache_size=0` as-is — the
   Supabase pooler requires it.
5. **Do not** refactor shared helpers out into a common base class
   yet. At N=2 or N=3 integrations, duplication is fine. Extract a
   `ReadOnlySupabaseBase` only when the 4th Supabase project lands.

Mirror the corresponding `tests/unit/test_*_tool.py` into
`tests/unit/test_<slug>_tool.py` — rename references. Run them to
confirm green before moving on.

Register the tool conditionally in
`src/trillion/tools/registry.py`:

```python
if settings.supabase_<slug>_url:
    from trillion.tools.<slug>_tool import Query<Slug>Tool

    self.register(Query<Slug>Tool(settings.supabase_<slug>_url))
```

Add the setting to `src/trillion/config.py` near the other
`supabase_*_url` fields:

```python
supabase_<slug>_url: str = ""  # asyncpg DSN for trillion_analytics on the <slug> DB
```

### Step 6 — write the schema doc

Create `context/<slug>-supabase-schema.md`. Any existing
`context/*-supabase-schema.md` in the repo is a valid template.
Fill in:

- Verification date.
- Table-by-table columns with types + nullability — copy these from
  the output of Step 4's `list_tables` + `describe_table` via the
  same asyncpg script. **Do not guess column names.**
- 5–10 worked query examples in the domain's language (what will
  the user actually ask about?).
- A "critical gotchas" section at the top — any non-obvious
  translations ("when the user says X, they mean table Y column Z").

Then add the new doc to `context/_manifest.toml` under `always_load`:

```toml
always_load = [
    ...
    "<slug>-supabase-schema.md",
]
```

Keep the total always-loaded context under ~3000 tokens.

### Step 7 — restart + verify end-to-end

Kill and restart Trillion. There is no hot-reload path for tools
or system prompt; a restart is mandatory.

```bash
pkill -f 'doppler run -p trillion'
doppler run -p trillion -c dev -- uv run python -m trillion
```

Watch the startup log for:
- `Registered N tools: ..., query_<slug>, ...` — proves registration.
- No `ERROR` lines during startup.

Then ask Trillion a domain question by voice — the canonical smoke
test is "how many <things> do we have?" She should call the new tool
and return a count. If she can't, grep the log for
`<slug>_tool] ERROR` and debug from there.

## Anti-patterns to refuse

Even if the user asks, do NOT:

- Use the default `postgres` role for Trillion's connection.
- Store the DB password anywhere outside Doppler (no `.env.local`
  files committed, no inline constants, no config files in
  `context/`).
- Skip the schema doc — Claude needs it to write good SQL.
- Add write-capable tools. The entire architecture is read-only by
  design; write paths belong in a separate, explicitly-confirmed
  tool if they're ever added.
- Grant access to tables you don't need (e.g. `auth.users`). Stick
  to `public` schema.

## When you're done

Reply to the user with:

1. Files changed (paths).
2. The new tool name.
3. The verification command you ran and its output.
4. The first sample voice question they should try.

Do not say "I believe this should work now." Either you saw a real
query return a real row, or you haven't finished.
