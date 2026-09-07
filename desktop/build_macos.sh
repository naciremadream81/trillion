#!/usr/bin/env bash
# Build Trillion.app — playbook/desktop-app.md Tier 5.
#
# Assembles a .app by hand. No packager: this is a local app over a local
# server, and a hand-built bundle is auditable in a way a packager's output
# is not.
#
#     ./desktop/build_macos.sh          # writes desktop/dist/Trillion.app
#     open desktop/dist/Trillion.app    # then drag it to the dock
#
# Three of the steps below look optional and are not:
#
#   THE EMBEDDED PYTHON is Tier 2's Problem B made real. macOS reads the
#   microphone usage string from the MAIN BUNDLE OF THE PROCESS. Run under
#   Homebrew's Python, that bundle is Python.app — which has no mic string —
#   and macOS denies the microphone SILENTLY, with no prompt at all, however
#   correct the delegate hook is. Copying the framework binary in makes this
#   app the main bundle, so the prompt says "Trillion".
#
#   THE AD-HOC SIGNATURE is not about trust. It gives macOS's privacy system
#   (TCC) a STABLE IDENTITY to bind the microphone grant to. Without it the
#   grant does not survive a relaunch or a reboot — you get re-prompted, or
#   silently re-denied, forever.
#
#   THE ICON is what stops the dock showing a generic blank, which makes the
#   whole thing feel unfinished no matter how well it works.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$HERE")"
DIST="$HERE/dist"
APP="$DIST/Trillion.app"
BUNDLE_ID="com.trillion.desktop"
ICON_SRC="${ICON_SRC:-$PROJECT_ROOT/static/icons/orb-512.png}"

if [[ "$(uname)" != "Darwin" ]]; then
  echo "This builds a macOS .app and only runs on macOS." >&2
  exit 1
fi

# The app's own virtualenv, kept separate from the server's on purpose: the
# window needs pywebview and nothing else, and the server should not gain a
# GUI dependency.
VENV="$HERE/.venv"
if [[ ! -x "$VENV/bin/python" ]]; then
  echo "==> creating $VENV"
  python3 -m venv "$VENV"
  "$VENV/bin/pip" install --quiet --upgrade pip pywebview pyobjc-framework-WebKit
fi

# The FRAMEWORK python, not the venv shim — it finds Python.framework through
# its own absolute install name, which is what lets it be copied.
FRAMEWORK_PYTHON="$("$VENV/bin/python" -c 'import sys; print(sys.base_prefix)')/bin/python3"
PYTHON_HOME="$("$VENV/bin/python" -c 'import sys; print(sys.base_prefix)')"
SITE_PACKAGES="$("$VENV/bin/python" -c 'import site; print(site.getsitepackages()[0])')"

if [[ ! -x "$FRAMEWORK_PYTHON" ]]; then
  echo "Could not find the framework Python at $FRAMEWORK_PYTHON." >&2
  echo "Install python.org's framework build; a Homebrew Python cannot carry" >&2
  echo "the microphone usage string (Tier 2, Problem B)." >&2
  exit 1
fi

echo "==> assembling $APP"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

# ── The icon ────────────────────────────────────────────────────────────────
if [[ -f "$ICON_SRC" ]]; then
  echo "==> icon from $ICON_SRC"
  ICONSET="$DIST/Trillion.iconset"
  rm -rf "$ICONSET"; mkdir -p "$ICONSET"
  for size in 16 32 128 256 512; do
    sips -z $size $size        "$ICON_SRC" --out "$ICONSET/icon_${size}x${size}.png"      >/dev/null
    sips -z $((size*2)) $((size*2)) "$ICON_SRC" --out "$ICONSET/icon_${size}x${size}@2x.png" >/dev/null
  done
  iconutil -c icns "$ICONSET" -o "$APP/Contents/Resources/Trillion.icns"
  rm -rf "$ICONSET"
else
  echo "!! no icon at $ICON_SRC — the dock will show a blank." >&2
fi

# ── Info.plist ──────────────────────────────────────────────────────────────
cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key>              <string>Trillion</string>
  <key>CFBundleDisplayName</key>       <string>Trillion</string>
  <key>CFBundleIdentifier</key>        <string>$BUNDLE_ID</string>
  <key>CFBundleExecutable</key>        <string>trillion</string>
  <key>CFBundleIconFile</key>          <string>Trillion</string>
  <key>CFBundlePackageType</key>       <string>APPL</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>LSMinimumSystemVersion</key>    <string>11.0</string>
  <key>NSHighResolutionCapable</key>   <true/>
  <!-- The text macOS shows in the microphone prompt. In Trillion's voice,
       because this is the sentence Sean reads when he grants it. -->
  <key>NSMicrophoneUsageDescription</key>
  <string>Trillion listens when you talk to it. Nothing is recorded or stored.</string>
</dict>
</plist>
PLIST

# ── The embedded Python (Tier 2, Problem B) ─────────────────────────────────
echo "==> embedding the framework Python"
cp "$FRAMEWORK_PYTHON" "$APP/Contents/MacOS/python3"

# ── The launcher ────────────────────────────────────────────────────────────
cat > "$APP/Contents/MacOS/trillion" <<LAUNCHER
#!/usr/bin/env bash
# PYTHONHOME points at the framework so the copied binary finds its stdlib;
# PYTHONPATH at the app venv so it finds pywebview. Logged where it can be
# tailed, because a bundle that fails silently is undebuggable.
export PYTHONHOME="$PYTHON_HOME"
export PYTHONPATH="$SITE_PACKAGES"
cd "$PROJECT_ROOT"
exec "\$(dirname "\$0")/python3" "$HERE/shell.py" >> "\$HOME/Library/Logs/Trillion.log" 2>&1
LAUNCHER
chmod +x "$APP/Contents/MacOS/trillion"

# ── The ad-hoc signature (see the header) ───────────────────────────────────
echo "==> signing (ad-hoc)"
codesign --force --sign - --identifier "$BUNDLE_ID" "$APP/Contents/MacOS/python3"
codesign --force --sign - --identifier "$BUNDLE_ID" "$APP"

echo
echo "Built $APP"
echo "  open $APP        # then drag it to the dock and keep it there"
echo "  tail -f ~/Library/Logs/Trillion.log"
echo
echo "First voice use should prompt for the microphone BY NAME, once, and"
echo "never again — including after a reboot. If it re-prompts, the signature"
echo "step is the suspect; if there is no prompt at all, the embedded Python"
echo "is not what's running."
