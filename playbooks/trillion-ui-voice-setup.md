# Build Your Own Voice-First UI — 3 Prompts

These are three copy-paste prompts for recreating the visual language of **Trillion**, a voice-first AI assistant interface. Drop each one into your favorite AI coding tool (Claude Code, Cursor, v0, Bolt, etc.) in a fresh HTML file, then iterate. Together they give you a luminous teal orb floating in a dark cosmic void, a glass-morphism shell around it, and a big friendly mic button at the bottom. Build them in order — orb first, shell second, mic last — and you'll end up with something that looks and feels like Trillion.

Shared design tokens across all three prompts:
- Background: `#0E0F13` (primary), `#16171D` (surfaces)
- Accent: `#2DD4A8` (teal)
- Fonts: Inter (UI), JetBrains Mono (timestamps/numbers)
- Easing: `cubic-bezier(0.16, 1, 0.3, 1)`
- Aesthetic: dark, glassy, minimal, spacious, futuristic

---

## 1. The Luminous Orb + Cosmic Background

**What this is:** The centerpiece of the interface — a glowing teal orb that sits in the middle of the viewport, surrounded by animated nebula clouds and a twinkling starfield. It's the "face" of the assistant. The orb's brightness reacts to voice activity, so it feels alive when the assistant is speaking.

```
Build a full-viewport Three.js scene as the hero visual for a voice-first AI
assistant. Dark cosmic background (#0E0F13 base) with slowly drifting nebula
clouds in teal, purple, and blue wisps (use noise-based shaders or layered
radial gradients) plus a subtle twinkling starfield.

At the center, render a luminous teal orb (#2DD4A8) with three glow layers:
(1) a wide soft atmospheric bloom, (2) a medium halo, (3) a bright inner core.
Use additive blending and bloom post-processing so the orb truly glows.

Expose a `uVoiceBright` uniform (0.0 – 1.0) that intensifies the inner core
and halo when raised — I'll drive it from audio amplitude later. Include a
gentle idle pulse (sine over ~4s) so it breathes when quiet.

Vanilla HTML + Three.js via CDN. Canvas should cover the full viewport at
z-index 1. No UI yet — just the orb and space.
```

---

## 2. The Glass Shell — Header, Activity Panel, Response Cards

**What this is:** The chrome that floats over the orb. A frosted-glass header with status indicator at the top, a collapsible activity panel down the right edge (inbox, tasks, drafts), and floating response cards that appear around the orb when the assistant returns data. This is how information is layered onto the scene without hiding it.

```
Build a glass-morphism UI shell that overlays a full-viewport dark canvas.
Font: Inter. Accent color: #2DD4A8. Surface: rgba(14,15,19,0.15) with
backdrop-filter: blur(14px) and a 1px border of rgba(255,255,255,0.06).

Three pieces:

1) A 56px fixed header spanning the viewport. Left: "Trillion" wordmark with
   a small teal dot. Right: three ghost icon buttons (neural map, alerts with
   badge, settings) and a 10px status dot that pulses teal when "listening",
   shifts to blue/purple when "processing", and sits gray when "idle".

2) A 260px right-side activity panel with stacked sections: Inbox Replies,
   Scout Tasks, Flux Tasks, Relay Drafts. Uppercase 10px section headers
   with counts. Entries slide in from the right over 300ms using
   cubic-bezier(0.16, 1, 0.3, 1). Panel collapses to 36px on toggle.

3) Floating response cards, 240px wide (revenue variant 300px), with a 2px
   teal top accent bar, rgba(22,23,29,0.75) background, 16px blur, and a
   gentle float animation (±6px vertical, 4s loop) with rotateX(8deg) on
   entry.

All overlays use pointer-events carefully so the canvas stays interactive.
```

---

## 3. The Bottom Mic Control Bar

**What this is:** The primary way users talk to the assistant — a big round microphone button pinned to the bottom of the screen. It swaps between a mic icon (idle) and a stop square (listening), and pulses with a teal ring when active. This is the one element every user will touch, so it has to feel inviting and responsive.

```
Build a fixed bottom control bar for a voice-first AI UI. The bar itself is
transparent (pointer-events: none) and centers a single 64×64px circular
microphone button.

Idle state: dark circle (#16171D), 1px border rgba(255,255,255,0.08), white
microphone SVG icon centered.

Active (listening) state: teal border (#2DD4A8), swap icon to a white stop
square, add a glowing box-shadow (0 0 24px rgba(45,212,168,0.5)), and render
an expanding pulse ring (pseudo-element, 0 → 10px spread, opacity 1 → 0,
1.4s infinite, easing cubic-bezier(0.16, 1, 0.3, 1)).

Below the button, a single line of 11px uppercase hint text in
rgba(255,255,255,0.4) with letter-spacing 0.08em — e.g. "TAP OR SAY
'HEY TRILLION'".

Clicking the button toggles the listening state and dispatches a
`trillion:mic-toggle` custom event so the rest of the app can react.
Vanilla HTML/CSS/JS, inline SVG icons, no frameworks.
```

---

## Tips for iterating

- **Go one prompt at a time.** Get the orb looking right before you add the shell, and so on. Each prompt is written to be visually complete on its own.
- **Tune the glow.** The orb's magic is in the bloom. If it looks flat, crank post-processing bloom strength and lower the orb's base color a touch — glow should do the work.
- **Match the easing.** `cubic-bezier(0.16, 1, 0.3, 1)` on every transition is what makes the whole thing feel like the same app. Don't swap it for `ease-in-out`.
- **Dark and quiet.** Resist the urge to add more color. The interface's character comes from restraint — one accent, lots of black, lots of space.
