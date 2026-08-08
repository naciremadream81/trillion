# Hybrid Setup Prompt

Paste the prompt below into Claude Code (or your AI coding assistant of choice) when you're ready to move your local AI assistant's memory to the cloud. The prompt is intentionally generic, and works for any app that uses Postgres for persistence, not just voice assistants.

---

## The Prompt

You're going to walk me through migrating my AI assistant's local Postgres database to a hybrid setup where the database lives on a cloud VPS (DigitalOcean Droplet). The goal: my assistant remembers me identically across every device I use, instead of being tied to one machine.

### Before you start, ask me

Don't proceed until I've answered all of these:

1. What language / framework is the app built in (Python + FastAPI, Node, Rails, Go, other)?
2. Where is the Postgres connection currently configured (env file, config module, hard-coded)?
3. Do I have an existing local Postgres database with data I want to keep, or am I starting fresh?
4. Do I already have a DigitalOcean account and an SSH key uploaded to it, or do we need to set those up first?
5. Do I want to point a domain at the droplet, or use the bare IP?

### The plan you'll execute with me

#### 1. Provision a DigitalOcean Droplet
- Recommend a sensible size (Basic / Regular Intel, 2 GB RAM, 1 vCPU is plenty to start).
- Pick the region closest to me.
- Use SSH key authentication only, never password auth.
- Default to Ubuntu 24.04 LTS unless I push back.

#### 2. Harden the droplet before installing anything
- Create a non-root user with sudo.
- Enable UFW. Open only port 22 (SSH) and port 5432 (Postgres), nothing else.
- Configure UFW to accept Postgres connections only from my home/office IP (defense in depth: TLS + password is still the real lock).
- Enable `unattended-upgrades` for automatic security patches.

#### 3. Install and configure Postgres
- Install the latest stable Postgres from the official apt repo (not the Ubuntu default).
- Create a dedicated DB user for the app with a strong, randomly-generated password. Have me copy the password directly into a password manager, never echo it back to me in chat.
- Create the app's database, owned by that user.
- Enable TLS: set `ssl = on` in `postgresql.conf` and either generate a self-signed cert or use Let's Encrypt if I pointed a domain at the droplet.
- Edit `pg_hba.conf` so all remote connections require `hostssl` with password.
- Set `listen_addresses = '*'` (safe because UFW is the gatekeeper).
- Restart Postgres and verify it's actually listening on the public interface.

#### 4. Migrate existing data (only if I have local data to keep)
- `pg_dump` from local → restore into the cloud DB with `pg_restore` or `psql -f`.
- Verify row counts match table-by-table before and after.
- Do not drop the local DB. Keep it as a fallback for at least a few days.

#### 5. Update the app to point at the cloud
- Find every place in the codebase that builds a Postgres connection string.
- Move all credentials to environment variables, never hard-coded.
- Add `sslmode=require` (or stricter) to the connection string.
- Update `.env.example` (or equivalent) so future-me knows which variables to set.
- Keep the local-mode connection working behind a flag so I can switch back instantly if something breaks.

#### 6. Verify end-to-end
- From my laptop: `psql "postgresql://..."` should connect cleanly.
- Run the app pointing at the cloud DB. Trigger whatever creates state (a conversation, a task, a memory write) and confirm the row lands in the cloud DB.
- Drop my network, reconnect, restart the app, then confirm state restores.
- Test from a second device if I have one available.

#### 7. Document what we did
- Add a "Cloud Postgres setup" section to the project README capturing: droplet hostname, DB name + user, where the password lives, the connection string template, and the rollback steps.

### Hard rules

- **Never paste my passwords or SSH keys into chat output.** Generate them in my terminal or on the droplet directly and have me put them straight into a password manager.
- **Never open ports beyond 22 and 5432.** No bare HTTP, no Postgres-over-internet without TLS.
- **Never migrate data destructively.** Dump first, restore second, verify counts third.
- **Test connectivity before changing app config.** `psql` from my laptop must succeed before I touch any code.
- **Confirm with me before any destructive step:** rebooting the droplet, dropping a DB, force-pushing config files.

### When you're done

Report back with:
- The final connection string format (password redacted).
- The exact files you changed and the one-line reason for each.
- A list of follow-up tasks I should plan separately: scheduled backups, monitoring/alerts, secret rotation, off-site backup mirror, point-in-time recovery setup.
