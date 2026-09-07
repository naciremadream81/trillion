# Static assets

## `celebrate.mp3` — the celebration sound (not shipped)

`index.html` fetches `/static/celebrate.mp3` when a payment celebration
fires. **Drop your own file here.** The playbook is explicit that this should
be your jingle, not a tone the code synthesises — so nothing is shipped, and
until you add one there is simply no sound.

Nothing breaks without it. The file is fetched and decoded once, and a
missing or blocked file is caught: the visual celebration is the primary
signal and always plays. Browser autoplay policy means the sound stays silent
until you have interacted with the page at least once in that session, which
is another reason the visual carries the message.

Mute it, or turn celebrations off entirely, from the console:

    trillionCelebrate.mute(true)
    trillionCelebrate.off(true)

Fire a test celebration at any amount (dollars):

    trillionCelebrate.test(1300)

## `icons/` — the PWA icons

Generated, not hand-drawn — a luminous orb on Trillion's own ground, written
by a stdlib PNG writer so the repo gains no image dependency for four static
files. Regenerate them by editing the palette in that snippet and re-running
it; the manifest references `orb-192`, `orb-512`, `orb-maskable-512`, and
`apple-touch-icon`.
