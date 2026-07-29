# `empty-roster/` — the deliberately instance-free roster directory for the live self-run

*Internal fixture note — not operator-facing; the jargon below is for whoever maintains the meta-check.*

When the meta-check goes live (S5) it enumerates itself as one more `custom/script` instance and must run against
its own seeded mini-scenario instead of re-entering the live check set (which would recurse). The self-fixture's
`target.json` points the self-run's `ENGINE_ROSTER_DIR` **here** — at this directory — precisely because it holds
**no `*.json` check file**. The self-run therefore enumerates **zero** `custom/script` instances, covers only the
non-biting `presence` fixture in `../kind-presence/`, emits the expected "did NOT catch" finding, and terminates —
no meta-meta-check.

**This directory must stay free of any top-level `*.json`.** Adding one would make the self-run enumerate it as a
check instance and break the termination guarantee. (Pointing `ENGINE_ROSTER_DIR` at this dedicated empty directory,
rather than at the scenario directory itself, is what makes the termination robust against a stray file rather than
resting on the scenario directory happening to have no top-level `*.json`.)
