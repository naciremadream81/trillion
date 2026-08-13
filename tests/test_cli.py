"""
Tests for the unified CLI entry point (cli.py).

Run from the project root:
    python -m unittest tests.test_cli
"""

import sys
import types
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import cli


class FakeMainModule(types.ModuleType):
    def __init__(self):
        super().__init__("main")
        self.main = AsyncMock()


class FakeServeModule(types.ModuleType):
    def __init__(self):
        super().__init__("serve")
        self.main = MagicMock()


class TestCliDispatch(unittest.TestCase):
    def setUp(self):
        self.fake_main = FakeMainModule()
        self.fake_serve = FakeServeModule()
        self._patcher = patch.dict(sys.modules, {"main": self.fake_main, "serve": self.fake_serve})
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()

    def test_bare_command_runs_the_terminal_chat(self):
        with patch.object(sys, "argv", ["trillion"]):
            cli.main()
        self.fake_main.main.assert_called_once()
        self.fake_serve.main.assert_not_called()

    def test_serve_subcommand_runs_the_web_server(self):
        with patch.object(sys, "argv", ["trillion", "serve"]):
            cli.main()
        self.fake_serve.main.assert_called_once()
        self.fake_main.main.assert_not_called()

    def test_serve_subcommand_is_stripped_from_argv(self):
        with patch.object(sys, "argv", ["trillion", "serve"]):
            cli.main()
            self.assertEqual(sys.argv, ["trillion"])

    def test_extra_args_pass_through_to_the_chat_entry_point(self):
        with patch.object(sys, "argv", ["trillion", "--provider", "openai"]):
            cli.main()
            self.fake_main.main.assert_called_once()
            self.assertEqual(sys.argv, ["trillion", "--provider", "openai"])


if __name__ == "__main__":
    unittest.main()
