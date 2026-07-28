#!/usr/bin/env python3
"""Release integrity — the engine manifest's per-package versions must agree with each module's own
manifest version, and the two sets must match one-for-one.

`.engine/engine.json` records a `packages` map (module id -> version); each
`.engine/modules/<id>/manifest.json` records its own `version`. The two are kept in sync only by
construction — the instantiator writes the packages map FROM the manifests, and a release cut
(release_cut.py) writes both together. Nothing else re-checks the invariant, so a partial write, a
crash mid-cut, or a hand-edit could leave a SPLIT-BRAIN: the engine manifest claiming one version for
a module while the module's own manifest claims another. Downstream that is a silently mis-versioned
release (a missed migration). This HARD check catches the disagreement at merge; it reads only
committed files and changes nothing.

A `custom/script` rule (the conduct-weakening pattern): it prints a finding.v1 array on stdout and
returns 0. Clean (every package agrees, sets match) => an empty array => green.
"""
from __future__ import annotations
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate            # noqa: E402  (the finding constructor)
import module_coherence    # noqa: E402  (the one present-set reader)


def findings(tier: str, engine=None, manifests=None) -> list:
    """One finding per package whose engine-manifest version disagrees with (or is missing from) the
    module's own manifest, in either direction. `engine`/`manifests` are injectable for the demo/tests."""
    out = []
    engine = engine if engine is not None else module_coherence.load_engine_manifest()
    if engine is None:
        return out  # a missing engine manifest is the engine-manifest schema check's concern, not this one
    pkgs = engine.get("packages", {}) or {}
    mans = manifests if manifests is not None else [m for _rel, m in module_coherence.discover_manifests()]
    seen = set()
    for man in mans:
        mid = man.get("id")
        if not mid:
            continue
        seen.add(mid)
        mver, pver = man.get("version"), pkgs.get(mid)
        if pver is None:
            out.append(validate.finding(tier, f"The engine manifest (.engine/engine.json) does not list the "
                f"installed module '{mid}' (present at version {mver}) in its packages map. Every installed "
                f"module needs a matching packages entry. To fix: add \"{mid}\": \"{mver}\" to packages in "
                f".engine/engine.json."))
        elif pver != mver:
            out.append(validate.finding(tier, f"Version split-brain for '{mid}': the engine manifest "
                f"(.engine/engine.json) records {pver} but the module's own manifest records {mver}. The two "
                f"must agree — a release cut writes both together. To fix: set both to the intended version."))
    for mid, pver in pkgs.items():
        if mid not in seen:
            out.append(validate.finding(tier, f"The engine manifest lists package '{mid}' (version {pver}) "
                f"but no such module is installed. To fix: remove it from packages in .engine/engine.json, or "
                f"reinstall the module."))
    return out


def _fixture_state():
    """The (engine, manifests) to check when the negative-fixture meta-check points us at a seeded
    split-brain tree via env (the self-map-drift ENGINE_SELF_MAP_PATH pattern). Paths are ROOT-relative
    or absolute. Returns (None, None) — i.e. use the live tree — when the override is absent."""
    ep = os.environ.get("ENGINE_RELEASE_INTEGRITY_ENGINE")
    mp = os.environ.get("ENGINE_RELEASE_INTEGRITY_MODULES")
    if not (ep and mp):
        return None, None
    import glob
    root = validate.ROOT
    ep = ep if os.path.isabs(ep) else os.path.join(root, ep)
    mp = mp if os.path.isabs(mp) else os.path.join(root, mp)
    engine = validate.load_json(ep)
    mans = [validate.load_json(p) for p in sorted(glob.glob(os.path.join(mp, "*", "manifest.json")))]
    return engine, mans


def emit(fs: list) -> int:
    print(json.dumps(fs))
    return 0


def _demo() -> int:
    """Show the check over a planted split-brain and a clean pair — nothing on disk is touched."""
    print("RELEASE INTEGRITY DEMO — a hard check that engine.json versions match each module manifest.\n")
    clean_engine = {"packages": {"core": "1.2.0", "qa-review": "1.2.0"}}
    clean_mans = [{"id": "core", "version": "1.2.0"}, {"id": "qa-review", "version": "1.2.0"}]
    split_engine = {"packages": {"core": "1.2.0", "qa-review": "1.1.0"}}     # qa-review disagrees
    split_mans = [{"id": "core", "version": "1.2.0"}, {"id": "qa-review", "version": "1.2.0"}]
    clean = findings("hard", engine=clean_engine, manifests=clean_mans)
    split = findings("hard", engine=split_engine, manifests=split_mans)
    print(f"  clean (all agree): {len(clean)} findings")
    print(f"  split-brain (qa-review 1.1.0 in engine.json vs 1.2.0 in its manifest): {len(split)} finding(s)")
    for f in split:
        print(f"    - {f.get('message')}")
    ok = len(clean) == 0 and len(split) == 1
    if not ok:
        print("\nDEMO UNEXPECTED: the clean pair must pass and the split-brain must flag exactly one "
              f"finding — got clean={len(clean)}, split={len(split)}.", file=sys.stderr)
        return 1
    print("\nDEMO PASSED: agreeing versions pass; a disagreement is flagged for the merge.")
    return 0


def main(argv: list) -> int:
    if argv and argv[0] == "demo":
        return _demo()
    tier = os.environ.get("ENGINE_RULE_TIER", "hard")
    engine, mans = _fixture_state()
    return emit(findings(tier, engine=engine, manifests=mans))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
