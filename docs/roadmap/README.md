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
- a mechanics-bearing leaf without updated mechanics evidence.

The marker recorded after a successful operator test is:

```text
<!-- xevious-playtest:v1 commit=<40-character-head-sha> -->
```

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

## Migration and recovery

[`migration.json`](migration.json) is the resumable journal. The migration:

1. validates the manifest and stops before writes on any mismatch;
2. snapshots milestones, Project fields/items/views, and every protected field
   of active PR #34;
3. creates or converts parents and leaves by stable `roadmap-key`, journaling
   each returned issue and Project identity;
4. attaches native parent relationships, milestones, and derived Project
   fields in separate passes;
5. closes imported-history leaves only after their evidence and relationships
   exist;
6. reads the complete live state back and proves exact parentage, milestones,
   state, uniqueness, and that PR #34 did not change.

An interrupted migration is resumed by rerunning `apply`; it rolls forward from
stable keys and journaled IDs. Issue deletion or silent closure is never used as
rollback because GitHub history and notifications cannot be undone. If PR #36
is abandoned, the journal and `roadmap-migration: PR #36` markers identify the
incomplete projection to resume or explicitly supersede.

Useful commands:

```bash
python3 tools/roadmap.py validate
python3 tools/roadmap.py plan
python3 tools/roadmap.py snapshot
python3 tools/roadmap.py apply
python3 tools/roadmap.py reconcile
python3 tools/roadmap.py handoff
```

Existing Project views and the Engine-owned summary fields are immutable to the
migration. The only fields it adds are `Roadmap role`, `Delivery slice`, and
`Proof level`; each is derived from the manifest.
