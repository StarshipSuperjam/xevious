---
id: eADR-0029
title: The contract threshold and the standing-rule tier
status: accepted
date: 2026-06-29
---

## Decision

A decision earns a recorded contract only when it clears a bar on all four counts at once: it is architecturally significant, it constrains future work, it is hard to reverse, and it has a genuine rejected alternative. Everything below that bar is narrative recorded in the pull request, not a contract. The bar itself is governed by a standing rule — a tier-2 directive that outranks mechanics and guidance but yields to any contract — and that rule is held by a layered control: the bar stated as posture, a hard structural check that any contract carry a substantive anti-choice and significance statement, and a soft signal that surfaces an anomalous contract-creation rate at the next start. Standing rules are themselves a distinct surface tier: ongoing "what you must do" directives, one file each, separate from contracts (which record a one-time decision).

## Significance

This is the law that bounds the body of contracts itself, so it does not metastasize into a graveyard of trivia. It locks in that the contract count is a deliberately scarce signal: anything later wanting to record a decision must first justify clearing the four-part bar, and may not lower it by adding a fifth axis or skipping one. It fixes that the threshold is policed by posture plus structural presence plus a rate signal — never a hard numeric cap on how many contracts may exist. It also fixes the tier structure standing rules occupy (tier-2, below contracts, above mechanics) and the rule that a standing rule may carry tunable knobs whose force is operational, not a peer of the trust-model rules. Later work must route every below-threshold decision into the pull-request narrative, and must not invent a parallel place to park rejected decisions.

## Rationale

Left unbounded, a decision-record habit over-produces: every small choice becomes a ceremony, the records lose signal, and the scarce ones that matter drown. A hard cap was the obvious lever but the wrong one — it punishes a legitimately large stretch of work and fights the living-document goal. The honest control tiers by what can actually be checked: structural presence of an anti-choice and significance statement is mechanically verifiable, but whether a decision is genuinely significant is a judgment that stays posture, backstopped by a rate signal that flags an anomalous burst at the next start as a safety net for an operator who is not reading every record. Standing rules earn their own tier because an ongoing directive is a different thing from a settled one-time decision, and conflating them would lose the distinction between "what was decided once" and "what you must keep doing."

## Anti-choice

The strongest rejected alternative was a hard cap on contracts per session — a blunt count that would mechanically stop over-production. It lost because it is a blunt instrument that fights the living-document goal and punishes legitimately large work: a genuinely contract-dense stretch is not pathology, and a counter cannot tell the difference, while the real failure mode (trivial decisions dressed as contracts) is a significance judgment a number cannot make. A second rejected alternative was a dedicated rejected state for spurned decisions — a graveyard of rejected records. It lost because a rejected alternative already has a proven home as an anti-choice inside the prevailing contract; a separate graveyard of files adds storage and sweep cost for no signal. A third: framing a tuning rule that merely holds operational thresholds as a peer of the trust-model rules. It lost because that would falsely claim the trust model rests on it; an operational knob is foundational to a subsystem's operation, not to the trust model, and the honest framing keeps the two ranks distinct.

## Status

accepted
