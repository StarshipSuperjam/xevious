---
id: eADR-0015
title: Telemetry surfaces problems, it does not fix them
status: accepted
date: 2026-06-29
---

## Decision

Self-monitoring is built as a remediation loop the AI closes across sessions, never as autonomous self-healing. The loop is fixed: detect drift over the Engine's own work, triage a persistent signal into tracked debt, surface that debt at the next boot, have the AI remediate under the normal guardrails, and confirm the fix by validation. The single act self-monitoring performs on its own is triage — opening or updating one tracked issue for a signal; it changes no other standing state. The system is described to the operator as self-surfacing, and is never described as self-healing.

## Significance

This locks in that noticing a problem and repairing it are different steps with a session boundary and an operator merge between them: the only thing that closes automatically is the issue when its signal goes absent, and clearing that flag retires a now-quiet signal, it never repairs anything. Later work must respect that the autonomous footprint stops at triage — nothing downstream may auto-edit content, auto-close real debt, or otherwise act as if reporting were fixing. Any honest claim made to the operator about this system is bounded to "it watches and tells you," and any feature that would heal unattended is out of bounds here. Remediation itself rides the ordinary build path (a draft PR, its checks, the operator's merge), so this law also fixes that surfaced debt is fixed the same trustworthy way as any other change.

## Rationale

The honest mechanism really is self-surfacing plus next-session action: an AI agent, under guardrails, fixes what was surfaced, and validation confirms it — there is no daemon that quietly repairs in the background. The operator approves on evidence rather than by reading code, so the burden is on this system to never overstate itself. Calling it self-healing would invite exactly the unsafe trust this design exists to prevent: the operator would assume surfaced problems are already handled and stop watching. Keeping the autonomous act down to a single bounded write (triage) also keeps the system mechanical and free of judgment — deciding which problems matter, or altering standing state to make them go away, is the kind of judgment that belongs to the audit rung (eADR-0028), not to a counting-and-trending loop.

## Anti-choice

The strongest rejected alternative was to present and build the system as self-healing — to let it close the loop unattended and tell the operator it keeps itself well. It lost because it is a false promise: the moment a surfaced problem sits unfixed, the claim is exposed and trust breaks, and a non-engineer operator who believed it would have stopped checking precisely when checking mattered. A weaker variant — a hard volume cap that drops or coalesces low-severity signal once a limit is hit — was also rejected, because it would force the system to decide which signals matter (a judgment it must not make) or to mutate standing state on its own (a step toward the self-healing this law forbids); the bound on volume is instead structural, since every recurrence of a signal collapses onto its one source-keyed issue.

## Status

accepted
