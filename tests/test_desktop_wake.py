"""
Tests for the desktop app's server-wake logic — playbook/desktop-app.md
Tier 4.

Scope note, stated plainly: this covers the ONLY part of desktop/ that can be
tested off macOS. Tier 2 (the WKWebView media-capture hook), Tier 3
(window.open routing) and Tier 5 (the .app bundle, the embedded Python, the
ad-hoc signature) are macOS runtime behaviours with no test surface on Linux,
and they are unverified. desktop/README.md says so too.

What IS here is the part most likely to be wrong anyway: "is it up", "how do
I start it on this machine", and "how long do I wait".

Run from the project root:
    python -m unittest tests.test_desktop_wake
"""

import os
import sys
import unittest
import urllib.error
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "desktop"))

import wake  # noqa: E402


class TestIsUp(unittest.TestCase):
    def test_a_200_is_up(self):
        class Response:
            def __enter__(self): return self
            def __exit__(self, *a): return False

        with patch.object(wake.urllib.request, "urlopen", return_value=Response()):
            self.assertTrue(wake.is_up("http://x/"))

    def test_a_401_is_UP_not_down(self):
        # The subtle one. A server with TRILLION_WEB_AUTH_TOKEN set answers an
        # unauthenticated probe with 401 — it is emphatically running.
        # Reading that as "down" would make the app try to start an
        # already-running server on every single launch.
        error = urllib.error.HTTPError("http://x/", 401, "Unauthorized", {}, None)
        with patch.object(wake.urllib.request, "urlopen", side_effect=error):
            self.assertTrue(wake.is_up("http://x/"))

    def test_connection_refused_is_down(self):
        with patch.object(wake.urllib.request, "urlopen", side_effect=ConnectionRefusedError()):
            self.assertFalse(wake.is_up("http://x/"))

    def test_a_timeout_is_down_not_an_exception(self):
        with patch.object(wake.urllib.request, "urlopen", side_effect=TimeoutError()):
            self.assertFalse(wake.is_up("http://x/"))


class TestWakeCommand(unittest.TestCase):
    def test_launchctl_wins_where_it_exists(self):
        with patch.object(wake.shutil, "which", lambda name: "/bin/launchctl" if name == "launchctl" else None):
            command = wake.wake_command()
        self.assertEqual(command[:2], ["launchctl", "kickstart"])

    def test_systemd_is_used_when_there_is_no_launchctl(self):
        with patch.object(wake.shutil, "which", lambda name: "/bin/systemctl" if name == "systemctl" else None):
            command = wake.wake_command()
        self.assertEqual(command[:3], ["systemctl", "--user", "start"])

    def test_no_supervisor_is_a_real_answer_not_a_failure(self):
        with patch.object(wake.shutil, "which", lambda name: None):
            self.assertIsNone(wake.wake_command())


class TestStartServer(unittest.TestCase):
    def test_it_asks_the_supervisor_when_there_is_one(self):
        called = {}

        def runner(command, **kwargs):
            called["command"] = command

        with patch.object(wake.shutil, "which", lambda n: "/bin/systemctl" if n == "systemctl" else None):
            message = wake.start_server("/project", runner=runner)
        self.assertIn("systemctl", called["command"])
        self.assertIn("supervisor", message)

    def test_it_launches_serve_directly_with_no_supervisor(self):
        called = {}

        def runner(command, **kwargs):
            called["command"] = command
            called["kwargs"] = kwargs

        with patch.object(wake.shutil, "which", lambda n: None):
            message = wake.start_server("/project", runner=runner)
        self.assertTrue(called["command"][1].endswith("serve.py"))
        # Detached, so quitting the window doesn't take the server with it.
        self.assertTrue(called["kwargs"]["start_new_session"])
        self.assertIn("directly", message)

    def test_a_failure_to_start_is_reported_not_raised(self):
        def runner(command, **kwargs):
            raise OSError("no such file")

        with patch.object(wake.shutil, "which", lambda n: None):
            message = wake.start_server("/project", runner=runner)
        self.assertIn("could not start", message)

    def test_a_supervisor_failure_still_waits(self):
        # Somebody may start it by hand ten seconds later; the poll is the
        # real test, not this call.
        def runner(command, **kwargs):
            raise OSError("unit not found")

        with patch.object(wake.shutil, "which", lambda n: "/bin/systemctl" if n == "systemctl" else None):
            message = wake.start_server("/project", runner=runner)
        self.assertIn("waiting anyway", message)


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class TestWaitUntilUp(unittest.TestCase):
    def test_it_returns_as_soon_as_the_server_answers(self):
        answers = [False, False, True]
        clock = FakeClock()
        self.assertTrue(wake.wait_until_up(
            "http://x/", 60,
            probe=lambda url: answers.pop(0),
            sleep=lambda s: setattr(clock, "now", clock.now + s),
            clock=clock,
        ))

    def test_it_gives_up_at_the_timeout(self):
        clock = FakeClock()
        self.assertFalse(wake.wait_until_up(
            "http://x/", 5,
            probe=lambda url: False,
            sleep=lambda s: setattr(clock, "now", clock.now + s),
            clock=clock,
        ))

    def test_it_probes_before_sleeping(self):
        # An already-up server must not cost a second of splash.
        slept = []
        clock = FakeClock()
        wake.wait_until_up("http://x/", 60, probe=lambda url: True,
                           sleep=slept.append, clock=clock)
        self.assertEqual(slept, [])

    def test_it_does_not_loop_forever_on_a_zero_timeout(self):
        clock = FakeClock()
        self.assertFalse(wake.wait_until_up("http://x/", 0, probe=lambda url: False,
                                            sleep=lambda s: None, clock=clock))


class TestSplash(unittest.TestCase):
    def test_the_splash_is_self_contained(self):
        # It has to render before the server that would serve its assets is
        # running, so it can reference nothing external.
        html = wake.splash()
        self.assertNotIn("src=", html)
        self.assertNotIn("<link", html)
        self.assertIn("Waking", html)

    def test_it_is_on_brand_and_dark(self):
        html = wake.splash()
        self.assertIn("#0E0F13", html)   # --bg
        self.assertIn("#2DD4A8", html)   # --accent

    def test_the_message_is_replaceable_for_the_timeout_case(self):
        html = wake.splash(wake.TIMEOUT_MESSAGE)
        self.assertIn("didn", html)
        self.assertNotIn("Waking Trillion up", html)

    def test_it_honours_reduced_motion(self):
        self.assertIn("prefers-reduced-motion", wake.splash())


if __name__ == "__main__":
    unittest.main()
