---
id: eADR-0001
title: The Engine is a versioned template
status: accepted
date: 2026-06-29
---

## Decision

The Engine ships as a GitHub repository template — a tracked file tree copied into a fresh project as a single commit — and it stays upgradeable for the life of that project. Anything that can be a committed file is one, so the largest possible share of the Engine travels, diffs, and is reviewable. Because the copy is a detached one-time snapshot with no live upstream, the whole Engine is modeled as versioned packages; a committed manifest records the installed version of each. On operator request, an upgrade fetches a tagged release, overlays only Engine-namespaced paths (Engine code is replaced wholesale; operator config and ignored data are preserved), runs migrations in dependency order, validates coherence, and lands a reviewed pull request. If the release source is unreachable, the project stays on its current version rather than breaking.

## Significance

This fixes the Engine's two most basic facts: how it arrives and how it changes after arrival. Every later capability must assume it lives in tracked files that travel by template copy, and that improvements reach an already-generated project only by overlaying a tagged release — never by a live pull, because the copy has no upstream. Two walls follow that nothing downstream may breach: an upgrade replaces Engine code wholesale but must never overwrite operator config or ignored data, and an upgrade is never applied in place — it lands through the reviewed pull-request gate (eADR-0005) so it stays reversible and accountable. The committed manifest is the authority on what version is installed; provisioning and the control plane build directly on it. Settings that cannot be files (such as branch protection) are the rare exception that must be bootstrapped, not assumed to travel.

## Rationale

The template copy mechanism duplicates files but not repository settings, so the more of the Engine that lives in tracked files, the more of it survives the copy intact and can be inspected. That same mechanism produces a detached project with no remote pointing back, which means a later fix has no path home unless arrival and upgrade are designed together from the start. Modeling the Engine as versioned packages with declared migrations is the smallest move that gives even the always-present core a route into the field, since the package grammar already carried version and migration. Overlaying only Engine paths keeps the Engine/product separation honest; routing the result through review keeps a non-engineer from being handed an unreviewed mutation of governance code; degrading on an unreachable source keeps that same operator from being stranded on a failed update.

## Anti-choice

The weighed alternative was to treat distribution as a clone-and-configure step and leave the core un-upgradeable — accept that a generated project is frozen at its birth version. It was rejected on both halves. Clone misframes what actually ships: it underweights how much of the Engine can travel as reviewable files and overweights manual setup. Freezing the core is worse: the most-wanted fixes would never reach the field, and the one obvious repair — re-attaching the template as a live remote and merging — produces exactly the merge conflicts on customized Engine files that a non-engineer cannot resolve. Publishing through an external package registry was also set aside as a heavier dependency than the GitHub releases already on hand, and a separate core-overlay mechanism was rejected for splitting into two what one versioned-package model serves for both core and features.

## Status

accepted
