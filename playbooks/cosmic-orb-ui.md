# Build a living cosmic interface for your AI agent

I want you to give my AI agent a face: a single full-screen 3D scene with a **voice-reactive orb** floating in a **procedural deep-space nebula**, surrounded by a **constellation of floating sub-agents** that orbit the orb, light up when they're dispatched, and settle near their panels while they work.

This is a description of the experience and the engineering approach, not a code listing. Use your own judgment for the exact implementation, but match the look, the feel, and the structure described here closely — the goal is a polished, alive interface, not a placeholder. The reference build uses **three.js** (loaded from a CDN, no bundler required) for the WebGL, with the orb and background rendered as separate layers and a thin DOM overlay for text labels.

**Work tier by tier.** Each tier below ends with something visible on screen and a verification step. Don't start a tier until the previous one renders correctly, and don't merge tiers together — the whole point is that each layer is independently debuggable.

---

## Tier 0 — Interview me first (don't write code yet)

Before touching any files, ask me these questions and wait for my answers. Keep it to one round and group them so I can answer quickly. If I skip one, use the default in parentheses.

**About the agent and stack**

1. What's my agent's **name** and a one-line tagline? (Used in the header and labels.)

2. What's my web stack — plain HTML with a module script, React, Vue, Svelte, or something else? (Default: **plain HTML, no build step**. If I name a framework, wrap each system in a component but keep the rendering approach and visual tuning identical.)

3. How does my frontend learn the agent's **current state** and its tool/agent activity today — a WebSocket, server-sent events, polling, or nothing yet? (Default: **assume a WebSocket** and give me a tiny `setState(name)` function plus event hooks I can call from anywhere. If I have nothing yet, build a small demo driver that cycles through the states so the scene is alive without a backend.)

4. Do I have a **live audio stream** — microphone input and/or spoken responses — that the orb should physically react to, or should it use a **synthetic motion** that looks like speech? (Default: build the real audio-analysis path **and** a synthetic fallback, and auto-select between them.)

**About identity and look**

5. What's my agent's **primary accent color** — the hue the orb glows when it's ready and active? (Default: a calm **teal**.)

6. Which **conversational states** should the orb express? (Default: idle, listening, processing, speaking, and error — the full reference set.)

**About the sub-agents (the floating constellation)**

