# The roster

**This directory ships empty, and that is the correct state.**

A dossier is a file of numbered principles attributed to a real, named
person, and the board presents them to Sean as that person's documented
view. A dossier whose citations were never checked against real published
sources is therefore not a rough draft — it is fabricated advice in
somebody's name, which is the specific failure the whole design in
`playbook/the-board.md` exists to prevent.

So no seats are shipped. They are researched, fact-checked, and written by
`agent/board/research.py`, which needs a search API key
(`BRAVE_SEARCH_API_KEY` or `FIRECRAWL_API_KEY`) and network access.

## Seating an advisor

    trillion board research "Advisor Name" --seat "Pricing and packaging"

That runs both stages: research, then an independent adversarial pass that
assumes the file is wrong and tries to refute every entry. Entries that
survive are written here marked `verification: sourced`. Entries that don't
are dropped, and the reason is printed — **read the rejections**, they are as
informative as what survived.

If fewer than five entries survive, no file is written. A thin dossier of six
verified entries is far better than nine with one invented; a dossier of two
is not a seat.

## Writing one by hand

Legitimate, and the format is documented in `agent/board/dossier.py`. Mark
every entry `verification: user`. Seats may cite your own entries — your
knowledge is real input — but the chair is told, so it can say "this rests on
something you wrote yourself" instead of presenting it as documented. Never
hand-set `verification: sourced`; only the adversarial pass earns that, and
editing an entry through the UI drops it back to `user` automatically.

## Two rules that bite

**Doctrine ids are explicit, never positional.** `D3` means whatever the line
marked `D3` says, forever. Reordering entries is safe; renumbering them
re-points every citation on every meeting already stored.

**Retire, never delete.** Removing an entry frees its id, and the next entry
to inherit `D3` silently re-points old citations at different content — a
wrong attribution, not a broken link. Use the retire path.

## A malformed file

...loses its own seat, logs why at warning level, and leaves the rest of the
roster working. It never vanishes silently: a quorum that quietly shrinks is
worse than a stale one.
