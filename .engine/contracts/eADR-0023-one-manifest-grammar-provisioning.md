---
id: eADR-0023
title: Provisioning runs on one manifest grammar
status: accepted
date: 2026-06-29
---

## Decision

Standing up a repo and managing engine capability over its life run on a single manifest grammar, split into two subsystems that share it. A one-time, self-deleting instantiator does first-run setup; a permanent module manager adds, removes, upgrades, and cleanly uninstalls thereafter. Every module's manifest declares both the files it provides and the wiring it requires — hooks, registrations, check rosters, ontology, permissions — declaratively and reversibly. One shared library applies and reverses that wiring, and a coherence check confirms the installed set is consistent. The setup that turns the review gate on travels as a committed, re-runnable operation that fails loud and visibly until it is applied — never a step the operator is merely told to perform.

## Significance

This fixes that installing or removing a capability is a mechanical, reversible operation, not hand-surgery: anything that adds capability must declare what it provides and what it wires, and the shared library must be able to reverse exactly that. The two subsystems are bound to one grammar — the permanent manager reuses the same primitives the instantiator composes, so wiring logic never dies when the instantiator retires. Later work must respect that the system installing capability exists before the capability grammar it applies, so it cannot itself be an installed module; that the review-gate setup is a traveling artifact whose loud failure is the design, not an oversight; and that the coherence check is a hard gate — broken wiring is surfaced and paused, never made the silent operating baseline.

## Rationale

An operator builds through the engine rather than by reading its code, so install side-effects cannot be left as reconciliation work nobody does. If a module were only files plus dependencies, every install would mean manually fixing up settings, registrations, suites, and ontology by hand — the exact "every feature becomes a refactor" failure that made an earlier broad attempt uncontrollable. Pushing wiring into the manifest, applied and reversed by one library, makes breadth survivable: a new capability attaches additively and removes cleanly. The same reasoning drives the traveling gate-setup: the review gate is what every other guardrail depends on, and an operator silently skips a setup step they were only told about — so the fix must itself be a committed file that nags until done, because an unprotected branch that looks set up is worse than one that is loudly not.

## Anti-choice

Model modules as pure file collections and let install side-effects be reconciled by hand. Rejected: it forces manual reconciliation of settings, registrations, suites, and ontology on every install, which does not scale past a handful of capabilities and reproduces the breadth-collapse it is meant to avoid. The weaker variant — keep the manifest grammar but ship the review-gate setup as a documented command the operator runs — was also rejected: silent omission of that one step leaves the protected branch unprotected, and the burden of proof is on the engine, so the gate that makes it trustworthy cannot ride on a non-engineer remembering to run a command.

## Status

accepted
