# Hunting lanes

One lane fires per run. Heading order is the rotation order — the scheduler
takes the weekday's index modulo however many lanes are live, so adding or
removing a lane reshuffles the rotation but never breaks a scheduled run.

Each `## Heading` starts a lane. The heading is slugified into the label
stored on whatever that lane finds, so renaming a heading cosmetically
(`Open Source Friction` → `Open-source friction`) keeps the same label,
while renaming it substantively starts a new one on purpose.

This text above the first heading is for you — the parser ignores it. An
override stored in `agent_documents` (key `scout_lanes`) replaces this whole
file; if it parses to zero lanes, this file is used instead and the log says
so.

## Open source friction

Hunt in issue trackers. Look for issues that are open, old, and have more
thumbs-up than replies — the shape of "many people want this, the maintainer
does not want to build it". Closed-as-wontfix is just as good: the maintainer
has told you they will not compete with you.

Prefer projects with real usage over popular ones. A tool with 800 stars and
forty people arguing in one thread is a better signal than a 40k-star project
where the same thread is one of hundreds.

## Manual workarounds

Hunt for people describing their own duct tape. The vocabulary is specific:
"I wrote a script that", "every Monday I export", "we keep a spreadsheet
that", "I just do it by hand". Someone maintaining a workaround has already
proven the problem is worth effort — the only open question is whether it is
worth money.

Weight the maintenance burden, not the initial build. A one-off script is a
solved problem. A script that breaks every time the upstream format changes
is a product.

## Small business operations

Hunt where a person runs a small operation and the software assumes a big
one. Scheduling, invoicing, stock, compliance paperwork, client
communication — the recurring complaint is a tool priced and designed for a
team of fifty being the only option for a team of two.

Be careful with the hard kill on regulated territory here: bookkeeping-
adjacent is fine, giving financial advice is not, and moving money is not.

## Developer tooling gaps

Hunt for the friction developers describe in their own workflow — the build
step everyone scripts around, the config format everyone gets wrong, the
error message nobody can decode. Developers are unusually good at describing
a problem precisely and unusually likely to have tried to fix it already, so
the evidence tends to be strong.

The buildability score matters most in this lane: developers will not adopt a
tool that is harder to install than the problem is to endure.

## Data trapped in the wrong shape

Hunt for people who have the information they need but cannot use it — locked
in a PDF, split across exports, in a format one tool writes and another
cannot read. The complaint usually sounds like a request for an export
feature that will never ship.

These are strong buildability candidates: inputs and outputs are both
concrete, and correctness is testable without a human looking at it.
