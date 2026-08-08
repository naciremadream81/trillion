"""
Software Factory DOCS-step writer: turns a build plan and its test outcome
into a project README.md.

Same pure-render-then-write split as agent/factory/spec_md.py: readme_markdown()
is pure string-building (testable without touching disk), write_readme() is
the thin I/O wrapper.
"""

from __future__ import annotations

import os

from ..sanitize import clean_for_prompt

DEFAULT_README_NAME = "README.md"


def _bullet_list(items: list[str]) -> str:
    if not items:
        return "- (none)\n"
    return "".join(f"- {item}\n" for item in items)


def _tests_section(plan: dict, test_passed: bool | None, test_output: str) -> list[str]:
    test_command = plan.get("test_command") or ""
    if not test_command:
        return ["## Tests", "", "No automated tests were planned for this project.", ""]
    if test_passed is None:
        return [
            "## Tests",
            "",
            f"Test command: `{test_command}` (not run)",
            "",
        ]
    status = "PASSED" if test_passed else "FAILED"
    return [
        "## Tests",
        "",
        f"Test command: `{test_command}` — **{status}**",
        "",
        "```",
        test_output.rstrip(),
        "```",
        "",
    ]


def readme_markdown(description: str, plan: dict, test_passed: bool | None, test_output: str) -> str:
    """
    Render the project README content. No file I/O here — kept separate from
    write_readme() so tests can assert on content without touching disk.
    """
    brief = clean_for_prompt(description)
    lines = [
        f"# {plan.get('project_name', 'untitled-project')}",
        "",
        plan.get("summary", ""),
        "",
        "## Original brief",
        "",
        brief,
        "",
        "## Tech stack",
        "",
        plan.get("tech_stack", ""),
        "",
        "## Files",
        "",
        _bullet_list(plan.get("files", [])),
        "## Entry point",
        "",
        f"`{plan.get('entry_point', '')}`",
        "",
        *_tests_section(plan, test_passed, test_output),
        "---",
        "",
        "Built autonomously by Trillion's Software Factory.",
    ]
    return "\n".join(lines).rstrip() + "\n"


def write_readme(
    project_dir: str, description: str, plan: dict, test_passed: bool | None, test_output: str
) -> str:
    """Write <project_dir>/README.md (creating the directory if needed) and
    return the path written."""
    os.makedirs(project_dir, exist_ok=True)
    path = os.path.join(project_dir, DEFAULT_README_NAME)
    with open(path, "w", encoding="utf-8") as f:
        f.write(readme_markdown(description, plan, test_passed, test_output))
    return path
