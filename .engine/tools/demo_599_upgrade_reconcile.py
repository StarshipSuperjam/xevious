#!/usr/bin/env python3
"""Behavioral FALSIFICATION for issue #599 — an engine update RECONCILES a deployed tree to the release:
it removes an engine file the release renamed away, so a stale orphan does not linger and break the engine's
own checks. The copy-only overlay it replaces never deleted anything, which is how the un-prefixed `.claude/
agents/*.md` orphans survived the `engine-*` rename and tripped 20 provider-parity findings (issue #599).

FAIL-THEN-PASS on the SAME fixture; the only difference between the two arms is whether the reconcile's DELETE
leg runs:
  * POSITIVE (the fix): the real reconcile runs — it removes the renamed agent's OLD path, so the tree is
    provider-parity clean (its Codex render has no orphaned Claude twin), and the update opens for review.
  * NEGATIVE CONTROL (the bug): the delete leg is disabled (deliver-only) — the old agent path SURVIVES with
    no `.codex/` counterpart, and `provider-parity` (a hard CI check) flags exactly the #599 asymmetry.

The orphan is a real engine agent renamed to an OLD name in the deployed clone (with no `.codex/` twin) that
the release no longer provides — the exact shape of #599's rename orphans. It exercises the REAL overlay +
reconcile + coherence against a throwaway COPY of this engine, faking only the network/PR. It is a PERMANENT
regression: its companion test (`test_module_manager.TestUpgradeReconcile...the_599_falsification_demo_passes`)
runs it, so it travels with the engine and guards this forever-relevant upgrade behaviour in every generated
repo (rather than retiring as construction-only evidence).
"""
from __future__ import annotations
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate                 # noqa: E402
import module_manager as mm     # noqa: E402  (the real upgrade + reconcile under test)
import provider_parity_check    # noqa: E402  (the hard CI check the orphan trips — run via findings(root=))

# A real engine agent renamed to an OLD name in the deployed clone, with NO `.codex/` twin — the #599 shape.
_ORPHAN_AGENT_REL = ".claude/agents/engine-legacy-orphan.md"

_COPY_IGNORE = shutil.ignore_patterns(".venv", "__pycache__", "worktrees", "node_modules", "*.pyc", ".git")
_COPY_DIRS = (".engine", ".claude", ".codex", ".agents", ".github")
_COPY_FILES = (".mcp.json", ".gitignore", "CLAUDE.md", "AGENTS.md")


def _clone_engine(real_root: str, dest: str) -> str:
    """Copy this repo's real (coherent) engine surface into `dest` — a genuine engine the child can boot and
    the structural gate can pass, so the falsification isolates the reconcile behaviour, not a broken fixture."""
    os.makedirs(dest, exist_ok=True)
    for rel in _COPY_DIRS:
        src = os.path.join(real_root, rel)
        if os.path.isdir(src):
            shutil.copytree(src, os.path.join(dest, rel), ignore=_COPY_IGNORE, symlinks=True)
    for rel in _COPY_FILES:
        src = os.path.join(real_root, rel)
        if os.path.isfile(src):
            os.makedirs(os.path.dirname(os.path.join(dest, rel)) or dest, exist_ok=True)
            shutil.copy2(src, os.path.join(dest, rel))
    return dest


def _plant_rename_orphan(live: str) -> None:
    """In the DEPLOYED clone, plant a renamed-away engine agent: an old-named `.claude/agents/*.md` with NO
    `.codex/` twin, CLAIMED by a module's manifest so the reconcile owns it (in `old_owned`) — exactly the
    rename orphan the release no longer ships. The release clone is untouched, so its manifests do NOT provide
    this path (it is not in the KEEP set), and the reconcile deletes it."""
    with open(os.path.join(live, _ORPHAN_AGENT_REL), "w", encoding="utf-8") as fh:
        fh.write("# a real engine agent renamed away — the old path, orphaned (issue #599)\n")
    # Claim it in the deployed clone's audit-library manifest, so old_owned includes it (a literal provides
    # entry, exactly like every real engine agent — never a wildcard).
    man_path = os.path.join(live, ".engine", "modules", "audit-library", "manifest.json")
    with open(man_path, encoding="utf-8") as fh:
        man = json.load(fh)
    man["provides"]["agent"] = list(man["provides"].get("agent") or []) + [_ORPHAN_AGENT_REL]
    with open(man_path, "w", encoding="utf-8") as fh:
        json.dump(man, fh, indent=2)


