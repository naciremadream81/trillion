# Build a Head-of-Design Sub-Agent

You are about to add a head-of-design sub-agent to a multi-agent
codebase. The goal: when the orchestrator dispatches a design task,
this sub-agent should produce mockups that are **genuinely award-
quality** — not "competent but generic SaaS dark mode."

The biggest single mistake to avoid: treating mockup generation as
"ask the model to write HTML." That has a hard quality ceiling and
no amount of prompt engineering will break it. **Modern award-
winning design is composed from high-quality primitives, not
authored from scratch.** The architecture below reflects that.

Work through the tiers in order. Don't skip Tier 0 — the interview
is what keeps the rest of the work grounded in the real codebase
rather than this prompt's assumptions.

---

## Tier 0 — Interview the user (do this BEFORE any code)

Ask the user the following, one question at a time. Do not proceed
until you have answers.

1. **What is the existing agent codebase?** (Where does the
   orchestrator live? What language? What dispatch pattern do other
   sub-agents follow?)

2. **What projects will this design agent serve?** (One project?
   Multiple? How does the agent know which one it's working on?
   Most codebases need a `project_slug` → `filesystem_path` map.)

3. **Where do generated mockups live, and how are they viewed?**
   (Will mockups be served by the existing web app via an HTTP
   endpoint? Loaded into an iframe? Opened by a separate viewer?)

4. **Which model runs the agent's planning loop?** (Cheap and fast
   for planning is fine — Sonnet-class. The composition step will
   spawn Claude Code separately for the actual building.)

5. **Does the user have access to:**
   - The Claude Code CLI (`claude` binary in `$PATH`)?
   - An `ANTHROPIC_API_KEY`?
   - A `GEMINI_API_KEY` (for AI image generation — optional but
     transformative)?
   - Their secret management tool of choice (Doppler, Vault, raw
     `.env`, etc.)?

6. **What's the visual ambition?** ("Award-winning, best in class"
   means a different brief than "internal tool dashboards.")
   Don't over-promise — calibrate the brief to the user's actual
   bar.

7. **What's the per-task cost ceiling?** (Plan around it from the
   start. A scaffold-and-compose dispatch costs ~$3–6 the first
   time per project; ~$1–3 per subsequent screen. Image generation
   adds ~$0.04 per image. Set a hard cap.)

After the interview, restate what you understood and confirm before
writing any code.

---

## Tier 1 — The three-tier document model

The agent's job is to read and write three documents per project,
each with different volatility and audience.

```
<project>/
  design.md                 # PUBLIC, STABLE — the design system
  .prism/
    brief.md                # PRIVATE, EVOLVING — strategic memory
    references/<feature>/   # screenshots / reference images
    preview/                # gitignored — the per-project mockup app (Tier 2)
  features/
    <feature>.md            # PUBLIC, RAPIDLY EVOLVING — per-feature spec
    <feature>/              # historical artifacts; mockups now live in .prism/preview
```

