---
id: eADR-0002
title: Repo-authoritative truth; derived indexes are replaceable
status: accepted
date: 2026-06-29
---

## Decision

Canonical state lives in committed, human-readable files. Every index, cache, query store, vector store, or boot slice built on top of that state is a derivative that can be thrown away and regenerated from the canonical source. Structural state in particular — the surface graph and its coverage map — is generated from the source surfaces rather than hand-authored, and its regeneration is fingerprint-gated so it cannot silently drift from what the source actually says. No derivative is ever the only copy of anything.

## Significance

This locks in which artifacts are allowed to be lost and which are not. Anything regenerable is gitignored and disposable; anything authoritative is committed and reviewable. Later work must respect that boundary: a system may build any accelerator it likes on top of canonical state, but it may not let that accelerator hold the only copy of a fact, and it may not hand-curate structural state that the source could derive. Because the structural graph is purely derived, a merge or rebase conflict on it is spurious — both sides are valid regenerations of one source tree, resolved by regenerating from the reconciled tree, never by a hand-merge or a side-pick, and never surfaced for anyone to resolve. The same property makes derived state upgrade-safe: an overlay replaces the derived artifacts and the next regeneration self-corrects them to the actual surfaces. This is the structural-knowledge half of the rule; the experiential ledger (eADR-0019) carries its own canonical-vs-index split under the same spirit, and the wiring seam (eADR-0009) keeps such accelerators swappable.

## Rationale

Two failure modes are being closed at once. First, if a derived store becomes the sole record, a rebuild becomes a data-loss event and offline cold-start truth is gone — so the canonical copy must always be the committed source, and the index merely an accelerator. Second, structural state that is hand-authored rots silently: the source moves and the hand-kept map quietly lies, with nothing to catch the gap. Deriving the structure from the source and gating regeneration on a fingerprint of the source means the structure cannot drift unnoticed — a changed surface without a matching regeneration is caught as a finding, not discovered later as a wrong answer. The cost is that derivation must run somewhere and stay current; that cost is paid on the build path (when surfaces change) where latency is tolerable, never at orientation where it is not.

## Anti-choice

The strongest rejected alternative was to hand-curate the structural graph — author and maintain the entity-and-edge map directly as the canonical artifact. It is tempting because hand-authoring gives precise control over exactly what the graph says and needs no generator. It loses because the map drifts out of sync the moment the source evolves and no one updates it, and that drift is invisible: the graph keeps answering confidently while quietly contradicting the surfaces it claims to describe. Derivation plus a fingerprint gate trades that silent rot for a loud, catchable staleness signal, which is the only acceptable posture when the answers are trusted on evidence rather than re-verified by reading.

## Status

accepted
