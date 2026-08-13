"""
Tests for agent/security/log_redact.py (agent-security.md §1.1).

Run from the project root:
    python -m unittest tests.test_log_redact
"""

import unittest

from agent.security.log_redact import redact


class TestRedact(unittest.TestCase):
    def test_empty_input_returns_empty_string(self):
        self.assertEqual(redact(""), "")
        self.assertEqual(redact(None), "")

    def test_redacts_bearer_header(self):
        result = redact("Authorization: Bearer abc123.def456")
        self.assertIn("Authorization: Bearer <redacted>", result)
        self.assertNotIn("abc123", result)

    def test_redacts_anthropic_api_key(self):
        result = redact("key is sk-ant-api03-abcdefghijklmnop")
        self.assertNotIn("abcdefghijklmnop", result)
        self.assertIn("<redacted-api-key>", result)

    def test_redacts_stripe_style_key(self):
        result = redact("stripe key sk_live_4242424242424242abcd")
        self.assertNotIn("4242424242424242abcd", result)
        self.assertIn("<redacted-api-key>", result)

    def test_redacts_github_token(self):
        result = redact("token ghp_1234567890abcdefghij1234567890abcd")
        self.assertNotIn("1234567890abcdefghij", result)
        self.assertIn("<redacted-api-key>", result)

    def test_redacts_jwt(self):
        jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"
        result = redact(f"token={jwt}")
        self.assertIn("<redacted-jwt>", result)
        self.assertNotIn(jwt, result)

    def test_redacts_connection_string_password(self):
        result = redact("postgres://myuser:supersecret@db.example.com:5432/mydb")
        self.assertIn("postgres://myuser:<redacted>@db.example.com", result)
        self.assertNotIn("supersecret", result)

    def test_redacts_credit_card_keeping_last_four(self):
        result = redact("card on file: 4242 4242 4242 4242")
        self.assertIn("4242", result)
        self.assertNotIn("4242 4242 4242 4242", result)
        self.assertIn("<redacted-card>", result)

    def test_redacts_email_local_part(self):
        result = redact("contact sean@example.com for details")
        self.assertNotIn("sean@example.com", result)
        self.assertIn("<redacted>@example.com", result)

    def test_benign_text_passes_through_unchanged(self):
        text = "the build finished with 3 warnings and 0 errors"
        self.assertEqual(redact(text), text)

    def test_max_len_truncates_after_redaction(self):
        text = "a" * 1000
        self.assertEqual(len(redact(text, max_len=50)), 50)

    def test_default_max_len_is_500(self):
        text = "a" * 1000
        self.assertEqual(len(redact(text)), 500)


if __name__ == "__main__":
    unittest.main()
