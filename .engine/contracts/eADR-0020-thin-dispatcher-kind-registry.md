---
id: eADR-0020
title: Validation is a thin dispatcher over a closed kind-registry
status: accepted
date: 2026-06-29
---

## Decision

Validation is built as two separated parts: checks are data and check *logic* is a small registry of callable kinds. The core is a thin dispatcher — it loads the rule files, routes each to its declared kind's callable, collects the results, and reports them by the rule's enforcement tier. It holds no opinion of its own about how hard any rule bites; the rule's tier decides that. A closed set of core kinds ships with the foundation (schema, shape, presence, coverage, coherence). A new kind is added only by providing a conforming callable that is discovered because it is present — the same discovery axis as agent rosters and suite membership — never by editing a central file or threading a wiring seam; a custom/script escape hatch covers one-offs. Every kind callable returns the same shape: a pass/fail verdict plus zero or more findings on the shared finding base, where a check finding's severity is the rule's own tier. A rule whose kind is unregistered is promoted to a finding, never a silent pass.

## Significance

This locks the validator's growth law: adding a check adds a data file and adding a logic kind adds a presence-discovered callable, so the core never grows as the rule set grows. Anything later built on validation must respect three fixed seams: (1) the dispatcher stays opinion-free — strength lives in the rule's tier and the suite's context, never in dispatcher code; (2) the core kind set is closed, so new logic arrives as a provided callable or rides custom/script, never as an edit to the core; and (3) every callable, including module-added and script kinds, returns the uniform pass/fail-over-finding result, so the dispatcher can collect and tier results without special-casing any kind. A kind that cannot be found or run is a finding, not a gap — so a governance rule can never be quietly un-enforced.

## Rationale

A validator that fuses its check inventory with its check logic grows without bound — every new check means new validator code, and the file that holds all of it becomes the system's largest and most fragile. Splitting rules-as-data from a small kind-registry holds the core flat: the registry of kinds grows slowly and deliberately while the rule set grows freely as cheap data. Discovery by presence rather than a wiring seam is what keeps installing a new kind mechanical instead of surgical — a provided callable joins because it is there and leaves when its file is removed, with no central list to edit and un-edit. Forcing every callable to return one uniform result is what lets the dispatcher stay genuinely thin: it can collect and report by tier without knowing anything about the kind that produced the finding.

## Anti-choice

A module-added kind could have been treated as a swappable implementation behind a named contract — the same shape as a degradable interface, with a declared fallback surfaced when the implementation is absent. This was weighed and rejected. A check kind has no sensible fallback: when a kind cannot be found or run, the honest outcome is a tracked finding that fails the required gate, the exact opposite of quietly falling back to a default and continuing green. Modelling a kind as a degradable interface would force a contrived fallback onto logic that must instead fail loud, or carve it out from the very contract that defines an interface. A kind binds by presence like an interface, but it is not one — so it is discovered, and its absence is a finding. (The monolithic accreting validator and a free-form predicate mini-DSL were also rejected: the first is the god-file failure this law exists to prevent; the second trades a closed, honest kind set for unbounded complexity and an injection surface.)

## Status

accepted
