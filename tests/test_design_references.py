"""
Tests for Tiers 5 and 7 — reference images and AI image generation.

Tier 7 is the playbook's "single biggest quality lever": words describe a
vibe, an image fixes it. Its validation matters more than most because these
paths are handed to a subprocess that reads files.

Tier 5 is self-skipping without a key, so what's testable without one is the
part that carries the playbook's three named traps: the full basePath-
prefixed URL, the palette-and-forbidden-colours prompt, and the quota error
that looks like a bug and isn't.

Run from the project root:
    python -m unittest tests.test_design_references
"""

import os
import shutil
import tempfile
import unittest

from agent.design.image_gen import (
    QUALITY_MODELS,
    asset_path,
    asset_url,
    build_image_prompt,
    is_available,
    unavailable_reason,
)
from agent.design.references import (
    ALLOWED_EXTENSIONS,
    MAX_REFERENCES,
    list_references,
    resolve_reference_images,
)


class TestReferenceImages(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.project = os.path.join(self.tmp, "demo-project")
        self.refs = os.path.join(self.project, ".prism", "references", "landing")
        os.makedirs(self.refs)
        for name in ("a.png", "b.jpg", "notes.txt"):
            open(os.path.join(self.refs, name), "wb").close()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_listing_returns_only_images(self):
        self.assertEqual(list_references(self.project, "landing"), ["a.png", "b.jpg"])

    def test_listing_an_absent_feature_is_empty_not_an_error(self):
        self.assertEqual(list_references(self.project, "nope"), [])

    def test_valid_references_resolve_to_project_relative_paths(self):
        resolved, problems = resolve_reference_images(self.project, "landing", ["a.png", "b.jpg"])
        self.assertEqual(problems, [])
        self.assertEqual(len(resolved), 2)
        for path in resolved:
            self.assertFalse(os.path.isabs(path))
            self.assertIn("references", path)

    def test_traversal_is_refused(self):
        # These paths are handed to a subprocess that reads files.
        for name in ("../../../etc/passwd", "..\\\\..\\\\secret.png", "/etc/passwd"):
            with self.subTest(name=name):
                resolved, problems = resolve_reference_images(self.project, "landing", [name])
                self.assertEqual(resolved, [])
                self.assertTrue(problems)

    def test_non_images_are_refused(self):
        resolved, problems = resolve_reference_images(self.project, "landing", ["notes.txt"])
        self.assertEqual(resolved, [])
        self.assertIn("not an image", problems[0])

    def test_a_missing_reference_is_reported_not_fatal(self):
        # The usual cause is the directory name not matching the slug the
        # agent chose — invisible unless it is said out loud.
        resolved, problems = resolve_reference_images(
            self.project, "landing", ["a.png", "ghost.png"]
        )
        self.assertEqual(len(resolved), 1)
        self.assertIn("not found", problems[0])

    def test_too_many_references_are_capped_and_reported(self):
        for i in range(MAX_REFERENCES + 3):
            open(os.path.join(self.refs, f"r{i}.png"), "wb").close()
        names = [f"r{i}.png" for i in range(MAX_REFERENCES + 3)]
        resolved, problems = resolve_reference_images(self.project, "landing", names)
        self.assertEqual(len(resolved), MAX_REFERENCES)
        self.assertTrue(any("first" in p for p in problems))

    def test_no_references_requested_is_clean(self):
        self.assertEqual(resolve_reference_images(self.project, "landing", None), ([], []))

    def test_a_bad_feature_slug_is_reported(self):
        resolved, problems = resolve_reference_images(self.project, "../etc", ["a.png"])
        self.assertEqual(resolved, [])
        self.assertTrue(problems)

    def test_extensions_cover_what_a_screenshot_actually_is(self):
        for ext in (".png", ".jpg", ".jpeg", ".webp"):
            self.assertIn(ext, ALLOWED_EXTENSIONS)


class TestReferencePromptBlock(unittest.TestCase):
    def test_the_prompt_tells_claude_code_to_actually_look(self):
        from agent.design.composer import build_composition_prompt
        from agent.design.design_tokens import default_tokens_yaml, parse_tokens
        from agent.design.docs import ProjectScan, render_design_doc

        tokens = parse_tokens(render_design_doc(ProjectScan(name="d"), default_tokens_yaml()))
        prompt = build_composition_prompt(
            project_slug="p", feature_slug="f", screen_name="s", description="d",
            tokens=tokens, reference_images=[".prism/references/f/a.png"],
        )
        self.assertIn("Read TOOL", prompt)
        self.assertIn("override category defaults", prompt)
        self.assertIn(".prism/references/f/a.png", prompt)


class TestImageGeneration(unittest.TestCase):
    def test_it_self_skips_without_a_key(self):
        prev = os.environ.pop("GEMINI_API_KEY", None)
        try:
            self.assertFalse(is_available())
            self.assertIn("GEMINI_API_KEY", unavailable_reason())
        finally:
            if prev is not None:
                os.environ["GEMINI_API_KEY"] = prev

    def test_the_url_carries_the_full_base_path_prefix(self):
        # The playbook's named trap: plain <img> is not auto-prefixed the way
        # next/image is, so a bare /assets/... 404s under the serving prefix
        # and the image silently never appears.
        url = asset_url("demo-project", "landing", "backdrop")
        self.assertTrue(url.startswith("/api/design/demo-project/preview/"))
        self.assertTrue(url.endswith("/assets/landing/backdrop.png"))

    def test_the_asset_path_lands_where_next_will_copy_it(self):
        tmp = tempfile.mkdtemp()
        try:
            path = asset_path(tmp, "landing", "backdrop")
            self.assertIn(os.path.join("public", "assets", "landing"), path)
            self.assertTrue(path.endswith("backdrop.png"))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_asset_paths_refuse_traversal(self):
        from agent.design.docs import DesignDocError

        tmp = tempfile.mkdtemp()
        try:
            with self.assertRaises(DesignDocError):
                asset_path(tmp, "../etc", "passwd")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_the_prompt_names_both_the_palette_and_what_to_avoid(self):
        # "Cyberpunk" alone returns violet and cyan from any image model.
        prompt = build_image_prompt(
            "a server room",
            palette={"background": "#0B0C0F", "accent": "#E8B44A"},
            forbidden_colors=["violet", "cyan"],
        )
        self.assertIn("#0B0C0F", prompt)
        self.assertIn("Do NOT use violet, cyan", prompt)

    def test_the_prompt_rules_out_text_in_the_image(self):
        # These are backdrops other elements get composed on top of.
        prompt = build_image_prompt("a server room")
        self.assertIn("No text", prompt)

    def test_both_quality_tiers_map_to_a_model(self):
        self.assertEqual(set(QUALITY_MODELS), {"standard", "premium"})
        self.assertTrue(all(QUALITY_MODELS.values()))


if __name__ == "__main__":
    unittest.main()