7. List my **sub-agents**: for each, a short id, a display name, a one-line specialty, and an accent color. (Default: invent three plausible ones so the constellation isn't empty — for example a support agent, a research agent, and a build agent.)

8. When a sub-agent is actively working, should it **leave orbit and settle near a panel** in the UI, or just pulse in place? (Default: settle near a matching on-screen element if one exists, otherwise pulse in orbit.)

Once I've answered, restate the plan back to me in three or four lines, then begin Tier 1.

---

## The big picture (read once, then build incrementally)

The whole interface is **three stacked, full-screen layers**:

- **Background layer (furthest back):** a deep-space sky — drifting nebula clouds, twinkling stars, a faint colored glow pooled behind the orb — plus a faint 3D web of glowing nodes and dust floating in the distance.

- **Orb layer (middle):** the orb itself and everything attached to it — its glow halo, the orbital rings, the sub-agent avatars, and the beams that connect them. This layer is transparent so it composites cleanly over the background.

- **Label layer (front):** ordinary text labels for the sub-agents, drawn as plain HTML positioned to follow each avatar's spot on screen.

Keep the background and the orb as **separate renderers**. That separation lets the orb stay crisp and smooth while the background can quietly drop to half its framerate on weaker devices without anyone noticing.

A single animation loop drives everything. The most important stylistic rule: **nothing ever snaps.** Every visual property — color, brightness, size, rotation speed — eases gradually toward its target each frame, so a state change feels like a living thing settling into a new mood rather than a hard cut. Use the same gentle easing everywhere so the whole scene shares one rhythm.

---

## Tier 1 — The orb itself

**Goal:** a centered, slowly tumbling **wireframe sphere** with a soft glow halo, floating on a black background. No audio, no states, no nebula yet.

The orb is a **wireframe icosahedron** (a faceted sphere) subdivided finely enough that its surface can ripple smoothly. Its surface is continuously displaced by layered, drifting **noise**, so even at rest it looks like it's gently breathing — never a static ball. Shade it with a **fresnel rim glow**: the edges facing away from the camera glow brighter than the center, which is what gives it that translucent, energy-field look rather than a solid object. Behind it, place a slightly larger, very soft **glow shell** that adds a faint colored halo.

Start it dim and teal, rotating slowly on two axes at once (mostly around the vertical axis, with a slight tilt), with the surface gently undulating from the breathing motion. Position the camera so the orb sits centered with comfortable empty space around it — that space is where the sub-agents will eventually orbit.

**Verify Tier 1:** a dim teal wireframe sphere with a faint halo, slowly tumbling, its surface rippling softly. No console errors. If it looks like a solid ball, the wireframe mode is off. If it's invisible, check the camera distance and that the material is transparent.

---

## Tier 2 — The cosmic background

**Goal:** the black void becomes deep space — slowly drifting nebula clouds in a few colors, two layers of twinkling stars at different densities, a soft glow pooled behind the orb in the orb's current color, and a faint distant web of glowing clustered nodes connected by thin lines, with floating dust.

Build this as the separate background layer. It has two parts drawn one after the other:

- **The sky.** A full-screen procedural backdrop: a dark base that's slightly brighter in the center and darkens toward the edges, with a few independent layers of soft **nebula cloud** drifting slowly in different directions and colors (cool greens, a touch of magenta, a touch of blue). Add **stars** as two layers — one dense and fine, one sparser and larger — each twinkling on its own gentle rhythm. Add a soft **glow centered on the orb** that picks up the orb's live color, so the orb feels like it's actually lighting the space behind it. Finish with a subtle darkening toward the corners so the eye stays centered.

- **The distant network.** A faint three-dimensional web floating deep behind the orb: around a hundred small glowing **nodes** gathered into roughly eight **colored clusters**, with thin **lines** connecting any two nodes that are close together, plus a few hundred motes of **dust**. Everything is dim and additive so it reads as faint depth, not clutter. Group the whole web together so it can slowly rotate and drift as one. (Roughly halve the node and dust counts on mobile.)

Give the background camera a very slow, gentle wandering drift so the field feels alive without being distracting. Fade the whole background in over about a second and a half on load so it doesn't pop in.

**Verify Tier 2:** stars twinkle, nebula clouds drift, a faint colored web floats far behind the orb, and there's a soft colored glow pooled right behind the orb. The orb still tumbles in front of it all.

---

## Tier 3 — Conversational states and voice reactivity

**Goal:** the orb expresses what the agent is doing through color, surface motion, and glow, and it physically reacts to real audio. Switching to the speaking state should visibly transform it within about a second.

Define a handful of **states**, each one a target "mood" the orb eases toward (remember: never snap). Here's the intended feel of each — keep these characters even when you remap to my accent color:

- **Idle** — dim, slow, almost dormant. The resting state.

- **Listening** — a warm **amber** glow, bright and high-energy. Amber is deliberately the one warm color in an otherwise cool palette, so "I'm recording you" is unmistakable even in the corner of your eye. Big halo.

- **Processing** — the orb dims and its surface churns faster while its body color slowly cycles between two hues, as if it's thinking. In this state the **orbital rings** (Tier 5) become the main event.

- **Speaking** — bright and lively, with the surface deforming dramatically in time with the voice, plus a subtle size pulse and a little extra spin.

- **Error** — red, sharp-edged, and nearly frozen.

When the orb is speaking or processing, also brighten the background glow behind it and let that glow track the orb's current color, so the whole scene responds together.

For **voice reactivity**, read two values from the audio each frame — an overall **loudness** and a **bass** level — and feed them into the orb's surface displacement and size. Smooth these values **asymmetrically**: let them jump up quickly but fall away slowly, so the orb leaps to life on a loud syllable and eases back down rather than twitching frame to frame.

If real audio isn't available, drive the orb with a **synthetic motion** that mimics the cadence of speech (a gentle wavering for listening, a livelier churn for speaking) so it always looks alive. Auto-select: use the real signal when audio is flowing, fall back to synthetic otherwise.

> **If I have real audio on mobile, warn me about the iOS pitfall:** iOS Safari is notoriously fussy about feeding audio into an analyzer while also playing it aloud. The robust pattern is to play the audible audio one way and feed a **silent duplicate** of the same audio into the analyzer separately, just to read its levels. Also create the audio context from inside a real user tap, and use a separate audio context for the microphone. Mention this so I'm not surprised when it matters.

**Verify Tier 3:** switching to speaking turns the orb teal, churns its surface, pulses its size, swells the halo, and brightens the background. Listening turns it warm amber. Error turns it red and nearly still. Every transition takes about a second with no hard cuts.

---

## Tier 4 — The floating sub-agent constellation

**Goal:** each sub-agent appears as a small glowing **avatar orbiting the orb** along its own gently tilted path, with a name and specialty labeled just beneath it. The label follows the avatar around the screen and fades as the avatar swings behind the orb. This is the signature visual — get it right.

Each sub-agent is described by a small bundle of info your backend can serve (or that I hardcode for now): an id, a display name, a one-line specialty, an accent color, an optional avatar image, and a few **orbit parameters** — how far out it orbits, how fast, where on the circle it starts, and how tilted its path is.

A few rules that make the constellation read well:

- **Spread the agents out.** Give each a different starting position around the circle, a slightly different orbit distance and speed, and alternating tilt directions, so their paths sit in different planes and they never clump up or move in lockstep.

- **Keep them in a safe band** — orbiting clearly outside the orb's glow but not so far they drift off screen. They should also stay clear of the orbital rings you'll add in Tier 5.

For **avatars**, use a three-step fallback so it always looks finished: use an explicit image if one is provided, otherwise look for one by naming convention, otherwise **generate one on the fly** — a soft colored halo, a disc that fades from a dark core out to the agent's accent color, a thin rim, and the agent's initial in the center. Generated avatars mean a brand-new agent looks polished instantly with zero art. Put a soft additive glow behind each avatar so it reads as a little light source.

For each agent, also create a small **text label** (name on top, specialty beneath) as plain HTML in the front layer. Every frame, figure out where the agent currently sits **on screen** and move its label to follow, placing it just below the avatar. Two touches make this feel three-dimensional rather than like floating stickers: **fade the label down** when its agent is on the far side of the orbit (and hide it entirely when it's directly behind), and **stack labels by depth** so a near agent's name sits over a far one's. Make sure labels don't flash in the top-left corner on first load — only reveal each one once its real position has been computed. Hide the whole constellation on small mobile screens.

Each avatar should also **breathe** — a subtle, slow pulse in its glow, slightly out of phase per agent — so the constellation feels alive even when nothing's happening.

**Verify Tier 4:** the agents drift around the orb on different tilted paths; each name label tracks its avatar and dims and recedes as it passes behind the orb, then brightens as it comes back around; nothing flashes in the corner on load; hovering a label works while clicks elsewhere pass through to the scene.

---

## Tier 5 — Reactions: dispatch, working, docking, and the rings

**Goal:** the constellation reacts to what's actually happening. When an agent is dispatched, a beam shoots from the orb out to it and a ring expands; while it works, it pulses and can leave orbit to settle near its panel; and while the agent is "thinking," glowing rings sprout around the orb. These moments are what make the system feel responsive instead of decorative.

- **Dispatch.** When an agent is dispatched, fire a luminous **beam from the orb out to that agent**, drawn in the agent's accent color, that races out and back over a few seconds while the agent **flares** brighter and larger, peaking in the middle and easing back. At the peak, spawn an expanding **ring** at the agent that scales outward and fades, like a sonar ping. A small ring also pulses at the orb at the very start, as the "sending" beat. Build the beam so it reads as a thick glowing line rather than a thin wire.

- **Working.** While an agent is actively running a task, give it a steady pulsing halo on top of everything else, so you can glance at the constellation and instantly see who's busy.

- **Docking (optional).** If the working agent has a matching panel or tab in the UI, let it **leave its orbit and float over to settle near that panel**, then drift back to orbit when the task finishes. Make it move **in quickly and out slowly** — a snappy arrival so you notice it, a graceful return so it doesn't feel abrupt — and keep it a calm, steady size while docked. Optionally draw a faint tether line from the orb to the docked agent.

- **The rings (the "helix").** While the orb is in the processing state, fade in a few thin glowing **rings** orbiting just outside it, each tilted differently and rotating at its own speed, with bright pulses traveling around them and the color drifting from teal toward purple. These should be the focal point of the processing state. Optionally also add flowing **energy arcs** from the orb to a tool whenever that tool runs, and faint **pulse waves** that ripple outward from the orb periodically while it's thinking.

**Verify Tier 5:** dispatching an agent sends a colored beam out and back with a flare and an expanding ring; marking an agent as working makes it pulse and (if it has a panel) float over to dock there and back; entering the processing state fades in rotating rings around the orb that drift toward purple.

---

## Tier 6 — Wiring, performance, and accessibility

- **Wire it to real events.** Connect my transport (WebSocket or whatever I use) so that incoming events set the orb's state, trigger dispatch and working animations on the right agent, and can even **add a new agent to the constellation at runtime** (generating its avatar and label on the fly). Expose the key functions globally so I can test them from the browser console.

- **Performance mode.** Add a toggle that drops the **background** layer to a lower resolution and renders it every other frame while the orb stays full quality. The background drifts so slowly that the lower framerate is invisible, and it roughly halves the cost of the most expensive part of the scene.

- **Reduced motion.** Respect the system "reduce motion" setting: hold the sub-agents still at their starting positions and calm the camera drift.

- **Battery and focus.** Pause the animation when the tab is hidden or when a full-screen panel covers the scene, and resume cleanly without a jarring jump when it comes back. Clean up graphics resources properly when tearing anything down — this matters especially on iOS.

**Final verification:** run through every state and every agent event with the scene visible. Confirm it's smooth on desktop, that the constellation hides cleanly on mobile, that there are no graphics warnings in the console, and that a cold load looks finished — generated avatars, no broken-image boxes — even before any backend has responded.

---

## The feel, in one paragraph

The north star: it should look **alive, not animated.** The orb is always subtly breathing; the background always slowly drifting; the sub-agents always gently orbiting and pulsing. Every change is a smooth settle, never a cut. Colors are cool and deep — teal, cyan, blue, purple — so the one warm amber "listening" state jumps out unmistakably. The orb feels like a translucent field of energy, lit from its own edges, that the user is genuinely talking to, and the sub-agents feel like a small team of specialists circling it, ready to be called in. Build it layer by layer, verify each one on screen before moving on, and don't sacrifice the easing or the breathing motion to save effort — that constant gentle life is the whole effect.
