---
id: eADR-0009
title: Modules declare files and wiring
status: accepted
date: 2026-06-29
---

## Decision

A capability is packaged as a module, and a module declares two things, not one: the files it provides and the wiring it requires. Wiring is every side-effect beyond copying files — hook registrations, MCP server definitions, ontology records, permissions, ignore lines — and each is declared from a closed, named vocabulary so that a shared library can both apply it and reverse it mechanically. Installing a module is then a single mechanical act, never hand-surgery; removing one is the same act run backward.

## Significance

This locks in that no capability may touch shared state in a way the system cannot itself undo. Every later module — present or yet to be designed — must express its install footprint entirely as declared files plus declared wiring drawn from the closed vocabulary; there is no escape hatch for an arbitrary install step. Because the wiring is declared and reversible, the coherence check (eADR-0020) can confirm, after any install, uninstall, or upgrade, that what a module declared is exactly what is applied and nothing engine-owned is applied that no module declared — wiring reversibility is enforced, not trusted. Anything that needs a genuinely new kind of side-effect is a deliberate, reviewed change to the shared wiring library, never a per-module improvisation. This is the discipline that earns fault-containment at the seams (eADR-0008); the module shape alone confers nothing.

## Rationale

Modeling a module as files plus dependencies only would make every install hand-surgery: someone would have to manually reconcile settings, servers, permissions, and ontology each time a capability went in or out. That is exactly the failure where every added feature becomes a system-wide refactor — the breadth that becomes unmanageable. Forcing wiring to be declared, and drawn from a vocabulary where every directive has a guaranteed reverser, trades a little expressive freedom for two guarantees that matter more: install and uninstall are mechanical and symmetric, and a validator can prove the installed set is internally consistent. Reversal keys on engine-owned identity rather than on raw content, so undoing a module never disturbs an entry the operator or the product authored independently.

## Anti-choice

The strongest rejected alternative was the simpler and more familiar one: a module as a pure collection of files, with any required side-effects handled by an install script the module carries. It lost because an arbitrary script is an irreversible mutation of shared state with no guaranteed way back — the precise shape of the failure this law exists to prevent. Side-effects done that way cannot be reliably reversed on uninstall, cannot be checked for consistency, and leave the operator to manually reconcile settings and servers by hand. The friction of having to add a new reverser-paired seam to the shared library, rather than dropping a script into a module, is the firewall, not an oversight.

## Status

accepted
