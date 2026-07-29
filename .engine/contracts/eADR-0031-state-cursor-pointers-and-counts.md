---
id: eADR-0031
title: The state cursor holds pointers and counts only
status: accepted
date: 2026-06-29
---

## Decision

The "where am I?" cursor every session reads first holds pointers and counts only — a single small, committed, schema-checked machine-state file. It carries the project's standing-situation pointers (active phase and milestone) and a debt count plus an as-of marker and a pointer into the integration-debt register. It holds nothing that grows: no work inventory, no narrative log, no persisted session stance, no depth. Every growing thing stays with its owner, and the cursor stays cheap because it is the first thing read on every cold start.

## Significance

This locks the cursor as a thin pointer, not a store, and fixes the boundary against everything that would fatten it. Later work must respect that the canonical record of in-flight and planned work is native git/GitHub — open branches and pull requests are in-flight, open Issues are deferrals and backlog, Milestones are the plan — ordered at read time by the attention ranker (eADR-0032), never copied into a committed list here. The debt register itself lives in telemetry; the cursor's count is a derived convenience for the offline read, never the authority. Narrative belongs to pull request bodies plus memory; stance is decided fresh each session and never persisted. Any system that needs "what's next," "what just happened," or "how bad is the debt" must assemble it from those owners through this cursor, not grow the cursor to hold it. The exact field set and schema are a build-spec detail this law deliberately leaves open.

## Rationale

A cold-booting session has no idea where the project stands, and because this file is committed it is also the floor when every out-of-repo service is unreachable. Both pulls — orient instantly, and survive offline — are served by keeping it minimal: the first read stays cheap, and the cheap cursor is exactly what a degraded boot can still read from git alone. Keeping it minimal also keeps it honest. Every value the cursor might duplicate already has a live, authoritative home in native git/GitHub or in a sibling system. A committed copy of any of those would drift from its source the moment the source moved, and a stale duplicate read as truth is worse than no copy at all. So the cursor holds only what must be cheap-and-offline (the standing pointers and the last-known count, each rendered with provenance so it is never mistaken for current) and points at the rest.

## Anti-choice

The strongest rejected alternative was to let the cursor commit a candidate work list — a persisted "what to do next" — alongside a bounded narrative companion and a saved session stance, so a booting session could orient from one self-contained file without reaching out to git, GitHub, or any sibling. It was rejected because each of those three is a second copy of truth that already lives canonically elsewhere: "what's next" is the live branch/PR/Issue/Milestone record, narrative is the draft and merged pull request bodies plus memory, and stance is a per-session decision. A committed work list would drift from the native record and quietly become a competing source of truth; a committed prose log would duplicate the pull requests and rot; and a persisted stance could resurrect a crashed write-enabled session unattended. The thin-cursor cost — a degraded boot must name its offline bound instead of pretending completeness — is the honest trade and is paid by siblings, not by fattening the cursor.

## Status

accepted
