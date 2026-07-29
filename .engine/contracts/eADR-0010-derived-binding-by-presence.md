---
id: eADR-0010
title: Derived binding by presence
status: accepted
date: 2026-06-29
---

## Decision

Wherever the engine must discover *which* providers, implementations, or members are present — the agent roster, check-suite membership, the implementations behind a swappable contract, the workflows that run — it derives that set from the **presence and self-declaration** of the things themselves, never from a central list an install has to edit. Adding a participant is a file drop; removing it is a file deletion; the set is re-derived, never surgically mutated. This discovery axis is held deliberately separate from the closed wiring seam (eADR-0009), which remains the only mechanism for keyed, reversible edits to genuinely shared state (`hook`, `mcp`, `ontology-entry`, `permission`, `gitignore`). A consumer that must *find* its providers derives them; a module that must *edit* shared settings still wires. The two never collapse into one.

## Significance

This locks in reversibility for everything discovered. Any system that enumerates participants — validation deciding which checks belong to a suite, the gate machinery deciding which agents exist, a contract deciding which implementation answers, the lifecycle deciding which workflows are live — must read presence and self-declaration, and must never introduce a registry, index, or manifest that an install mutates to register a member. A new provider proves its membership by existing in the right shape and declaring itself; nothing else grants or revokes it. Later work may add new discovery axes but inherits the firewall: a removal must always reduce to deleting the file, with no orphaned registry entry left to hunt down. The one thing this does not license is folding shared-state wiring into presence: edits that touch the closed seam still wire, because their reverser guarantee depends on being keyed, not on being present.

## Rationale

The same pattern had already appeared in four independent places, and each instance bought the same thing: removing a participant becomes a deletion rather than a careful edit to a list that another hand also touches. A central registry is the classic failure mode — installs append to it, removals forget to prune it, and the registry drifts out of agreement with the files it indexes until no one trusts either. Deriving the set from the files removes the second source of truth entirely, so there is nothing to drift. The countervailing force is that some bindings genuinely *are* shared mutable state — two modules editing the same settings file cannot each "be present" without a keyed, reversible record of who wrote what — and for those the closed wiring seam (eADR-0009) is the correct, deliberately different mechanism. Drawing the line at *discovery versus shared-state edit* keeps both guarantees intact: presence gives discovery its file-drop reversibility, wiring gives shared edits their keyed reverser.

## Anti-choice

The strongest rejected alternative was to make this a blanket rule — "no central registries anywhere; everything binds by presence." It loses because it would falsify the wiring seam (eADR-0009). The `hook`, `mcp`, `permission`, `gitignore`, and `ontology-entry` bindings are edits to shared state that several participants touch at once; their reversibility comes from being keyed and individually undoable, not from a file simply existing. A blanket presence rule would claim those bindings work a way they do not, and would erase the keyed-reverser firewall that contains shared-state churn. Scoping the law to the *discovery* axis — which set is present — keeps the win where it actually applies and leaves the genuinely different shared-edit mechanism standing.

## Status

accepted
