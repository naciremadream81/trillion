"""
Tests for the scout document endpoints — playbook/opportunity-scout.md Tier 5.

Two things are being defended here.

The first is the trap the playbook calls the most expensive one in the whole
document: a settings panel that gates on a lookup table doesn't error when an
entry is missing, it LIES — it shows an empty textarea and "no default
available" for a document that is loaded on every single run, and the operator
concludes the feature doesn't exist. TestEveryEditableDocumentHasARealDefault
below asserts against the real EDITABLE_DOCUMENTS tuple rather than a copy of
it, because a hand-copied lookup in a test file is not coverage: it drifts
from the original, both stay green, and the bug ships.

The second is the full round trip through the real endpoints — read, edit,
read back as overridden, revert, read back clean with nothing left behind.

Run from the project root:
    python -m unittest tests.test_scout_documents_endpoint
"""

import os
import shutil
import tempfile
import unittest

from aiohttp.test_utils import AioHTTPTestCase

import serve as serve_module
from agent.factory.software.doctrine import (
    DOCTRINE_KEY,
    EDITABLE_DOCUMENTS,
    LANES_KEY,
    find_editable,
    load_document,
    validate_document,
)
from agent.factory.software.storage import BuildRepo
from agent.providers.base import BaseProvider, ProviderResponse, TextChunk, TokenUsage
from agent.tools.registry import ToolRegistry


class FakeProvider(BaseProvider):
    @property
    def model_name(self):
        return "fake-model"

    async def stream(self, messages, system, tools=None):
        yield TextChunk(text="")
        yield ProviderResponse(text="", tool_calls=[], usage=TokenUsage(), model=self.model_name)


class TestEveryEditableDocumentHasARealDefault(unittest.TestCase):
    def test_no_registered_document_is_missing_its_shipped_file(self):
        # Derived from the registry itself. If someone adds a third document
        # and forgets to ship its file, this fails — which is the entire
        # point. A list retyped here would not notice.
        missing = [d.key for d in EDITABLE_DOCUMENTS if not load_document(d.path).strip()]
        self.assertEqual(missing, [], f"editable documents with no real default: {missing}")

    def test_every_registered_document_has_a_validator_that_accepts_its_default(self):
        # A default the validator rejects is the same lie in a different
        # shape: the panel renders it, the operator saves it back unchanged,
        # and the save fails for reasons they cannot act on.
        for document in EDITABLE_DOCUMENTS:
            with self.subTest(key=document.key):
                self.assertIsNone(validate_document(document.key, load_document(document.path)))

    def test_every_registered_document_has_a_distinct_key_and_path(self):
        keys = [d.key for d in EDITABLE_DOCUMENTS]
        paths = [d.path for d in EDITABLE_DOCUMENTS]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertEqual(len(paths), len(set(paths)))

    def test_find_editable_routes_by_the_registry_not_by_the_caller(self):
        # A write that trusts the key in the URL will paste a lane document
        # over the doctrine: same table, no error, no way to notice.
        self.assertIsNotNone(find_editable(DOCTRINE_KEY))
        self.assertIsNone(find_editable("scout_doctrine/../lanes"))
        self.assertIsNone(find_editable("some_other_agent_prompt"))
        self.assertIsNone(find_editable(""))


