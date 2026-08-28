"""
Tests for the design agent's document model and token system —
playbooks/design-subagent.md Tier 1.

Two things carry the weight here and both are tested hard:

  - **Containment.** This feature spawns Claude Code to write files, so
    "which directory may it touch" is its load-bearing question. The jail is
    agent/tools/project_fs.py's, reused rather than reimplemented.
  - **Strict token validation.** The composer must "bail loudly if the YAML
    block is missing or invalid — don't try to compose against a half-shaped
    system", because a guessed value produces a mockup built on invented
    colours that reads as a design failure rather than a config one.

Run from the project root:
    python -m unittest tests.test_design_docs
"""

import os
import shutil
import tempfile
import unittest

from agent.design.design_tokens import (
    FORBIDDEN_FAMILIES,
    DesignTokens,
    TokenError,
    default_tokens_yaml,
    extract_tokens_block,
    hex_to_hsl,
    parse_tokens,
    render_globals_css,
    render_tailwind_config,
)
from agent.design.docs import (
    DesignDocError,
    ProjectScan,
    bootstrap_project,
    ensure_feature_doc,
    list_project_files,
    list_projects,
    read_project_file,
    render_design_doc,
    resolve_project_root,
    scan_project,
    validate_slug,
    write_project_file,
)


class Settings:
    def __init__(self, root):
        self.software_factory_root = root


class TestSlugValidation(unittest.TestCase):
    def test_kebab_case_is_accepted(self):
        for slug in ("app", "my-app", "a1-b2-c3"):
            with self.subTest(slug=slug):
                self.assertEqual(validate_slug(slug), slug)

    def test_traversal_and_separators_are_refused(self):
        for slug in ("../etc", "a/b", "a\\b", "/abs", "."):
            with self.subTest(slug=slug):
                with self.assertRaises(DesignDocError):
                    validate_slug(slug)

    def test_shouty_and_malformed_are_refused(self):
        for slug in ("Foo", "a--b", "-a", "a-", "", "  ", "a" * 100):
            with self.subTest(slug=slug):
                with self.assertRaises(DesignDocError):
                    validate_slug(slug)

    def test_it_raises_rather_than_sanitizing(self):
        # A silently rewritten slug means the agent and the filesystem
        # disagree about where a project lives, which surfaces much later as
        # a confusing empty read.
        with self.assertRaises(DesignDocError):
            validate_slug("My Project")


