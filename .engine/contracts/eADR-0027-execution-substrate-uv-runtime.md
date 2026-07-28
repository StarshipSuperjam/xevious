---
id: eADR-0027
title: Group-scoped uv-managed Python tool-runtime
status: accepted
date: 2026-06-29
---

## Decision

The engine's executable tools are written in **Python** and run inside an engine-owned, **uv-managed** runtime: a committed `.engine/pyproject.toml` plus `.engine/uv.lock`, and a gitignored `.engine/.venv/` materialized by `uv sync`. uv manages its own pinned standalone interpreter, so the engine never depends on or mutates the machine's system Python. uv is auto-bootstrapped on first run behind an explicit consent gate — a heavier trust class than a scoped permission grant — installed PATH-independently to a known engine location and invoked by absolute path, and it **never** degrades to system Python. The sync is **group-scoped**: each dependency-carrying capability declares one dependency-group in `pyproject.toml` named by its own module id (matched by normalized name), `uv.lock` resolves the full set for reproducibility, and the runtime installs only the groups derived from the present module set — so a deselected capability ships no live dependency surface.

## Significance

This locks in that every executable tool the engine runs is Python on a self-contained, reproducible runtime the operator never has to install or debug. The lockfiles are foundation artifacts replaced wholesale on upgrade, never preserved like operator config — later work must not treat them as a tunable seam or add a dependency directive to the closed wiring vocabulary (eADR-0009). Dependency presence is keyed off the existing module id, so the manifest grammar gains no new field; capability selection and dependency materialization stay one derived fact, preserving "installed means present, absent capability carries no live defect." When the runtime cannot materialize, the engine degrades **loud** to the interpreter-independent git-native boot floor — orienting-only, with a recoverable retry — and accepts the named bound that an engine whose runtime never comes up is inoperable. No tool, hook, or check may assume an ambient interpreter; all invoke the engine's own venv or `uv run`.

## Rationale

A feature with no pre-existing grammar becomes a system refactor when discovered at build time, and the language and isolation of the engine's tools is exactly such grammar — yet it sat unnamed while the engine already ran Python everywhere. Naming it removes a build-time landmine: a silently-assumed system interpreter would put a non-engineer on the PEP-668 / global-pollution rocks the moment a dependency was needed. uv earns the choice over stdlib tooling because it manages the interpreter version itself, making the runtime self-contained rather than coupled to whatever Python the machine happens to carry. Group-scoped sync is the crux that keeps capability selection honest at the dependency layer; keying the group by module id keeps the join trivial and grammar-free. The cost paid deliberately: a heavier first-run consent moment, and an inoperable-if-unmaterialized bound made honest rather than hidden behind a fallback.

## Anti-choice

The strongest rejected alternative was **degrading to the machine's system Python whenever uv is absent**, with stdlib venv + pip as the runtime. It was rejected because it reintroduces the exact coupling the substrate exists to remove: stdlib venv cannot manage the interpreter, so the machine's Python version silently becomes a dependency, and a system-Python fallback is a dishonesty violation — it would let the engine appear to run on an environment it cannot vouch for, stranding a non-coder on errors they cannot diagnose. Degrading **loud** to the git-native floor and naming the inoperable bound is the honest posture; a quiet fallback trades the operator's trust for the appearance of resilience. Two narrower alternatives also lost: a single superset sync (installs deselected capabilities' dependencies, a live supply-chain surface) and per-module resolution at install time (puts network/resolution failure on the add path) — group-scoped install off a fully-resolved shipped lock is both reproducible and offline after first sync.

## Status

accepted
