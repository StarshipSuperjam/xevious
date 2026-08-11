---
id: eADR-0039
title: Whole-module removal is a reconcilable, disclosed upgrade outcome, authorized by the release's own removal record
status: accepted
date: 2026-08-04
---

## Decision

When a release drops a **whole module**, a deployed engine that still holds that module now **reconciles it away
and announces the loss in plain language** on update — rather than hard-refusing the update, which is what it did
before. Reconciling a dropped module means: its owned files are removed, its wiring is reversed, its own manifest
folder (`.engine/modules/<id>/`) is deleted, and it is pruned from `engine.json` `packages` — the same teardown
`remove()` performs. The operator is told, in operator language, what they could ask the engine for before and no
longer can, beside the file-level list, in the update preview, the pull-request body, and the applied echo.

The mechanism is bound by these rules:

- **The release's own record is the authorization, not just the text.** A dropped module carries a plain-language
  line in the release's `engine.json` `removed_capabilities` (module-id → `{description, removed_in}`). That
  record does double duty: it is the operator-facing explanation **and** the release's proof that the module's
  absence is *intentional*. An installed module absent from a release **with** a record is reconciled; absent
  **without** a record still **refuses** the whole update (a broken or incomplete release — refuse-don't-guess).
  Trusting the record is safe because the release tree is already the trust root for the overlaid engine code.
- **The record lives in `engine.json`, never a changelog.** It is the one committed inventory that outlives a
  removed module's manifest, and it is release-cut-authored. A second history store (a changelog, a notes file)
  is forbidden by eADR-0014, so the record is a structured, current-state block there — keyed by module-id (one
  entry per removed module), not the append-only version-keyed history its within-module sibling
  (`retired_capabilities`) uses.
- **Authored at removal, stamped at the cut, guarded at the cut.** `remove --removal-notice` writes the line at
  removal time; the release cut stamps its `removed_in` and **refuses to cut** a release that drops a module
  without its line — the third sibling of the migration and retired-capability accumulation guards. The cut also
  refuses a release whose surviving module still depends on a module it removes.
- **Single-homed dropped set.** One `dropped_ids` set (installed, absent-from-release, recorded) drives **both**
  the reconcile and the disclosure, so a module can never be reconciled-away without being announced, or announced
  without being reconciled — the two can never disagree.
- **Recovery is undo, not re-run.** The teardown durably prunes `engine.json`, so a drop half-state (a later gate
  refusal) is recovered by **undo** — which restores the module so a fresh update re-detects and re-announces it —
  not by a plain re-run, which would complete with the module already pruned and never disclose the loss.
- **Every dropped-module deletion is recoverable.** Because a whole-module drop is automatic and release-initiated
  (no per-file operator intent), the git-tracked-only guard is widened to **every** file a dropped module owns:
  an untracked, unrestorable file is left in place and surfaced, never deleted.

**Boundaries (accepted).** (1) The record's obsolescence prune is bounded by `min_upgradeable_from`: once a
removal's `removed_in` is at or below the floor, no supported upgrader can still hold the module, so the entry may
be dropped. (2) A rename reads as a removal-plus-addition; the cut requires a truthful removal notice for the old
id and does not auto-detect that the capability re-appears under a new name.

## Significance

Before this, a release that dropped a whole module refused every holder's update outright, with a raw
"the release does not contain the installed module 'X'" message and no path forward — the *largest* capability
loss got the *rawest* treatment and blocked the operator, who could not self-diagnose that the dropped module was
the cause. This makes a whole-module removal a first-class, clean, disclosed upgrade outcome, closing that gap
(StarshipSuperjam/engine-template#688) and completing the removal-notice family whose within-module half shipped earlier. Any later reader
touching the update must keep the single `dropped_ids` set driving both the reconcile and the disclosure, and keep
the removal record in `engine.json` rather than inventing a second store.

## Rationale

The record has to live somewhere that survives the module it describes; a removed module's manifest is gone, and
eADR-0014 forbids a bespoke changelog, so the release-cut-authored `engine.json` inventory is the only lawful home.
Making that same record the *authorization* to reconcile — rather than a separate flag — is what lets the update
distinguish an intentional drop from a broken release without a second signal that could disagree with the first;
it costs nothing extra because the release tree is already trusted to overlay executable code. Undo-only recovery
follows the existing reconcile half-state posture (the delete leg already routes an interrupted reconcile to undo,
because a re-run recomputes its candidate set from the mutated tree). The tracked-guard is widened because, unlike
`remove()` — a deliberate per-module operator act — a release-driven drop deletes a whole module's files with no
operator in the loop, so the recoverability invariant must hold for all of them, not just the glob-suspect ones.

## Anti-choice

The rejected alternative was **Option A: keep the refusal, only make it legible** — render the plain-language line
into the missing-module refusal and leave the update blocked. It was the smaller change, and it does tell the
operator what they lost. It lost because it does not actually resolve the loss: the operator remains unable to
update until they themselves deduce which module is the blocker and manually uninstall it — at which point the
notice's own suppression rule (the module is no longer installed) means they never see the plain sentence anyway.
That is a worse outcome than the one the notice describes. Reconciling the drop makes the update *complete* while
disclosing the loss, which is what the operator actually needs. Also rejected, per eADR-0014: recording the removal
in a changelog or the recomputed release notes rather than the `engine.json` inventory.

## Status

accepted
