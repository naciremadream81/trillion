# Turn my local AI agent into a native desktop app

I already have an AI agent that runs as a **local web server** — I open it in a browser tab today. I want you to turn that tab into a **real desktop app**: a chrome-less native window pointed at my own [localhost](http://localhost), launched from a **dock/taskbar icon**, with its own name and icon, that wakes the server if it's asleep and Just Works when I click it.

This is a description of the experience and the engineering approach, not a code listing. Use your own judgment for the exact implementation, but match the structure and the hard-won details described here closely — most of the difficulty in this task is invisible until it bites, and the tiers below are ordered so each bite is isolated and fixable on its own.

**The reference build is macOS**, because that's where the sharp edges are: it uses **pywebview** (a thin Python wrapper over the system web view — **WKWebView** on macOS) pointed at a FastAPI server on `localhost`, packaged as a hand-rolled `.app` bundle. If I'm on Windows or Linux, keep the same *shape* — chrome-less native window over my local server, plus the permission/pop-up/wake handling — but adapt the platform specifics (on Windows pywebview uses the Edge WebView2 runtime; on Linux it uses GTK WebKit; Tauri is a reasonable alternative on any platform if I'd rather ship a compiled binary). Tell me plainly when a step is macOS-only so I know what to translate.

**Work tier by tier.** Each tier below ends with something I can launch and a verification step. Don't start a tier until the previous one works, and don't collapse tiers together — the whole point is that when the mic is dead or the sign-in pop-up vanishes, we know exactly which layer to look at.

---

## Tier 0 — Interview me first (don't write code yet)

Before touching any files, ask me these questions and wait for my answers. Keep it to one round and group them so I can answer quickly. If I skip one, use the default in parentheses.

**About my machine and my agent**

1. What **OS** am I on — macOS, Windows, or Linux? (Default: **macOS**. If Windows/Linux, keep the shape but swap the platform specifics and warn me where the reference doesn't translate.)

2. What's my agent's **name** and the **URL** its web UI runs at locally? (Default: *`http://localhost:PORT/`[](http://localhost:PORT/`**)** — ask me for the port if you can't find it. This name becomes the window title, the dock label, and the bundle name.)

3. What **web framework/server** hosts it (FastAPI/uvicorn, Flask, Node/Express, something else), and where does the project live on disk? (Used only to read how it starts — you won't modify the server.)

**About keeping the server alive**

4. How does that server get **started and kept running** today — a `launchdsystemd` service, a Docker container, a terminal I leave open, or nothing formal? (Default: **assume it's supervised and usually already up**; if it can be down, build a splash screen that starts it and waits.)

5. When I click the app icon and the server is **not** running, what should happen — should the app **start the server itself**, or just show a "waiting for server" splash and poll until it appears? (Default: **kick the supervisor, then poll** — start it if you safely can, otherwise wait and show a clear message.)

**About the microphone (this is the hard part — skip it entirely if my agent is text-only)**

6. Does my agent need the **microphone** (is it voice-first)? (Default: **infer from the project** — if there's a mic/voice pipeline, do the full permission work below; if it's text-only, skip all of it and save us the hardest 60% of this task.)

7. Which **Python** built my environment — Homebrew `python@3.x`, pyenv, `uv`-managed, system, or a bundled runtime — and what version? (macOS-only, mic-only: this decides where the framework Python binary lives, which the app has to embed so the OS attributes the mic prompt to *my app* and not to "Python.")

**About sign-in and links**

8. Does my agent use any **OAuth sign-in** (Google/GitHub/etc.) or open **external links** from inside the UI? (Default: **assume yes** and route every pop-up / `window.open` / `target=_blank` out to the system browser — this is a real bug in embedded web views that silently breaks sign-in if we don't.)

**About the bundle**

9. Do I have an **icon** (a square PNG, ideally 512×512 or larger), and what **bundle identifier** should the app use? (Defaults: **fall back to any existing app/PWA icon** in the project if I don't hand you one; bundle id like *`com.MYNAME.desktop`** in reverse-DNS.)

10. Is this for **my own machine only**, or do I want to **distribute** it to others? (Default: **local only** → ad-hoc code signature, no notarization. If I want to distribute, flag that I'll need a Developer ID certificate and notarization, and that mic/entitlements get stricter — but build the local version first.)

Once I've answered, restate the plan back to me in three or four lines — what the app will be, whether it does the mic work, and how it wakes the server — then read my project **read-only** (how the server starts, what port, whether there's a mic pipeline, where an icon lives) and begin Tier 1.

---

## The big picture (read once, then build incrementally)

Here's the mental model, because it's the thing that makes this whole task feel small once it clicks: **almost none of my agent moves.** The entire brain — the voice pipeline, the model calls, the memory, the WebSocket — already lives in my local server and keeps running exactly as it does now. What we're building is a **window**. A native, chrome-less window whose only job is to display `http://localhost:PORT/` and behave like an app: an icon on the dock, no address bar, no browser tabs, its own name in the menu bar, and a few native behaviors the browser gave me for free that an embedded web view does not.

So the deliverable is small — on macOS it's essentially **two files**: a short Python **shell** that opens the web view and patches in the missing native behaviors, and a **build script** that wraps that shell into a `.app` bundle with an icon and a signature. The reason this task has a reputation is that an *embedded* web view is subtly not a browser. Three things the browser handled invisibly stop working, and each fails **silently** — no error, just a dead mic or a sign-in that goes nowhere. The tiers exist to surface those three, one at a time.

The three silent failures, named up front so they're not a surprise:

- **The microphone is denied with no prompt.** An embedded web view won't grant `getUserMedia` on its own, *and* the OS looks in the wrong place for the "why does this app want your mic" string. Tier 2.

- **Sign-in pop-ups vanish.** OAuth and external links open via `window.open`, and an embedded view drops those on the floor instead of opening them. Tier 3.

- **A cold click hits a sleeping server.** If the server is down when I launch, the window loads an error page instead of my agent. Tier 4.

Build the plain window first (Tier 1), then fix those three in order, then bundle it (Tier 5). Each is independently testable by launching the thing and watching one specific behavior.

---

## Tier 1 — The bare window

**Goal:** a native, chrome-less window that opens my agent's UI at `http://localhost:PORT/` and nothing else. No mic work, no bundle, no icon yet — just proof that the web view renders my agent.

On macOS, use **pywebview** `pip install pywebview` into a dedicated virtualenv for the app — keep it separate from the server's environment). Write a small **shell script in Python** that creates one window pointed at my URL, with a sensible default size and a minimum size so the layout never collapses, and starts the event loop. Run it directly `python shell.py`) for now — we bundle it later.

**Verify Tier 1:** running the shell opens a native window showing my agent's UI, with no browser chrome — no address bar, no tabs. It should look like an app, not a browser. If the window is blank, confirm the server is actually up at that URL first (load it in a normal browser). If my agent is **text-only**, this tier plus Tier 3 (links) and Tier 5 (bundle) may be the entire job — we can skip Tier 2 completely.

---

## Tier 2 — Make the microphone actually work (macOS, voice agents only)

**Goal:** the voice pipeline works inside the app exactly like it does in a browser tab — I click into the app, speak, and the orb reacts. Skip this whole tier if my agent doesn't use the mic.

This is the hardest tier, and it's **two separate problems** that both have to be solved or the mic stays dead. Do them in this order and test after each, because they fail identically (a silent denial), and you want to know which one you just fixed.

**Problem A — the web view never asks.** pywebview's macOS delegate is missing the one WKWebView hook that answers "may this page use the camera/microphone?" With that hook absent, the web view defaults to **deny**, and `getUserMedia` fails with no prompt. The fix is to **subclass pywebview's Cocoa browser delegate**, implement the media-capture permission method, and have it **grant** the request (WebKit's "grant" decision). Because pywebview looks up its delegate class by attribute at window-creation time, **swapping the class in before you create the window** is enough to take effect — you don't fork the library.

**Problem B — the OS blames the wrong app.** This is the subtle one. macOS reads the microphone **usage-description string** from the **main bundle of the process** hosting the web view. If the app runs under Homebrew's Python, the main bundle is `Python.app` `org.python.python`), which has **no** mic usage string — so macOS denies the mic **silently**, with no permission prompt at all, no matter how correctly Problem A is solved. The fix is to make **my app** the main bundle: **copy the framework Python binary into the app bundle** (it still finds its `Python.framework` via the binary's absolute install name), and point `PYTHONHOME` at the framework and `PYTHONPATH` at the app's virtualenv. Now the hosting process's main bundle is *my* app, carrying *my* `NSMicrophoneUsageDescription`, and the prompt says my agent's name. (This is why Tier 1's `python shell.py` can't be the final form — it has the wrong main bundle. We'll wire the copy and the env vars up in the bundle at Tier 5; for now, just implement the delegate subclass from Problem A and note that the mic won't fully work until the bundle exists.)

**Verify Tier 2 (after the bundle exists in Tier 5):** launching the app and using voice triggers a **real macOS microphone prompt that names my app**, and once granted, the orb reacts to my voice inside the app just like in a browser. If there's **no prompt at all**, it's Problem B (wrong main bundle / missing usage string). If there's a prompt but audio still fails, it's Problem A (the grant hook). Keep those two failure signatures in mind — they're how you tell the problems apart.

---

## Tier 3 — Let sign-in and external links escape to the real browser

**Goal:** OAuth sign-in and any "opens in a new window" link work — they open in my **system browser**, complete there, and my agent picks up the result. Do this even for text-only agents if they have sign-in or external links.

Embedded web views route `target=_blank` **link clicks** to the system browser, but *`window.open()` from JavaScript** — which is exactly how OAuth pop-ups and many activity-feed links open — does **nothing**: no new window, no error, the click just dies. The fix on macOS is to implement the WKWebView "create a new web view" hook in the same delegate subclass from Tier 2, and instead of opening a child web view, **hand the URL to the system browser** and return nothing. This works cleanly for OAuth because the provider redirects back to a callback on my own `localhost`, which my **server** handles — so the sign-in completes regardless of which browser hosted the pop-up.

**Verify Tier 3:** clicking a sign-in button (or any external link) in the app opens my **default browser**, the flow finishes there, and my agent reflects the signed-in state. Before the fix, that button does nothing; after, it opens externally and completes.

---

## Tier 4 — A splash screen that wakes a sleeping server

**Goal:** clicking the app when the server is **down** shows a friendly, on-brand "waking up" splash, starts (or waits for) the server, and swaps to the real UI the moment it's ready — instead of loading a browser error page.

On launch, first **check whether the server is up** (a quick request to my URL, expecting a success response). If it's up, open the window straight at the URL. If it's down, open the window on a small inline **splash** — a dark page with a pulsing orb and a short "Waking [agent] up…" line, matched to my agent's palette — and on a background thread: **start the server the way my setup expects** (for a `launchd` service, `launchctl kickstart` the label, which is a harmless no-op if it's already running; for `systemd`, `systemctl --user start`; for Docker, `docker start`; if there's no supervisor, launch the server process directly), then **poll** the URL once a second for up to ~60 seconds and, the instant it answers, **load the real UI** in the existing window. If it never comes up, leave the splash with a clear message rather than a blank window.

**Verify Tier 4:** stop the server, then launch the app — I should see the splash, and within a few seconds it should start the server and swap to my agent's UI on its own. Launch it again while the server is already up — it should skip the splash and load instantly.

---

## Tier 5 — Bundle it into a dock-launchable app with my icon

**Goal:** a double-clickable app with **my icon** that I can keep in the dock, that carries the right permission strings, and (on macOS) has a stable signed identity so the mic grant sticks across launches and reboots.

On macOS, write a **build script** that assembles a `.app` bundle by hand (no heavy packager needed for a local app):

- *`Info.plist`** — the app's name and display name, a reverse-DNS **bundle identifier**, the executable name, a minimum system version, high-resolution support, the **icon file** name, and — critically for voice — the *`NSMicrophoneUsageDescription`** string (this is the text macOS shows in the mic prompt; write it in my agent's voice).

- **The icon** — from my square PNG, generate the standard set of sizes (16 up to 512, plus @2x variants) into an iconset and compile it to a single `.icns` with the system tools `sips` + `iconutil`). If I didn't give you an icon, fall back to any existing app/PWA icon in the project so the dock is never a generic blank.

- **The embedded Python** (voice agents — this is Tier 2's Problem B made real) — **copy the framework Python binary into the bundle's `MacOS` directory** so my app is the main bundle.

- **A launcher script** as the bundle's executable — sets `PYTHONHOME` to the framework and `PYTHONPATH` to the app's virtualenv, `cd`s into my project, and execs the embedded Python running the Tier 1–4 shell, logging somewhere I can `tail` when something goes wrong.

- **A code signature** — an **ad-hoc** signature (sign with `-`) on both the copied binary and the bundle, using my bundle identifier. This isn't about trust; it gives the OS's privacy system (**TCC**) a **stable identity** to bind the microphone grant to, so I don't get re-prompted — or silently re-denied — on every launch. If I said I want to **distribute**, this is where you flag that I'll need a real Developer ID certificate and notarization instead, and that the entitlements get stricter.

Then tell me the one command to build it and the one command to launch it, and that I can **drag the app to my dock** and **keep it there** to launch from the icon from then on.

**Verify Tier 5:** the built app appears with **my icon**; double-clicking it (or clicking the dock icon) opens my agent in the native window; the **first** voice use prompts for the mic **with my app's name** and, once granted, **never asks again** — including after I quit, relaunch, and reboot. If the mic re-prompts or silently dies after a reboot, the signature/identity step is the suspect. If sign-in still vanishes, revisit Tier 3.

---

## Troubleshooting map (hand me this, because these all fail quietly)

- **Mic prompt never appears** → wrong main bundle or missing usage string (Tier 2, Problem B): confirm the embedded Python binary is what's running and the `NSMicrophoneUsageDescription` is in the plist.

- **Mic prompt appears but audio is dead** → the grant hook isn't firing (Tier 2, Problem A): confirm the delegate subclass is swapped in *before* the window is created.

- **Sign-in button does nothing** → `window.open` isn't routed (Tier 3): confirm the new-web-view hook opens the system browser and returns nothing.

- **Blank window on launch** → the server was down and there's no splash/wake (Tier 4), or the URL/port is wrong.

- **Mic re-prompts or breaks after a reboot** → unstable code identity (Tier 5): confirm the ad-hoc signature with a fixed bundle identifier on both the binary and the app.

- **Generic/blank dock icon** → the `.icns` didn't build or isn't referenced by the plist (Tier 5).

---

## The feel, in one paragraph

The north star: clicking the dock icon should feel like **opening an app, not a browser** — my agent, full and alive, in a window that's unmistakably its own, with the mic just working and sign-in just working, whether or not the server was awake when I clicked. Remember that the brain never moved: this is a thin, honest window over the local server I already trust, plus the three small native behaviors an embedded web view quietly drops. Build the plain window first, fix the mic, fix the pop-ups, wake the server, then wrap it in a signed bundle with my icon — one tier at a time, launching and watching one specific behavior after each — and the reputation this task has for being fiddly turns into an afternoon of small, verifiable wins.
