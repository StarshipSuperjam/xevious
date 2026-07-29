---
id: eADR-0013
title: One trust gate — consent on evidence, never code review
status: accepted
date: 2026-06-29
---

## Decision

The human gate that makes this engine trustworthy is informed consent on an evidence bundle — never a reading of code. This holds at every layer and from the very first commit: the person who approves a change is a non-engineer, so no layer's safety may rest on a human reading the diff. The gate-holder consents on the strength of evidence dischargeable without reading code — deterministic mechanical validation (binary, pass/fail), independent cold-context cross-checks (whose worth is their independence and adversarial pressure, not the approver re-verifying them), behavioral demonstration the approver runs themselves, and an honest self-report record that names its own tier of confidence. Confidence in any change is bounded by how much of it has a non-AI correlate — mechanical or behavioral — and that bound is stated plainly, never dressed up as assurance the approver could check by inspection.

## Significance

This establishes a single, uniform kind of trust gate across build-orchestration, the control-plane, and every gate the engine raises: each one must produce evidence a non-coder can weigh and run, and may never assume the approver reads source. Where the unbypassable gate sits is settled separately (eADR-0005, placement); this law settles its nature — what the gate consumes and what it may demand. Later work must respect that the burden of proof is on the engine: a gate that can only be cleared by trusting an AI's word, or by reading code, is non-conformant. What may differ between layers is latitude, not kind — a maintainer building the template can spend freely on review depth; the deployed operator wants to walk away — but both meet the same evidence-based gate. The irreducible floor is the seed itself, the change with the least behavioral correlate and no second human; that floor is named openly, not hidden, and no construction step earns an exception to it.

## Rationale

A trustworthy autonomous builder cannot route its trust through code review when the only person at the gate does not read code. Two forces meet here. First, the approver is a non-engineer sole gate-holder from the first commit, with no second engineer to delegate inspection to — so any safety story that ends in "and then a human checks the code" is simply false. Second, the engine itself is the thing being made trustworthy, and an AI vouching for its own work is not evidence. Resolving both forces means the only currency the gate can accept is evidence that does not pass through AI judgment to be believed: mechanical checks anyone can re-run, and behavioral demonstrations the approver drives. AI cross-checks still earn their place — independence and adversarial pressure catch real defects — but they are weighed as pressure, not counted as proof. The trade is honest scope for false comfort: the engine concedes that confidence is bounded by behavioral coverage rather than claiming any change is airtight.

## Anti-choice

The strongest rejected alternative was a special exception for construction: let the earliest, hardest-to-demonstrate changes — the seed machinery itself — clear on engineer-grade code review, on the theory that bootstrap is a different regime than the deployed world. It lost on a fact, not a preference: there is no engineer to do that review, at construction or ever, and designing for one that does not exist would build a gate no one can hold. Granting construction a code-review exception would also fork the trust model into two kinds of gate, breaking the uniformity this law exists to guarantee, and would quietly relocate the truthfulness burden into artifacts the actual approver cannot read. The honest move is to hold the seed to the same evidence gate, shrink its residual risk with minimality and platform-native protections and a runnable checklist, and name what remains rather than paper it over.

## Status

accepted
