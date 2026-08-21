"""
Tests for the preview scaffold and component catalog — Tiers 2 and 4 of
playbooks/design-subagent.md.

The three next.config settings are asserted individually rather than as "a
config was written", because each is one of the playbook's named failure
modes and each fails in a way that surfaces deep inside a Claude Code run
rather than here.

Run from the project root:
    python -m unittest tests.test_design_scaffold
"""

import os
import shutil
import tempfile
import unittest

from agent.design.component_catalog import (
    MAGICUI_COMPONENTS,
    SHADCN_COMPONENTS,
    SNAPSHOT_LIBRARIES,
    all_entries,
    install_command,
    render_font_catalog_for_prompt,
    render_for_prompt,
    render_full_catalog_markdown,
    validate_component_names,
)
from agent.design.design_tokens import default_tokens_yaml, parse_tokens
from agent.design.docs import DesignDocError, ProjectScan, render_design_doc
from agent.design.scaffold import (
    MIN_FREE_BYTES,
    expected_output_path,
    prepare_scaffold,
    preview_base_path,
    scaffold_files,
)


def make_tokens():
    return parse_tokens(render_design_doc(ProjectScan(name="demo"), default_tokens_yaml()))


class TestCatalog(unittest.TestCase):
    def test_only_cli_installable_libraries_are_catalogued(self):
        # The tier's named stumbling block: Aceternity and Reactbits are
        # copy-paste only, so cataloguing them makes Claude Code attempt an
        # install that cannot work.
        catalogued = {e.name.lower() for e in all_entries()}
        for name, _ in SNAPSHOT_LIBRARIES:
            self.assertNotIn(name.lower(), catalogued)
        self.assertIsNone(install_command("spotlight"))
        self.assertIsNone(install_command("background-beams"))

    def test_every_entry_has_a_working_install_command(self):
        for entry in all_entries():
            with self.subTest(entry=entry.name):
                self.assertTrue(entry.install.startswith(("npx ", "npm ")))
                self.assertIn(entry.name, entry.install)

    def test_known_components_resolve(self):
        self.assertEqual(install_command("card"), "npx shadcn@latest add card")
        self.assertEqual(install_command("particles"), "npx magicui-cli add particles")

    def test_unknown_names_are_separated_rather_than_raising(self):
        # A hint naming a component we don't stock is a reason to drop that
        # hint, not to fail the dispatch.
        known, unknown = validate_component_names(["card", "spotlight", "", "particles"])
        self.assertEqual(known, ["card", "particles"])
        self.assertEqual(unknown, ["spotlight"])

    def test_the_prompt_rendering_stays_compact(self):
        # It is paid for on every turn.
        self.assertLess(len(render_for_prompt()), 1200)

    def test_the_prompt_rendering_forbids_uncatalogued_installs(self):
        self.assertIn("not installable", render_for_prompt())

    def test_the_full_catalog_names_what_is_unavailable(self):
        markdown = render_full_catalog_markdown()
        self.assertIn("Not available here", markdown)
        for name, _ in SNAPSHOT_LIBRARIES:
            self.assertIn(name, markdown)

    def test_tier_six_required_elements_are_stocked(self):
        # Tier 6 requires a visible background texture from a specific set;
        # the catalog has to actually carry them.
        names = {e.name for e in MAGICUI_COMPONENTS}
        for required in ("grid-pattern", "dot-pattern", "particles"):
            self.assertIn(required, names)

    def test_the_font_catalog_lists_the_blocked_families(self):
        rendered = render_font_catalog_for_prompt()
        self.assertIn("Space Grotesk", rendered)
        self.assertIn("Never use", rendered)


