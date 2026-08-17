"""
Tests for serve.py's web-port selection.

Run from the project root:
    python -m unittest tests.test_serve_port
"""

import socket
import unittest
from unittest.mock import patch

import serve


class TestServePortSelection(unittest.TestCase):
    def test_default_port_falls_forward_when_busy(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            busy_port = listener.getsockname()[1]

            with patch("builtins.print") as print_mock:
                selected = serve._select_web_port("127.0.0.1", busy_port)

        self.assertGreater(selected, busy_port)
        self.assertLessEqual(selected, busy_port + 10)
        print_mock.assert_called_once_with(
            f"Port {busy_port} is busy on 127.0.0.1; starting on {selected} instead."
        )

    def test_configured_port_falls_forward_when_busy(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            busy_port = listener.getsockname()[1]

            with patch("builtins.print"):
                selected = serve._select_web_port("127.0.0.1", busy_port)

        self.assertGreater(selected, busy_port)

    def test_strict_busy_port_exits_with_clear_message(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            busy_port = listener.getsockname()[1]

            next_port = busy_port + 1
            with self.assertRaisesRegex(SystemExit, f"TRILLION_WEB_PORT={next_port}"):
                serve._select_web_port("127.0.0.1", busy_port, strict=True)


if __name__ == "__main__":
    unittest.main()
