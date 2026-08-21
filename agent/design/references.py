"""
Reference images — playbooks/design-subagent.md, Tier 7, which the playbook
calls the single biggest quality lever. "Words describe a vibe. An image
fixes it."

Users drop screenshots into `.prism/references/<feature>/`, name them in
`generate_mockup`'s `reference_images`, and the composition prompt tells
Claude Code to actually LOOK at them — it has vision, so the images override
category defaults in a way no amount of adjectives can.

Validation is strict for the usual reason plus one specific to this tier:
these paths are handed to a subprocess that reads files. `..`, absolute
paths, and non-image extensions are all refused, and existence is checked
here so a typo surfaces as a clear message rather than as Claude Code
quietly composing without the reference it was told to anchor on.

The playbook's stumbling block for this tier is worth restating because it
is not a code problem: **the directory name must match the feature_slug the
agent actually chose**, not what was said out loud. References filed under
`saas-landing-hero/` are invisible to a dispatch that slugged the feature
`saas-landing-page`. resolve_reference_images returns what it could not find
so the caller can say so rather than silently proceeding.
"""

from __future__ import annotations

import os

from .docs import DesignDocError, REFERENCES_DIR, assert_within_project, validate_slug

ALLOWED_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".webp"})
MAX_REFERENCES = 6


def references_dir(project_root: str, feature_slug: str) -> str:
    feature_slug = validate_slug(feature_slug, "feature slug")
    return assert_within_project(project_root, os.path.join(REFERENCES_DIR, feature_slug))


def list_references(project_root: str, feature_slug: str) -> list:
    """Image filenames already filed for a feature, sorted."""
    try:
        directory = references_dir(project_root, feature_slug)
    except DesignDocError:
        return []
    if not os.path.isdir(directory):
        return []
    return sorted(
        name
        for name in os.listdir(directory)
        if os.path.splitext(name)[1].lower() in ALLOWED_EXTENSIONS
    )


def resolve_reference_images(project_root: str, feature_slug: str, names) -> tuple:
    """
    Turn requested reference names into paths Claude Code can read.

    Returns (resolved, problems). Problems are returned rather than raised:
    a missing reference is a reason to compose without it and say so, not to
    fail a dispatch that would otherwise succeed.
    """
    resolved, problems = [], []
    if not names:
        return resolved, problems

    try:
        directory = references_dir(project_root, feature_slug)
    except DesignDocError as e:
        return [], [str(e)]

    for raw in list(names)[:MAX_REFERENCES]:
        name = str(raw or "").strip()
        if not name:
            continue
        # Reject before resolving: these paths are handed to a subprocess
        # that reads files.
        if os.path.isabs(name) or ".." in name.replace("\\", "/").split("/"):
            problems.append(f"{name!r}: only plain filenames inside the references directory")
            continue
        if os.path.splitext(name)[1].lower() not in ALLOWED_EXTENSIONS:
            problems.append(
                f"{name!r}: not an image ({', '.join(sorted(ALLOWED_EXTENSIONS))})"
            )
            continue
        try:
            path = assert_within_project(directory, name)
        except DesignDocError:
            problems.append(f"{name!r}: escapes the references directory")
            continue
        if not os.path.isfile(path):
            problems.append(f"{name!r}: not found in .prism/references/{feature_slug}/")
            continue
        resolved.append(os.path.relpath(path, project_root))

    if len(list(names)) > MAX_REFERENCES:
        problems.append(f"only the first {MAX_REFERENCES} references were used")
    return resolved, problems
