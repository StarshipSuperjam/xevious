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
   `python3 <release>/.engine/tools/instantiator.py arrive --target <project-path>`. Any Python 3.9 or newer
   runs the arrival (macOS system `python3` is fine — the engine builds its own newer runtime from there); on
   an older interpreter it stops before touching the project and prints a command to re-run with. Without
   `--accept-all` this is read-only: it changes nothing — whether or not it finds overlaps — and reports each
   place the engine and the project would overlap, in plain language: what the engine would do, what the owner
   keeps or loses, and the choices — accept, leave it as is, or stop. It also notes if the project already has a
   team reviewing changes, and recommends the team setup if so.
3. **Review the overlaps with the owner.** For each overlap, state the consequence and let the owner decide. If
   the owner wants to keep something the engine would otherwise place, sort that out first (for example, move
   their file, or settle on the team or solo setup). If the check found no overlaps, there is nothing to settle
   — go straight to the next step. Nothing has been changed at this point.
4. **Add the engine.** Once the overlaps are settled, run the same command with `--accept-all`, plus the owner's
   reviewer choice and any add-on choices (for example
   `arrive --target <project-path> --accept-all --tier team --keep "" --decline "memory-semantic-recall" --handle their-account`).
   Add-ons are chosen by name: `--keep "a,b"` turns on the optional add-ons the owner wants, and
   `--decline "x,y"` turns off any add-on that is on by default but the owner would rather leave out — the
   smaller starting profile that suits adopting the engine incrementally (naming the same add-on in both is
   refused before anything is written; a declined add-on can be added later — see
   [add a module](module-add.md), an install, not a toggle). The engine is placed alongside the project; its working-guide floor is inserted into the project's
   own CLAUDE.md **and** AGENTS.md — the two runtime instruction files the engine keeps as a matched pair —
   keeping the owner's content; the engine records where it fetches its own updates from, carried from the
   release; a security-contact file is seeded only if the project has none; the project's README and LICENSE are
   left exactly as they are; the reviewer is set; the main branch is protected — when the owner's sign-in can
   administer the repository — so a change now needs a reviewed pull request and the branch can't be force-pushed
   or deleted (if the sign-in can't, the setup says so plainly and step 6 turns protection on later); the
   engine's **own** checks are turned on separately in step 6, because the workflows that produce them only reach
   the branch when this arrival merges; and the whole arrival is opened as a single pull request. If an overlap
   was not accepted, the run stops and changes nothing — sort it out and run the arrival again.
5. **Review and merge the pull request.** The arrival lands the engine's files as one pull request the owner
   approves. Until it is merged, none of the engine's files are on the main branch (branch protection is a GitHub
   setting turned on in step 4, not carried by this pull request); merging is the owner's consent, and reverting
   it removes the engine's files again.
6. **Turn the engine's required checks on — once, after the merge.** The arrival deliberately did **not** yet
   require the engine's own checks (`engine-ci`, `engine-guard`): the workflows that produce them arrive inside
   the pull request itself, so requiring them before it merged would have made that very pull request impossible
   to merge. Now that it has merged and those workflows are on the branch, run
   `python .engine/tools/bootstrap.py finalize` from the project to turn the required checks on. It checks the
   workflows are actually on the branch first (and refuses, rather than deadlock, if they are not — usually a
   sign the pull request has not merged yet), and it is safe to run again. After this, the review gate is fully
   in force. Until it is run, the boot briefing keeps flagging that the gate is not yet fully on.
7. **Get grounded — the onboarding read.** Once the arrival is merged, the engine's **first act on the existing
   project is to read it**, not to build. In Explore mode, go through the project and save a durable
   understanding of it to the engine's memory, so every later session starts grounded instead of cold — the
   engine joined a project with a history it does not yet carry, and this read is how it catches up. Follow the
   onboarding-read operation, then hand off to the first build. This is a read of the project, not a change to
   it.

## Done when

The engine's files are in place alongside the project's, every overlap was surfaced and settled by the owner's
choice, setup ran and protected the main branch, and the arrival is open as a pull request the owner can approve
— or the arrival stopped cleanly at an overlap the owner chose to keep, with nothing changed. Once the arrival
is merged, the engine's required checks were turned on with the one-time `finalize` step, and the engine has run
the onboarding read (Explore-mode, saved to memory) so it starts grounded on the project it joined.

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
exact additions are recorded (across both the arrival and the `finalize` step), so a later clean removal takes
back exactly what was added. If the project has more than one rule covering main, or protects it a different
way, the engine adds its own rule alongside and says so.

**Why the checks are turned on in two steps.** A required check can only report once the workflow that produces
it is on the branch — and the engine's workflows arrive *inside* the arrival pull request, so they aren't on the
branch until it merges. If the arrival required those checks immediately, the very pull request that carries
them could never go green, and nothing could merge. So the arrival protects the branch but leaves its own checks
un-required, and the one-time `finalize` step (step 6) turns them on after the merge. Between the merge and
`finalize`, the branch is protected (a pull request is required; no force-push; no deletion) but the engine's
checks are not yet required — run `finalize` promptly, and the boot briefing will keep reminding you until you
do. **If a project reached a stuck state under an older engine** — its main branch already *requires*
`engine-ci`/`engine-guard` while the pull request that adds their workflows can't merge — clear it by hand once:
remove the required-checks rule from the branch (the repository's branch-protection settings, or the engine's
own de-bootstrap "keep" path, which leaves protection on without the checks), merge the stuck pull request, then
run `finalize`.

**On an older Python, the Codex config wire may defer.** The arrival runs its setup on the system Python (3.9 or
newer). One optional step — registering the engine's Codex helper server in an existing, non-empty
`.codex/config.toml` — needs Python 3.11+ to validate that file before editing it. On 3.9 with a non-empty Codex
config already present, the engine leaves the file untouched and says so, and setup pauses so you can finish it
under the engine's own 3.11 runtime (or add the block by hand) rather than risk corrupting your config. This is
the uncommon case; a project with no Codex config, or an empty one, is set up cleanly on 3.9. One consequence to
know: a Codex block written this way on a 3.9-only machine also can't be *removed* by the engine on that same
machine (the same validation gap) until a 3.11 runtime is available — remove the marked block by hand if needed.

**Reviewed, reversible, and re-enterable.** The arrival lands as a pull request behind the project's review gate,
so it is approved and can be undone by reverting it. If the arrival stops at an overlap, running it again picks
up from the overlap step — nothing shared was changed.

**Run the fetched release, not a local copy.** The engine's first-run setup tool retires itself once setup is
done, so to add the engine to a further project you fetch a fresh release again rather than reusing a copy from
an already-set-up project.
