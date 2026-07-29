---
id: eADR-0003
title: Ship the substrate, not the data
status: accepted
date: 2026-06-29
---

## Decision

This template carries the machinery of its stores — schemas, capture code, query services, validators — but never the data that machinery accumulates. Experiential stores (the memory ledger and every index, cache, or boot slice derived from it) live out-of-repo and are gitignored; a freshly stood-up project begins with empty stores and grows its own. The template installs the apparatus, not a starting corpus.

## Significance

This fixes the boundary between what travels and what stays local. Every store that holds high-volume, per-instance experiential data must ship empty and gitignored, so a new project never inherits another project's recall and routine work is never taxed by reviewing or diffing that data. It does not extend to contracts, decisions, or structure derived deterministically from committed source — those remain authoritative committed files. Later stores must declare which side of this line they sit on: if a store holds experiential, per-instance, high-volume data, it ships empty and stays out of the repo; if it holds reviewable truth, it stays committed. No store may quietly commit its accumulated data, and none may make a gitignored derivative the only copy.

## Rationale

Experiential recall is high-volume, grows per project, and is not worth gating at human review — it is the kind of state that should stay local rather than ride in tracked files. Two forces drive it out of the repo: committing it would tax every routine change with churn nobody reads, and it would leak this engine's own development memory into every project stood up from the template. Shipping the machinery empty satisfies both — the capability arrives intact and ready, while the data each project earns stays that project's own. The trade given up is the diffability of committed text; for experiential recall that loses to leakage and friction, though it is exactly why reviewable contracts and decisions are kept committed instead.

## Anti-choice

The strongest rejected alternative was repo-authoritative memory: store experiential recall as committed, human-readable cards so every remembered thing is diffable, reviewable, and travels with the project like any other tracked file. It lost because the very property that makes committed text valuable for contracts — it travels — is a defect for experiential data: it carries this engine's development memory into unrelated adopter projects and burdens every routine change with high-volume churn no one reviews. Diffability does not outweigh cross-project leakage and review friction for recall specifically, so memory is held out-of-repo while the reviewable contracts and decisions it once threatened to crowd stay committed.

## Status

accepted
