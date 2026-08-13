"""
Tests for the Settings surface (agent/config.py).

Run from the project root:
    python -m unittest tests.test_config
"""

import os
import unittest

from agent.config import get_settings


class TestBraveSearchApiKey(unittest.TestCase):
    def setUp(self):
        self._prev = os.environ.get("BRAVE_SEARCH_API_KEY")

    def tearDown(self):
        if self._prev is None:
            os.environ.pop("BRAVE_SEARCH_API_KEY", None)
        else:
            os.environ["BRAVE_SEARCH_API_KEY"] = self._prev

    def test_defaults_to_empty_string(self):
        os.environ.pop("BRAVE_SEARCH_API_KEY", None)
        settings = get_settings()
        self.assertEqual(settings.brave_search_api_key, "")

    def test_reads_from_env(self):
        os.environ["BRAVE_SEARCH_API_KEY"] = "test-key-123"
        settings = get_settings()
        self.assertEqual(settings.brave_search_api_key, "test-key-123")


class TestFirecrawlApiKey(unittest.TestCase):
    def setUp(self):
        self._prev = os.environ.get("FIRECRAWL_API_KEY")

    def tearDown(self):
        if self._prev is None:
            os.environ.pop("FIRECRAWL_API_KEY", None)
        else:
            os.environ["FIRECRAWL_API_KEY"] = self._prev

    def test_defaults_to_empty_string(self):
        os.environ.pop("FIRECRAWL_API_KEY", None)
        settings = get_settings()
        self.assertEqual(settings.firecrawl_api_key, "")

    def test_reads_from_env(self):
        os.environ["FIRECRAWL_API_KEY"] = "fc-test-key-123"
        settings = get_settings()
        self.assertEqual(settings.firecrawl_api_key, "fc-test-key-123")


class TestFirecrawlBaseUrl(unittest.TestCase):
    def setUp(self):
        self._prev = os.environ.get("FIRECRAWL_BASE_URL")

    def tearDown(self):
        if self._prev is None:
            os.environ.pop("FIRECRAWL_BASE_URL", None)
        else:
            os.environ["FIRECRAWL_BASE_URL"] = self._prev

    def test_defaults_to_cloud_api(self):
        os.environ.pop("FIRECRAWL_BASE_URL", None)
        settings = get_settings()
        self.assertEqual(settings.firecrawl_base_url, "https://api.firecrawl.dev")

    def test_reads_from_env(self):
        os.environ["FIRECRAWL_BASE_URL"] = "http://localhost:3002"
        settings = get_settings()
        self.assertEqual(settings.firecrawl_base_url, "http://localhost:3002")


class TestSearchProvider(unittest.TestCase):
    def setUp(self):
        self._prev = os.environ.get("TRILLION_SEARCH_PROVIDER")

    def tearDown(self):
        if self._prev is None:
            os.environ.pop("TRILLION_SEARCH_PROVIDER", None)
        else:
            os.environ["TRILLION_SEARCH_PROVIDER"] = self._prev

    def test_defaults_to_empty_string(self):
        os.environ.pop("TRILLION_SEARCH_PROVIDER", None)
        settings = get_settings()
        self.assertEqual(settings.search_provider, "")

    def test_reads_from_env(self):
        os.environ["TRILLION_SEARCH_PROVIDER"] = "firecrawl"
        settings = get_settings()
        self.assertEqual(settings.search_provider, "firecrawl")


class TestMemoryPath(unittest.TestCase):
    def setUp(self):
        self._prev = os.environ.get("TRILLION_MEMORY_PATH")

    def tearDown(self):
        if self._prev is None:
            os.environ.pop("TRILLION_MEMORY_PATH", None)
        else:
            os.environ["TRILLION_MEMORY_PATH"] = self._prev

    def test_defaults_to_memory_facts_md(self):
        os.environ.pop("TRILLION_MEMORY_PATH", None)
        settings = get_settings()
        self.assertEqual(settings.memory_path, "memory/facts.md")

    def test_reads_from_env(self):
        os.environ["TRILLION_MEMORY_PATH"] = "/tmp/custom-facts.md"
        settings = get_settings()
        self.assertEqual(settings.memory_path, "/tmp/custom-facts.md")


class TestNotesVaultPath(unittest.TestCase):
    def setUp(self):
        self._prev = os.environ.get("TRILLION_NOTES_VAULT_PATH")

    def tearDown(self):
        if self._prev is None:
            os.environ.pop("TRILLION_NOTES_VAULT_PATH", None)
        else:
            os.environ["TRILLION_NOTES_VAULT_PATH"] = self._prev

    def test_defaults_to_aires_ai_brain(self):
        os.environ.pop("TRILLION_NOTES_VAULT_PATH", None)
        settings = get_settings()
        self.assertEqual(settings.notes_vault_path, os.path.expanduser("~/AiresAiBrain"))

    def test_reads_from_env(self):
        os.environ["TRILLION_NOTES_VAULT_PATH"] = "/tmp/custom-vault"
        settings = get_settings()
        self.assertEqual(settings.notes_vault_path, "/tmp/custom-vault")


class TestNotesIndexPath(unittest.TestCase):
    def setUp(self):
        self._prev = os.environ.get("TRILLION_NOTES_INDEX_PATH")

    def tearDown(self):
        if self._prev is None:
            os.environ.pop("TRILLION_NOTES_INDEX_PATH", None)
        else:
            os.environ["TRILLION_NOTES_INDEX_PATH"] = self._prev

    def test_defaults_to_memory_notes_index_db(self):
        os.environ.pop("TRILLION_NOTES_INDEX_PATH", None)
        settings = get_settings()
        self.assertEqual(settings.notes_index_path, "memory/notes_index.db")

    def test_reads_from_env(self):
        os.environ["TRILLION_NOTES_INDEX_PATH"] = "/tmp/custom-index.db"
        settings = get_settings()
        self.assertEqual(settings.notes_index_path, "/tmp/custom-index.db")


class TestWebHost(unittest.TestCase):
    def setUp(self):
        self._prev = os.environ.get("TRILLION_WEB_HOST")

    def tearDown(self):
        if self._prev is None:
            os.environ.pop("TRILLION_WEB_HOST", None)
        else:
            os.environ["TRILLION_WEB_HOST"] = self._prev

    def test_defaults_to_loopback(self):
        os.environ.pop("TRILLION_WEB_HOST", None)
        settings = get_settings()
        self.assertEqual(settings.web_host, "127.0.0.1")

    def test_reads_from_env(self):
        os.environ["TRILLION_WEB_HOST"] = "0.0.0.0"
        settings = get_settings()
        self.assertEqual(settings.web_host, "0.0.0.0")


class TestWebAuthToken(unittest.TestCase):
    def setUp(self):
        self._prev = os.environ.get("TRILLION_WEB_AUTH_TOKEN")

    def tearDown(self):
        if self._prev is None:
            os.environ.pop("TRILLION_WEB_AUTH_TOKEN", None)
        else:
            os.environ["TRILLION_WEB_AUTH_TOKEN"] = self._prev

    def test_defaults_to_empty_string(self):
        os.environ.pop("TRILLION_WEB_AUTH_TOKEN", None)
        settings = get_settings()
        self.assertEqual(settings.web_auth_token, "")

    def test_reads_from_env(self):
        os.environ["TRILLION_WEB_AUTH_TOKEN"] = "test-token-abc"
        settings = get_settings()
        self.assertEqual(settings.web_auth_token, "test-token-abc")


if __name__ == "__main__":
    unittest.main()
