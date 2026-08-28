"""
AI image generation — playbooks/design-subagent.md, Tier 5.

Pure-TSX composition gets most of the way to a visually rich mockup;
atmospheric backdrops and conceptual illustrations are the last stretch that
needs actual imagery.

**Self-skipping.** No GEMINI_API_KEY, or no `google-genai` installed, means
the tool is simply not registered — the same posture as the analytics tool,
the search tool, the mining tracker, and the design agent itself. An absent
capability is better than a registered one that fails on every call.

Three stumbling blocks from the playbook, all encoded below:

  - **Return the FULL URL with the basePath prefix.** Plain `<img>` tags are
    not auto-prefixed the way `next/image` is, so a bare `/assets/...` 404s
    under the serving prefix. The composition prompt already tells Claude
    Code to use these URLs verbatim; this is the other half of that.
  - **Don't use `next/image`.** Static export with `unoptimized: true` makes
    plain `<img>` simpler and equivalent.
  - **Repeat the forbidden colours in the prompt.** "Cyberpunk" alone gets
    violet and cyan from any image model. The brief's palette has to be
    named, and so do the colours it rules out, or the generated image fights
    the design system it was meant to serve.

Also worth knowing before enabling this: image models are **not** in
Gemini's free tier. An unbilled key authenticates fine and then rejects
every generation with a zero quota, which reads like a bug and isn't one.
"""

from __future__ import annotations

import os
import re

from .docs import DesignDocError, assert_within_project, validate_slug
from .scaffold import preview_base_path

# Standard is GA and cheap; premium is the preview model and roughly 3x.
QUALITY_MODELS = {
    "standard": "gemini-2.5-flash-image",
    "premium": "gemini-3-pro-image-preview",
}
DEFAULT_QUALITY = "standard"

ASSETS_SUBDIR = os.path.join("public", "assets")
SLUG_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


class ImageGenError(RuntimeError):
    pass


def is_available() -> bool:
    """Whether image generation can actually run right now."""
    if not (os.getenv("GEMINI_API_KEY") or "").strip():
        return False
    try:
        import google.genai  # noqa: F401
    except ImportError:
        return False
    return True


def unavailable_reason() -> str:
    if not (os.getenv("GEMINI_API_KEY") or "").strip():
        return "GEMINI_API_KEY is not set"
    try:
        import google.genai  # noqa: F401
    except ImportError:
        return "the google-genai package is not installed (pip install google-genai)"
    return ""


def asset_url(project_slug: str, feature_slug: str, image_slug: str) -> str:
    """
    The URL to hand the composer — FULL, with the basePath prefix baked in.

    Returning a bare `/assets/...` here is the playbook's named trap: plain
    `<img>` is not prefixed the way `next/image` is, so the shortened form
    404s under the serving prefix and the image silently never appears.
    """
    return f"{preview_base_path(project_slug)}/assets/{feature_slug}/{image_slug}.png"


def asset_path(project_root: str, feature_slug: str, image_slug: str) -> str:
    """Where the PNG is written, so Next copies it to out/assets at build."""
    feature_slug = validate_slug(feature_slug, "feature slug")
    image_slug = validate_slug(image_slug, "image slug")
    from .docs import PREVIEW_DIR

    preview_root = assert_within_project(project_root, PREVIEW_DIR)
    return assert_within_project(
        preview_root, os.path.join(ASSETS_SUBDIR, feature_slug, f"{image_slug}.png")
    )


def build_image_prompt(
    description: str,
    *,
    palette: dict | None = None,
    forbidden_colors=None,
    aspect_ratio: str = "16:9",
) -> str:
    """
    Compose the generation prompt.

    Naming the palette AND the colours to avoid is the difference between an
    image that serves the design system and one that fights it — "cyberpunk"
    alone returns violet and cyan from any model. Aspect ratio is hinted in
    the prompt because the v2 SDK does not expose it directly for image+text
    models.
    """
    parts = [description.strip()]
    if palette:
        named = ", ".join(f"{name} {value}" for name, value in palette.items())
        parts.append(f"Use strictly this palette: {named}.")
    if forbidden_colors:
        parts.append(
            "Do NOT use " + ", ".join(str(c) for c in forbidden_colors)
            + " — these clash with the design system this image is for."
        )
    parts.append(
        f"Composition: {aspect_ratio} aspect ratio. No text, no lettering, no watermarks, "
        "no UI chrome. This is a backdrop or illustration that other elements "
        "will be composed on top of."
    )
    return " ".join(parts)


async def generate_image(
    project_root: str,
    project_slug: str,
    feature_slug: str,
    image_slug: str,
    description: str,
    *,
    palette: dict | None = None,
    forbidden_colors=None,
    quality: str = DEFAULT_QUALITY,
    aspect_ratio: str = "16:9",
) -> dict:
    """
    Generate one image and save it into the preview app's assets.

    Returns {"url", "path", "model"}. Raises ImageGenError with a plain
    explanation on any failure — including the zero-quota case, which is by
    far the most likely first experience and does not look like what it is.
    """
    if not is_available():
        raise ImageGenError(f"Image generation is unavailable: {unavailable_reason()}.")

    quality = quality if quality in QUALITY_MODELS else DEFAULT_QUALITY
    model = QUALITY_MODELS[quality]

    try:
        target = asset_path(project_root, feature_slug, image_slug)
    except DesignDocError as e:
        raise ImageGenError(str(e)) from e

    prompt = build_image_prompt(
        description, palette=palette, forbidden_colors=forbidden_colors,
        aspect_ratio=aspect_ratio,
    )

    try:
        from google import genai

        client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
        response = await _generate(client, model, prompt)
    except ImageGenError:
        raise
    except Exception as e:  # noqa: BLE001
        message = str(e)
        if "quota" in message.lower() or "billing" in message.lower():
            raise ImageGenError(
                "Gemini rejected the request for quota. Image models are not in "
                "the free tier — the key authenticates but generates nothing until "
                "billing is enabled on the AI Studio project."
            ) from e
        raise ImageGenError(f"Image generation failed: {type(e).__name__}: {e}") from e

    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, "wb") as f:
        f.write(response)

    return {
        "url": asset_url(project_slug, feature_slug, image_slug),
        "path": target,
        "model": model,
    }


async def _generate(client, model: str, prompt: str) -> bytes:
    """
    The SDK call, isolated so the surrounding logic stays testable without
    a network or a key.
    """
    import asyncio

    def call() -> bytes:
        result = client.models.generate_content(model=model, contents=prompt)
        for candidate in getattr(result, "candidates", None) or []:
            content = getattr(candidate, "content", None)
            for part in getattr(content, "parts", None) or []:
                inline = getattr(part, "inline_data", None)
                if inline is not None and getattr(inline, "data", None):
                    return inline.data
        raise ImageGenError("Gemini returned no image data.")

    return await asyncio.to_thread(call)
