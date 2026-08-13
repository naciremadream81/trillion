"""
Tests for agent/security/subprocess_env.py (agent-security.md §1.3).

Run from the project root:
    python -m unittest tests.test_subprocess_env
"""

import os
import unittest
from unittest.mock import patch

from agent.security.subprocess_env import full, shell_minimal, with_keys


class TestShellMinimal(unittest.TestCase):
    def test_excludes_secrets(self):
        with patch.dict(
            os.environ,
            {"PATH": "/usr/bin", "HOME": "/home/x", "ANTHROPIC_API_KEY": "sk-ant-secret"},
        ):
            env = shell_minimal()
        self.assertNotIn("ANTHROPIC_API_KEY", env)

    def test_includes_baseline_keys_present_in_environ(self):
        with patch.dict(os.environ, {"PATH": "/usr/bin", "HOME": "/home/x"}):
            env = shell_minimal()
        self.assertEqual(env["PATH"], "/usr/bin")
        self.assertEqual(env["HOME"], "/home/x")

    def test_omits_baseline_keys_not_present_in_environ(self):
        with patch.dict(os.environ, {"PATH": "/usr/bin"}, clear=True):
            env = shell_minimal()
        self.assertNotIn("DISPLAY", env)


class TestWithKeys(unittest.TestCase):
    def test_adds_only_named_keys(self):
        with patch.dict(
            os.environ,
            {
                "PATH": "/usr/bin",
                "ANTHROPIC_API_KEY": "sk-ant-secret",
                "GITHUB_TOKEN": "ghp_secret",
                "STRIPE_KEY": "sk_live_secret",
            },
        ):
            env = with_keys("ANTHROPIC_API_KEY")
        self.assertEqual(env["ANTHROPIC_API_KEY"], "sk-ant-secret")
        self.assertNotIn("GITHUB_TOKEN", env)
        self.assertNotIn("STRIPE_KEY", env)

    def test_missing_named_key_is_silently_omitted(self):
        with patch.dict(os.environ, {"PATH": "/usr/bin"}, clear=True):
            env = with_keys("ANTHROPIC_API_KEY")
        self.assertNotIn("ANTHROPIC_API_KEY", env)


class TestFull(unittest.TestCase):
    def test_requires_non_empty_reason(self):
        with self.assertRaises(ValueError):
            full("")
        with self.assertRaises(ValueError):
            full("   ")

    def test_returns_full_environment_with_reason(self):
        with patch.dict(os.environ, {"SOME_SECRET": "value"}):
            env = full("test needs the full env")
        self.assertEqual(env["SOME_SECRET"], "value")


if __name__ == "__main__":
    unittest.main()
