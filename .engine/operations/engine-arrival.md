---
title: Add the engine to an existing project — fetch it, check for overlaps, and set it up reviewably
---

## Purpose

How the engine joins a project that **already has its own files**. There is no "Use this template" step on a
project that already exists, so the engine is fetched from its published releases at one pinned version, placed
alongside the project's files in its own namespaced corners, checked for any overlap with what is already there
— each overlap surfaced in plain language with a choice — and then set up by the same first-run setup the engine
uses everywhere. Nothing lands on the main branch without review. Enter this runbook to add the engine to an
existing project. Before starting, make sure: the project is on GitHub and you are signed in from the command
line (`gh auth status` reports signed in — the engine is fetched with your `gh`); you are on a clean working
branch of the project, not its main branch and with nothing uncommitted, so the arrival is a reviewable set the
owner approves rather than an in-place edit; and you know which pinned engine release tag to install (if unsure,
use the latest release). You run the engine's own tools from the **fetched release** you extract below — never a
local copy on the project, which the engine retires after setup.

## Steps

1. **Fetch the pinned engine release into a temporary folder.** Download the engine at the named release **tag**
   — a fixed, pinned version, never a moving branch, because the engine is executable code and a pinned tag is
   the supply-chain control — and extract it to a temporary folder outside the project. This gives you the
   engine's tools to run; it does not yet touch the project.
2. **Check for overlaps — read-only, before anything is changed.** From the extracted release, run
   `python3 <release>/.engine/tools/instantiator.py arrive --target <project-path>`. Without `--accept-all`
   this is read-only: it changes nothing — whether or not it finds overlaps — and reports each place the engine
   and the project would overlap, in plain language: what the engine would do, what the owner keeps or loses,
   and the choices — accept, leave it as is, or stop. It also notes if the project already has a team reviewing
   changes, and recommends the team setup if so.
3. **Review the overlaps with the owner.** For each overlap, state the consequence and let the owner decide. If
   the owner wants to keep something the engine would otherwise place, sort that out first (for example, move
   their file, or settle on the team or solo setup). If the check found no overlaps, there is nothing to settle
   — go straight to the next step. Nothing has been changed at this point.
4. **Add the engine.** Once the overlaps are settled, run the same command with `--accept-all`, plus the owner's
   reviewer choice and any add-ons kept (for example
   `arrive --target <project-path> --accept-all --tier team --keep "" --handle their-account`). The engine is
   placed alongside the project; its working-guide block is inserted into the project's own CLAUDE.md, keeping
   the owner's content; a security-contact file is seeded only if the project has none; the project's README and
   LICENSE are left exactly as they are; the reviewer is set; the review gate is turned on; and the whole
   arrival is opened as a single pull request. If an overlap was not accepted, the run stops and changes
   nothing — sort it out and run the arrival again.
5. **Review and merge the pull request.** The arrival lands as one pull request the owner approves. Until it is
   merged, the project's main branch is unchanged; merging it is the owner's consent, and reverting it removes
   the engine again.
6. **Get grounded — the onboarding read.** Once the arrival is merged, the engine's **first act on the existing
   project is to read it**, not to build. In Explore mode, go through the project and save a durable
   understanding of it to the engine's memory, so every later session starts grounded instead of cold — the
   engine joined a project with a history it does not yet carry, and this read is how it catches up. Follow the
   onboarding-read operation, then hand off to the first build. This is a read of the project, not a change to
   it.

## Done when

The engine's files are in place alongside the project's, every overlap was surfaced and settled by the owner's
choice, setup ran and turned on the review gate, and the arrival is open as a pull request the owner can approve
— or the arrival stopped cleanly at an overlap the owner chose to keep, with nothing changed. Once the arrival
is merged, the engine has run the onboarding read (Explore-mode, saved to memory) so it starts grounded on the
project it joined.

## Notes

**Surfaced, never silent.** Every overlap is shown before anything is changed; the engine never overwrites a
project file without the owner's choice, and on a shared file (like the project's CLAUDE.md) it adds only its own
clearly-marked block and keeps the rest. The later consistency check expects the project's own files and does not
re-flag them — the overlap check is the single place overlaps are reported.

**The project's front page and license stay the project's.** On an existing project the engine seeds no README
and no LICENSE, and leaves any the project has untouched. A security-contact file (`SECURITY.md`) is added only
if the project has none: if the project already carries one — in its root, `.github/`, or `docs/` — the overlap
check surfaces it and the engine leaves it exactly as it is, seeding nothing, so the owner sees plainly that
their existing disclosure channel was found and kept, not quietly replaced.

**Your branch protection is added to, never replaced.** If the project already protects its main branch with
its own rule, the engine adds its checks to that rule in place — and adds any missing force-push, deletion, or
pull-request protection — rather than creating a second rule, and it leaves everything else of the rule exactly
as it was. Anything it cannot add without changing a setting the owner chose is reported, not overwritten. The
exact additions are recorded, so a later clean removal takes back exactly what was added. If the project has
more than one rule covering main, or protects it a different way, the engine adds its own rule alongside and
says so.

**Reviewed, reversible, and re-enterable.** The arrival lands as a pull request behind the project's review gate,
so it is approved and can be undone by reverting it. If the arrival stops at an overlap, running it again picks
up from the overlap step — nothing shared was changed.

**Run the fetched release, not a local copy.** The engine's first-run setup tool retires itself once setup is
done, so to add the engine to a further project you fetch a fresh release again rather than reusing a copy from
an already-set-up project.
