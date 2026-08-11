---
title: Supported-version deployed upgrade and rollback matrix
status: accepted
date: 2026-08-10
---

## Rule

Every engine release must be provably upgradeable-to, and cleanly undoable, from every supported source
version — before the release pull request opens. The **supported source set** is defined, mechanically, as
every released version tag (`vX.Y.Z`) present in the cutting checkout at or above the recorded clean-upgrade
floor `min_upgradeable_from` (`.engine/engine.json`). At each release cut the deployment gate
(`.engine/tools/release_gate.py`, run from `.github/workflows/release.yml`) projects each of those releases to
its deployed shape, runs a real practice upgrade to the candidate, and then a real undo of that staged
update — one row of the matrix per supported version. A release for which any supported transition's upgrade
or undo fails is blocked and opens no pull request. The per-transition outcomes are recorded in the release
pull request's Validation section, and the same matrix can be run between cuts on demand via the
`release-gate` workflow.

## Scope

This governs engine releases cut from the engine's home repository. The gate is home-repo-only and inert on a
deployed repository or a product cut (a deployed repository runs its own `engine-ci` directly). "Supported"
means at or above the floor: versions **below** the floor are out of scope for this matrix and are not tested
by it — they predate the floor-preflight code and cannot self-refuse (see Rationale). The recovery this proves
is the undo of a stalled or staged update (the operator's `rollback`); it does not exercise reverting an
already-merged upgrade pull request, which is a separate, guided recovery.

## Rationale

An engine that changes quickly is only trustworthy to deploy if a real deployed copy can move onto each new
release and, if something goes wrong, get cleanly back off it. Testing that once is not enough — the set of
supported sources shifts every release — so it is made a standing, executed check rather than a one-time
exercise. Tying the supported set to the single recorded floor keeps the promise honest: the matrix tests
exactly the versions the engine also promises a clean upgrade to, and the release evidence names the floor and
the count so a reviewer can see the matrix was not silently shrunk by a bumped floor or a missing tag. Sources
below the floor are excluded deliberately: the machinery that would let an old copy refuse an unsafe upgrade
did not exist yet in those releases, so no test run against their real trees could demonstrate a clean
refusal — which is the whole reason a floor exists. That floor behavior is proven separately by
`demo_599d_upgrade_floor.py` and `test_module_manager.TestUpgradeFloorPreflight`. This work closes
StarshipSuperjam/engine-template#703, the closing item of the StarshipSuperjam/engine-template#599
upgrade-integrity family.

## Enforcement-tier

**Layered, and stated honestly.** The strong part is a **hard, fail-closed gate at release cut**: the
deployment gate runs before the pull request is authored, and any blocked transition (or any setup failure)
stops the cut with no pull request opened; `release.yml` additionally cross-checks an independent home-repo
signal so an origin misdetection cannot ship an engine release ungated (the
StarshipSuperjam/engine-template#676 protection). The **on-demand** `release-gate` workflow is advisory: it
reads and reports between cuts and its record is the run's step summary, which ages out — it opens nothing and
blocks nothing. One honest gap: the gate is a workflow **step**, not a check rule in a suite, so removing that
step from `release.yml` is caught only as a non-blocking disclosure by the guardrail guard, not as a hard
block — the real backstop against that is the operator's review of the workflow change at merge. A mechanical
presence check for the gate step is a candidate future hardening, tracked separately.
