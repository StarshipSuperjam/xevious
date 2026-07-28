---
id: eADR-0006
title: Three enforcement tiers, named honestly
status: accepted
date: 2026-06-29
---

## Decision

Every rule that enforces anything declares exactly one of three enforcement tiers, and the three never blur into each other. Hard-fail rules bite mechanically and block — command hooks, schema validators, required checks. Soft-warn rules nudge and record but do not block — advisory checks and telemetry trends. Posture rules are expectations carried in standing rules and rituals, with no machine behind them. A rule's tier is declared, not inferred, and the name on it must be the truth about how hard it actually bites.

## Significance

This fixes that enforcement strength is always legible: anyone reading a rule knows whether it stops bad work, merely flags it, or only asks. It locks in two prohibitions every later check, hook, validator, and ritual must respect. A posture expectation may never be presented as if a machine were holding the line behind it, and detecting or reporting a problem may never be counted as having fixed it. Later work that adds a check or a hook inherits the obligation to name its tier honestly and, for any hard-fail rule, to be a gate that can genuinely be made to fail — a hard rule that can never fail is posture wearing a machine's name, which this law forbids. The remediation loop (eADR-0015) carries the second half: a soft-warn signal is the start of detect-triage-surface-remediate-validate, not its end.

## Rationale

The operator cannot read code and judges trust on evidence, so the one thing that must never happen is a guarantee that does not hold — a rule that looks enforced but only hopes. Collapsing the tiers is exactly how that happens: when advisory output reads like a block, or a posture expectation is described as guarded, the operator over-trusts and the gap is silent. Three named tiers keep the promise on each rule equal to what stands behind it. The split also matches where force belongs — mechanical refusal is reserved for governance-critical invariants and the unbypassable human-review gate, while local rules nudge the working AI to self-correct rather than pile up friction without proportional trust.

## Anti-choice

The weighed alternative was a single notion of enforcement — "the system checks it" — leaving how hard each check bites as an implementation detail. Rejected, because that is the precise failure this law exists to prevent: it lets a posture expectation be dressed as machine-enforced and lets reporting a drift pass for repairing it. An earlier large system collapsed exactly this way — expectations stated as if guarded, telemetry that surfaced problems and was mistaken for solving them — and the trust it bought was counterfeit. A unified tier reads simpler but spends its simplicity on the operator's confidence, which is the one resource the engine cannot afford to borrow against.

## Status

accepted
