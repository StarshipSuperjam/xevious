---
id: eADR-0004
title: Degrade to git-native
status: accepted
date: 2026-06-29
---

## Decision

Every capability that leans on a service living outside the repository must carry a committed, in-repo fallback. When that service is unreachable, starting a session and orienting yourself still succeed from tracked files alone — and the engine states plainly which capability is degraded rather than failing hard or silently filling the gap. Tracked files are the floor; out-of-repo substrates are depth layered on top, never the floor itself.

## Significance

This locks the floor of every session beneath the reach of any outside service. Boot, orientation, knowledge, memory, and any service-mediated lookup may run richer when their substrate is live, but none may make a live substrate a precondition for the engine coming up usable. Later work must keep the committed fallback genuinely sufficient on its own, must surface degradation loudly and in plain language (naming what is reduced and that full capability is usually one restart away), and must never substitute a quietly reduced answer for a full one. A figure or claim read from a degraded source is rendered so its provenance is unmistakable. Whatever the operator depends on to get oriented and act lives in tracked files, so an outage strands no one.

## Rationale

The operator works through the engine and cannot reach inside it to diagnose a failed dependency; an engine that cannot start, or that starts and quietly misleads, when a service blips is one the operator cannot trust to recover unattended. Routing the floor through committed files instead — the same files version control already carries — costs almost nothing, because git is present wherever the work is. The trade is a small ceiling on how much the cheap floor can show against a hard guarantee that the floor always exists; the richer substrate-backed layer is kept as additive depth, so nothing is lost when it is up and nothing is required when it is down.

## Anti-choice

The strongest rejected alternative was to depend directly on the out-of-repo substrates and treat their outages as ordinary errors to surface and retry — no committed fallback, one source of truth, no duplicated floor to keep honest. It lost because it makes a transient outage a wall for an operator who has no way around it, and because "surface the error" degrades in practice into either a hard stop or a silent, reduced answer — exactly the two failure modes a non-engineer cannot detect or repair. A guaranteed cheap floor that is always present beats a richer single source that is sometimes absent.

## Status

accepted
