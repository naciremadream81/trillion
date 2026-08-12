"""
Tests for the tool registry construction (agent/tools/registry.py).

Run from the project root:
    python -m unittest tests.test_registry
"""

import unittest

from agent.config import Settings
from agent.tools.registry import build_registry


class TestBuildRegistry(unittest.TestCase):
    def test_web_search_registered_when_key_configured(self):
        settings = Settings(brave_search_api_key="fake-brave-key")
        registry = build_registry(settings)
        self.assertIn("web_search", registry.names())

    def test_web_search_not_registered_when_key_missing(self):
        settings = Settings(brave_search_api_key="")
        registry = build_registry(settings)
        self.assertNotIn("web_search", registry.names())


if __name__ == "__main__":
    unittest.main()