class TestScaffoldContents(unittest.TestCase):
    def setUp(self):
        self.files = scaffold_files(make_tokens(), "demo-project")

    def test_static_export_is_set(self):
        self.assertIn("output: 'export'", self.files["next.config.mjs"])

    def test_trailing_slash_is_set(self):
        # Without it Next emits <screen>.html and the serving endpoint looks
        # for out/<feature>/<screen>/index.html, which isn't there. The
        # playbook is explicit that the fix is this setting, not relaxing
        # what the server expects.
        self.assertIn("trailingSlash: true", self.files["next.config.mjs"])

    def test_both_base_path_and_asset_prefix_are_set(self):
        # basePath alone leaves asset URLs unprefixed, so every script and
        # stylesheet 404s under the serving prefix.
        config = self.files["next.config.mjs"]
        prefix = preview_base_path("demo-project")
        self.assertIn(f"basePath: '{prefix}'", config)
        self.assertIn(f"assetPrefix: '{prefix}'", config)

    def test_images_are_unoptimized(self):
        # Static export has no image optimizer.
        self.assertIn("unoptimized: true", self.files["next.config.mjs"])

    def test_fonts_import_by_identifier_not_display_name(self):
        fonts = self.files["lib/fonts.ts"]
        self.assertIn("Instrument_Serif", fonts)
        self.assertNotIn("{ Instrument Serif }", fonts)

    def test_a_non_variable_family_gets_an_explicit_weight(self):
        # next/font/google rejects a non-variable family with no weight, and
        # that failure surfaces inside a Claude Code run rather than here.
        self.assertIn("weight: ['400']", self.files["lib/fonts.ts"])

    def test_a_variable_family_gets_no_weight(self):
        fonts = self.files["lib/fonts.ts"]
        body_block = fonts[fonts.index("export const bodyFont"):]
        self.assertNotIn("weight:", body_block.split("});")[0])

    def test_layout_applies_all_three_font_variables(self):
        layout = self.files["app/layout.tsx"]
        for variable in ("displayFont.variable", "bodyFont.variable", "monoFont.variable"):
            self.assertIn(variable, layout)

    def test_globals_css_comes_from_the_tokens(self):
        self.assertIn("--background: 225 15% 5%", self.files["app/globals.css"])

    def test_components_json_carries_the_base_colour(self):
        self.assertIn('"baseColor": "neutral"', self.files["components.json"])

    def test_the_catalog_is_written_for_claude_code_to_read(self):
        self.assertIn("Component catalog", self.files["prism/component_catalog.md"])

    def test_build_output_is_gitignored(self):
        ignored = self.files[".gitignore"]
        for entry in ("node_modules/", ".next/", "out/"):
            self.assertIn(entry, ignored)


