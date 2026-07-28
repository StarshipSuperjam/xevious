---
id: eADR-0033
title: Boot is a read-only refresh family that honours deferrals
status: accepted
date: 2026-06-29
---

## Decision

Orientation is a family of read-only cognition-refresh moments, not a single startup ritual: a heavy cold-start pack at session start, a near-zero per-prompt scent every turn, and post-compaction re-orientation riding the next scent. Boot owns only the event model — which moments fire, on which hook, at what cadence and cost tier — and is the integration point that renders, in plain language, the operator-facing readouts its neighbours hand it; it never regenerates derived or committed state, and its sole local write is a gitignored presentation marker recording what was already shown.

## Significance

This locks orientation as plural, read-only, and unconditional-with-a-floor: refresh fires on its own, never as a step the operator must invoke, and never as a regeneration of canonical state. It fixes that boot is a renderer of other systems' contracts, not an originator — it surfaces a refused state cursor, reversible forgetting, an unprotected branch, and degraded substrates in plain words, but the detection and the fix belong to the systems that own them. Later work must respect this seam: a neighbour may refine its own internals and its own gate, but boot fixes only the disclosure, and any new operator-facing alarm must arrive as a deferral boot renders, ranked behind the governance-critical ones, never as logic boot invents.

## Rationale

A cold session must reground itself without depending on the operator to remember a command, and most of what it must say is already owned elsewhere — the cursor store, recall, the branch-protection signal, the substrate health. Making orientation a family lets the heavy cost fall where latency is tolerable (building) and stay near zero where it is not (every prompt), while a single rendering point keeps the operator from meeting four different voices for four different problems. The trade is deliberate: boot accepts being downstream of everything and inventing nothing, so that each upstream system can settle its own contract independently and boot simply honours the handoff rather than racing it.

## Anti-choice

The strongest rejected alternative framed boot as setting a per-event cost ceiling that the prioritiser then allocates within. It lost because the prioritiser already owns the within-event budget split and its flex — a clean session gets more orientation, a high-debt one less — so a boot-owned ceiling would contradict that ownership and split one decision across two systems; the honest line is event-model here, within-event budget there. A second rejected option had a malformed state file hard-halt the session-start moment via an exit code. It lost because that moment has no safe halt: an exit-halt strands a non-engineer with a dead session and no recourse, where the correct posture is fail-loud within fail-open — surface the refusal, emit a finding, and fall through to the committed floor so the session degrades plainly instead of crashing.

## Status

accepted
