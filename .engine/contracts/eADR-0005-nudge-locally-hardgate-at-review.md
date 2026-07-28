---
id: eADR-0005
title: Nudge locally, hard-gate at human review
status: accepted
date: 2026-06-29
---

## Decision

Local checks and hooks only *nudge* the working AI toward self-correction; the single hard, unbypassable gate sits where a human reviews and merges — the protected branch. A local gate may hard-block only a governance-critical invariant (one that would corrupt the gate itself); everything else it touches is advisory. Concretely: an in-session write restriction, an advisory check, or a pre-action hook is a fallible signal that errs toward letting work proceed and explains itself in plain language when it intervenes; the merge into the protected branch is the only refusal nothing in a session can route around.

## Significance

This fixes WHERE enforcement lives: any new check, hook, or in-session restriction is built as a local nudge, not a wall, and the only thing permitted to truly stop work is the human merge. Later work must not dress a local mechanism as absolute ("the AI cannot do X") — the honest claim is that it cannot do so silently or by default, visible and effortful, with the merge as the wall. It also forbids the opposite error: a local hard-block that taxes ordinary work without protecting a gate-critical invariant. This decision settles the *placement* of the gate; it does not settle what *kind* of gate the human one is — that the human gate is informed consent on evidence rather than code review is a separate law (eADR-0013). The two compose: this decision says the wall is at human review; eADR-0013 says that wall is consent, not inspection. New gates honor both without restating either.

## Rationale

The operator directs and merges through the engine rather than reading its code, so trust cannot rest on a human catching mistakes in review — the burden of proof is on the engine, and the wall has to be a mechanical one the operator can rely on without inspecting. A local gate that hard-blocks creates friction in every session while protecting nothing the merge does not already protect, and it tempts an over-claim: the platform can ignore some in-session denials and shell matching is best-effort, so a local restriction presented as a wall is a lie waiting to be found. Pushing real enforcement to the merge keeps local signals cheap, fallible, and honest, and reserves mechanical refusal for the one place it cannot be bypassed. The exception — a governance-critical invariant may hard-fail locally — exists because the gate must not be falsifiable by the very change it judges.

## Anti-choice

The strongest rejected alternative was to make local gates authoritative: have in-session hooks hard-deny risky actions so the AI is stopped at the source, before anything reaches review. It was rejected because the enforcement is not real — the platform ignores some in-session denials and command matching is only best-effort, so an authoritative-looking local gate would be bypassable in practice while claiming not to be, exactly the over-claim that destroys trust the engine has to earn. It also taxes every ordinary session with refusals that protect nothing the merge does not, and produces unexplained blocks on legitimate work. The honest split — local nudge, single hard wall at the human merge — gives a refusal the operator can actually count on without pretending the cheap signals are walls.

## Status

accepted
