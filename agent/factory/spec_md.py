"""
Tier 2 spec-markdown writer: turns a SkillsReport (agent/factory/research.py)
and a drafted spec (agent/factory/draft.py) into a human-readable
agent-specs/<slug>.md file — the artifact a human reviews (or later recalls
why) a spawned agent was configured a certain way.

Same env-override pattern as agent/factory/storage.py's default_db_path().
"""

from __future__ import annotations

import os

from .sanitize import clean_for_prompt

DEFAULT_SPECS_DIR = "agent-specs"


def default_specs_dir() -> str:
    """Where spec markdown files are written. Override with $TRILLION_AGENT_SPECS_DIR."""
    return os.getenv("TRILLION_AGENT_SPECS_DIR", DEFAULT_SPECS_DIR)


def _bullet_list(items: list[str]) -> str:
    if not items:
        return "- (none)\n"
    return "".join(f"- {item}\n" for item in items)


def spec_markdown(role_description: str, report: dict, spec: dict) -> str:
    """
    Render the spec markdown content. No file I/O here — kept separate from
    write_spec_markdown() so tests can assert on content without touching disk.
    """
    role = clean_for_prompt(role_description)
    lines = [
        f"# {spec['slug']}",
        "",
        f"**Slug:** `{spec['slug']}`",
        f"**Domain:** {report.get('domain', '')}",
        "",
        "## Requested role",
        "",
        role,
        "",
        "## Competencies",
        "",
        _bullet_list(report.get("competencies", [])),
        "## Generated system prompt",
        "",
        spec.get("system_prompt", ""),
        "",
        "## Granted tools",
        "",
        _bullet_list(spec.get("tool_allowlist", [])),
        "## Tool wishlist",
        "",
        "Tools this agent could use but that don't exist yet in Trillion's "
        "catalog — a roadmap for what to build next.",
        "",
        _bullet_list(report.get("tools_wishlist", [])),
        "## Design patterns",
        "",
        _bullet_list(report.get("design_patterns", [])),
        "## Sources",
        "",
        _bullet_list(report.get("sources", [])),
    ]
    return "\n".join(lines).rstrip() + "\n"


def write_spec_markdown(
    role_description: str, report: dict, spec: dict, specs_dir: str | None = None
) -> str:
    """Write agent-specs/<slug>.md (creating the directory if needed) and
    return the path written."""
    target_dir = specs_dir if specs_dir is not None else default_specs_dir()
    os.makedirs(target_dir, exist_ok=True)
    path = os.path.join(target_dir, f"{spec['slug']}.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(spec_markdown(role_description, report, spec))
    return path
