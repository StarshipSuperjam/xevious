# Dependency-aware Xevious roadmap

The roadmap is designed to keep a delivery from claiming completion before it
can be exercised. Its committed desired state is
[`manifest.json`](manifest.json). GitHub Issues, milestones, native sub-issue
links, and the Xevious Project board are projections of that file.

## Ownership

- `docs/spec/build-plan.md` owns the capability phases and their order.
- `docs/BUILD_PLAN.md` owns the engineering delivery sequence.
- `manifest.json` decomposes that sequence into capability parents, atomic
  component leaves, blockers, milestones, proof levels, and delivery slices.
- `criteria.json` is the independent exact obligation roster; validation fails
  if a criterion is dropped, invented, or assigned twice.
- GitHub Issues own discussion and delivery evidence for each projected item.
- Project #4 is a replaceable operational view. Its delivery fields never
  become the only record of scope or dependency.

A capability parent has no milestone and never closes directly from a pull
request. Its native sub-issues are the independently closable units. A pull
request may compose leaves from several capability parents, but it uses one
`Closes #N` line for every leaf it actually completes.

## Closure evidence

The required project check reads the manifest and migration journal before a
pull request may close a roadmap leaf. It rejects:

- a capability parent as a close target;
- a provisional leaf whose specification is not settled;
- a leaf with an open blocker, unless the same pull request closes it;
- a gameplay or operator-tested leaf without the `playtest-approved` label
  and a repository-owner comment identifying the exact tested head commit;
- a mechanics-bearing leaf without matching updated mechanics evidence;
- a leaf without changed automated success and failure evidence;
- a leaf whose prerequisite delivery slices still contain open work.

The marker recorded after a successful operator test is:

```text
<!-- xevious-playtest:v1 commit=<40-character-head-sha> -->
```

Automated tests added by a delivering PR identify both sides of every atomic
obligation with comments such as:

```text
# roadmap-evidence: SYS-02 success
# roadmap-evidence: SYS-02 failure
```

Only markers on lines newly added by that PR count. Existing comments or
unrelated tests cannot satisfy the closure gate.

The issue-closure workflow applies the same contract to a manually closed
roadmap item and reopens an invalid closure. The protected-branch merge and the
operator's review remain the binding gate.

## Provisional work and imported history

Leaves under a `draft` specification are visible so the full plan is visible,
but they carry `spec:draft`, remain non-executable, and cannot close. Settling
their owning specification is an explicit blocker.

Completed slices are imported as history leaves with their original delivering
pull request. They are not retroactively certified under today's evidence
contract. Incomplete foundations—including the live entity, collision, random,
and dispatch paths formerly hidden by closed issue #14—remain open leaves.

## Recording delivery: the slice PR carries it

`manifest.json` is the source of truth and GitHub is a projection, so a slice's
delivery is recorded **in the same pull request that delivers it** — the manifest
can never lag `main`.

1. In the slice PR, once it closes its leaf issues (one `Closes #N` per delivered
   leaf), record the delivery and commit the edit into the PR:

   ```bash
   python3 tools/roadmap.py deliver --pr <this PR#>
   ```

   This flips each closed leaf from `status: "planned"` to `"history"` with
   `delivered_by: <PR#>` — a minimal per-line manifest edit. `deliver` accepts an
   **open** PR precisely so this rides the same PR; the required closure check then
   refuses the merge unless the manifest at the PR head records every leaf the PR
   closes. At merge, GitHub closes the issues and the same merge lands the manifest
   that records them, so the two always agree.

2. After it merges, project the manifest onto GitHub from an updated `main`:

   ```bash
   python3 tools/roadmap.py apply       # keep the delivered issues closed, board Status → Done
   python3 tools/roadmap.py reconcile   # verify the live projection matches the manifest
   ```

   This second step is **manual by design**: GitHub Actions' own token cannot read
   or write the org Project board and no personal-access token is provisioned, so
   nothing pushes the board automatically. If you forget it nothing breaks and no
   issue reopens — the next `reconcile` simply reports the drift and names the leaf,
   and running `apply` fixes it. A converged `apply` writes nothing
   (`patched 0 issues, 0 board fields`). If `apply` changes the `migration.json`
   journal, land that change on `main` through a small pull request, like any other
   change (nothing reaches `main` except by pull request).

**Dropping a leaf.** There is no `dropped` status yet. If a `planned` leaf must be
cancelled before one exists, leave its issue open and edit the manifest by hand
under review — do **not** hand-close the issue, which the closure guard reopens.

## Archived board cards

GitHub auto-archives a card about two weeks after its issue closes, so most
delivered leaves' cards are archived. The tooling is archive-aware: `apply` and
`reconcile` read archived cards too and accept an archived Done card as correct, so
a delivered leaf never reads as a missing or drifted card.

## Commands

```bash
python3 tools/roadmap.py validate         # the manifest is internally consistent
python3 tools/roadmap.py plan [--live]     # counts; --live adds a read-only GitHub diff
python3 tools/roadmap.py deliver --pr N    # record a PR's leaves delivered (run in the slice PR)
python3 tools/roadmap.py apply             # project the manifest onto Issues + the board
python3 tools/roadmap.py reconcile         # verify the live projection matches the manifest
```

[`migration.json`](migration.json) is the identity journal: it caches the GitHub
issue and card ids for each stable `roadmap-key` plus the project header `apply`
bootstraps from. Existing Project views and the engine-owned summary fields are the
operator's, not the manifest's; the tool owns only the delivery-leaf,
capability-parent, and imported-history views and the derived `Roadmap role`,
`Delivery slice`, and `Proof level` fields.
