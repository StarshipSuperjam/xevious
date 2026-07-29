---
id: eADR-0034
title: The dual-runtime architecture — one canonical core, native adapters, parity or a declared exception, and honest degradation
status: accepted
date: 2026-07-17
---

## Decision

The engine has exactly one canonical policy home — `.engine/` — and reaches each AI runtime through a native
adapter that only translates: today the Claude Code adapter (`CLAUDE.md`, `.claude/`, `.mcp.json`) and the
Codex adapter (`AGENTS.md`, `.agents/`, `.codex/`), whose three namespaces the engine reserves exactly as it
reserves the Claude ones. Three invariants bind every adapter, now and for any runtime added later:

- **Single-sourced translation.** An adapter never carries its own copy of a policy, a workflow, or a gate.
  Where a runtime needs material inline (a reviewer persona, a typed command), the adapter file is a committed
  render generated from the canonical `.claude/` source and held in sync by check — the catalog records it as a
  render, not a second original. Runtime differences are absorbed at two seams only: a provider-normalization
  layer at the hook boundary, and provider-specific wiring seam types that write each runtime's own registration
  files. Adding a runtime is additive — new adapter files and new seam types — and nothing multiplexes an
  existing seam across two targets or edits another adapter to make room; the existing adapter stays
  byte-identical where no change is intended, pinned by regression test.
- **Parity, or a declared exception.** Every capability the engine exposes to one runtime has a counterpart on
  the other, and a standing check enforces it in both directions. The only sanctioned differences live in one
  committed exception ledger, and an entry declares not just absence but reduced strength — a present-but-weaker
  counterpart (an instruction where the other runtime has mechanical enforcement, a protection the platform can
  switch off) is a ledger entry with a reason, never a footnote in prose. The ledger sits inside the
  guardrail-weakening floor, so widening an exception is held for the operator's deliberate acknowledgment like
  any other loosening. "Parity" therefore means presence *and* declared strength, never a silent claim of
  equivalence.
- **Honest degradation.** Where a runtime withholds a guarantee the engine cannot verify from inside a
  repository, the engine degrades loudly instead of pretending: it detects its own silent-off state and
  discloses it in plain words, no green signal ever conflates "did not run" with "passed", and a
  partially-recognized input is refused rather than half-captured. Enforcement the platform cannot mirror is a
  ledger exception, never a quiet claim of coverage, and the acceptance step for such behavior is a live
  post-merge validation against a written pass bar — a failure inside that bar is a defect owed a fix, never a
  scope removal.

## Significance

This locks the extension grammar for every future runtime: one core, thin adapters, and capability routed
through the core, so a feature built once appears in every adapter or turns the merge gate red — in both
directions, forever — until it is paired or ledgered; removing a capability from the runtime that has it, to
dodge a ledger entry, is the failure mode the bidirectional check exists to forbid, caught by the reverse
direction rather than rewarded. It fixes what "parity" the operator is told (presence and
declared strength) and what a green check may claim (only what a check or a demonstration actually proved;
everything else named as unproven). And it makes the safe posture loud: a gate that silently stopped running, or
a memory store silently half-written, is worse than none, so later work inherits the duty to make its own
silent-off state visible and to keep provider-specific vocabulary inside the two named seams. A non-engineer
governs this engine through one merge gate; two diverging policy copies, an unflagged weaker counterpart, or a
green that hid a dead gate would each hand them something false to trust.

## Rationale

A non-engineer operator approves on a mechanical signal, not code review, so single-sourcing, parity, and
degradation each have to be a check rather than an intention. One core with generated renders is the cheapest
honest way to keep required-inline material single-sourced, and letting a check fail when a render drifts is
cheaper than trusting a hand-copy. Normalizing runtime payloads at the hook boundary keeps every downstream gate
and tool provider-blind, which is what lets the existing enforcement protect a second runtime without being
rewritten. A guarded exception ledger gives the parity check an honest escape valve for real platform
differences without letting the valve itself become the quiet way around the check. And loud degradation costs
only an occasional notice the operator must act on, weighed against the cost of a single silently-corrupted
memory store or a session that edited files with the write-gate off and no one told.

## Anti-choice

The strongest rejected alternative added a `provider` field to the existing wiring seam types instead of new
seam types. It lost because each existing seam resolves to exactly one target file and is verified by mirroring
that file's full content; one directive writing two different files in two formats would break that mirror,
complicate orphan detection, and — decisively — force edits through the Claude appliers, putting the
byte-stability promise at risk for no gain over two small additive seams. Two narrower alternatives were weighed
and rejected as well, recorded here rather than spun into separate contracts: a presence-only parity check with
platform differences documented in the support matrix (rejected because prose does not travel to the merge gate —
a reader of a green parity check would conclude the runtimes are equivalent while a materially weaker counterpart
shipped unflagged, and "documented difference" would drift into "unreviewed gap"); and a tolerant
transcript parser that tries any input it is handed (rejected because tolerance is the wrong virtue against a
format the platform reserves the right to change — a partially-recognized transcript writes fragments into
long-term memory that look like captured conversation, and a corrupted store is strictly worse than an honestly
empty one).

## Status

accepted
