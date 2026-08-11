---
id: eADR-0033
title: Boot is a read-only refresh family that honours deferrals
status: accepted
date: 2026-06-29
---

## Decision

Orientation is a family of read-only cognition-refresh moments, not a single startup ritual: a heavy cold-start pack at session start, a near-zero per-prompt scent every turn, and post-compaction re-orientation riding the next scent. Boot owns only the event model — which moments fire, on which hook, at what cadence and cost tier — and is the integration point that renders, in plain language, the operator-facing readouts its neighbours hand it; it never regenerates derived or committed state, and its sole local write is a gitignored presentation marker recording what was already shown.

Boot also owns the **mechanical fit** of the assembled cold-start pack to the platform's per-value output size limit — distinct from the cognitive priority above. Because the platform silently replaces an over-limit value with a truncated preview, boot measures the rendered pack before injecting it and sets aside whole lower-value components, in a fixed order, rather than truncating one mid-content. This fit is governed by the `briefing-budget` policy — a character budget and a set-aside rank per growing component. The **governance and consent content is never set aside**; the **status dashboard is the last thing set aside** — kept in every ordinary session, yielded only under extreme pressure (after every other component, when a heavy load of governance alarms — themselves never set aside — leaves no room). A margin canary holds a stated character margin under the size limit in the clean case, so ordinary structural growth of the never-shed content is caught before it starts eroding the dashboard's room; it is not an absolute promise the dashboard never sheds. The set-aside order, first to last: work neighbourhood → where-we-left-off → the pin index → the status dashboard; a set-aside is disclosed in plain words — a pin set aside raises a distinct, always-shown alert so the operator prunes rather than silently loses it.

## Significance

This locks orientation as plural, read-only, and unconditional-with-a-floor: refresh fires on its own, never as a step the operator must invoke, and never as a regeneration of canonical state. It fixes that boot is a renderer of other systems' contracts, not an originator — it surfaces a refused state cursor, reversible forgetting, an unprotected branch, and degraded substrates in plain words, but the detection and the fix belong to the systems that own them. Later work must respect this seam: a neighbour may refine its own internals and its own gate, but boot fixes only the disclosure, and any new operator-facing alarm must arrive as a deferral boot renders, ranked behind the governance-critical ones, never as logic boot invents.

It also locks the byte-fit as **governed, not emergency**: every component that grows carries a named budget and a set-aside rank, so trimming becomes a stated policy rather than a silent truncation, and a margin canary keeps the never-shed content (governance + consent) plus the routine dashboard within a stated headroom in the clean case, so structural growth is caught before it starts eroding the dashboard's room. The growth-vector table (dials in the `briefing-budget` policy):

| Component | Grows with | Dial | Set-aside rank |
|---|---|---|---|
| Work neighbourhood | graph relationships near the work | `neighborhood_groups_max` | 1 (first set aside) |
| Where we left off | quoted length of past-session lines | `excerpt_chars` | 2 |
| Pin index | number of operator pins | `pin_index_title_chars` | 3 |
| Status dashboard (routine body) | project state, counts | `dashboard_chars_max` (growth alarm) | 4 (last set aside; a heavy alarm load can still shed it, disclosed) |
| Governance + consent | — | — | never set aside |
| Clean-case headroom | never-shed content + routine dashboard | `margin_floor_chars` (hard code min) | — |

## Rationale

A cold session must reground itself without depending on the operator to remember a command, and most of what it must say is already owned elsewhere — the cursor store, recall, the branch-protection signal, the substrate health. Making orientation a family lets the heavy cost fall where latency is tolerable (building) and stay near zero where it is not (every prompt), while a single rendering point keeps the operator from meeting four different voices for four different problems. The trade is deliberate: boot accepts being downstream of everything and inventing nothing, so that each upstream system can settle its own contract independently and boot simply honours the handoff rather than racing it.

The byte-fit governance is the same posture applied to the platform's hard size limit: past that limit the platform silently substitutes a truncated preview, which once told an operator "nothing alarming was cut" about content it never saw. A per-component budget with a fixed set-aside order and a disclosed margin turns that silent loss into a governed, announced trim — and the margin's hard code floor keeps the number that defines "eroded" from being quietly lowered at one remove.

## Anti-choice

The strongest rejected alternative framed boot as setting a per-event cost ceiling that the prioritiser then allocates within. It lost because the prioritiser already owns the within-event budget split and its flex — a clean session gets more orientation, a high-debt one less — so a boot-owned ceiling would contradict that ownership and split one decision across two systems; the honest line is event-model here, within-event budget there. A second rejected option had a malformed state file hard-halt the session-start moment via an exit code. It lost because that moment has no safe halt: an exit-halt strands a non-engineer with a dead session and no recourse, where the correct posture is fail-loud within fail-open — surface the refusal, emit a finding, and fall through to the committed floor so the session degrades plainly instead of crashing.

The byte-fit budget added here does **not** reopen that first rejected ceiling. That ceiling was a *cognitive* one — how many items of each kind are worth surfacing — and it stays with the prioritiser (eADR-0032, the attention policy's item-count budgets and five-kind ranking), applied *before* rendering. The `briefing-budget` dials are a *mechanical* one: how the already-prioritised, already-rendered pack is trimmed to fit the platform's physical byte limit. Attention decides what is worth showing; boot decides how the rendered result is made to physically fit. Keeping the two in separate policies with that stated boundary is what prevents either from silently re-owning the other's decision.

## Status

accepted
