---
id: eADR-0014
title: One history; living documents everywhere else
status: accepted
date: 2026-06-29
---

## Decision

Change history lives in exactly one place: the structured pull-request body, which the pull request carries as the durable record of what happened and why. Two surfaces are governed by exception. A decision record is append-only — a standing decision is never edited; it is superseded by a newer record, and a rejected option survives as a named anti-choice inside the prevailing record rather than as its own state. One carve-out applies: the engine's own founding records (the `eADR-####` canon shipped in `.engine/contracts/`, which an engine update replaces wholesale) are a living cold-copy snapshot — revised in place to current truth and carrying no supersession chain — because a deployed copy carries no prior history, and the pull-request body that revises them already holds the one history of why. A deployment's own decision records (under `.engine/contracts/instance/`, preserved across every update) stay append-only. Every other document — specifications, guidance, catalogs, narrative — is rewritten in place to its current truth and carries no inline change history: no "previously," no version banners, no diff-against-the-past.

## Significance

This locks in that any document other than a decision record reads as authored-complete-today, and that the single trustworthy account of how state reached its current shape is the pull-request body — below the decision-record threshold, narrative is recorded there or is simply done. Later work must respect three things: no second history store may be invented (a bespoke log, a changelog file, a per-doc revision section) — narrative routes to the PR body or to a decision record, never to a third place; a deployment's own decision records may only grow and be superseded, never edited in place, while the engine's founding canon is instead revised in place and replaced wholesale by an engine release; and the decision-record threshold (eADR-0029) governs what is heavy enough to earn its own record versus what stays below the bar. For a deployment's own records, that append-only rule is what lets a record truthfully cite a document a later rewrite has since changed; the founding canon keeps its citations truthful the other way — it is itself rewritten to current truth, and each revision's pull-request body preserves the history the cold-copy snapshot no longer carries. The control-plane enforcement layer (eADR-0021) is what gates that pull-request body's completeness at the merge: this record fixes the one-history principle, that record fixes the gate over it — one record and its enforcement, not two owners of the same history.

## Rationale

An operator who does not read code weighs change on its written record, so the record must be singular and trustworthy. Two failure modes are in tension. If history is scattered — inline "used to" notes, parallel changelogs, per-document revision logs — no document is reliably current and the operator must reconcile contradictory accounts to know what is true now. If history is erased everywhere, the chain of why is lost. The resolution splits the two needs onto two substrates: living documents stay current by being rewritten in place and holding zero history, while the why is preserved in exactly one durable channel. A deployment's own decision records are append-only because a decision is a commitment others built on; editing one would silently rewrite the past those commitments were made against, whereas superseding it keeps the chain auditable. The engine's founding canon is the exception: it ships as a cold copy with no prior history to carry, an accreting supersession chain would be construction residue that must not travel into every deployed repo, and the one history the operator weighs — the pull-request body of the change — already records why each revision was made. So the canon is kept current in place, and only a genuinely new kind of decision earns a new founding record. The pull-request body is the one history because the platform already carries it as the durable artifact of every change — reusing it avoids standing up a store that would itself need maintaining and could drift from reality.

## Anti-choice

The strongest rejected alternative was to keep a dedicated narrative store — a changelog surface, separate from the pull request — as the home for below-threshold session history. It lost because it duplicates what the pull request already carries: every change arrives as a pull request whose body is the natural, durable, structured record, so a parallel store is a second history that must be kept in sync and inevitably diverges from the one the platform already maintains. It also reintroduces exactly the scatter this law exists to prevent — two places an operator must consult to reconstruct what happened. Folding narrative into the pull-request body keeps history singular and removes a surface that earned its keep only by restating what the pull request already says.

## Status

accepted
