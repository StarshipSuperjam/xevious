---
id: eADR-0030
title: Conduct — the operator-stance floor
status: accepted
date: 2026-06-29
---

## Decision

The operator's standing behavioral stance — how the AI engages: dialog, when to push back, provenance before decisions, plain language, not fragmenting trivial work — is carried by a dedicated tier-3 prose surface, the *code of conduct*, present in every repo from the first cold session. It rides the always-present core, loads at the grounding floor by `@import` into the root `CLAUDE.md` (not via the boot hook), composes two committed layers — universal defaults shipped as machinery, an operator override that is the deployment's own config — by whole-rule supersession keyed on a stable `id`, and is seeded from the maintainer's template so the operator's stance travels every repo and is preserved thereafter. It shapes behavior and never enforces: a pure-posture whisper, structurally unable to weaken any guardrail.

## Significance

This locks in a named home for behavioral stance that is distinct from every other carrier — separate from per-project narrative (which accumulates through use and ships empty), from structural fact, from design rationale, and from any enforcement gate. Later work must respect: the stance is present at cold boot even when the boot hook fails, because it is floor-loaded, not hook-injected; it is tier-3 posture and may never bind to a hard check or gate (that is the standing-rule surface's job, one tier up); the operator override is config preserved across an upgrade overlay while defaults are overlaid wholesale; composition is per-rule by `id`, never a prose merge; and the set stays bounded because it is paid on every session. No future surface may route trust through this stance or let it disable a mechanical guardrail — those hold regardless of any prose the model reads.

## Rationale

A non-engineer's Engine must carry *their* operating rules from the first boot of every repo without re-teaching, and no existing home fit: per-project memory ships empty and is the wrong place for the cold-boot floor; the standing-rule override is numeric-only; the root floor file is thin template machinery. So a dedicated surface was needed. The design assembles only already-proven patterns — non-empty config that ships, operator-config-preserved-across-overlay, an engine-mediated authoring verb, floor `@import` — rather than new machinery, keeping the addition minimal enough to ride core. The honest tiers are named, never inflated: it is pure posture; the floor load is platform-reliable but not airtight; the safety rests on mechanical guardrails that do not depend on model compliance plus the fact that every change is committed and merge-visible, with a content guard as defense-in-depth only.

## Anti-choice

The strongest rejected alternative was inlining the stance directly into the root `CLAUDE.md` floor. It lost because it conflates operator-tunable config with thin template machinery and inflates the every-session floor that every repo pays; a separate `@import`ed file keeps the floor thin and the layer operator-owned and preserved across upgrades. The other weighed options each failed a specific test: a bare textual pointer in the floor file does not load the stance — only `@import` inlines it; a numeric per-key merge cannot compose prose, which supersedes whole-rule by `id`; an optional module could be deselected, defeating the must-be-everywhere goal, so it rides core instead; and injecting the stance through the boot pack vanishes on a hook failure, where the floor `@import` survives.

## Status

accepted
