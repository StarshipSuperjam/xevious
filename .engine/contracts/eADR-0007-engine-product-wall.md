---
id: eADR-0007
title: The engine/product wall
status: accepted
date: 2026-06-29
---

## Decision

The engine and the product it builds are separated by a wall, and the engine is bound to one side of it. The engine confines itself to namespaced corners — its own `.engine/` directory plus keyed, reversible entries in the tool-dictated shared slots (the root `CLAUDE.md`, `.claude/`, engine-owned `.github/` files, and platform-fixed root files like `.mcp.json` and `.gitignore`) — and the product owns the repository root, scaffolded exactly as its ecosystem expects. The wall is enforced by file-precise path-ownership, not by boxing the product into a non-standard layout. Conceptually the engine is a *contributor* to the product, not a *component* of it: both run knowledge → actions → output, but the dependency runs engine → product and never the reverse. The product is built *by* the engine, never *on* it.

## Significance

This locks the top-level partition every other surface presupposes: ownership paths, workflow homes, surface locations, and substrate paths all dereference it. Any later structure attaches additively inside the reserved namespace and must never claim a product path. Three guarantees later work must respect: (1) asymmetric awareness — the product never depends on the engine, so it ships and runs standalone (to an app store, to production) with the engine removed; removing the engine degrades future buildability but never the product. (2) Clean removal — like a contributor leaving, the engine can be lifted out without unbuilding what shipped. (3) No imposed coupling — the operator may intertwine engine and product by choice, but the design never forces it. A shared root file stays product-owned; the engine owns only its delimited, keyed entries within it, governed by the wiring seam (eADR-0009).

## Rationale

Topology is presupposed by every other system, so it cannot be bolted on later — and the wall is its load-bearing cut. Confining the engine to dot- and tool-namespaced corners is the *most* product-respecting choice available: those names do not collide with any product ecosystem, whereas boxing the product under a directory fights root-expecting toolchains and a flat engine at the root collides outright. Because the engine is naturally confinable and a product is not, the boundary is drawn by confining the engine, never by quarantining the product. Enforcing the wall by ownership rather than physical separation means a product that already carries its own `.claude/` content or root orientation file co-exists with the engine's entries rather than being seized. The contributor framing supplies the deepest rationale: a contributor's tools are not the product, a contributor leaving does not unbuild what shipped, and the product cannot depend on its tooling and still ship elsewhere — collapsing the wall, clean removal, and product-agnosticism into one intuitive frame.

## Anti-choice

The strongest rejected alternative was to quarantine the product under a dedicated subdirectory and let the engine spread across the root. This was rejected because it imposes a non-standard layout on every adopter and breaks ecosystem tooling that expects to own the root (Go, Rust, and JS framework conventions all assume root residence); it inverts the natural confinability — boxing the thing that resists boxing and freeing the thing that confines cleanly. A second rejected frame modeled the engine as a substrate or platform the product *sits on top of*. That phrasing asserts a product → engine dependency, which is exactly the inference the wall forbids: a product built "on" the engine would be stranded the moment it shipped anywhere the engine does not travel. The dependency arrow must run one way only.

## Status

accepted
