---
id: eADR-0017
title: "Engine identifiers are engine-namespaced; decision records use eADR-#### (a deployment's own instance records, <project-slug>-eADR-####)"
status: accepted
date: 2026-06-29
---

## Decision

Every engine surface instance that carries a human-facing identifier — one used in references, commit messages, or knowledge-graph entities, not merely a file path — is engine-namespaced: prefixed to mark it as the engine's, so an engine identifier can never collide with a product's own. Decision records use the scheme `eADR-####`. The same wall applies once more within the engine itself: the engine's own founding canon keeps `eADR-####`, while a deployment's own per-instance engine decisions — authored under `.engine/contracts/instance/` and preserved across engine updates — carry a per-project namespace on the same `eADR` root, `<project-slug>-eADR-####`, so a deployment's own record can never collide with the engine's canon as that canon grows. This identifier prefix is a human-facing wall only: which population a record belongs to is decided by its folder / engine-owned-set membership, never by the prefix, so a prefix that ever disagreed with its folder defers to the folder. Each new surface that needs an identifier chooses its own engine-prefixed scheme when it joins the catalog.

## Significance

This extends the engine/product separation from paths to identifiers: confining engine files to their own corner of the repo is not enough, because a bare identifier travels into commit messages, cross-references, and knowledge-graph entities where the product's namespace lives too. The law fixes that the separation is a property of identifiers, not only of file locations, and it states this once at the grammar level so every future surface inherits namespacing instead of rediscovering it. Any later surface that mints a human-facing identifier must adopt an engine-prefixed scheme; the decision-record surface (the engine's why) is bound specifically to `eADR-####` for the engine's own canon — a deployment's own per-instance records extend this to `<project-slug>-eADR-####` on the same root, applying the identical law one level in (engine-vs-engine) — and a builder may rely on the canon token being collision-free against a product that runs its own ADR system.

## Rationale

A product built through the engine commonly runs its own decision-record system, so a bare `ADR-####` would clash the moment two records meet in a commit message or a citation. Path confinement keeps engine files out of product space but does nothing for a bare token, which has no path — so the wall has to reach identifiers explicitly or it leaks exactly where it is hardest to see. Stating the rule as a grammar law rather than a per-record convention costs a small amount of up-front generality but buys inheritance: no future surface can quietly reintroduce a colliding identifier, because the law already governs it.

## Anti-choice

The strongest rejected alternative was to keep the namespacing as a convention local to the decision-record template — leaving the grammar silent and prefixing only where it was obviously needed. It was rejected because a convention that lives in one surface does not bind the next surface that invents an identifier; each new one would re-decide, and the wall would erode one surface at a time. A bare `ADR-####` token was also weighed and rejected outright as the direct collision the law exists to prevent, and a wholly distinct token (dropping the recognizable ADR pattern) was rejected because the familiar form with an engine prefix reads most clearly to a human while still guaranteeing separation.

## Status

accepted
