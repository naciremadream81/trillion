"""
The composition prompt and dispatch — Tier 3's other half, carrying Tier 6.

Tier 6 is the tier the playbook says "separates competent from award-quality",
and it is entirely prompt: two non-negotiable sections that go into every
composition run.

**THE BRIEF IS LAW.** The brief holds standing decisions including explicit
forbidden moves. When a task's wording conflicts with the brief, the brief
wins, and the conflict gets surfaced rather than silently resolved. This is
the design equivalent of the untrusted-content rule elsewhere in this
codebase: a request does not get to quietly override a standing decision.

**Visual elements are REQUIRED.** Named, countable requirements — a visible
background texture at opacity >= 0.4 (not the >= 0.25 that reads as "did I
leave something out?"), an inline product surface, at least two continuously
running motions, hover states on three-plus elements, three-plus mono
marginalia at 14-16px. The playbook is emphatic that visual_direction must
name specifics rather than adjectives: "premium cyberpunk hero" is worthless,
"grid-pattern at 0.5 opacity layered with drifting amber particles" is a
brief.

The prompt runs 4-6KB. The playbook says it is worth every byte, and the
reason is that everything vague in it becomes something generic in the output.
"""

from __future__ import annotations

import os

from .component_catalog import render_for_prompt
from .design_tokens import DesignTokens
from .docs import BRIEF_DOC, DESIGN_DOC, FEATURES_DIR

QUALITY_LEVELS = ("standard", "premium")


def render_brief_is_law() -> str:
    return """## THE BRIEF IS LAW

`.prism/brief.md` holds standing design decisions, including an explicit list
of forbidden moves. Those decisions outrank the wording of this task.

- If this task asks for something the brief forbids, DO NOT do it. Build the
  rest, and state the conflict plainly in your final message so it can be
  settled by a human.
- If the brief is silent on something, use your judgement and say what you
  chose and why.
- Never quietly reinterpret the brief to fit the task. A task is one request;
  the brief is every request."""


def render_visual_requirements() -> str:
    return """## Visual elements are REQUIRED, present, and continuous

Every hero you compose must contain ALL of the following. These are countable
requirements, not suggestions — a screen missing any of them is incomplete.

1. **Ambient background texture, VISIBLE.** Opacity >= 0.4, not <= 0.25.
   Use `grid-pattern`, `dot-pattern`, or `particles`. Layering two is
   encouraged. A texture nobody can see is the same as no texture, and it is
   the single most common way a generated screen reads as flat.
2. **An inline product surface** — composed in TSX, showing what the product
   actually DOES. A conversation excerpt with animated typing, a voice
   waveform, a command palette, a status readout, a code block with
   annotation overlays. A hero without a product surface is incomplete.
3. **Continuous motion — at least two things running at all times.** Not
   once on load. Scanline drift, a breathing pulse, number tickers, an
   oscillating waveform, a blinking caret.
4. **Hover states on at least three elements.** Not only the CTA.
5. **Three or more mono marginalia annotations, sized 14-16px.** Not 11px —
   at 11px they read as debris rather than as deliberate.

Vague direction produces generic output. "Premium cyberpunk hero" is not a
direction. "Grid-pattern at 0.5 opacity layered with drifting amber
particles, a conversation excerpt in mono with animated typing on the latest
line, a breathing pulse on the status dot" is."""


def render_quality_bar(quality: str) -> str:
    if quality == "premium":
        return """## Quality bar — PREMIUM

This screen should stand up next to the best work on Godly or Awwwards.
Asymmetry over symmetry. Type does the hierarchy before colour does. Density
where density earns attention. If a section could appear on any SaaS landing
page, it is wrong — rebuild it."""
    return """## Quality bar — STANDARD

Clean, considered, and specific to this product. Real type scale, restrained
palette, no filler sections. Better than default output, without the time a
premium pass takes."""


def render_forbidden_moves() -> str:
    return """## Forbidden

- No generic gradient-on-dark SaaS hero.
- No three-column icon + heading + two-lines feature grid.
- No stock-photo people at laptops.
- No bento grid used as a substitute for hierarchy.
- No lorem ipsum — write real copy for this product.
- Do not install any library not listed in the palette above; it will fail."""


