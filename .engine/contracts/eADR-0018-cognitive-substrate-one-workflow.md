---
id: eADR-0018
title: The cognitive substrate is one workflow
status: accepted
date: 2026-06-29
---

## Decision

State, memory, knowledge, attention, and boot are one cognitive workflow, not five independent parts. The decomposition is by what holds canonical state versus what is derived: two stores (knowledge, which is derived from surfaces and committed; memory, which is experiential and gitignored), one register (the integration-debt register, owned by telemetry), one cursor (state — tiny pointers, including a debt count), and two functions that hold no store of their own (attention, a prioritization policy plus a deterministic ranking function; and boot/orientation, which assembles and injects). This substrate is consulted by reflex, not on demand: orientation is delivered as a push event family — a cold-start boot pack, a per-prompt scent, post-compaction re-orientation, and close — each read-only, and each member that ranks what to surface powered by attention rather than by a precedence of its own; a member that surfaces nothing to be ranked consults it not at all. The per-prompt scent's job is to make consultation a reflex; it may do so by pushing a cue that provokes a model-driven read of the substrate rather than by injecting recalled content directly, so the reflex to consult is installed by push while the content it surfaces may be pulled in response — but consultation may never regress to pull-only, the wait-to-be-asked posture this law exists to remove. Nothing in this substrate is allowed to become a sixth box that holds its own mutable scored state.

## Significance

This locks the shape of cognition before any of its parts are built: exactly two things hold canonical state, one cursor points at them, one register tracks debt elsewhere, and two functions derive over the rest. Any later cognitive work must place a new capability into one of those existing roles — it may not stand up a new mutable store, and it may not let attention or boot accumulate state of their own. The per-part designs (how state is shaped, how attention ranks, how boot assembles) are settled by their own laws and must each honor this integration: a clean seam between the part that holds truth and the part that derives over it. The push-driven consultation reflex is also fixed here — consultation is the default reflex driven by the orientation events, and any new way of getting the substrate in front of the model rides that event family rather than reverting to wait-to-be-asked retrieval. What is load-bearing is the reflex, not its payload: the per-prompt scent may carry a cue that provokes a model-driven read rather than injected content, but it may not thin to no per-prompt event at all, which would be pull-only by another name.

## Rationale

The hard problem in cognition is never the individual part — it is the seam between parts. The prior attempt had decent boxes but failed at every join: memory and knowledge were unwired, attention was buried in scattered constants rather than a named function, knowledge and debt and health signal were over-mixed into one muddle, and nothing consulted the substrate mid-session at all. Decomposing by canonical-versus-derived gives each part exactly one job and one clean contract with its neighbors, which is what kills those seam failures. Modeling attention and boot as pure functions over the existing stores — rather than as new stores — is the smallest design that removes the buried-constants problem without inventing more mutable state that would rot. And making delivery a push is the only mechanical lever that turns a stateless model's instinct to grep the files into a reflex to consult the substrate: a tool that must be explicitly invoked is a tool that gets skipped.

## Anti-choice

The strongest alternative was to treat the five parts as independent systems and design each in isolation, then wire them together afterward. It was rejected because the deliverable is the integrated cognition, not five clean boxes — and isolated design is precisely what reproduces the seam failures, since each part optimizes its own contract while the joins between them go unowned. A weaker variant proposed a dedicated attention store: a hand-authored, mutable, scored working-set with decay and learned ranking. That was rejected because hand-authored scored state rots silently and would duplicate machinery that the claim, scope, and mode systems already own; attention earns its place as a policy plus a deterministic function, holding no store. Leaving consultation pull-only — the substrate helps only when explicitly told to look — was also rejected: that pull-only posture is exactly the defect this law exists to remove.

## Status

accepted