class TestScoutDocumentEndpoints(AioHTTPTestCase):
    async def get_application(self):
        self.tmp = tempfile.mkdtemp()
        self._prev_env = {
            key: os.environ.get(key)
            for key in (
                "TRILLION_SOFTWARE_FACTORY_DB",
                "TRILLION_FACTORY_DB",
                "TRILLION_NOTES_VAULT_PATH",
                "TRILLION_NOTES_INDEX_PATH",
                "TRILLION_HEARTBEAT_DB",
                "TRILLION_CSP_REPORT_DB",
                "TRILLION_WEB_AUTH_TOKEN",
                "GITHUB_TOKEN",
                "TRILLION_GITHUB_WATCHED_REPOS",
                "TRILLION_FACTORY_AUTONOMOUS_THEMES",
            )
        }
        os.environ["TRILLION_SOFTWARE_FACTORY_DB"] = os.path.join(self.tmp, "software_factory.db")
        os.environ["TRILLION_FACTORY_DB"] = os.path.join(self.tmp, "factory.db")
        os.environ["TRILLION_NOTES_VAULT_PATH"] = os.path.join(self.tmp, "vault")
        os.environ["TRILLION_NOTES_INDEX_PATH"] = os.path.join(self.tmp, "notes_index.db")
        os.environ["TRILLION_HEARTBEAT_DB"] = os.path.join(self.tmp, "heartbeat.db")
        os.environ["TRILLION_CSP_REPORT_DB"] = os.path.join(self.tmp, "csp_reports.db")
        os.environ.pop("TRILLION_WEB_AUTH_TOKEN", None)
        os.environ.pop("GITHUB_TOKEN", None)
        os.environ.pop("TRILLION_GITHUB_WATCHED_REPOS", None)
        os.environ.pop("TRILLION_FACTORY_AUTONOMOUS_THEMES", None)

        serve_module._provider = FakeProvider()
        serve_module._registry = ToolRegistry()
        serve_module._agent = None
        return serve_module.build_app()

    async def tearDownAsync(self):
        for key, value in self._prev_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        shutil.rmtree(self.tmp, ignore_errors=True)

    async def read(self):
        resp = await self.client.request("GET", "/api/scout/documents")
        self.assertEqual(resp.status, 200)
        data = await resp.json()
        return {d["key"]: d for d in data["documents"]}, data

    async def test_read_returns_every_registered_document_populated(self):
        documents, payload = await self.read()
        self.assertEqual(set(documents), {d.key for d in EDITABLE_DOCUMENTS})
        for key, document in documents.items():
            with self.subTest(key=key):
                # The anti-lying assertion, at the HTTP boundary this time:
                # a populated, enabled textarea, not an empty one.
                self.assertTrue(document["effective"].strip(), key)
                self.assertTrue(document["default"].strip(), key)
                self.assertTrue(document["editable"], key)
                self.assertFalse(document["is_overridden"], key)
        self.assertFalse(payload["any_overridden"])
        self.assertGreater(len(payload["lanes"]), 1)

    async def test_full_round_trip_edit_then_revert(self):
        # The Tier 5 verification, end to end through the real endpoints.
        edited = "## Only Lane\n\nHunt exactly one thing.\n"
        resp = await self.client.request(
            "POST", "/api/scout/documents", json={"documents": {LANES_KEY: edited}}
        )
        self.assertEqual(resp.status, 200)

        documents, payload = await self.read()
        self.assertTrue(documents[LANES_KEY]["is_overridden"])
        self.assertEqual(documents[LANES_KEY]["effective"], edited)
        self.assertTrue(payload["any_overridden"])
        self.assertEqual([lane["label"] for lane in payload["lanes"]], ["only-lane"])

        # The narrow/broad distinction: the lanes override must not make the
        # doctrine look overridden, or reverting the doctrine becomes a no-op
        # delete that looks like it worked.
        self.assertFalse(documents[DOCTRINE_KEY]["is_overridden"])
        self.assertEqual(documents[DOCTRINE_KEY]["effective"], documents[DOCTRINE_KEY]["default"])

        resp = await self.client.request(
            "POST", "/api/scout/documents/revert", json={"key": LANES_KEY}
        )
        self.assertEqual(resp.status, 200)

        documents, payload = await self.read()
        self.assertFalse(documents[LANES_KEY]["is_overridden"])
        self.assertEqual(documents[LANES_KEY]["effective"], documents[LANES_KEY]["default"])
        self.assertFalse(payload["any_overridden"])
        # Nothing left behind.
        repo = BuildRepo(os.environ["TRILLION_SOFTWARE_FACTORY_DB"])
        self.assertEqual(repo.all_documents(), {})

    async def test_a_rejected_document_leaves_no_partial_write(self):
        # Validate all, then write. Otherwise a rejected second textarea
        # leaves the first one half-saved and the operator cannot tell.
        resp = await self.client.request(
            "POST",
            "/api/scout/documents",
            json={"documents": {DOCTRINE_KEY: "a perfectly good doctrine",
                                LANES_KEY: "no headings here at all"}},
        )
        self.assertEqual(resp.status, 400)
        payload = await resp.json()
        self.assertIn(LANES_KEY, payload["errors"])
        self.assertNotIn(DOCTRINE_KEY, payload["errors"])

        repo = BuildRepo(os.environ["TRILLION_SOFTWARE_FACTORY_DB"])
        self.assertEqual(repo.all_documents(), {}, "the valid half was written anyway")

    async def test_an_empty_doctrine_is_refused_with_a_usable_message(self):
        resp = await self.client.request(
            "POST", "/api/scout/documents", json={"documents": {DOCTRINE_KEY: "   "}}
        )
        self.assertEqual(resp.status, 400)
        payload = await resp.json()
        self.assertIn("Revert", payload["errors"][DOCTRINE_KEY])

    async def test_an_unregistered_key_is_refused(self):
        resp = await self.client.request(
            "POST", "/api/scout/documents", json={"documents": {"system_prompt": "hi"}}
        )
        self.assertEqual(resp.status, 400)

    async def test_reverting_an_unregistered_key_is_refused(self):
        resp = await self.client.request(
            "POST", "/api/scout/documents/revert", json={"key": "system_prompt"}
        )
        self.assertEqual(resp.status, 400)

    async def test_an_empty_save_is_refused(self):
        resp = await self.client.request("POST", "/api/scout/documents", json={"documents": {}})
        self.assertEqual(resp.status, 400)

    async def test_reverting_a_document_with_no_override_is_harmless(self):
        resp = await self.client.request(
            "POST", "/api/scout/documents/revert", json={"key": DOCTRINE_KEY}
        )
        self.assertEqual(resp.status, 200)
        documents, _ = await self.read()
        self.assertTrue(documents[DOCTRINE_KEY]["effective"].strip())


if __name__ == "__main__":
    unittest.main()
