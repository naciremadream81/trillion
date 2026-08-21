"""
The component palette — playbooks/design-subagent.md, Tier 4.

The premise this tier rests on, from the playbook's opening: "modern
award-winning design is composed from high-quality primitives, not authored
from scratch." Asking a model to write HTML has a hard quality ceiling; no
prompt engineering breaks it. So the composer gets a curated catalog of what
is actually installable, and composes from it.

**Only libraries with working CLIs are catalogued.** This is the tier's named
stumbling block: Aceternity UI and Reactbits are copy-paste only, and
"if you reference them in the catalog before pre-bundling a snapshot, CC
will try to install them and fail." They are deliberately absent below rather
than listed-with-a-caveat, because a caveat in a 4KB prompt is a caveat that
gets skimmed. When someone pre-bundles a snapshot, they go in — and
SNAPSHOT_LIBRARIES documents that path without arming it.

Two renderings, because the audiences differ: a compact one for the system
prompt (where every token is paid for on every turn) and a verbose one
written to disk for Claude Code to read on demand.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CatalogEntry:
    name: str
    use_for: str
    install: str
    library: str


SHADCN_COMPONENTS = tuple(
    CatalogEntry(name, use_for, f"npx shadcn@latest add {name}", "shadcn/ui")
    for name, use_for in (
        ("button", "every action; variants carry hierarchy"),
        ("card", "grouped content with a border and padding"),
        ("dialog", "modal interruptions that need a decision"),
        ("sheet", "side panels for secondary flows"),
        ("tabs", "switching between peer views"),
        ("input", "single-line text entry"),
        ("textarea", "multi-line entry"),
        ("form", "validated forms, wired to react-hook-form"),
        ("select", "choosing one of several"),
        ("badge", "small status and category markers"),
        ("avatar", "a person or account"),
        ("tooltip", "brief clarification on hover"),
        ("dropdown-menu", "contextual actions off a trigger"),
        ("separator", "a rule between sections"),
        ("skeleton", "loading placeholders shaped like the content"),
        ("table", "dense tabular data"),
        ("accordion", "progressive disclosure of long content"),
        ("scroll-area", "bounded scrolling with a styled bar"),
        ("switch", "an immediate on/off setting"),
        ("progress", "determinate progress"),
    )
)

MAGICUI_COMPONENTS = tuple(
    CatalogEntry(name, use_for, f"npx magicui-cli add {name}", "MagicUI")
    for name, use_for in (
        ("particles", "ambient drifting background texture"),
        ("grid-pattern", "structural background grid — a Tier 6 required element"),
        ("dot-pattern", "quieter background texture than a grid"),
        ("border-beam", "a travelling highlight around a card border"),
        ("blur-fade", "entrance reveals that don't feel like a slide deck"),
        ("text-reveal", "headline reveal tied to scroll"),
        ("marquee", "continuous horizontal motion — logos, testimonials"),
        ("animated-list", "items arriving in sequence"),
        ("bento-grid", "an asymmetric feature layout"),
        ("shimmer-button", "a CTA that draws the eye without shouting"),
        ("number-ticker", "animated counters — a Tier 6 continuous-motion option"),
        ("sparkles-text", "emphasis on a single word or phrase"),
    )
)

NPM_PACKAGES = (
    CatalogEntry(
        "framer-motion",
        "custom motion when the libraries above don't cover it",
        "npm install framer-motion",
        "npm",
    ),
    CatalogEntry(
        "lucide-react",
        "icons; already a shadcn dependency",
        "npm install lucide-react",
        "npm",
    ),
)

# Documented, deliberately NOT catalogued for the composer — see the module
# docstring. Copy-paste libraries with no CLI: referencing them makes Claude
# Code attempt an install that fails.
SNAPSHOT_LIBRARIES = (
    ("Aceternity UI", "spotlight, background-beams, tracing-beam, 3D card effects"),
    ("Reactbits", "text effects, scroll animations"),
)


def all_entries() -> list:
    return list(SHADCN_COMPONENTS) + list(MAGICUI_COMPONENTS) + list(NPM_PACKAGES)


def install_command(name: str) -> str | None:
    """The install command for one component, or None if it isn't catalogued."""
    for entry in all_entries():
        if entry.name == name:
            return entry.install
    return None


def validate_component_names(names) -> tuple:
    """
    Split requested components into (known, unknown).

    Unknown names are returned rather than raising: a hint that mentions a
    component we don't stock is a reason to drop that hint, not to fail the
    whole dispatch. The composer's prompt only ever lists the known ones,
    which is what stops Claude Code attempting an install that cannot work.
    """
    known, unknown = [], []
    catalog = {entry.name for entry in all_entries()}
    for name in names or []:
        cleaned = str(name).strip()
        if not cleaned:
            continue
        (known if cleaned in catalog else unknown).append(cleaned)
    return known, unknown


def render_for_prompt(max_per_library: int = 12) -> str:
    """
    Compact rendering for the system prompt, where every token is paid for on
    every turn. Names and install commands only — the full descriptions live
    in the on-disk catalog Claude Code can read when it needs them.
    """
    shadcn = ", ".join(e.name for e in SHADCN_COMPONENTS[:max_per_library])
    magic = ", ".join(e.name for e in MAGICUI_COMPONENTS[:max_per_library])
    return (
        "Available primitives — compose from these rather than authoring from scratch:\n"
        f"- shadcn/ui (`npx shadcn@latest add <name>`): {shadcn}\n"
        f"- MagicUI (`npx magicui-cli add <name>`): {magic}\n"
        "- npm: framer-motion, lucide-react\n"
        "Anything not listed here is not installable in this project. Do not "
        "attempt to install a library that isn't named above."
    )


def render_full_catalog_markdown() -> str:
    """
    The verbose reference written to .prism/preview/prism/component_catalog.md
    for Claude Code to read on demand.
    """
    lines = [
        "# Component catalog",
        "",
        "Everything installable in this project. Compose from these primitives;",
        "authoring equivalents from scratch has a hard quality ceiling.",
        "",
        "**Anything not listed here is not available.** Do not attempt to install",
        "a library that does not appear below — it will fail and cost a build.",
        "",
    ]
    for title, entries in (
        ("shadcn/ui", SHADCN_COMPONENTS),
        ("MagicUI", MAGICUI_COMPONENTS),
        ("npm packages", NPM_PACKAGES),
    ):
        lines += [f"## {title}", "", "| Component | Use for | Install |", "|---|---|---|"]
        lines += [f"| `{e.name}` | {e.use_for} | `{e.install}` |" for e in entries]
        lines.append("")
    lines += [
        "## Not available here",
        "",
        "These are copy-paste libraries with no CLI. They are genuinely good and",
        "are listed so nobody re-derives why they're missing — but until a local",
        "snapshot is pre-bundled into this project, an install attempt will fail.",
        "",
    ]
    lines += [f"- **{name}** — {use_for}" for name, use_for in SNAPSHOT_LIBRARIES]
    lines.append("")
    return "\n".join(lines)


def render_font_catalog_for_prompt() -> str:
    """The curated families, most distinctive first, plus what's blocked."""
    from .design_tokens import BODY_FONTS, DISPLAY_FONTS, FORBIDDEN_FAMILIES, MONO_FONTS

    return (
        f"Display: {', '.join(DISPLAY_FONTS[:6])}\n"
        f"Body: {', '.join(BODY_FONTS[:6])}\n"
        f"Mono: {', '.join(MONO_FONTS[:5])}\n"
        f"Never use (reads as default AI design, rejected at validation): "
        f"{', '.join(sorted(FORBIDDEN_FAMILIES))}"
    )