def _orphan_parity_finding(root: str) -> bool:
    """True iff provider-parity hard-flags the orphaned agent at `root` (its Claude side with no Codex twin)."""
    return any(f.get("severity") == "hard" and "engine-legacy-orphan" in (f.get("message") or "")
               for f in (provider_parity_check.findings("hard", root=root) or []))


def _deliver_only(release_tree, candidates, old_owned, old_by_id):
    """The NEGATIVE-CONTROL reconcile: the DELIVER leg only, no DELETE — reproduces the copy-only overlay that
    left #599's rename orphans in place."""
    delivered = mm._deliver_synced(release_tree, candidates, project_retire=True)
    fixtures = sorted(r for r in delivered
                      if any(r == ns or r.startswith(ns + "/") for ns in mm.module_coherence.FIXTURE_PATHS))
    return fixtures, {"engine": [], "suspect": [], "left_in_place": []}


def main() -> int:
    real_root = validate.ROOT   # capture the REAL repo before any redirect
    failures = []
    print("=" * 78)
    print("DEMO #599 — an engine update reconciles a deployed tree: it REMOVES the file a release renamed away,")
    print("so a stale orphan cannot linger and break the engine's own checks. Same fixture, two arms; the only")
    print("difference is whether the reconcile's delete leg runs.")
    print("=" * 78)

    # ---- POSITIVE: the real reconcile deletes the rename orphan; the tree stays provider-parity clean ----
    with tempfile.TemporaryDirectory() as d:
        live = _clone_engine(real_root, os.path.join(d, "live"))
        release = _clone_engine(real_root, os.path.join(d, "release"))
        _plant_rename_orphan(live)
        with mm._redirect_root(live):
            # release injected, NO opener/backup -> practice subprocess tail: the REAL reconcile + structural gate.
            result = mm.upgrade(ref="v-demo", release_tree=release)
        orphan_gone = not os.path.exists(os.path.join(live, _ORPHAN_AGENT_REL))
        parity_clean = not _orphan_parity_finding(live)
        gate_clean = not [f for f in (result.get("findings") or []) if f.get("severity") == "hard"]
        print("\n[POSITIVE — the real reconcile runs]")
        print(f"  rename orphan removed (old path gone):        {orphan_gone}")
        print(f"  provider-parity clean (no orphaned twin):     {parity_clean}")
        print(f"  structural gate clean (no hard finding):      {gate_clean}")
        if not orphan_gone:
            failures.append("POSITIVE: the reconcile did NOT remove the renamed agent's old path")
        if not parity_clean:
            failures.append("POSITIVE: provider-parity still flagged the orphan after the reconcile")
        if not gate_clean:
            failures.append("POSITIVE: the structural gate hard-flagged the reconciled tree")

    # ---- NEGATIVE CONTROL: disable the delete leg — the orphan survives and provider-parity flags it ----
    seen_pr = []
    with tempfile.TemporaryDirectory() as d:
        live = _clone_engine(real_root, os.path.join(d, "live"))
        release = _clone_engine(real_root, os.path.join(d, "release"))
        _plant_rename_orphan(live)
        original = mm._reconcile_surface
        mm._reconcile_surface = _deliver_only     # deliver-only: the copy-only overlay that leaves orphans
        try:
            with mm._redirect_root(live):
                # inject an opener -> the IN-PROCESS tail (fixture-safe coherence gate, which does not see
                # a .claude/agents orphan) -> the update would OPEN despite the orphan a CI check will catch.
                result = mm.upgrade(ref="v-demo", release_tree=release,
                                    opener=lambda **k: seen_pr.append(k.get("branch")) or {"number": 0},
                                    backup=lambda *a, **k: {"ok": 1})
            orphan_survives = os.path.exists(os.path.join(live, _ORPHAN_AGENT_REL))
            parity_flags = _orphan_parity_finding(live)
        finally:
            mm._reconcile_surface = original
        print("\n[NEGATIVE CONTROL — delete leg disabled (the copy-only overlay)]")
        print(f"  rename orphan SURVIVES (old path present):    {orphan_survives}")
        print(f"  provider-parity HARD-flags the orphan:        {parity_flags}")
        if not orphan_survives:
            failures.append("NEGATIVE: the orphan did not survive — the control did not reproduce the bug")
        if not parity_flags:
            failures.append("NEGATIVE: provider-parity did NOT flag the surviving orphan (weak falsification)")

    print("\n" + "=" * 78)
    if failures:
        print("DEMO #599 FAILED:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("DEMO #599 PASSED: the reconcile removes a renamed-away engine file so the tree stays consistent and "
          "provider-parity clean; the copy-only overlay (delete leg disabled) leaves the orphan, which a hard "
          "CI check catches. The delete leg is load-bearing.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
