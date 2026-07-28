#!/usr/bin/env python3
"""Behavioral FALSIFICATION for issue #594 — a release's NEWLY-INTRODUCED wire seam is applied by that
release's OWN upgrade, because the version-sensitive tail runs as the FRESHLY-OVERLAID code (a fresh child
interpreter), not the pre-upgrade in-memory library.

The bug: `upgrade()` overlays the new release's `.engine/tools/*.py` (which includes `wiring.py`) onto disk,
but the running process keeps the `wiring`/`module_coherence` it imported at startup — so a wire seam a
release newly introduces (v0.3.0's codex-mcp/codex-hook) could never be applied by its own upgrade.

This demo is FAIL-THEN-PASS on the SAME fixture; the ONLY difference between the two arms is whether the tail
runs in a child process or in-process:
  * POSITIVE (the fix): the release introduces a synthetic seam `demo-echo`; the tail runs in a fresh CHILD
    of the overlaid code, so the new applier runs → the wire is applied (a marker file appears) and coherence
    is clean.
  * NEGATIVE CONTROL (the bug): force the tail IN-PROCESS (inject an opener). The stale in-memory `wiring`
    lacks `demo-echo`, so the wire is NOT applied and coherence hard-flags "declares a demo-echo wire that is
    not applied" — exactly #594. Revert the subprocess split and the positive arm regresses to this.

A synthetic seam is required for a real negative control: this engine already ships the codex seams, so the
in-process `wiring` would already know them. `demo-echo` is a seam the CURRENT wiring does not know, so the
in-process arm genuinely reproduces the stale-library gap.

It exercises the REAL overlay + wiring + coherence against a throwaway COPY of this engine, faking only the
network/PR (a practice run). It is a PERMANENT regression: its companion test
(`test_module_manager.TestUpgradeTailAndSafeCli.test_the_594_falsification_demo_passes`) runs it, so it
travels with the engine and keeps guarding this forever-relevant upgrade behaviour in every generated repo
(rather than retiring as construction-only evidence — a surviving test importing it is why it stays).
"""
from __future__ import annotations
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate            # noqa: E402
import module_manager as mm  # noqa: E402  (the real upgrade under test)

# Where the synthetic applier writes its marker — a `.json` path under `.engine/state/`, which `core`'s
# manifest already claims (`state: [".engine/state/*.json"]`), so it does not trip the ownership leg.
_MARKER_REL = ".engine/state/demo_echo_marker.json"
_DEMO_WIRE = {"type": "demo-echo", "name": "demo1", "path": _MARKER_REL}

# Appended to the RELEASE copy's wiring.py: teaches it the `demo-echo` seam the live engine lacks. Modelled
# on `permission` — `declared_wire_identity` returns None for it, so only the forward `is_applied` coherence
# leg matters (the exact shape of #594). Nothing here is committed; it lives only in the throwaway release.
_WIRING_PATCH = '''

# ---- demo-echo: a synthetic seam a release INTRODUCES (issue #594 falsification; never shipped) ----
def _demo_echo_apply(directive):
    _marker = os.path.join(validate.ROOT, directive["path"])
    os.makedirs(os.path.dirname(_marker), exist_ok=True)
    with open(_marker, "w", encoding="utf-8") as _fh:
        _fh.write('{"applied": true}\\n')
    return _ok(f"applied demo-echo -> {directive['path']}", _marker)


def _demo_echo_reverse(directive):
    _marker = os.path.join(validate.ROOT, directive["path"])
    if os.path.exists(_marker):
        os.remove(_marker)
    return _ok("reversed demo-echo", _marker)


_APPLIERS["demo-echo"] = _demo_echo_apply
_REVERSERS["demo-echo"] = _demo_echo_reverse
SEAMS = frozenset(_APPLIERS)

_demo_orig_is_applied = is_applied


def is_applied(directive):
    if isinstance(directive, dict) and directive.get("type") == "demo-echo":
        return os.path.exists(os.path.join(validate.ROOT, directive["path"]))
    return _demo_orig_is_applied(directive)
'''

_COPY_IGNORE = shutil.ignore_patterns(".venv", "__pycache__", "worktrees", "node_modules", "*.pyc", ".git")
# The engine surface a real coherent engine needs on disk for the child to boot and for `check_coherence` to
# pass: the whole `.engine`, the shared-file wiring targets (`.claude`, `.codex`, `.mcp.json`), and the floor
# sources. `.venv`/caches/worktrees are excluded (see _COPY_IGNORE).
_COPY_DIRS = (".engine", ".claude", ".codex", ".agents", ".github")
_COPY_FILES = (".mcp.json", ".gitignore", "CLAUDE.md", "AGENTS.md")


