---
id: eADR-0025
title: The draft PR is the claim
status: accepted
date: 2026-06-29
---

## Decision

Build work is carried by two native git/GitHub records and no third. The draft pull request is the claim and the change surface: it holds what has been built — the opening claim, the integrated commits, the human merge gate, and the contract narrative. The build Issue is the forward-plan surface: an ordered commit-sequence checklist authored at planning, holding what is not yet built, so progress reads as "N of M done" and the next chunk is the next unchecked item. There is no separate claim artifact, no reserved slot number, and no close ritual; a build is done when its PR is submitted, and the only unbypassable wall is the operator's merge of the protected branch.

## Significance

This fixes that all durable build state lives in records GitHub already keeps — the PR for the change, the build Issue for the plan — never an engine-private ledger. Because the forward plan is GitHub-derived, an unattended session can resume a build whose authoring session is gone; and because it is GitHub-derived, that resume is bounded by GitHub availability — offline means no plan to read and the session safely does not proceed. Later work must respect that the PR is not the only durable state (the plan is in the Issue), that the build Issue is a plan's decomposition rather than a new backlog or committed work-inventory, and that nothing manufactures a close ceremony around the merge. Anything that reviews a build attaches its judgment to the PR contract (eADR-0021), never to a new artifact; the merge is the sole wall and every nudge before it is honestly a nudge.

## Rationale

A close mechanism that invents its own claim object — a reserved subject, a slot to allocate, a close-shape to police — spends real effort guarding a ritual instead of shipping the change, and that friction compounds into a spiral where closing work costs more than doing it. Native records already encode every state a build passes through: open, committed, submitted, merged. Splitting the two questions a build answers — "what has been built" and "what is not yet built" — onto the two records that already answer them keeps each surface single-purpose, lets a cold session reconstruct exactly where a build stands from git alone, and makes the operator's merge the one decision that matters. The cost paid is honest degradation: when GitHub is unreachable the plan is unreadable, so a session fails safe rather than guessing.

## Anti-choice

The rejected alternative was a dedicated claim artifact — a reserved-subject commit or allocated slot that announces and tracks the build as its own object, with a structured close ritual to retire it. It lost because that machinery is precisely the friction it claims to manage: every reserved subject needs an allocator, every close-shape needs an allowlist to police, and the apparatus grows faster than the work it wraps, turning closing a change into its own project. The native records carry the same states with none of the bookkeeping, so the dedicated artifact buys nothing the PR and build Issue do not already give and charges a recurring tax for it.

## Status

accepted
