---
title: Finding disposition
status: accepted
date: 2026-06-03
---

## Rule

Every concern the AI raises while working must reach exactly one durable outcome — never a "maybe later" left floating in the conversation:

- If it blocks the work at hand → stop and surface it for a decision (escalate).
- If it is small and directly related to the current work → fix it in line.
- If it is real but outside the current work → open a tracked issue and move on.

A "not urgent, we'll get to it" aside with no record created is a violation of this rule.

## Scope

Applies to anything the AI surfaces during a working session. The "fix it in line" outcome is deliberately narrow: it is allowed only when the fix is both small and directly related to the work in hand. Anything larger, or unrelated, becomes a tracked issue instead — work is never quietly expanded to absorb it.

## Rationale

The point is simple: no concern the AI raises should quietly disappear into a chat transcript nobody re-reads. By forcing every concern to a fix, a tracked issue, or an escalation, the operator can trust that nothing important was noticed and then silently dropped. Tracked issues are not a hidden backlog either — they are brought back to the operator in plain language the next time the engine starts up, so they surface on their own rather than waiting to be hunted down.

## Enforcement-tier

- **Posture** — the disposition habit itself is an expectation the AI is trusted to follow on every concern it raises.
- **Hard-fail (the close gate's, not this policy's)** — the end-of-session ritual pushes back until every concern raised has been given a disposition, and hands the operator a plain-language summary instead of leaving them to scour the transcript. That ritual is built as the turn-close `Stop` hook; this policy doc itself stays posture, while the gate enforces a strong local block over the findings that were recorded.
- The durable, unbypassable backstop is the human review at the protected-branch merge — not any local check. Even a concern that slips every step above is still caught there.
