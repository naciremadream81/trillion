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


if __name__ == "__main__":
    unittest.main()
