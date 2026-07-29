---
title: Model routing and execution posture
status: accepted
date: 2026-07-27
---

## Rule

The engine adjusts **its own** operation to the execution environment doing the work — it does not tell the
operator which model is running (their harness already shows that). At session start the engine observes the
runtime, the engine release, and the hashes of its instruction-floor files, and compares them against the
qualification baseline the operator committed (`.engine/state/execution.json`). The comparison yields a
posture, and the posture selects which self-instructions the engine loads:

- **matched** — this environment is qualified for this repository and every checked component agrees. The
  engine loads the *qualified* posture below and operates per the capability→model bindings
  (`.engine/policies/model-bindings.json`).
- **changed / unqualified / unknown** — the environment drifted, was never qualified here, or the baseline
  could not be read. The engine loads the *conservative default*: full ceremony, no model-dependent
  shortcuts.

The engine loads the *qualified* posture only for a `matched` environment; every other posture loads the
conservative default. The running model's identity is not reliably observable to the engine, so no posture
ever treats the live model as guaranteed — the bindings are intent, not a proof of which model answered.

The two posture blocks below are operator-tunable. Each is the fenced `text` block immediately after its
`posture:` marker; keep the marker and the fence intact when editing, because a malformed block falls back to
the engine's built-in conservative default rather than failing loudly.

<!-- posture:qualified -->
```text
This execution environment matches the qualification recorded for this repository — no drift from the qualified snapshot.
The review personas run on the models bound to their capability tiers in .engine/policies/model-bindings.json.
This does not license reduced ceremony: operate with your normal care. The running model's identity is not verified by the engine.
```

<!-- posture:conservative-default -->
```text
Execution environment is not a verified qualified match here — run your full, careful ceremony.
Make no model-dependent shortcuts; the running model's identity is not verified by the engine.
```

The qualification act is the operator's: `uv run --directory .engine -- python tools/execution_environment.py
record <env>` stamps a proposed baseline, and merging that diff is what qualifies the environment. The engine never qualifies itself. Because the
deriver reads the working-tree baseline, a recorded-but-unmerged qualification is in effect for that session's
own worktree; durability and sharing come from the merge. In this repository the baseline ships at genesis
(unqualified), so the engine runs the conservative default here — the feature earns its qualified posture in a
deployed repository, not in the engine's own store.

## Scope

Governs how the engine selects its execution posture and which model realizes each agent's capability tier. It
does **not** override any deterministic control: protected `main`, human merge, the validation gates, explicit
Build authority, and guardrail acknowledgment are unaffected by any posture. Posture selects self-instructions
and model bindings only; it never modulates a review gate.

To retune the fleet, edit `.engine/policies/model-bindings.json`: each capability tier (`judgment`,
`mechanical`) and each per-persona override binds a durable model alias (opus, sonnet, haiku, …; never a
versioned id) and an effort (`low`/`medium`/`high`). Then run `uv run --directory .engine -- python
tools/agent_bindings.py render` to stamp the personas; a CI check fails with that exact instruction if the
bindings and the stamped personas ever drift.

## Rationale

The engine cannot meter its own token spend or choose its own model mid-session — it does not own the
model-invocation loop, and no runtime exposes usage or the model name to it at session start. So a cost router,
per-task budgets, and a token ledger were rejected: they would be scaffolding the engine cannot enforce, only
pretend about. An automated behavioural qualification suite was also rejected — self-grading is circular and
costs real tokens per model release. What the engine *can* do is what this policy does: record which
environments the operator has qualified, notice drift from that snapshot, and shape which model realizes each
capability tier. Model identity is deliberately capability-shaped, never a pinned model name in a persona file
(a versioned id rots). On Codex the running model id is not exposed at all; the deriver behaves uniformly and
simply records no model identity there — a data-availability fact, not a capability asymmetry. The bindings are
an evolving judgment, retuned by the operator: edit `.engine/policies/model-bindings.json` and re-run
`agent_bindings.py render`, and a lesson about which model suits which work is promoted into the file by a
reviewed change. `AUDIT_MODEL` (the audit workflow's model knob) is the one place a model is named at
invocation and is left to the operator.

## Enforcement-tier

Posture — guidance the engine follows, at the honest tier the boot briefing and conduct floor occupy, backed
by the operator's merge, not a mechanical wall. The committed baseline and the bindings file have their shape
enforced by hard schema checks at merge; their *values* (which environment is qualified, which model realizes
a tier) are the operator's, changed only by a reviewed, merged edit.
