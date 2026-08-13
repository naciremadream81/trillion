"""
Tests for agent/security/startup_guard.py (agent-security.md §1.5, adapted).

Run from the project root:
    python -m unittest tests.test_startup_guard
"""

import unittest

from agent.security.startup_guard import UnsafeBindError, check_bind_safety


class TestCheckBindSafety(unittest.TestCase):
    def test_loopback_bind_allowed_without_auth(self):
        check_bind_safety("127.0.0.1", auth_configured=False)
        check_bind_safety("localhost", auth_configured=False)
        check_bind_safety("::1", auth_configured=False)

    def test_public_bind_refused_without_auth(self):
        with self.assertRaises(UnsafeBindError):
            check_bind_safety("0.0.0.0", auth_configured=False)

    def test_public_bind_allowed_with_auth(self):
        check_bind_safety("0.0.0.0", auth_configured=True)

    def test_error_message_names_the_host_and_the_fix(self):
        with self.assertRaises(UnsafeBindError) as ctx:
            check_bind_safety("0.0.0.0", auth_configured=False)
        message = str(ctx.exception)
        self.assertIn("0.0.0.0", message)
        self.assertIn("TRILLION_WEB_AUTH_TOKEN", message)


if __name__ == "__main__":
    unittest.main()