class TestContainment(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.tmp, "demo-project"))
        self.settings = Settings(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_project_resolves_inside_the_root(self):
        root = resolve_project_root("demo-project", self.settings)
        self.assertTrue(root.startswith(os.path.realpath(self.tmp)))

    def test_traversal_out_of_the_root_is_refused(self):
        with self.assertRaises(DesignDocError):
            resolve_project_root("../..", self.settings)

    def test_writing_outside_a_project_is_refused(self):
        root = resolve_project_root("demo-project", self.settings)
        for path in ("../escape.txt", "../../etc/passwd", "/etc/passwd"):
            with self.subTest(path=path):
                with self.assertRaises(DesignDocError):
                    write_project_file(root, path, "nope")

    def test_reading_outside_a_project_is_refused(self):
        root = resolve_project_root("demo-project", self.settings)
        with self.assertRaises(DesignDocError):
            read_project_file(root, "../../../etc/passwd")

    def test_listing_skips_node_modules_and_build_output(self):
        # These are enormous and never interesting to a design read; walking
        # into them would make every listing useless and slow.
        root = resolve_project_root("demo-project", self.settings)
        for noisy in ("node_modules/pkg", ".next/cache", "out/static", ".git/objects"):
            os.makedirs(os.path.join(root, noisy), exist_ok=True)
            open(os.path.join(root, noisy, "f.txt"), "w").close()
        write_project_file(root, "app/page.tsx", "x")
        listed = list_project_files(root)
        self.assertIn("app/page.tsx", listed)
        self.assertFalse([p for p in listed if "node_modules" in p or ".next" in p])


class TestBootstrap(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = os.path.join(self.tmp, "demo-project")
        os.makedirs(self.root)
        self.settings = Settings(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_bootstrap_writes_both_documents(self):
        result = bootstrap_project("demo-project", self.settings)
        self.assertEqual(set(result["written"]), {"design.md", ".prism/brief.md"})

    def test_bootstrap_is_idempotent(self):
        bootstrap_project("demo-project", self.settings)
        # An existing document is the one a human has been editing;
        # regenerating it would silently throw that away.
        self.assertEqual(bootstrap_project("demo-project", self.settings)["written"], [])

    def test_force_regenerates(self):
        bootstrap_project("demo-project", self.settings)
        self.assertEqual(
            set(bootstrap_project("demo-project", self.settings, force=True)["written"]),
            {"design.md", ".prism/brief.md"},
        )

    def test_the_bootstrapped_design_doc_parses(self):
        # The playbook's stumbling block for this tier: "a skeleton with
        # placeholder values produces skeleton mockups". The bootstrap has to
        # commit to concrete values, and they have to be valid.
        result = bootstrap_project("demo-project", self.settings)
        tokens = parse_tokens(read_project_file(result["project_root"], "design.md"))
        self.assertTrue(tokens.fonts["display"])
        self.assertTrue(tokens.colors["accent"].startswith("#"))

    def test_the_bootstrapped_doc_contains_no_placeholders(self):
        result = bootstrap_project("demo-project", self.settings)
        design = read_project_file(result["project_root"], "design.md")
        for placeholder in ("TODO", "FIXME", "XXX", "<insert"):
            self.assertNotIn(placeholder, design)

    def test_the_brief_carries_forbidden_moves(self):
        # Tier 6's "THE BRIEF IS LAW" needs something to enforce.
        result = bootstrap_project("demo-project", self.settings)
        brief = read_project_file(result["project_root"], ".prism/brief.md")
        self.assertIn("Forbidden moves", brief)

    def test_the_scan_seeds_from_a_readme(self):
        with open(os.path.join(self.root, "README.md"), "w") as f:
            f.write("# Demo\n\nA tool that turns meetings into money you can see.\n")
        result = bootstrap_project("demo-project", self.settings)
        self.assertIn("meetings into money", read_project_file(result["project_root"], ".prism/brief.md"))

    def test_the_scan_seeds_from_package_json(self):
        with open(os.path.join(self.root, "package.json"), "w") as f:
            f.write('{"name": "cost-ticker", "description": "Live meeting cost."}')
        scan = scan_project(self.root)
        self.assertEqual(scan.name, "cost-ticker")
        self.assertEqual(scan.description, "Live meeting cost.")

    def test_a_bare_project_still_bootstraps(self):
        # Best-effort throughout: a project that is just a directory should
        # still get a usable starting point.
        self.assertTrue(bootstrap_project("demo-project", self.settings)["written"])

    def test_bootstrapping_a_missing_project_raises(self):
        with self.assertRaises(DesignDocError):
            bootstrap_project("no-such-project", self.settings)

    def test_feature_docs_are_created_once(self):
        result = bootstrap_project("demo-project", self.settings)
        path = ensure_feature_doc(result["project_root"], "landing-page")
        with open(path, "a") as f:
            f.write("\nedited by a human\n")
        ensure_feature_doc(result["project_root"], "landing-page")
        with open(path) as f:
            self.assertIn("edited by a human", f.read())


class TestTokenParsing(unittest.TestCase):
    def setUp(self):
        self.doc = render_design_doc(ProjectScan(name="demo"), default_tokens_yaml())

    def test_the_default_block_parses(self):
        tokens = parse_tokens(self.doc)
        self.assertEqual(set(tokens.fonts), {"display", "body", "mono"})

    def test_hex_colours_survive_comment_stripping(self):
        # The bug this guards: stripping `#` as a YAML comment from the raw
        # line eats every hex colour, because `background: "#0B0C0F"` becomes
        # `background: "`. Hex colours are most of what this block holds.
        tokens = parse_tokens(self.doc)
        self.assertEqual(tokens.colors["background"], "#0B0C0F")
        self.assertEqual(tokens.colors["accent"], "#E8B44A")

    def test_a_real_trailing_comment_is_still_stripped(self):
        doc = self.doc.replace("radius: 0.5rem", "radius: 0.5rem # roomy")
        self.assertEqual(parse_tokens(doc).radius, "0.5rem")

    def test_a_whole_line_comment_is_ignored(self):
        doc = self.doc.replace("fonts:", "# the type system\nfonts:")
        self.assertTrue(parse_tokens(doc).fonts["display"])

    def test_a_missing_block_is_refused(self):
        with self.assertRaises(TokenError):
            parse_tokens("# design.md with no tokens block")

    def test_an_unrelated_yaml_block_is_not_mistaken_for_the_real_one(self):
        # design.md is prose as well as config; a doc containing an example
        # yaml snippet must not be read as the design system.
        doc = "# Design\n\n```yaml\nnot: the tokens\n```\n"
        with self.assertRaises(TokenError):
            parse_tokens(doc)

    def test_forbidden_fonts_are_blocked(self):
        # These are what every model reaches for by default, so allowing them
        # means the output announces how it was made.
        for family in list(FORBIDDEN_FAMILIES)[:4]:
            with self.subTest(family=family):
                doc = self.doc.replace("body: Geist", f"body: {family}")
                with self.assertRaises(TokenError) as ctx:
                    parse_tokens(doc)
                self.assertIn(family, str(ctx.exception))

    def test_a_bad_hex_is_refused_by_name(self):
        doc = self.doc.replace('"#E8B44A"', "chartreuse")
        with self.assertRaises(TokenError) as ctx:
            parse_tokens(doc)
        self.assertIn("colors.accent", str(ctx.exception))

    def test_a_bad_shadcn_base_colour_is_refused(self):
        # Anything outside the enum makes `npx shadcn init` fail deep inside
        # a Claude Code run rather than here.
        doc = self.doc.replace("shadcn_base_color: neutral", "shadcn_base_color: purple")
        with self.assertRaises(TokenError):
            parse_tokens(doc)

    def test_a_unitless_radius_is_refused(self):
        with self.assertRaises(TokenError):
            parse_tokens(self.doc.replace("radius: 0.5rem", "radius: 8"))

    def test_a_missing_required_colour_is_refused_by_name(self):
        doc = self.doc.replace('  accent: "#E8B44A"\n', "")
        with self.assertRaises(TokenError) as ctx:
            parse_tokens(doc)
        self.assertIn("accent", str(ctx.exception))

    def test_a_missing_font_role_is_refused_by_name(self):
        doc = self.doc.replace("  mono: JetBrains Mono\n", "")
        with self.assertRaises(TokenError) as ctx:
            parse_tokens(doc)
        self.assertIn("mono", str(ctx.exception))


class TestHexToHsl(unittest.TestCase):
    def test_known_conversions(self):
        self.assertEqual(hex_to_hsl("#000000"), "0 0% 0%")
        self.assertEqual(hex_to_hsl("#ffffff"), "0 0% 100%")
        self.assertEqual(hex_to_hsl("#ff0000"), "0 100% 50%")

    def test_shorthand_hex_expands(self):
        self.assertEqual(hex_to_hsl("#fff"), hex_to_hsl("#ffffff"))

    def test_a_non_hex_raises(self):
        with self.assertRaises(TokenError):
            hex_to_hsl("rebeccapurple")


class TestRendering(unittest.TestCase):
    def setUp(self):
        self.tokens = parse_tokens(render_design_doc(ProjectScan(name="d"), default_tokens_yaml()))

    def test_globals_css_carries_hsl_triples_not_hex(self):
        css = render_globals_css(self.tokens)
        self.assertIn("--background: 225 15% 5%", css)
        self.assertNotIn("#0B0C0F", css)

    def test_globals_css_has_the_tailwind_directives(self):
        css = render_globals_css(self.tokens)
        for directive in ("@tailwind base", "@tailwind components", "@tailwind utilities"):
            self.assertIn(directive, css)

    def test_tailwind_config_binds_the_font_variables(self):
        config = render_tailwind_config(self.tokens)
        for variable in ("var(--font-display)", "var(--font-body)", "var(--font-mono)"):
            self.assertIn(variable, config)

    def test_google_font_families_dedupe(self):
        tokens = DesignTokens(fonts={"display": "Inter", "body": "Inter", "mono": "DM Mono"})
        self.assertEqual(tokens.google_font_families(), ["Inter", "DM Mono"])


if __name__ == "__main__":
    unittest.main()
