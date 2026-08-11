#!/usr/bin/env python3
"""The module-surfaces registry — every file an engine MODULE provides, mapped to its owning module id.

Generated from ALL module manifests in the SOURCE repo (where every module is present), committed to
`.engine/provisioning/module-surfaces.json`, and shipped UNCHANGED to every deployment — it is NOT regenerated
per deployment, so it keeps listing a module's surfaces even after that module is DECLINED and its manifest is
gone. That is what lets a deployed repo recognize a path missing *because its owning optional module was
declined* as a legitimate absence rather than a broken reference: the link-integrity check consults
`declined_surface_owner` to TOLERATE a dangling link to such a path instead of failing it (StarshipSuperjam/engine-template#646).

`load` and `declined_surface_owner` read only the committed registry + `engine.json` (both travel) and import
nothing heavy, so `validate._coverage_links` can call them without a circular import; `derive`/`generate`
import `module_coherence` lazily and run only where the full manifest set is present (the source repo).
"""
from __future__ import annotations
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate  # noqa: E402

REGISTRY_REL = os.path.join(".engine", "provisioning", "module-surfaces.json")


def derive(root: str | None = None) -> dict:
    """{relpath: [owning_module_id, ...]} for every file the PRESENT module manifests provide — the FULL owner
    list (not collapsed), so a path shared by several modules is only tolerated when NONE of its owners is
    installed. Complete only where every module is present (the source repo), which is why the committed file
    is generated there and travels."""
    import module_coherence  # lazy: module_coherence imports validate, so keep it out of import time
    surfaces: dict = {}
    for rel, owners in module_coherence.provides_claims(module_coherence.discover_manifests()).items():
        surfaces[rel] = sorted(owners)
    return surfaces


def generate(root: str | None = None) -> dict:
    surfaces = derive(root)
    path = os.path.join(root or validate.ROOT, REGISTRY_REL)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"surfaces": {k: surfaces[k] for k in sorted(surfaces)}}, fh, indent=1, ensure_ascii=False)
        fh.write("\n")
    return surfaces


def load(root: str | None = None) -> dict:
    """The committed registry {relpath: [module_id, ...]}, or {} when absent/unreadable (degrade to NO
    tolerance, never crash the link check that consults it)."""
    path = os.path.join(root or validate.ROOT, REGISTRY_REL)
    try:
        return (validate.load_json(path) or {}).get("surfaces") or {}
    except Exception:  # noqa: BLE001 — an unreadable registry degrades to no tolerance, never a crash
        return {}


def _installed_module_ids(root: str | None = None) -> set:
    """The installed module ids (engine.json `packages`), or the EMPTY set when it cannot be read. Callers must
    treat empty as 'could not determine' and fail CLOSED — a valid engine always has at least `core`."""
    try:
        eng = validate.load_json(os.path.join(root or validate.ROOT, ".engine", "engine.json"))
        return set((eng or {}).get("packages") or {})
    except Exception:  # noqa: BLE001
        return set()


def declined_surface_owner(abs_path: str, root: str | None = None) -> "str | None":
    """The owning module id if `abs_path` belongs to a REAL module NOT installed in this deployment — so its
    absence is a legitimate decline, not a broken reference — else None. Two cases: a file under a module's own
    directory `.engine/modules/<mid>/` (only when `<mid>` is a real, catalogued module), and an overlaid
    surface the registry maps to modules (only when NONE of its owners is installed). Returns None for a path
    owned by an installed module, a path no module owns, or a path shaped like a module dir whose name is not a
    real module (a typo / renamed / removed dir) — all of which stay HARD broken links. FAILS CLOSED: if the
    installed set cannot be determined it tolerates nothing, so a corrupt engine.json can never soften a link."""
    root = root or validate.ROOT
    installed = _installed_module_ids(root)
    if not installed:                       # a valid engine always has `core`; empty means the read failed
        return None                         # fail closed — never soften a link when the roster is unknown
    rel = os.path.relpath(abs_path, root)
    registry = load(root)
    known = {mid for owners in registry.values() for mid in owners}  # every real module owns some surface
    parts = rel.split(os.sep)
    if len(parts) >= 3 and parts[0] == ".engine" and parts[1] == "modules":
        mid = parts[2]
        if mid in known and mid not in installed:   # a REAL, declined module's own directory
            return mid
    owners = registry.get(rel)
    if owners and not any(o in installed for o in owners):   # tolerate only when NO owner is installed
        return sorted(owners)[0]
    return None


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "generate":
        generate()
        print(f"wrote {REGISTRY_REL}")
    else:
        print("usage: module_surfaces.py generate", file=sys.stderr)
        sys.exit(2)
