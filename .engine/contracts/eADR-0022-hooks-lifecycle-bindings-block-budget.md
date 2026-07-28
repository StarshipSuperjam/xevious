---
id: eADR-0022
title: Hooks are the in-session enforcement substrate; only two events may hard-block
status: accepted
date: 2026-06-29
---

## Decision

The engine binds a chosen subset of the platform's session-lifecycle events — start, the pre-action gate, the post-action point, the pre-compaction point, the turn-stop point, and session-end — and treats this binding layer as load-bearing law rather than the property of any one behavior. The events are the slots; the behaviors that ride them (boot injection, the close ritual, experiential capture, local nudges) belong to their owning systems. Of all these events, only the pre-action gate and the turn-stop point may hard-block a session; every other event may only nudge or inject. The set of invariants permitted to hard-block starts empty and is filled additively, one registered invariant at a time, by the system that owns each one.

## Significance

This fixes a single home for the cross-cutting lifecycle laws so they cannot scatter into the behaviors that consume them: which events exist, which may block, and how a gate fails are settled here, once. Every later system that wants to act in-session must attach to one of these named events and respect the block budget — a behavior that wants to refuse an action can only do so at the pre-action gate or the turn-stop point, and only by registering its invariant explicitly. The event set and the block-eligible set are both additive: later work may bind a new event or register a new blocking invariant by naming the need, but may never widen which event *kinds* are allowed to block, nor make blocking the default reflex. Mode-awareness is permanent: any blocking behavior must stay satisfiable without a human present, or an unattended run deadlocks.

## Rationale

This layer is presupposed by boot, close, capture, validation, and telemetry — all of them ride session events — so it cannot be added after those systems exist; it has to be standing grammar from the start. The block budget reflects a hard trade-off: a local refusal buys friction without proportional trust, because the operator cannot weigh a gate they never see, and the only truly unbypassable gate is human review at the merge. So local gates are deliberately weak — strong enough to make evasion take real effort, never strong enough to strand a non-engineer who cannot debug them. Confining blocking to two events and starting the invariant set empty keeps that friction minimal and forces every future block to be justified by its owner rather than assumed. The block-eligible set stays governance-critical only, so a casual "this should never happen" check cannot quietly become a wall.

## Anti-choice

The strongest rejected alternative was to scatter these laws into the systems that use them — let boot, close, and capture each define their own event handling and blocking rules where they live, with no central budget. It lost because the cross-cutting laws would then have no home: there would be no single place that says which events may block and how a crashing gate must behave, so the block budget would erode one local exception at a time and failure handling would drift per behavior. A second rejected path was to treat this as an ordinary optional capability rather than standing grammar; it lost because five systems depend on it and it cannot be retrofitted. A third was to pre-enumerate the blocking invariants now; it lost because naming the invariants up front front-runs decisions their owning systems have not yet made and presumes a fixed set where the honest answer is an additive, empty-by-default one.

## Status

accepted