def _clone_engine(real_root: str, dest: str) -> str:
    """Copy this repo's real (coherent) engine surface into `dest` — a genuine engine the child can boot and
    coherence can pass, so the falsification isolates the wiring behaviour, not a broken fixture."""
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


def _make_release(real_root: str, dest: str) -> str:
    """A throwaway release tree = a clone whose wiring.py learns the `demo-echo` seam and whose `core`
    manifest declares one `demo-echo` wire. That is the whole difference a 'new release' introduces here."""
    _clone_engine(real_root, dest)
    with open(os.path.join(dest, ".engine", "tools", "wiring.py"), "a", encoding="utf-8") as fh:
        fh.write(_WIRING_PATCH)
    core_manifest = os.path.join(dest, ".engine", "modules", "core", "manifest.json")
    man = json.load(open(core_manifest, encoding="utf-8"))
    man["wires"] = list(man.get("wires") or []) + [_DEMO_WIRE]
    with open(core_manifest, "w", encoding="utf-8") as fh:
        json.dump(man, fh, indent=2)
    return dest


def _hard(findings) -> list:
    return [f for f in (findings or []) if f.get("severity") == "hard"]


def _demo_echo_finding(findings) -> bool:
    return any("demo-echo" in (f.get("message") or "") for f in _hard(findings))


def main() -> int:
    real_root = validate.ROOT   # capture the REAL repo before any redirect
    failures = []
    print("=" * 78)
    print("DEMO #594 — a release's newly-introduced wire seam applies via a fresh child of the overlaid code.")
    print("Same fixture, two arms; the only difference is child-process (fixed) vs in-process (the bug).")
    print("=" * 78)

    # ---- POSITIVE: the fix — the tail runs in a child of the overlaid code, so `demo-echo` applies ----
    with tempfile.TemporaryDirectory() as d:
        live = _clone_engine(real_root, os.path.join(d, "live"))
        release = _make_release(real_root, os.path.join(d, "release"))
        marker = os.path.join(live, _MARKER_REL)
        with mm._redirect_root(live):
            # release injected, NO opener/backup -> practice subprocess tail (runs the overlaid wiring)
            result = mm.upgrade(ref="v-demo", release_tree=release)
        applied = os.path.exists(marker)
        clean = not _hard(result.get("findings"))
        print(f"\n[POSITIVE — child of the overlaid code]")
        print(f"  new seam applied (marker written): {applied}")
        print(f"  coherence clean (no hard finding):  {clean}")
        if not applied:
            failures.append("POSITIVE: the release's new demo-echo wire was NOT applied by the child tail")
        if not clean:
            failures.append(f"POSITIVE: coherence hard-flagged after the child applied the wire: "
                            f"{[f.get('message') for f in _hard(result.get('findings'))][:2]}")

    # ---- NEGATIVE CONTROL: the bug — force the tail in-process, the stale wiring lacks the seam ----
    seen_pr = []
    def _fake_opener(branch, title, body):
        seen_pr.append(branch)
        return {"number": 0, "title": title}

    with tempfile.TemporaryDirectory() as d:
        live = _clone_engine(real_root, os.path.join(d, "live"))
        release = _make_release(real_root, os.path.join(d, "release"))
        marker = os.path.join(live, _MARKER_REL)
        with mm._redirect_root(live):
            # injecting an opener forces the IN-PROCESS tail (the pre-fix behaviour) — the stale in-memory
            # wiring of THIS process has no demo-echo applier.
            result = mm.upgrade(ref="v-demo", release_tree=release, opener=_fake_opener)
        applied = os.path.exists(marker)
        flagged = _demo_echo_finding(result.get("findings"))
        opened = bool(result.get("pr"))
        print(f"\n[NEGATIVE CONTROL — in-process, stale wiring (reproduces #594)]")
        print(f"  new seam NOT applied (marker absent): {not applied}")
        print(f"  coherence hard-flags demo-echo:        {flagged}")
        print(f"  no pull request opened (paused):       {not opened}")
        if applied:
            failures.append("NEGATIVE: the in-process tail applied demo-echo — the control did not reproduce "
                            "the bug (is the split still load-bearing?)")
        if not flagged:
            failures.append("NEGATIVE: coherence did NOT hard-flag the unapplied demo-echo wire")
        if opened:
            failures.append("NEGATIVE: a pull request was opened despite the coherence break")

    print("\n" + "=" * 78)
    if failures:
        print("DEMO #594 FAILED:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("DEMO #594 PASSED: the fresh-child tail applies a release's new wire seam and stays coherent; the "
          "in-process path reproduces #594 (unapplied wire + a hard coherence finding). The split is "
          "load-bearing.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
