---
id: eADR-0019
title: Memory and knowledge are distinct substrates
status: accepted
date: 2026-06-29
---

## Decision

Narrative memory and structural knowledge are kept as two separate substrates with separate scopes, separate stores, and separate query paths. Memory answers "how did I get here?" — a transcript-first archive of what was actually said and decided, accumulated per project, plus a small set of explicit operator pins, recalled by a model-orchestrated read over a lexical index that a per-prompt cue provokes. Knowledge answers "how does this world work?" — the structural map of what surfaces exist and how they relate, derived from the repository's own committed truth. Neither writes into the other's store; they meet only through a read-time join on shared entity tags, never through persisted cross-edges.

## Significance

This locks the cognitive substrate into two systems that may not be merged or made to depend on each other's storage. Every later system that records experience routes to memory; every later system that needs the structure of the project routes to knowledge; nothing may collapse the two or route one through the other. The two retrieval shapes are fixed by this split: memory is recalled by the session model working over a lexical index of the transcript archive (and, only if earned, a similarity index behind the same seam), while knowledge is queried as a graph over derived structure. Any link between them must be computed at read time from shared tags — no design may introduce a persisted memory-to-knowledge edge maintained at write time. Forgetting, capture, and ranking belong to memory alone and must never reach into the structural map.

## Rationale

The two layers answer different questions on different axes — episodic versus structural — and are read in genuinely different ways: memory wants fuzzy recall of what was said and decided, knowledge wants exact traversal of how things connect. A store and an index tuned for one are wrong for the other, and a single substrate forced to serve both retrieval needs muddies each: structural queries drown in narrative noise, and narrative recall is flattened into rigid entities it was never shaped to hold. They also have different sources of truth — knowledge is derived from the repository and is regenerable from it, while memory is its own append-only record of a history nothing else holds — so giving them one lifecycle would force the regenerable and the irreplaceable to share fate. Keeping them apart lets each be tuned, scoped, and forgotten on its own terms.

## Anti-choice

The strongest rejected alternative was a single unified substrate — one store and one query path holding both the project's history and its structure, with a persisted bidirectional link maintained as records are written, so a query could walk from a decision straight into the surface it touched. It was rejected because it conflates two distinct retrieval needs and degrades both: the write-time cross-link is fragile machinery that goes stale the moment either side moves, and an episodic record forced into the same shape as structural entities loses the loose narrative form that makes recall useful. Earlier attempts at exactly this bridge left it unwired and unhelpful; the read-time join on shared tags delivered the same connection without the maintenance burden or the conflation.

## Status

accepted
