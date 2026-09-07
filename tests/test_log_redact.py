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

    def test_redacts_x_auth_token_header(self):
        result = redact("X-Auth-Token: s3cret-value-here")
        self.assertIn("X-Auth-Token: <redacted>", result)
        self.assertNotIn("s3cret-value-here", result)

    def test_redacts_token_query_param(self):
        # playbook/mobile-pwa.md §6 puts a live credential in the request
        # line, which is exactly the sort of thing that ends up in a log.
        result = redact('GET /api/tts/abc?token=s3cret-value-here HTTP/1.1')
        self.assertNotIn("s3cret-value-here", result)
        self.assertIn("token=<redacted>", result)

    def test_redacts_token_param_mid_query_without_eating_the_rest(self):
        result = redact("/api/tts?turn=42&token=s3cret&format=mp3")
        self.assertNotIn("s3cret", result)
        self.assertIn("turn=42", result)
        self.assertIn("format=mp3", result)

    def test_redacts_access_token_aliases(self):
        for name in ("access_token", "auth_token"):
            with self.subTest(name=name):
                result = redact(f"https://example.test/x?{name}=s3cret-value-here")
                self.assertNotIn("s3cret-value-here", result)
                self.assertIn(f"{name}=<redacted>", result)

    def test_leaves_unrelated_params_named_like_tokens_alone(self):
        # `tokens` is not `token` — a usage log line should survive intact.
        result = redact("/api/usage?tokens=1200")
        self.assertIn("tokens=1200", result)

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