class TestScaffoldWriting(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.project_root = os.path.join(self.tmp, "demo-project")
        os.makedirs(self.project_root)
        self.tokens = make_tokens()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_it_writes_every_file(self):
        result = prepare_scaffold(self.project_root, "demo-project", self.tokens)
        self.assertTrue(result["scaffolded"])
        for relative in scaffold_files(self.tokens, "demo-project"):
            self.assertTrue(
                os.path.isfile(os.path.join(result["preview_root"], relative)), relative
            )

    def test_it_is_idempotent_on_package_json(self):
        prepare_scaffold(self.project_root, "demo-project", self.tokens)
        again = prepare_scaffold(self.project_root, "demo-project", self.tokens)
        self.assertFalse(again["scaffolded"])
        self.assertEqual(again["written"], [])

    def test_a_second_scaffold_does_not_clobber_composed_pages(self):
        result = prepare_scaffold(self.project_root, "demo-project", self.tokens)
        page = os.path.join(result["preview_root"], "app", "landing", "hero", "page.tsx")
        os.makedirs(os.path.dirname(page))
        with open(page, "w") as f:
            f.write("composed output")
        prepare_scaffold(self.project_root, "demo-project", self.tokens, force=True)
        with open(page) as f:
            self.assertEqual(f.read(), "composed output")

    def test_it_refuses_when_the_disk_is_nearly_full(self):
        # Not in the playbook. Filling this volume doesn't just fail the
        # scaffold — Trillion runs on it.
        import agent.design.scaffold as scaffold_module

        original = scaffold_module.free_bytes
        scaffold_module.free_bytes = lambda path: MIN_FREE_BYTES - 1
        try:
            with self.assertRaises(DesignDocError) as ctx:
                prepare_scaffold(self.project_root, "demo-project", self.tokens)
            self.assertIn("Refusing to scaffold", str(ctx.exception))
        finally:
            scaffold_module.free_bytes = original

    def test_the_scaffold_stays_inside_the_project(self):
        result = prepare_scaffold(self.project_root, "demo-project", self.tokens)
        self.assertTrue(
            os.path.realpath(result["preview_root"]).startswith(
                os.path.realpath(self.project_root)
            )
        )

    def test_expected_output_path_matches_trailing_slash_layout(self):
        # The "did the build actually succeed" check, and it has to agree
        # with what trailingSlash: true makes Next emit.
        result = prepare_scaffold(self.project_root, "demo-project", self.tokens)
        path = expected_output_path(result["preview_root"], "landing", "hero")
        self.assertTrue(path.endswith(os.path.join("out", "landing", "hero", "index.html")))

    def test_expected_output_path_refuses_traversal(self):
        result = prepare_scaffold(self.project_root, "demo-project", self.tokens)
        with self.assertRaises(DesignDocError):
            expected_output_path(result["preview_root"], "../../etc", "passwd")


class TestPreviewServing(unittest.IsolatedAsyncioTestCase):
    """
    Codex review, P1: generate_mockup advertised /api/design/.../preview/ URLs
    and the exported Next app emits its assets under the same prefix, but no
    route existed — every advertised preview and every script it loaded 404'd.
    """

    async def asyncSetUp(self):
        from aiohttp.test_utils import TestClient, TestServer

        import serve as serve_module
        from agent.providers.base import BaseProvider, ProviderResponse, TextChunk, TokenUsage
        from agent.tools.registry import ToolRegistry

        class FakeProvider(BaseProvider):
            @property
            def model_name(self):
                return "fake"

            async def stream(self, messages, system, tools=None):
                yield TextChunk(text="")
                yield ProviderResponse(text="", tool_calls=[], usage=TokenUsage(), model="fake")

        self.tmp = tempfile.mkdtemp()
        self.projects = os.path.join(self.tmp, "projects")
        os.makedirs(os.path.join(self.projects, "demo-project"))

        self._prev = {k: os.environ.get(k) for k in (
            "TRILLION_DESIGN_AGENT", "TRILLION_SOFTWARE_FACTORY_ROOT",
            "TRILLION_FACTORY_DB", "TRILLION_NOTES_VAULT_PATH",
            "TRILLION_NOTES_INDEX_PATH", "TRILLION_HEARTBEAT_DB", "TRILLION_CSP_REPORT_DB")}
        os.environ["TRILLION_DESIGN_AGENT"] = "true"
        os.environ["TRILLION_SOFTWARE_FACTORY_ROOT"] = self.projects
        os.environ["TRILLION_FACTORY_DB"] = os.path.join(self.tmp, "f.db")
        os.environ["TRILLION_NOTES_VAULT_PATH"] = os.path.join(self.tmp, "v")
        os.environ["TRILLION_NOTES_INDEX_PATH"] = os.path.join(self.tmp, "n.db")
        os.environ["TRILLION_HEARTBEAT_DB"] = os.path.join(self.tmp, "h.db")
        os.environ["TRILLION_CSP_REPORT_DB"] = os.path.join(self.tmp, "c.db")

        # A built screen, laid out the way trailingSlash: true makes Next emit.
        out = os.path.join(self.projects, "demo-project", ".prism", "preview", "out")
        os.makedirs(os.path.join(out, "landing", "hero"))
        with open(os.path.join(out, "landing", "hero", "index.html"), "w") as f:
            f.write("<html>hero</html>")
        os.makedirs(os.path.join(out, "_next", "static"))
        with open(os.path.join(out, "_next", "static", "app.js"), "w") as f:
            f.write("console.log(1)")
        with open(os.path.join(out, "index.html"), "w") as f:
            f.write("<html>index</html>")

        self.serve_module = serve_module
        serve_module._provider = FakeProvider()
        serve_module._registry = ToolRegistry()
        self.client = TestClient(TestServer(serve_module.build_app()))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self.serve_module._provider = None
        self.serve_module._registry = None
        for k, v in self._prev.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    async def test_a_generated_screen_is_served(self):
        # The exact URL generate_mockup hands back.
        resp = await self.client.get("/api/design/demo-project/preview/landing/hero/")
        self.assertEqual(resp.status, 200)
        self.assertIn("hero", await resp.text())

    async def test_next_assets_are_served(self):
        # basePath + assetPrefix make the exported app request these.
        resp = await self.client.get("/api/design/demo-project/preview/_next/static/app.js")
        self.assertEqual(resp.status, 200)

    async def test_the_preview_root_serves_the_index(self):
        resp = await self.client.get("/api/design/demo-project/preview/")
        self.assertEqual(resp.status, 200)

    async def test_an_ungenerated_screen_is_a_clear_404(self):
        resp = await self.client.get("/api/design/demo-project/preview/landing/nope/")
        self.assertEqual(resp.status, 404)

    async def test_traversal_out_of_the_project_is_refused(self):
        for path in ("/api/design/demo-project/preview/../../../../etc/passwd",
                     "/api/design/../../etc/preview/passwd"):
            with self.subTest(path=path):
                resp = await self.client.get(path)
                self.assertNotEqual(resp.status, 200)

    async def test_an_unknown_project_is_a_404(self):
        resp = await self.client.get("/api/design/no-such-project/preview/landing/hero/")
        self.assertEqual(resp.status, 404)


if __name__ == "__main__":
    unittest.main()
