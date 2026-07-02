# Analytics database (Supabase) — schema

**Verified:** 2026-07-01 against the live database via `trillion_analytics` (read-only).
Query it with the `query_analytics` tool (actions: `query`, `list_tables`, `describe_table`).

## Critical gotchas
- This is **read-only**. Only `SELECT` / `WITH` queries work. Never attempt writes.
- When Sean says "**people**", "**leads**", or "**who**", he means the `contacts` table.
- Results are capped at 200 rows — use `count(*)` or `LIMIT` for large questions.

## Tables

### `contacts`
People / leads.

| column | type | nullable |
|---|---|---|
| `id` | bigint | no |
| `full_name` | text | no |
| `email` | text | yes |
| `company` | text | yes |
| `created_at` | timestamptz | no |

## Example questions → SQL

- **"How many contacts do we have?"**
  `SELECT count(*) FROM contacts;`
- **"List everyone."**
  `SELECT full_name, company FROM contacts ORDER BY full_name;`
- **"Who's at NASA?"**
  `SELECT full_name, email FROM contacts WHERE company ILIKE '%nasa%';`
- **"How many contacts have an email on file?"**
  `SELECT count(*) FROM contacts WHERE email IS NOT NULL;`
- **"Contacts by company."**
  `SELECT company, count(*) AS n FROM contacts GROUP BY company ORDER BY n DESC;`
- **"Most recently added contact."**
  `SELECT full_name, created_at FROM contacts ORDER BY created_at DESC LIMIT 1;`
- **"How many contacts were added this month?"**
  `SELECT count(*) FROM contacts WHERE created_at >= date_trunc('month', now());`
