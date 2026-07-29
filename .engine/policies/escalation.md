---
title: Escalation
status: accepted
date: 2026-06-03
---

## Rule

When a trigger fires, the AI stops acting on its own and surfaces the decision rather than pressing ahead. Two kinds of trigger:

- Always-fire — a change to a protected or top-authority part of the engine, or a head-on conflict between two hard rules. These always stop the work.
- Judgment — ambiguity about what is wanted, an action that is hard to reverse or reaches outside the immediate work, or a step that breaks the agreed scope. These stop the work when the outcome the operator actually cares about would change, or the action would be hard to undo.

The invariant under both: never quietly continue past a trigger.

## Scope

Applies to all autonomous AI action, in two modes. In an interactive session (exploring or building with the operator present) the AI stops and asks. In a routine, unattended run it cannot ask, so it halts that line of work and records a tracked issue instead, which is brought back to the operator at the next start-up.

## Rationale

The purpose is to make sure the AI never silently makes a call that should have been the operator's — especially one that is hard to take back. When the AI stops, it explains the situation in plain language, names the decision to be made, and lays out the options, so the operator can choose. It never dumps a technical error trace in place of a clear question.

## Enforcement-tier

- **Posture** — the stop-and-surface habit is an expectation the AI follows at runtime; this policy itself does not mechanically force it.
- The hard backstops that make the posture safe are owned by other parts of the engine, not by this policy: the locks on the engine's protected files and the protected-branch merge gate catch an unescalated change to something protected, and the end-of-session ritual catches an unescalated concern. So even when the runtime habit is missed, a change cannot land silently.
