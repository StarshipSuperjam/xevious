---
title: Spec structure integrity
status: accepted
date: 2026-07-18
---

## Rule

An instruction to loosen how tightly a description's *prose* pins the details — keeping what is written at the
level of durable rules and leaving the specifics to the sessions that build it — is **never** license to
delete or collapse the description's *structure*. A build or design session may loosen the wording; it may not
dismantle the structural apparatus the product-design module produces — the description corpus and the fuller
documents the module's scaffold defines (`.engine/modules/product-design/scaffold/`), which is the one place
that set is enumerated. Loosening the prose and removing the structure are different acts, and only the first
is ever what "keep it at the level of laws" authorizes.

## Scope

Applies whenever a session is authoring or revising a project's product description through the product-design
module — the concrete set of documents it protects is the one the module's scaffold defines
(`.engine/modules/product-design/scaffold/`), so this rule tracks that set rather than re-listing it. It
governs the *structure*, not the prose: how sparse or how detailed the writing inside each document is remains
a real, allowed choice (and the recorded light-vs-full depth choice is how an operator legitimately keeps a
description lighter). What is out of scope is any project that does not use the module — there is no spec
structure to protect there.

## Rationale

The module exists so a non-engineer can trust the engine to produce a genuine, structured design. That trust
collapses if a session can quietly flatten the structured documents into free prose while calling it
"keeping the description simple." The two were once conflated — "loosen the prose" was read as "remove the
structure" — and the structured description the module ships was bypassed on the first step of a new project.
This rule draws the line explicitly so the confusion cannot recur: sparse wording is fine and often right;
dismantling the structure is not, and no phrasing of a "keep it light" instruction changes that.

## Enforcement-tier

- **Posture** — the distinction itself is an expectation every authoring session is trusted to hold: loosen
  the prose, never the structure.
- **Soft/hard check (partial)** — the product-design form check (`engine/check/product-design-form`) gives
  the structural default teeth *within* an authored description: when the recorded depth is the full write-up,
  a missing backbone document is a hard finding; a description with no recorded depth is nudged, not blocked.
  It does **not** catch a session that authors a product description entirely outside the module (no structure
  to inspect) — that path is held by the intake being the default route and by the review below.
- **The durable backstop is the human review at the protected-branch merge** — a structural collapse that
  slips every step above is still caught there. No local check is the guarantee.