(The agent dir name is up to you — `prism`, `design`, `head_of_design`,
whatever fits. We'll use `prism` as the example name.)

**Build:**

- `design.md` template with **a structured `\`\`\`yaml tokens` block**
  parseable by your code, plus prose for human readers. The block
  encodes fonts (Google Font names), colors (hex strings), radius,
  shadcn theme settings.
- `brief.md` template with sections: positioning, persona, business
  goals, brand language, **standing design decisions (with explicit
  forbidden moves)**, ongoing themes, bootstrap notes.
- `features/<slug>.md` template scaffold.
- A bootstrap function that, on first dispatch for a project,
  generates starter versions of design.md and brief.md from a
  short repo scan (look for `package.json`, README, existing
  `*.css` files).

**Helper modules to write:**

- `docs.py` — `resolve_project_root(slug)`, `assert_within_project()`,
  `read_project_file()`, `list_project_files()`,
  `write_design_doc()`, `write_brief()`, `write_feature_doc()`.
- `design_tokens.py` — parses the YAML block, validates it (font
  names against a curated catalog, hex colors via regex, shadcn
  base-color enum), renders `tailwind.config.ts` and
  `app/globals.css` from the parsed tokens.

**Stumbling block to avoid:** Don't let `design.md` stay TODO-shaped.
A skeleton with placeholder values produces skeleton mockups. The
bootstrap should commit to *concrete* defaults (specific font names,
specific hex colors) so the first dispatch has something to compose
against. The user can iterate from there.

---

## Tier 2 — The per-project preview app (the architectural keystone)

Every project gets a Next.js + Tailwind + shadcn application at
`<project>/.prism/preview/`. This is the substrate on which mockups
get composed. The output target IS the design ceiling — vanilla
HTML caps quality below "award-winning."

**The scaffold (~13 files) includes:**

- `package.json` pinning Next 15+, React 19+, Tailwind 3.4+
- `next.config.mjs` rendered per-project with three crucial settings:
  - `output: 'export'` (static export, no Node runtime at view time)
  - `trailingSlash: true` (each route → `out/<path>/index.html`,
    predictable for your serving endpoint)
  - `basePath` AND `assetPrefix` set to your serving URL prefix
    (e.g. `/api/<project>/preview`) so Next-emitted asset URLs
    resolve correctly when served under that prefix
- `tailwind.config.ts` wired to the design tokens (CSS variables
  from globals.css)
- `components.json` for shadcn (`base-color`, `style` from tokens)
- `lib/fonts.ts` calling `next/font/google` with the families
  picked in design.md
- `app/layout.tsx` applying the font CSS variables to `<html>`
- `app/globals.css` with Tailwind directives + shadcn CSS variables
  derived from design.md hex colors (HSL-converted)
- `app/page.tsx` — a minimal index listing generated mockups
- `lib/utils.ts` with the standard `cn()` helper
- `prism/component_catalog.md` — the full reference for what
  components are available (see Tier 3)

**Idempotent.** The scaffold function checks for `package.json`
and skips if it exists. Subsequent dispatches reuse the scaffold.

**The build flow per dispatch:**

1. Scaffold-or-skip
2. (CC: `npm install` if first dispatch, else skip)
3. (CC: `npx shadcn@latest add <names>` for components needed)
4. (CC: writes `app/<feature>/<screen>/page.tsx`)
5. (CC: `npm run build` → static export to `out/`)
6. Your serving endpoint maps:
   - `/api/<project>/preview/_next/<path>` → `out/_next/<path>`
   - `/api/<project>/preview/assets/<path>` → `out/assets/<path>` (Tier 5)
   - `/api/<project>/preview/<feature>/<screen>/` → `out/<feature>/<screen>/index.html`

**Stumbling blocks to avoid:**

- **Default Next.js static export emits `<screen>.html`, not
  `<screen>/index.html`.** Without `trailingSlash: true` your
  serving endpoint will look for the wrong path and you'll spin
  in a retry loop. The fix is the next config setting; don't
  change your serving expectations to match the default.
- **Plain `<img>` tags don't auto-prefix with Next's basePath.**
  Only `next/image` does. This burns you when you start adding
  generated images (Tier 5). Plan for it from day one — your image
  helper should return absolute URLs with the prefix baked in.
- **CSP must allow `script-src 'self' 'unsafe-inline'`.** Next's
  inlined bootstrap script needs it. If you ship a strict
  no-script CSP from the HTML-mockup era, the React runtime
  silently fails to hydrate.

---

## Tier 3 — The composer: spawn Claude Code as a subprocess

The planning agent stays cheap (Sonnet, your existing client). The
*composition* step shells out to Claude Code via subprocess. CC
runs in the project root, has narrow Bash permissions, and has
access to your component catalog as on-disk files.

**Build a shared subprocess driver:**

- `claude_code_runner.py` — a `spawn_claude_code(prompt, cwd, model,
  max_turns, allowed_tools, on_event)` that runs `claude -p
  <prompt> --output-format stream-json --verbose --model <m>
  --max-turns <n> --allowedTools <list>`, drains NDJSON, forwards
  events via `on_event`, returns a `ClaudeCodeResult` dataclass.
- Sanitize the env: pass only `ANTHROPIC_API_KEY`, `HOME`, `PATH`,
  `USER`, `LANG`, `TMPDIR`. Block other secrets from leaking into
  the child.

**Wire `generate_mockup` as a tool on your agent:**

```python
{
    "name": "generate_mockup",
    "description": "Compose a single high-fidelity screen by adding a Next.js page...",
    "input_schema": {
        "type": "object",
        "required": ["feature_slug", "screen_name", "description"],
        "properties": {
            "feature_slug": {"type": "string"},
            "screen_name": {"type": "string"},
            "description": {"type": "string"},
            "visual_direction": {"type": "string"},
            "quality": {"type": "string", "enum": ["standard", "premium"]},
            "reference_images": {"type": "array", "items": {"type": "string"}},
            "components_hint": {"type": "array", "items": {"type": "string"}},
        },
    },
}
```

The execute branch:
1. Validates inputs.
2. Parses `design.md` tokens (bail loudly if the YAML block is missing
   or invalid — don't try to compose against a half-shaped system).
3. Calls `prepare_scaffold()` if first dispatch.
4. Builds CC's `-p` prompt (heavy templating; see below).
5. Calls `spawn_claude_code(...)` with `cwd=project_root`.
6. After CC returns, verifies the expected `out/<feature>/<screen>/index.html`
   exists (this is your "did the build actually succeed" check).
7. Returns a structured `MockupResult` to the agent.

**CC's allowed_tools list** — narrow but functional:
```python
[
    "Read", "Write", "Edit", "Glob", "Grep",
    "Bash(npm install:*)",
    "Bash(npm run:*)",
    "Bash(npx shadcn:*)",
    "Bash(npx shadcn@latest:*)",
    "Bash(npx magicui-cli:*)",
    "Bash(next build:*)",
    "Bash(next export:*)",
    "Bash(ls:*)",
    "Bash(mkdir:*)",
    "Bash(cat:*)",
]
```
Avoid `Bash(*)` — too permissive. Avoid no-Bash — CC can't run
the build. Scope to the prefixes you actually need.

**CC's `-p` prompt is heavily opinionated.** It tells CC explicitly:

- Which files to read first (`design.md`, `.prism/brief.md`,
  `features/<feature>.md`)
- Which scaffold steps to run if first-dispatch (else skip)
- The full component palette + install commands (see Tier 4)
- The required visual elements (see Tier 6)
- The quality bar with concrete examples
- The forbidden moves
- The expected output file path
- The `npm run build` step

The CC prompt typically runs 4–6KB. Worth every byte.

**Stumbling block:** CC's stream events are valuable for the UI.
Forward them through `on_event` callbacks → your existing WS event
family for whatever sub-agent progress UI you have. The user wants
to see "[CC] Read design.md", "[CC] Write app/.../page.tsx",
"[CC] npm run build" as it happens.

---

## Tier 4 — The component palette (a curated catalog)

Don't make CC guess what's available. Ship a curated catalog as
both a system-prompt section and a doc CC can read on demand.

**Recommended palette** (copy this):

| Library | Install | Use for |
|---|---|---|
| **shadcn/ui** | `npx shadcn@latest add <name>` | Every primitive: button, card, dialog, sheet, tabs, input, form, etc. |
| **MagicUI** | `npx magicui-cli add <name>` | Animated heroes, marquees, sparkles, animated lists, bento grids, particles, blur-fade, text-reveal, border-beam |
| **Aceternity UI** | Copy from local snapshot | Spotlight, background-beams, tracing-beam, 3D card effects (no CLI — pre-bundle a snapshot) |
| **Reactbits** | Copy from local snapshot | Text effects, scroll animations |
| **Framer Motion** | `npm install framer-motion` | Custom motion when libraries don't cover it |

`component_catalog.py` exposes:
- `SHADCN_COMPONENTS`, `MAGICUI_COMPONENTS`, etc. as
  `list[CatalogEntry(name, use_for, install, docs)]`
- `render_for_prompt()` — compact rendering for the system prompt
- `render_full_catalog_markdown()` — verbose reference dropped
  into `.prism/preview/prism/component_catalog.md` for CC

**Stumbling block:** Aceternity and Reactbits are copy-paste only
(no CLI). If you reference them in the catalog before pre-bundling
a snapshot, CC will try to install them and fail. Either ship the
snapshot first or only catalog libraries with working CLIs.

**Google Fonts catalog** — same pattern. Curate ~20 high-quality
families across three roles (display, body, mono). Order matters:
the most distinctive choices first. **Maintain a `FORBIDDEN_FAMILIES`
set** for fonts that signal "generic AI SaaS" (Space Grotesk,
Plus Jakarta Sans, etc.) — block them at validation time.

---

## Tier 5 — AI image generation

Pure-TSX composition (waveforms, terminals, status readouts, grid
patterns, particles) gets you 80% of the way to visually rich
mockups. The last 20% needs actual imagery: atmospheric backdrops,
product UI renders, conceptual illustrations.

**Build `image_gen.py`:**

- Wraps the `google-genai` SDK
- Reads `GEMINI_API_KEY` from env
- Two quality tiers:
  - `standard` → `gemini-2.5-flash-image` (~$0.04/image, GA)
  - `premium` → `gemini-3-pro-image-preview` (~$0.12/image, preview)
- Saves PNG to `<project>/.prism/preview/public/assets/<feature>/<slug>.png`
  (Next picks them up at build time and copies to `out/assets/...`)
- Returns the **full absolute URL** with basePath baked in
  (e.g. `/api/<project>/preview/assets/<feature>/<slug>.png`) —
  NOT the bare `/assets/...` path
- Path containment via `assert_within_project`; slugs validated
  via the same kebab-case regex you use elsewhere
- Aspect ratio hinted via prompt suffix (the v2 SDK doesn't expose
  it directly for image+text models)

**Wire `generate_image` as a tool on your agent:**

The agent's job: BEFORE calling `generate_mockup`, decide what
imagery the screen needs and call `generate_image` once per image.
Then pass the resulting URLs in `visual_direction` to CC.

**Update CC's prompt** to require:
- "Use the FULL URL verbatim from `visual_direction`. Don't strip
  the basePath prefix — plain `<img>` tags don't auto-prefix."
- "If the agent generated 2 images, your TSX must reference 2
  `<img>` tags. Don't silently drop one."

**Update your serving endpoint** to handle a third route:
`/api/<project>/preview/assets/<asset_path:path>` → serves from
`out/assets/<asset_path>` with the same CSP + safety guards as
the `_next/` route.

**Stumbling blocks to avoid:**

- **Image-gen models are NOT in Gemini's free tier.** Free tier
  gives you SDK access but the model rejects with quota = 0.
  User needs billing enabled on the AI Studio project.
- **Don't use `next/image` here.** With static export +
  `unoptimized: true`, plain `<img>` is simpler and equivalent.
- **Don't return `/assets/...` from your image helper.** Plain
  `<img>` tags don't get basePath auto-prefixed; the URL must
  carry the full prefix.
- **Match the brief's palette in the image prompt.** "Cyberpunk"
  alone gets you violet + cyan defaults from any image model. Your
  prompt must repeat the forbidden colors AND name the desired
  ones. "Near-black + amber, NO violet, NO cyan" is the kind of
  specificity that gets you what the brief committed to.

---

## Tier 6 — Brief enforcement + visual element requirements

This is the tier that separates "competent" from "award-quality."

**Two non-negotiable system prompt sections:**

### "THE BRIEF IS LAW"

The brief encodes the user's standing design decisions including
explicit forbidden moves. When the user's voice request conflicts
with the brief, **the brief wins**. Surface the conflict in
`open_questions` and ASK before proceeding. Never silently
override the brief based on the user's task wording.

### "Visual elements are REQUIRED, present, continuous"

Every hero must include ALL of:

1. **Ambient background texture — VISIBLE** (opacity ≥0.4, NOT
   ≤0.25). Pick from `grid-pattern`, `dot-pattern`, `particles`.
   Layering two is encouraged.
2. **An inline product surface** showing what the product *does*
   in TSX: conversation excerpt with animated typing, voice
   waveform, command palette, status readout, code annotation
   overlay. **A hero without a product surface is incomplete.**
3. **Continuous motion — at least two running at all times**
   (not just once on load): scanline drift, breathing pulse,
   number tickers, oscillating waveform, blinking caret.
4. **Hover states on at least three elements** — not just the CTA.
5. **Three+ mono marginalia annotations** sized 14–16px (not 11px).

The visual_direction passed to CC must name specifics, not
adjectives. Bad: "premium cyberpunk hero." Good: "grid-pattern at
0.5 opacity layered with drifting amber particles, conversation
excerpt in mono with animated typing on the latest line, breathing
amber status pulse with `LISTENING · 287ms` mono label,
`blur-fade` entry on wordmark, `border-beam` on CTA hover, hover-
reveal tooltips on three mono marginalia annotations."

**Rule of thumb:** if a user looking at the hero for 3 seconds
can't tell anything is animated or moving, the page failed.

**Stumbling blocks to avoid (we hit ALL of these):**

- **Restrained brief = restrained output.** Telling the agent
  "near-invisible motion" and "≤0.25 opacity" in the brief produces
  a near-blank page. The agent will follow the brief faithfully.
  If the user wants visual richness, the brief must commit to it.
- **The user's voice cues will override the brief unless you
  enforce the priority.** "Make it sci-fi" will cancel out a brief
  that says "no sci-fi" unless your system prompt explicitly tells
  the agent the brief wins.
- **Stale feature specs anchor generation.** If the user dispatches
  a feature where `features/<slug>.md` already exists from an
  earlier failed attempt, the agent will read that stale spec and
  often pull the new attempt back to the bad direction. Be willing
  to delete stale specs before re-dispatching.
- **Component installation does not equal component USE.** The
  agent might `npx magicui-cli add particles` and never reference
  the component in the TSX. Audit the rendered page TSX, not the
  install log.

---

## Tier 7 — Reference images (the single biggest quality lever)

Words describe a vibe. An image fixes it.

**Build:**

- A `.prism/references/<feature>/` convention where users (or a
  future Scout-like agent) drop screenshots of reference designs
- A `reference_images` arg on `generate_mockup` (paths under
  `.prism/references/<feature>/`)
- Path-traversal-safe validation (no `..`, no leading `/`, must
  be `.png|.jpg|.jpeg|.webp`, must exist)
- A prompt block that tells CC: "READ THESE FIRST WITH THE Read
  TOOL. Claude has vision and will actually see the images.
  Anchor your visual decisions against them — they override
  category defaults."

**Optional Tier 7.5 — capture references from URLs.** A future
upgrade: let the user provide URLs instead of files. Spawn a
headless browser (Playwright is straightforward), screenshot each
URL, save to `.prism/references/<feature>/`, then proceed normally.
Half a day's work; transformative.

**Stumbling block:** The reference dir name must match the actual
`feature_slug` the agent uses, not what the user verbally said.
If the user says "redo the saas landing hero" but the agent
classifies it as `saas-landing-page` (its own slug choice), the
references in `saas-landing-hero/` won't be findable.

---

## What success looks like

A typical dispatch should produce:

- **Layered ambient texture** (grid + drifting particles, both
  visible at first glance)
- **Massive editorial wordmark** at proper display scale (96–140px
  with hand-tuned tracking and leading)
- **Two or three product surfaces** on one page (status readout +
  conversation excerpt + voice waveform — composed in TSX, not
  generated images)
- **Two AI-generated images** referenced via `<img>` tags (an
  atmospheric backdrop + a product render, both non-generic)
- **Continuous motion** (ticker re-rolls, particle drift, breathing
  pulse, blinking caret) — multiple things moving even when idle
- **Mono marginalia** in three+ places, sized to be seen
- **One accent color** used precisely (no fills, no decorative
  use), not the category default

Per-dispatch cost: ~$3–6 first time on a project (scaffold + npm
install + first build), ~$1–3 per subsequent screen. Image
generation adds ~$0.04–$0.12 per image.

---

## The cardinal anti-patterns (in priority order)

1. **Don't generate vanilla HTML and expect award quality.** The
   substrate caps the ceiling. Use a real component framework.

2. **Don't let the user's voice override the brief silently.**
   "BRIEF IS LAW" must be a system-prompt section, not an aspiration.

3. **Don't write a restrained brief and expect rich output.**
   Specificity in BOTH directions matters — "≤0.25 opacity" produces
   a near-invisible page just as faithfully as "vivid neon" produces
   a tacky one.

4. **Don't trust the agent to use installed components.** `npx
   <something>` running successfully is not evidence the component
   appears in the rendered page. Validate the TSX.

5. **Don't ship plain `<img src="/assets/...">` with Next.js
   basePath.** Only `next/image` auto-prefixes. Plain `<img>` needs
   the full URL with basePath baked in.

6. **Don't expect the default Next static export layout.** Set
   `trailingSlash: true` so each route emits as
   `out/<path>/index.html` — predictable for your serving endpoint.

7. **Don't forget billing.** Image-gen APIs typically have free
   tiers that exclude image models entirely. Verify before
   integrating.

8. **Don't skip the interview.** This prompt is general; your
   codebase is specific. Tier 0 keeps the rest grounded.

---

## Verification per tier

Each tier should ship independently with a verification step:

| Tier | Ship test |
|---|---|
| 1 | `parse_tokens(design.md)` returns valid `DesignTokens`. Bootstrap from a fresh project produces a parseable design.md. |
| 2 | `prepare_scaffold(tmp_path, tokens, "test")` writes 13 files; second call reports `skipped=True`. |
| 3 | Mock `spawn_claude_code` and dispatch a stub `generate_mockup` call; verify the prompt template renders correctly with all required sections. |
| 4 | The component catalog renders for prompt without throwing. The scaffolder drops `prism/component_catalog.md` into the preview app. |
| 5 | `image_gen.generate_image(...)` with a stubbed `_client` saves a PNG and returns the full URL. Live test against the real API once billing is confirmed. |
| 6 | A dispatch with a clear brief produces a page TSX that contains AT LEAST: one MagicUI background component, one product surface function, three font-mono UPPERCASE annotations, one BorderBeam-on-hover or equivalent. |
| 7 | A dispatch with reference_images causes CC to read the image files (verify in the stream events) and the resulting visual_direction Prism passed includes specific anchors derived from them. |

If a tier doesn't pass its ship test, don't move on. The lower
tiers are load-bearing for everything above.

---

## Final notes

This is the architecture and the lessons from one specific build.
Your codebase's existing conventions (dispatch pattern, secret
management, serving infrastructure, UI events) might pull some
choices in different directions. **Bias toward the existing
conventions** where they exist — only break from them when the
tradeoff is clearly worth it.

Good luck. The end state — an agent that takes "design the hero"
and ships an actually-good Next.js page with composed components,
real Google Fonts, AI-generated atmospheric imagery, and live
animations — is genuinely worth the work.
