"""
generate_mockup — the design agent's composition tool.

Wraps Tier 3's dispatch: validate, parse the design system, scaffold if this
is a first dispatch, build the prompt, spawn Claude Code, and verify on the
filesystem that a page actually got built.

**Gated.** risk = CONSEQUENTIAL, so agent/safety/approval.py parks this until
Sean says yes. That is not belt-and-braces caution — this tool spawns a
subprocess that installs packages, writes files, and spends real money. It is
exactly the shape of thing AGENT.md's confirmation gate exists for, and the
tool that would be most annoying to discover running unattended.

**Not factory_allowed.** A spawned specialist must not be able to reach it:
specialists run without a gate by construction (see ConfigDrivenAgent), which
is safe precisely because every tool they can reach is read-only.

Success is the presence of out/<feature>/<screen>/index.html, not Claude
Code's report. A model saying it built something is not evidence.
"""

from __future__ import annotations

import os

from ..design.budget import BudgetExceeded, DesignBudget
from ..design.claude_code_runner import spawn_claude_code
from ..design.component_catalog import validate_component_names
from ..design.composer import QUALITY_LEVELS, build_composition_prompt
from ..design.design_tokens import TokenError, parse_tokens
from ..design.docs import (
    DESIGN_DOC,
    DesignDocError,
    bootstrap_project,
    ensure_feature_doc,
    read_project_file,
    resolve_project_root,
    validate_slug,
)
from ..design.scaffold import expected_output_path, prepare_scaffold
from ..safety.risk import CONSEQUENTIAL, READ_ONLY
from .base import BaseTool


