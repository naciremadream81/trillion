You are installing the Chief of Staff skill for me from
https://github.com/lightbulbmomentlabs/chief-of-staff.

Follow these steps. Be efficient: ask one question at a time, prefer
multiple-choice when possible, and always show me file contents before
writing them to disk.

# Step 1: Pick install location

Ask me where to put the skill files. Options:
- A. ~/.claude/skills/chief-of-staff/ (global, available in every Claude
     Code session). Recommended.
- B. ./.claude/skills/chief-of-staff/ (project-only, only in the current
     directory).
- C. Custom path I provide.

Create the directory if it doesn't exist.

# Step 2: Download the skill

Fetch these two files from GitHub and save them to the install path:
- https://raw.githubusercontent.com/lightbulbmomentlabs/chief-of-staff/main/SKILL.md
- https://raw.githubusercontent.com/lightbulbmomentlabs/chief-of-staff/main/north-star-template.md

Use WebFetch + Write, or curl via Bash, whichever you have permissions
for. After saving, confirm both files exist and have non-zero size.

# Step 3: Help me write my north-star

The skill hard-fails without a doctrine doc. We're writing one now.

Read the template you just downloaded so you know the structure
(frontmatter + body sections). Then ask me, one question at a time:

1. What's the product or business this north star is for? (one short
   name, used in the frontmatter `product` field)

2. What's your revenue target and target date? Examples: "$500K ARR by
   Q4-2026", "$1M ARR by 2027-Q1", "$10K MRR by end of August". This
   becomes the verbatim `target` line in the brief.

3. The skill uses a 90-day filter with four dimensions you tag work
   against. Default: `sales, delivery_speed, margin, retention`. Pick:
   - A. Use the defaults
   - B. Customize (I'll provide my four)

4. In one paragraph: what's your primary objective for the next 90
   days? Be concrete: "land 10 paying customers in the operator
   segment" beats "build a great product." This is the body's
   `## Objective` section.

5. List 5–8 operating rules. These are the *constraints* the skill
   uses to flag anti-priorities. Each rule must be concrete enough
   that a queued task could clearly violate it. Examples: "do not
   sell hours, ever"; "no partner relationships before $50K MRR";
   "every shipped feature must resolve a paying-customer pain in the
   same week"; "annual contracts only, no monthly self-serve at our
   price point."

   If I'm stuck, help me by asking pointed questions about what I've
   said no to recently and why.

6. (Optional) Commercial ladder, the stages a customer moves through.
   Examples: "Free trial → Pro → Org → Enterprise". Skip if not
   relevant to my business.

Once you have all answers, draft the full `north-star.md` using the
template's structure. Set `last_reviewed` to today's date in
YYYY-MM-DD format. Leave the Errata block empty, I'll add to it later
when doctrine drifts. Show me the complete file before writing.

Save it to `./north-star.md` in my current working directory (NOT the
skills directory, the skill will look for it from the working
directory).

# Step 4: Run my first brief

Now run the skill end-to-end, exactly as specified in the SKILL.md you
downloaded. Produce a real Chief of Staff brief from the doctrine we
just wrote. Use today's date.

# Hard rules

- Never write a file without showing me its full contents first.
- If I ask to skip Step 3, refuse. The skill hard-fails without a
  doctrine doc and producing a generic brief defeats the entire
  point. If I'm stuck on the doctrine, help me think it through
  rather than skipping.
- Keep questions tight. One at a time. Multiple-choice when possible.
- After Step 2, you may need to restart your Claude Code session for
  the skill to be auto-discovered. Tell me if so.
