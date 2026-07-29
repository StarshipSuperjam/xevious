---
id: eADR-0028
title: Audits are cold-context, read-only, adversarial self-review
status: accepted
date: 2026-06-29
---

## Decision

Periodic self-review of the running Engine is performed by a purpose-built audit persona — a cold-context agent instance, distinct from build-review lenses, that inspects standing accumulated state rather than a proposed change. It is bound by three posture laws: (1) it defaults to recommending **retirement** of accumulated local state and preserves a thing only on an affirmative "what does this do that nothing else does?" case, with no quota and the honest "nothing to retire, here is what I checked" always preferred to a manufactured nomination; (2) a fitness claim must rest on a **content probe run during this audit**, never a cached count, status field, or existence check; (3) each run reads **at least one randomly chosen in-repo artifact as if it had no project context**, testing whether its references resolve, whether its prose tells a cold reader how to use it, and whether operator-facing prose meets the operator-communication register. The persona is **read-only**: it reports findings and recommendations to engine-labeled issues and never writes engine or product state. The merge of a remediating change is the adjudication; the audit never heals autonomously. It runs only on **accumulated local state** in the repo it lives in — a machinery bug it cannot fix locally is drafted as an upstream report for the operator to file or ignore, never auto-filed and never silently phoned home.

## Significance

This establishes the **judgment rung** above the mechanical floor: validation asks "does this match its declared shape" per event, telemetry asks "are the signals trending bad" in aggregate, and the audit asks the one question neither can encode — "does this still earn its keep, or has the deployed instance silently drifted past it." Later work must respect three boundaries. First, the **read-only / report-never-heal** seam: nothing downstream may let the audit persona write engine or product surfaces; remediation is always a human-gated, reversible, propose-not-apply path. Second, the **cold-context, adversarial-by-default** posture is structural, not stylistic — any feature that feeds the audit its own warm project context or its own prior recommendations as evidence breaks the defense against compounding bias. Third, the audit operates **only on locally-remediable state**; template-owned machinery is told apart mechanically (it belongs to an installed package's provides set), is never a local retire-candidate, and a machinery problem takes the escalate-upstream path. The retire-default's no-quota rule binds every consumer: a recommendation surfaced to fill a slot is the failure mode, not the goal.

## Rationale

Without an adversarial, retirement-defaulting posture, any periodic review drifts toward "confirm it still works, preserve it," accreting dead weight no rule can catch — the same compound-drift failure the rest of the guardrail ladder exists to prevent, now reappearing at the review layer itself. The cold-context random pick exists because a review that "knows too much" about a repo stops seeing the cruft it has come to trust; reading an artifact as a stranger is the only way to catch references that no longer resolve and prose that no longer teaches. The function-probe rule exists because a count proves a thing *exists*, never that it still *does work* — only a probe run now distinguishes the two. Read-only-and-recommend is the trade that makes the rung trustworthy: the question is a judgment a check cannot make, so it must be made by an AI, but an AI that could enact its own judgment on standing state is exactly what cannot be trusted to self-modify, so the merge — a human act — is the consent.

## Anti-choice

The strongest rejected alternative was a **self-tuning self-maintenance loop**: let the audit not only flag cruft but re-weight attention, re-tune thresholds, and learn its own operating parameters from observed usage — the full procedural-memory learning ambition, hygiene *and* tuning in one always-on core. It was weighed seriously and rejected on converging grounds. It is **infeasible on the available signals** — the platform exposes no reliable "what was acted on" signal, and "acted on" is a semantic judgment the metering layer is forbidden from making. It **contradicts the determinism** of attention, which carries no decay state and no machine learning. A non-engineer operator **cannot meaningfully consent** to an engine quietly re-tuning itself. And it **over-weights the contagious core**, baking a single project's idiosyncrasies into machinery every downstream repo inherits. The valuable part of the ambition is real but deferred to a future optional module, pursued only with multi-project evidence — never shipped in required core. A second rejected option was reusing the **build-orchestration review personas** instead of a purpose-built one; it lost because a build reviewer inspects a proposed change while an audit inspects standing accumulated state — a different question needing a different persona.

## Status

accepted
