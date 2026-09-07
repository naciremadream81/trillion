# Trillion as a desktop app

A thin, honest window over the local server — `playbook/desktop-app.md`. The
brain never moved; this adds back the three native behaviours an embedded web
view quietly drops, and a splash so clicking the dock icon works even when
the server is asleep.

## Build and run

    python3 -m venv desktop/.venv
    desktop/.venv/bin/pip install pywebview pyobjc-framework-WebKit
    desktop/.venv/bin/python desktop/shell.py      # Tier 1–4, dev mode

    ./desktop/build_macos.sh                       # Tier 5: the .app
    open desktop/dist/Trillion.app                 # then drag it to the dock

Point it somewhere else with `TRILLION_DESKTOP_URL`.

## What is verified, and what is not

**Verified** (`tests/test_desktop_wake.py`, 19 tests): everything in
`wake.py` — the up-check including the 401-is-up case, supervisor detection,
detached direct launch, the poll and its timeout, and the splash.

**NOT verified.** Tiers 2, 3 and 5 are macOS runtime behaviours and there was
no Mac to run them on. `install_macos_delegate()`, the `window.open` routing,
and the whole of `build_macos.sh` are written from the playbook and have
never executed. Treat the first run as the real test, and use the map below —
every one of these fails silently.

## Troubleshooting

Two mic failures that look identical and are not:

| Symptom | Cause | Fix |
|---|---|---|
| **No mic prompt at all** | Tier 2 **Problem B** — the process's main bundle is `Python.app`, which carries no mic usage string, so macOS denies silently | Confirm the app is running the **embedded** `Contents/MacOS/python3`, and that `NSMicrophoneUsageDescription` is in the plist |
| **Prompt appears, audio dead** | Tier 2 **Problem A** — the grant hook isn't firing | Confirm the delegate subclass is swapped in **before** `create_window` |
| **Sign-in button does nothing** | Tier 3 — `window.open()` isn't routed | Confirm the new-web-view hook opens the system browser and returns `None` |
| **Blank window on launch** | Tier 4 — no splash, or the wrong port | Check `TRILLION_DESKTOP_URL`; `tail -f ~/Library/Logs/Trillion.log` |
| **Mic re-prompts after a reboot** | Tier 5 — unstable code identity | Re-run the ad-hoc `codesign` on **both** the binary and the bundle, with a fixed identifier |
| **Generic dock icon** | Tier 5 — the `.icns` didn't build | Check `static/icons/orb-512.png` exists and `sips`/`iconutil` ran |

## Why the embedded Python, really

macOS reads the microphone usage string from the **main bundle of the
process**. Under Homebrew's Python that is `org.python.python` — which has no
such string — so the mic is denied **with no prompt**, however correct the
delegate is. Copying the framework binary into `Contents/MacOS/` makes
*Trillion* the main bundle, so the prompt names Trillion. That is also why
`python shell.py` can never be the final form for voice.

The ad-hoc signature is not about trust: it gives macOS's privacy system a
**stable identity** to bind the grant to, so it survives a relaunch and a
reboot instead of re-prompting forever. For distribution you need a real
Developer ID certificate and notarization instead, and stricter entitlements.

## Linux and Windows

`wake.py` is platform-neutral and `shell.py` opens a window anywhere
pywebview runs — `install_macos_delegate()` returns `False` off macOS and the
window still opens. Only the mic hook, the pop-up routing and the bundler are
macOS-specific. The equivalents (GTK's `permission-request`, Windows'
`WebView2` `PermissionRequested`) are not written.
