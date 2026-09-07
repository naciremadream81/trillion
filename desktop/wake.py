"""
Waking a sleeping server — playbook/desktop-app.md Tier 4, minus the GUI.

Deliberately split out from shell.py so it imports and tests WITHOUT
pywebview, a display, or macOS. It is the only part of the desktop app whose
logic can be verified anywhere, and it is also the part most likely to be
wrong: "is it up", "how do I start it here", and "how long do I wait" are
three questions with different answers on every machine.

The behaviour it encodes: check whether the server answers. If it does, the
window opens straight at the real UI. If it doesn't, the window opens on a
splash and this module starts the server the way THIS machine expects, then
polls until it answers — and if it never does, says so plainly rather than
leaving a blank window.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request

DEFAULT_URL = "http://127.0.0.1:8123/"
DEFAULT_TIMEOUT = 60.0
POLL_SECONDS = 1.0
PROBE_TIMEOUT = 2.0

# The systemd unit and launchd label the deployment uses. Both are named
# here rather than guessed at call time, so a rename is one edit.
SYSTEMD_UNIT = os.getenv("TRILLION_SYSTEMD_UNIT", "trillion-orb.service")
LAUNCHD_LABEL = os.getenv("TRILLION_LAUNCHD_LABEL", "com.trillion.orb")


def is_up(url: str = DEFAULT_URL, timeout: float = PROBE_TIMEOUT) -> bool:
    """
    Whether the server answers.

    Any HTTP response at all counts as up — including a 401, which is exactly
    what a server with TRILLION_WEB_AUTH_TOKEN set returns to an unauthenticated
    probe. Treating that as "down" would make the app try to start an
    already-running server on every launch.
    """
    try:
        with urllib.request.urlopen(url, timeout=timeout):
            return True
    except urllib.error.HTTPError:
        return True   # it answered; see the docstring
    except Exception:  # noqa: BLE001 — connection refused, DNS, timeout: down
        return False


def wake_command() -> list[str] | None:
    """
    How to start the server ON THIS MACHINE, or None if nothing supervises it.

    Ordered by how sure we can be. `launchctl kickstart` and `systemctl
    --user start` are both harmless no-ops against an already-running
    service, which matters because the up-check and the start race whenever
    the server is mid-boot.

    Returning None is a real answer, not a failure: a server started by hand
    in a terminal has no supervisor to ask, and start_server() falls back to
    launching it directly.
    """
    if shutil.which("launchctl"):
        return ["launchctl", "kickstart", f"gui/{os.getuid()}/{LAUNCHD_LABEL}"]
    if shutil.which("systemctl"):
        return ["systemctl", "--user", "start", SYSTEMD_UNIT]
    return None


def start_server(project_root: str, *, runner=subprocess.Popen) -> str:
    """
    Ask this machine to start the server. Returns what it did, for the log.

    Never raises. A failure here is not fatal — the poll below is the real
    test, and a server somebody starts by hand ten seconds later is still a
    success from the window's point of view.
    """
    command = wake_command()
    if command is not None:
        try:
            runner(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return f"asked the supervisor: {' '.join(command)}"
        except Exception as e:  # noqa: BLE001
            return f"supervisor call failed ({type(e).__name__}: {e}); waiting anyway"

    # No supervisor. Launch it directly, detached, so quitting the window
    # doesn't take the server with it.
    python = os.path.join(project_root, ".venv", "bin", "python")
    if not os.path.exists(python):
        python = "python3"
    try:
        runner(
            [python, os.path.join(project_root, "serve.py")],
            cwd=project_root,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return "started serve.py directly"
    except Exception as e:  # noqa: BLE001
        return f"could not start the server ({type(e).__name__}: {e})"


def wait_until_up(url: str = DEFAULT_URL, timeout: float = DEFAULT_TIMEOUT,
                  *, poll_seconds: float = POLL_SECONDS, sleep=time.sleep,
                  clock=time.monotonic, probe=is_up) -> bool:
    """
    Poll until the server answers, or the timeout expires.

    `clock` is monotonic, not wall time: a clock adjustment mid-wait must not
    extend or truncate the window. Injectable so the tests don't sleep.
    """
    deadline = clock() + timeout
    while True:
        if probe(url):
            return True
        if clock() >= deadline:
            return False
        sleep(poll_seconds)


SPLASH_HTML = """\
<!doctype html>
<meta charset="utf-8">
<title>Trillion</title>
<style>
  :root {{ color-scheme: dark; }}
  html, body {{ height: 100%; margin: 0; background: #0E0F13; }}
  body {{
    display: flex; flex-direction: column; align-items: center; justify-content: center;
    gap: 26px; color: rgba(255,255,255,.45);
    font: 400 13px/1.5 -apple-system, BlinkMacSystemFont, system-ui, sans-serif;
    letter-spacing: .3px;
  }}
  .orb {{
    width: 84px; height: 84px; border-radius: 50%;
    background: radial-gradient(circle at 50% 45%, #2DD4A8 0%, #3B6FE0 55%, rgba(14,15,19,0) 72%);
    filter: blur(1px);
    animation: pulse 2.4s ease-in-out infinite;
  }}
  @keyframes pulse {{
    0%, 100% {{ transform: scale(.92); opacity: .75; }}
    50%      {{ transform: scale(1.06); opacity: 1; }}
  }}
  @media (prefers-reduced-motion: reduce) {{ .orb {{ animation: none; }} }}
  .msg {{ min-height: 1.2em; }}
</style>
<div class="orb"></div>
<div class="msg">{message}</div>
"""


def splash(message: str = "Waking Trillion up…") -> str:
    """The splash document. Inline rather than a file so it renders before
    the server that would have served it is even running."""
    return SPLASH_HTML.format(message=message)


TIMEOUT_MESSAGE = (
    "Trillion didn’t come up. Check the server log, then reopen this window."
)
