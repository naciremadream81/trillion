#!/usr/bin/env python
"""
The native window — playbook/desktop-app.md Tiers 1 through 4.

A thin, honest window over the local server. The brain never moved; this adds
back the three native behaviours an embedded web view quietly drops:

  Tier 2  the microphone, which WKWebView denies by default because
          pywebview's Cocoa delegate is missing the permission hook.
  Tier 3  window.open(), which does NOTHING in an embedded web view — the
          OAuth pop-up just dies, silently.
  Tier 4  a splash that wakes a sleeping server instead of showing a
          browser error page.

Run it directly while developing:

    python desktop/shell.py

But `python shell.py` can never be the final form for voice, and the reason
is Tier 2's Problem B: macOS reads the microphone usage string from the MAIN
BUNDLE OF THE PROCESS. Under Homebrew's Python that bundle is Python.app,
which has no such string, so macOS denies the mic **silently — no prompt at
all**, no matter how correct the delegate hook is. build_macos.sh fixes that
by copying the framework Python into the bundle so the app itself is the main
bundle. Until then the window works and the mic does not.

Install into a virtualenv of its OWN, not the server's:

    python3 -m venv desktop/.venv && desktop/.venv/bin/pip install pywebview
"""

from __future__ import annotations

import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from wake import (  # noqa: E402
    DEFAULT_TIMEOUT,
    DEFAULT_URL,
    TIMEOUT_MESSAGE,
    is_up,
    splash,
    start_server,
    wait_until_up,
)

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
URL = os.getenv("TRILLION_DESKTOP_URL", DEFAULT_URL)

WINDOW_TITLE = "Trillion"
WINDOW_SIZE = (1180, 820)
# A minimum, so the layout can never collapse into something unreadable —
# the orb UI's panels stack badly below roughly this width.
MIN_SIZE = (900, 640)


def install_macos_delegate() -> bool:
    """
    Tiers 2A and 3: grant media capture, and route window.open() out.

    pywebview looks its Cocoa browser delegate class up BY ATTRIBUTE at
    window-creation time, so replacing the attribute before the window is
    created is enough — no fork of the library, no patched install.

    Both hooks live here because they are the same delegate and they fail the
    same way: silently. Returns False on any non-macOS platform or if
    pywebview's internals have moved, so the window still opens.
    """
    if sys.platform != "darwin":
        return False
    try:
        import webbrowser

        from webview.platforms import cocoa  # type: ignore
    except Exception:  # noqa: BLE001
        return False

    base = getattr(cocoa, "BrowserView", None)
    delegate_base = getattr(base, "BrowserDelegate", None) if base else None
    if delegate_base is None:
        print("desktop: pywebview's Cocoa delegate was not where expected; "
              "the microphone and window.open will not work.")
        return False

    class TrillionDelegate(delegate_base):  # type: ignore[misc, valid-type]
        # Tier 2, Problem A. Absent, WKWebView defaults to DENY and
        # getUserMedia fails with no prompt at all. 1 == grant.
        def webView_requestMediaCapturePermissionForOrigin_initiatedByFrame_type_decisionHandler_(
            self, webview, origin, frame, capture_type, handler
        ):
            handler(1)

        # Tier 3. An embedded web view routes target=_blank LINK CLICKS to
        # the system browser but does nothing at all for window.open() from
        # JavaScript — which is exactly how OAuth pop-ups open. Hand the URL
        # to the real browser and return None: no child web view.
        def webView_createWebViewWithConfiguration_forNavigationAction_windowFeatures_(
            self, webview, configuration, navigation_action, window_features
        ):
            try:
                url = str(navigation_action.request().URL().absoluteString())
                if url:
                    webbrowser.open(url)
            except Exception:  # noqa: BLE001 — a dead pop-up beats a crash
                pass
            return None

    base.BrowserDelegate = TrillionDelegate
    return True


def main() -> int:
    try:
        import webview
    except ImportError:
        print("pywebview is not installed. In its OWN virtualenv:\n"
              "    python3 -m venv desktop/.venv\n"
              "    desktop/.venv/bin/pip install pywebview")
        return 1

    # BEFORE create_window — see install_macos_delegate's docstring.
    install_macos_delegate()

    up = is_up(URL)
    window = webview.create_window(
        WINDOW_TITLE,
        url=URL if up else None,
        html=None if up else splash(),
        width=WINDOW_SIZE[0], height=WINDOW_SIZE[1],
        min_size=MIN_SIZE,
        background_color="#0E0F13",   # so the first frame is never white
    )

    if not up:
        def wake():
            print("desktop:", start_server(PROJECT_ROOT))
            if wait_until_up(URL, DEFAULT_TIMEOUT):
                window.load_url(URL)
            else:
                # A message, not a blank window — the failure has to be
                # legible or it looks like the app is broken.
                window.load_html(splash(TIMEOUT_MESSAGE))

        threading.Thread(target=wake, daemon=True).start()

    webview.start()
    return 0


if __name__ == "__main__":
    sys.exit(main())
