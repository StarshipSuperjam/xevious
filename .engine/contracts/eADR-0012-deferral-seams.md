---
id: eADR-0012
title: Deferral seams — the integrator relays; the owner detects and owns
status: accepted
date: 2026-06-29
---

## Decision

Where two systems meet at a seam, detection of the upstream condition and the upstream mechanism stay with the owning side; the integrating side binds to the seam's stable channel contract, never to the enumerated set of producers or items that flow across it. An integrator surfaces, ranks, deduplicates, gates, dispatches, or applies over a channel whose membership it does not own — it acts on whatever the owners hand it and stays silent on which owners exist or what they detect. The integrator is not a passive pipe: it owns its acting-mechanism over the channel; what it does not own is the upstream detection or the upstream mechanism. "Relay" names the ownership boundary, not an absence of work.

## Significance

This locks the ownership axis of every cross-system seam, and it is the sibling of the presence-discovery axis (eADR-0010): that axis answers which providers are present; this one answers who owns what across a boundary. The two compose without merging — orientation surfacings are pure ownership-relay with no presence-binding, a check roster is pure presence-discovery, and a findings inbox is both at once. Later work must respect that an integrator binds to one channel contract per seam, so a new upstream producer or item attaches additively and an owner's later evolution refines only its own side and cannot force a change on the integrator's side. The orientation surface shows readouts but never owns staleness detection; the validation dispatcher routes results but owns no rule and no detection; the triage inbox surfaces findings but the producers detect and emit; provisioning applies a fix but the contract that defines it lives elsewhere. Anyone reasoning about a seam names this boundary rather than re-deriving the split, and anyone adding an upstream item must attach it on the owner's side, not by widening the integrator.

## Rationale

This disposition is the dominant decoupler of the whole design, and it had been re-derived independently under a different local vocabulary at every major seam. The force it answers is the cost of an integrator that binds to the roster of what crosses a seam rather than to the seam's channel: every time an upstream owner gains a new producer, item, or detection, a roster-bound integrator must change too, and the web of "this changed, so its integrator must change" requirements grows without bound. Binding the integrator to the channel and keeping detection and mechanism on the owning side collapses that web to one contract per seam. The trade-off paid is that the integrator does real work it must not be mistaken for owning — it deduplicates, ranks, orders, dispatches, applies — so the boundary is drawn precisely at detection and mechanism, not at effort, which is why "relay" is the ownership line and not a claim of idleness.

## Anti-choice

The strongest rejected alternative was to fold this into the presence-discovery axis (eADR-0010) as a single seam law. It lost because it conflates two genuinely distinct questions: set-membership-by-presence (which providers exist) versus ownership-across-a-boundary (who detects and who relays). The cases pull apart cleanly — orientation surfacings carry no presence-binding at all yet are pure ownership-relay, and a check roster is pure presence with no relay boundary — so a merged law would force one axis to masquerade as the other and lose the precision that lets the densest integrators reason about a seam by naming exactly one boundary. A second rejected reading was that a relaying integrator is therefore a dumb pipe with no work of its own; this was rejected because the integrators that ride this seam visibly own their acting-mechanism over the channel, and erasing that would mis-license stripping real behavior out of them.

## Status

accepted
