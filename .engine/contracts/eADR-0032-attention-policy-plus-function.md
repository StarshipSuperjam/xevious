---
id: eADR-0032
title: Attention is a policy plus a function, not a store
status: accepted
date: 2026-06-29
---

## Decision

The layer that answers "what do I focus on, and at what level?" is built from exactly two pieces and holds no canonical state of its own: a committed, governed policy carrying the budget allocation, the intra-category ranking weights, the trim order, and the debt-blocking rule; and a deterministic ranking function that reads the existing substrates — standing situation and committed debt count, structural adjacency, the debt register, and the native git/GitHub work record — and emits the ordering and the budget split. Its form is an ordered partition with weighted intra-partition ranking: candidates partition into the budget categories (blocking debt, in-flight work, recent decisions, structural neighbors, orientation) under hard cross-category precedence, while the weights only order candidates within a category. Reference time is a single explicit as-of timestamp passed in, so "same inputs" yields "same ordering"; there is no scored-token file, no decay state, and no machine learning.

## Significance

This establishes that prioritization is reviewable as governed data and reproducible as code, never an opaque store of mutable focus weights. The property a non-engineer leans on — blocking debt surfaces ahead of features — is guaranteed by the partition structure, not by a number someone must calibrate correctly; later work must keep that guarantee structural and must never reintroduce it as a tunable weight. It also fixes the ownership lines later systems must respect: this layer reads every substrate and owns none. Whether a candidate is tracked debt is the telemetry register's promotion decision; which open debt is blocking is this layer's own debt-blocking rule; the partition only orders what those determinations hand it. The orientation events that consume the ranking — which hook fires each, its budget, the degraded disclosure — belong to the orientation owner, not here. Only the concrete values (splits, weights, precedence order, trim order, the debt-blocking threshold) remain to be calibrated and fixture-tested; until that fixture exists, the claim that the right things surface first is unproven, and the fixture must test partition assignment, not only the weights.

## Rationale

Prioritization had to be explicit and reviewable instead of magic constants buried in boot code, but it also had to avoid becoming a parallel substrate that duplicates state the rest of the engine already owns. Splitting it into a governed policy plus a stateless function gives the operator a tunable contract they can read and the engine a reproducible computation, while the read-never-own posture means no neighbor's later change can be forced by this layer and this layer keeps no truth to drift. The ordered-partition form was the only shape under which the two things the design demanded at once — hard "unblocked first, blocking debt ahead of features" precedence and genuine ranking weights — both hold: precedence lives in the partition, weights live inside it. Determinism with an explicit recorded as-of timestamp keeps recency-dependent ordering reproducible across clock skew or a host change without the function owning any state. When an input is unreachable the function ranks over what remains and hands the degraded set to the orientation owner to disclose loudly, so a partial picture is never presented as confident.

## Anti-choice

Two alternatives were weighed and rejected. The first was making focus first-class as an actual store — a focus-token / working-set / decay structure holding hand-authored mutable scored state. Rejected: it re-implements claim, scope, and budget enforcement that the stance-gating and block-budget layers already own, and hand-authored mutable scored state is exactly the derive-don't-hand-author violation this layer exists to retire; "first-class" was honored instead by making the governing policy a first-class governed surface, not by adding a substrate. The second concerned the ranking form: a weighted-sum combiner, and a pure-lexicographic-tier combiner. Weighted sum was rejected because one miscalibrated weight could float a feature above blocking debt, and the safety property a non-engineer relies on must be structural, not weight-dependent. Pure lexicographic tiers were rejected because they make the ranking weights near-vestigial and the ordering rigid; the ordered partition uses both — precedence between categories, weights within — which neither single form delivers.

## Status

accepted