class GenerateMockupTool(BaseTool):
    name = "generate_mockup"
    description = (
        "Compose one high-fidelity screen for a project as a real Next.js page, "
        "built against its design system. Use when Sean asks for a mockup, a "
        "screen, a landing page, or a redesign of something in generated-projects/. "
        "This spawns Claude Code to install components, write the page, and run a "
        "build — it costs real money and takes minutes, so compose one screen per "
        "call and describe it properly rather than guessing."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "project_slug": {"type": "string", "description": "Which project, kebab-case."},
            "feature_slug": {"type": "string", "description": "Feature this screen belongs to, kebab-case."},
            "screen_name": {"type": "string", "description": "Screen name, kebab-case (e.g. 'hero')."},
            "description": {"type": "string", "description": "What the screen is and must communicate."},
            "visual_direction": {
                "type": "string",
                "description": (
                    "Specific visual direction — name the background treatment and "
                    "its opacity, the product surface, and what stays in motion. "
                    "Adjectives alone ('premium', 'modern') produce generic output."
                ),
            },
            "quality": {"type": "string", "enum": list(QUALITY_LEVELS)},
            "components_hint": {"type": "array", "items": {"type": "string"}},
            "reference_images": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["project_slug", "feature_slug", "screen_name", "description"],
    }

    factory_allowed = False   # see the module docstring
    risk = CONSEQUENTIAL      # spawns a subprocess, writes files, spends money
    requires_confirmation = None
    trusted_output = False

    def __init__(self, settings=None, budget=None, on_event=None) -> None:
        self.settings = settings
        self.budget = budget or DesignBudget()
        self.on_event = on_event

    async def run(self, **kwargs) -> str:
        try:
            project_slug = validate_slug(kwargs.get("project_slug", ""), "project_slug")
            feature_slug = validate_slug(kwargs.get("feature_slug", ""), "feature_slug")
            screen_name = validate_slug(kwargs.get("screen_name", ""), "screen_name")
        except DesignDocError as e:
            return f"[generate_mockup: {e}]"

        description = str(kwargs.get("description") or "").strip()
        if not description:
            return "[generate_mockup: a description of the screen is required.]"

        quality = str(kwargs.get("quality") or "standard").strip()
        if quality not in QUALITY_LEVELS:
            quality = "standard"

        # Refuse BEFORE spawning — a refusal that costs nothing is the whole
        # point of a ceiling.
        try:
            self.budget.check_before_dispatch()
        except BudgetExceeded as e:
            return f"[generate_mockup refused: {e}]"

        try:
            project_root = resolve_project_root(project_slug, self.settings)
        except DesignDocError as e:
            return f"[generate_mockup: {e}]"
        if not os.path.isdir(project_root):
            return f"[generate_mockup: no such project {project_slug!r}.]"

        # Bootstrap is idempotent and cheap; a project with no design system
        # would otherwise fail at token parsing with a confusing error.
        try:
            bootstrap_project(project_slug, self.settings)
            ensure_feature_doc(project_root, feature_slug, description)
        except DesignDocError as e:
            return f"[generate_mockup: could not prepare the project: {e}]"

        design_md = read_project_file(project_root, DESIGN_DOC)
        try:
            tokens = parse_tokens(design_md or "")
        except TokenError as e:
            # Bail loudly rather than composing against a half-shaped system.
            return (
                f"[generate_mockup: {project_slug}'s design.md is not usable: {e} "
                "Fix design.md before composing — a guessed design system produces "
                "a mockup built on invented values.]"
            )

        preview_package = os.path.join(project_root, ".prism", "preview", "package.json")
        node_modules = os.path.join(project_root, ".prism", "preview", "node_modules")
        first_dispatch = not os.path.isdir(node_modules)
        try:
            prepare_scaffold(project_root, project_slug, tokens)
        except DesignDocError as e:
            return f"[generate_mockup refused: {e}]"

        known, unknown = validate_component_names(kwargs.get("components_hint"))
        prompt = build_composition_prompt(
            project_slug=project_slug,
            feature_slug=feature_slug,
            screen_name=screen_name,
            description=description,
            tokens=tokens,
            visual_direction=str(kwargs.get("visual_direction") or ""),
            quality=quality,
            components_hint=known,
            reference_images=kwargs.get("reference_images"),
            first_dispatch=first_dispatch,
        )

        model = getattr(self.settings, "design_compose_model", None) if self.settings else None
        result = await spawn_claude_code(
            prompt,
            cwd=project_root,
            model=model,
            on_event=self.on_event,
        )

        preview_root = os.path.join(project_root, ".prism", "preview")
        built = expected_output_path(preview_root, feature_slug, screen_name)
        built_ok = os.path.isfile(built)

        self.budget.record(
            project_slug,
            feature_slug=feature_slug,
            screen_name=screen_name,
            cost_usd=result.total_cost_usd,
            succeeded=built_ok,
        )

        spent = f"${result.total_cost_usd:.2f}"
        if built_ok:
            url = f"/api/design/{project_slug}/preview/{feature_slug}/{screen_name}/"
            lines = [
                f"Built {feature_slug}/{screen_name} for {project_slug}.",
                f"View it at {url}",
                f"Cost {spent} over {result.num_turns} turns "
                f"({result.duration_seconds:.0f}s).",
            ]
            if unknown:
                lines.append(
                    f"Ignored components not in the palette: {', '.join(unknown)}."
                )
            if result.result_text:
                lines += ["", result.result_text[:1200]]
            return "\n".join(lines)

        # Verified on the filesystem, so this branch is reachable even when
        # Claude Code reported success.
        reason = result.error or "the build produced no output file"
        return (
            f"[generate_mockup did not produce a screen: {reason}] "
            f"Spent {spent}. Expected {os.path.relpath(built, project_root)} to exist. "
            f"{result.result_text[:600]}"
        )


class ListDesignProjectsTool(BaseTool):
    name = "list_design_projects"
    description = (
        "List the projects the design agent can work on, and whether each has a "
        "design system set up yet. Use before generate_mockup when Sean hasn't "
        "named a project, or when he asks what you could design."
    )
    input_schema = {"type": "object", "properties": {}}

    factory_allowed = True
    risk = READ_ONLY
    requires_confirmation = False
    trusted_output = True

    def __init__(self, settings=None) -> None:
        self.settings = settings

    async def run(self, **kwargs) -> str:
        from ..design.docs import list_projects

        try:
            slugs = list_projects(self.settings)
        except Exception as e:  # noqa: BLE001
            return f"[Could not list projects: {type(e).__name__}: {e}]"
        if not slugs:
            return "No projects available to design for yet."

        lines = []
        for slug in slugs:
            try:
                root = resolve_project_root(slug, self.settings)
                has_design = read_project_file(root, DESIGN_DOC) is not None
                built = os.path.isdir(os.path.join(root, ".prism", "preview", "node_modules"))
            except Exception:
                has_design, built = False, False
            state = "design system ready" if has_design else "not set up yet"
            if built:
                state += ", preview installed"
            lines.append(f"- {slug} ({state})")
        return "\n".join(lines)