def build_composition_prompt(
    *,
    project_slug: str,
    feature_slug: str,
    screen_name: str,
    description: str,
    tokens: DesignTokens,
    visual_direction: str = "",
    quality: str = "standard",
    components_hint=None,
    reference_images=None,
    image_urls=None,
    first_dispatch: bool = False,
) -> str:
    """The full -p prompt for one composition run."""
    feature_doc = os.path.join(FEATURES_DIR, f"{feature_slug}.md")
    output_path = f".prism/preview/app/{feature_slug}/{screen_name}/page.tsx"
    built_path = f".prism/preview/out/{feature_slug}/{screen_name}/index.html"

    parts = [
        f"# Compose one screen: {feature_slug}/{screen_name}",
        "",
        f"You are the head of design for **{project_slug}**. Compose a single "
        "high-fidelity screen as a Next.js page in the existing preview app.",
        "",
        "## Read these first, in this order",
        f"1. `{DESIGN_DOC}` — the design system. The ```yaml tokens block is authoritative.",
        f"2. `{BRIEF_DOC}` — standing decisions and forbidden moves.",
        f"3. `{feature_doc}` — what this feature needs (may not exist yet; that's fine).",
        "",
        "## The screen",
        description.strip(),
        "",
    ]

    if visual_direction.strip():
        parts += ["## Visual direction", visual_direction.strip(), ""]

    if image_urls:
        parts += [
            "## Generated imagery",
            "Use these URLs **verbatim**. Do not strip the path prefix — plain "
            "`<img>` tags are not auto-prefixed with basePath, so a shortened "
            "URL 404s. One `<img>` per URL; do not silently drop one.",
        ]
        parts += [f"- `{url}`" for url in image_urls]
        parts.append("")

    if reference_images:
        parts += [
            "## Reference images — READ THESE FIRST WITH THE Read TOOL",
            "You have vision and will actually see them. Anchor your visual "
            "decisions against what is in these images: spacing, density, type "
            "scale, where the eye lands first, how much room is left empty. "
            "**They override category defaults** — if a reference contradicts "
            "what a screen of this kind usually looks like, follow the "
            "reference. Do not copy their content or their copy.",
        ]
        parts += [f"- `{path}`" for path in reference_images]
        parts.append("")

    parts += [
        "## Design tokens (from design.md — do not invent alternatives)",
        f"- Display font: {tokens.fonts.get('display')} (`font-display`)",
        f"- Body font: {tokens.fonts.get('body')} (`font-sans`)",
        f"- Mono font: {tokens.fonts.get('mono')} (`font-mono`)",
        f"- Colours: {', '.join(f'{k} {v}' for k, v in tokens.colors.items())}",
        "Use the Tailwind tokens (`bg-background`, `text-foreground`, "
        "`text-accent`, `border-border`), not raw hex values.",
        "",
        "## Component palette",
        render_for_prompt(),
        "",
    ]

    if components_hint:
        parts += [
            "Components likely useful here: " + ", ".join(components_hint),
            "Install what you use before importing it.",
            "",
        ]

    if first_dispatch:
        parts += [
            "## First dispatch for this project",
            "The scaffold exists but its dependencies do not. Run, in order:",
            "1. `npm install` in `.prism/preview/`",
            "2. `npx shadcn@latest add <component>` for each shadcn component you need",
            "3. `npx magicui-cli add <component>` for each MagicUI component you need",
            "",
        ]
    else:
        parts += [
            "## Not the first dispatch",
            "`node_modules` already exists — do NOT run `npm install` again. Only "
            "install components you actually need and that aren't installed yet.",
            "",
        ]

    parts += [
        render_brief_is_law(),
        "",
        render_visual_requirements(),
        "",
        render_quality_bar(quality),
        "",
        render_forbidden_moves(),
        "",
        "## Output",
        f"1. Write the page to `{output_path}`.",
        "2. Run `npm run build` in `.prism/preview/`.",
        f"3. Confirm `{built_path}` exists. That file is the deliverable — if the "
        "build fails, fix it and build again rather than reporting success.",
        "",
        "Finish with a short summary: what you built, which components you "
        "installed, and any conflict you found between this task and the brief.",
    ]
    return "\n".join(parts)
