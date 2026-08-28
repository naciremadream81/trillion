"""
The three-tier document model — playbooks/design-subagent.md, Tier 1.

Three documents per project, each with different volatility and audience:

  design.md              PUBLIC, STABLE        the design system
  .prism/brief.md        PRIVATE, EVOLVING     strategic memory
  features/<slug>.md     PUBLIC, FAST-MOVING   per-feature spec

The split is the point. A design system that churns every dispatch isn't a
system; a brief that never changes can't hold what was learned last time.

**Containment.** Every path here goes through resolve_in_sandbox() from
agent/tools/project_fs.py — the same jail the Software Factory already uses,
deliberately not a second implementation. The design agent spawns Claude Code
to write files (Tier 3), so "which directory is it allowed to touch" is the
load-bearing question of this whole feature, and it should have exactly one
answer that is already tested.

**No TODO-shaped bootstrap.** The playbook's stumbling block for this tier:
"A skeleton with placeholder values produces skeleton mockups." The bootstrap
commits to concrete fonts and hex colours so the very first dispatch has
something real to compose against. Sean edits from there.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

from ..tools.project_fs import PathEscape, resolve_in_sandbox

# Project and feature slugs. Kebab-case only, and the length cap matters:
# these become directory names and, via the preview app, URL segments.
SLUG_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
MAX_SLUG_LENGTH = 64

PRISM_DIR = ".prism"
DESIGN_DOC = "design.md"
BRIEF_DOC = os.path.join(PRISM_DIR, "brief.md")
REFERENCES_DIR = os.path.join(PRISM_DIR, "references")
PREVIEW_DIR = os.path.join(PRISM_DIR, "preview")
FEATURES_DIR = "features"


class DesignDocError(RuntimeError):
    pass


def validate_slug(slug: str, what: str = "slug") -> str:
    """Kebab-case or refuse. Raises rather than sanitizing: a silently
    rewritten slug means the agent and the filesystem disagree about where
    a project lives, which surfaces much later as a confusing empty read."""
    cleaned = (slug or "").strip()
    if not cleaned:
        raise DesignDocError(f"{what} is required.")
    if len(cleaned) > MAX_SLUG_LENGTH:
        raise DesignDocError(f"{what} is longer than {MAX_SLUG_LENGTH} characters.")
    if not SLUG_PATTERN.match(cleaned):
        raise DesignDocError(
            f"{what} must be kebab-case (lowercase letters, digits, single hyphens): {slug!r}"
        )
    return cleaned


def projects_root(settings=None) -> str:
    """
    The one directory design work may touch.

    Deliberately the Software Factory's root rather than a new one: these are
    the projects being designed, the jail is already configured and tested,
    and a second root would be a second thing to get wrong.
    """
    if settings is None:
        from ..config import get_settings

        settings = get_settings()
    return os.path.abspath(settings.software_factory_root)


def resolve_project_root(slug: str, settings=None) -> str:
    """Absolute path to a project, refusing anything outside the root."""
    slug = validate_slug(slug, "project slug")
    root = projects_root(settings)
    try:
        return resolve_in_sandbox(root, slug)
    except PathEscape as e:
        raise DesignDocError(str(e)) from e


def assert_within_project(project_root: str, relative_path: str) -> str:
    """Resolve a path inside one project, or raise."""
    try:
        return resolve_in_sandbox(project_root, relative_path)
    except PathEscape as e:
        raise DesignDocError(str(e)) from e


def list_projects(settings=None) -> list:
    """Project slugs available to design for."""
    root = projects_root(settings)
    if not os.path.isdir(root):
        return []
    return sorted(
        name
        for name in os.listdir(root)
        if os.path.isdir(os.path.join(root, name)) and SLUG_PATTERN.match(name)
    )


def read_project_file(project_root: str, relative_path: str) -> str | None:
    path = assert_within_project(project_root, relative_path)
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except (OSError, UnicodeDecodeError):
        return None


def write_project_file(project_root: str, relative_path: str, content: str) -> str:
    path = assert_within_project(project_root, relative_path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return path


def list_project_files(project_root: str, relative_dir: str = "", limit: int = 200) -> list:
    """Relative paths under a project directory, bounded."""
    base = assert_within_project(project_root, relative_dir) if relative_dir else project_root
    if not os.path.isdir(base):
        return []
    found = []
    for dirpath, dirnames, filenames in os.walk(base):
        # node_modules and build output are enormous and never interesting to
        # a design read. Pruned in-place so os.walk doesn't descend into them.
        dirnames[:] = [
            d for d in dirnames if d not in {"node_modules", ".next", "out", ".git"}
        ]
        for name in filenames:
            found.append(os.path.relpath(os.path.join(dirpath, name), project_root))
            if len(found) >= limit:
                return sorted(found)
    return sorted(found)


# ── Bootstrap templates ──────────────────────────────────────────────────
# Concrete values, never TODOs. See the module docstring.

@dataclass
class ProjectScan:
    """What a short repo scan turned up, used to seed the bootstrap."""

    name: str = ""
    description: str = ""
    has_package_json: bool = False
    css_files: list = None

    def __post_init__(self) -> None:
        if self.css_files is None:
            self.css_files = []


def scan_project(project_root: str) -> ProjectScan:
    """
    Short scan to seed the bootstrap — package.json name/description, README
    first paragraph, any existing CSS. Best-effort throughout: a project that
    is just a README should still bootstrap.
    """
    import json

    scan = ProjectScan(name=os.path.basename(project_root.rstrip(os.sep)))

    raw = read_project_file(project_root, "package.json")
    if raw:
        scan.has_package_json = True
        try:
            data = json.loads(raw)
            scan.name = str(data.get("name") or scan.name)
            scan.description = str(data.get("description") or "")
        except (ValueError, TypeError):
            pass

    if not scan.description:
        for candidate in ("README.md", "readme.md", "README"):
            readme = read_project_file(project_root, candidate)
            if readme:
                for block in readme.split("\n\n"):
                    text = block.strip()
                    if text and not text.startswith(("#", "!", "[", "```", "|", "-")):
                        scan.description = " ".join(text.split())[:300]
                        break
                break

    scan.css_files = [p for p in list_project_files(project_root, limit=400) if p.endswith(".css")]
    return scan


def render_design_doc(scan: ProjectScan, tokens_yaml: str) -> str:
    """design.md — the parseable tokens block plus prose for a human."""
    described = scan.description or f"{scan.name}, a project in this workspace."
    return f"""# Design system — {scan.name}

{described}

This file is the **stable** half of the design model: the system, not the
feature. Change it deliberately. `.prism/brief.md` holds the strategic
reasoning and standing decisions; `features/<slug>.md` holds what a specific
screen needs.

The block below is parsed by `agent/design/design_tokens.py`. Keep it valid —
a malformed block stops a dispatch rather than being guessed at, because
composing against a half-shaped system produces half-shaped mockups.

```yaml tokens
{tokens_yaml.strip()}
```

## Type

The display family carries headings and anything that should feel authored.
The body family carries everything else and should stay quiet. Mono is for
data, annotations, and marginalia — it earns its place by being *specific*,
not by being decorative.

## Colour

The palette is deliberately narrow. A background, a foreground, one accent
that means "this matters", and a muted tone for everything that doesn't.
Adding a fifth colour is almost always a sign that a hierarchy problem is
being solved with hue instead of with weight or space.

## Motion

Motion is continuous, not decorative — something on the screen should always
be alive, and it should be tied to what the product *does* rather than
animating for its own sake.
"""


def render_brief(scan: ProjectScan) -> str:
    """brief.md — private, evolving, and the thing that outranks a task."""
    return f"""# Brief — {scan.name}

Private strategic memory. Not shipped, not public. This is what the design
agent reads *before* it reads the task, and where it records what it learned.

## Positioning

{scan.description or f"{scan.name} — positioning not yet articulated. Fill this in; it is the single highest-leverage section here."}

## Persona

Who this is for, in one paragraph. Be specific enough that a design decision
could be settled by asking "would they care?"

## Business goals

What the product needs to achieve. A design that is beautiful and does not
serve these has failed.

## Brand language

Adjectives are cheap; name concrete moves. "Dense, technical, unhurried"
beats "modern and clean".

## Standing design decisions

These outrank the wording of any individual task — see THE BRIEF IS LAW in
the agent's system prompt. When a request conflicts with something here, the
agent surfaces the conflict and asks rather than silently overriding.

- Type sets hierarchy before colour does.
- Every hero carries a product surface showing what the thing actually does.

### Forbidden moves

Explicit and enforced. These are the defaults that make generated work look
generated.

- No generic SaaS gradient-on-dark hero.
- No stock-photo people at laptops.
- No three-column feature grid of icon + heading + two lines.
- No bento grid used as a substitute for hierarchy.

## Ongoing themes

What keeps coming up across features. Updated as the agent learns.

## Bootstrap notes

Generated from a scan of this repo{' (package.json found)' if scan.has_package_json else ''}.
Concrete starting values were committed rather than left as placeholders —
edit them, but don't blank them.
"""


def render_feature_doc(feature_slug: str, description: str = "") -> str:
    return f"""# Feature — {feature_slug}

{description or "What this feature is, in a sentence or two."}

## Screens

One heading per screen, with what it has to communicate and what the visitor
should do next.

## Content

Real copy where it exists. Placeholder text designs a placeholder.

## Visual direction

Specifics, not adjectives. Name the background treatment and its opacity, the
product surface, and what stays in motion.

## Open questions

Anything the brief doesn't settle. The agent surfaces conflicts here rather
than guessing.
"""


def bootstrap_project(slug: str, settings=None, force: bool = False) -> dict:
    """
    Create design.md and .prism/brief.md for a project that has neither.

    Idempotent by default: an existing document is left alone, because it is
    the one a human has been editing and regenerating it would silently throw
    that away. `force` re-renders both, and is for a deliberate reset.

    Returns which files were written, so a first dispatch can tell Sean "I
    started you a design system" rather than doing it invisibly.
    """
    from .design_tokens import default_tokens_yaml

    project_root = resolve_project_root(slug, settings)
    if not os.path.isdir(project_root):
        raise DesignDocError(f"no such project: {slug!r}")

    scan = scan_project(project_root)
    written = []

    if force or read_project_file(project_root, DESIGN_DOC) is None:
        write_project_file(project_root, DESIGN_DOC, render_design_doc(scan, default_tokens_yaml()))
        written.append(DESIGN_DOC)
    if force or read_project_file(project_root, BRIEF_DOC) is None:
        write_project_file(project_root, BRIEF_DOC, render_brief(scan))
        written.append(BRIEF_DOC)

    os.makedirs(assert_within_project(project_root, REFERENCES_DIR), exist_ok=True)
    os.makedirs(assert_within_project(project_root, FEATURES_DIR), exist_ok=True)

    return {"project_root": project_root, "written": written, "scan": scan}


def ensure_feature_doc(project_root: str, feature_slug: str, description: str = "") -> str:
    """Create features/<slug>.md if it doesn't exist. Returns its path."""
    feature_slug = validate_slug(feature_slug, "feature slug")
    relative = os.path.join(FEATURES_DIR, f"{feature_slug}.md")
    if read_project_file(project_root, relative) is None:
        write_project_file(project_root, relative, render_feature_doc(feature_slug, description))
    return assert_within_project(project_root, relative)
