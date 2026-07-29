#!/usr/bin/env python3
"""Greenfield-intake detector — the OFFLINE, READ-ONLY signal behind boot's first-engagement nudge (#553).

When a project has an Engine but has not yet described what to build, boot offers the operator the
`engine-design` intake — the structured, checked way to write down a product description — so a non-engineer
DISCOVERS the intake rather than having to already know it exists. This module is the detector boot relays;
boot computes no new state and only OFFERS (it never runs the intake).

Two guards keep the offer honest and non-naggy, and are the whole logic:

1. **The intake must actually be installed.** `engine-design` is owned by the OPTIONAL product-design module,
   so on an engine where that module was opted out the command does not exist — offering it would be a
   dead-end. The detector fires only when the intake's runbook (`.engine/operations/product-intake.md`, which
   the module provides and nothing else does) is present. Runtime-agnostic: it keys on the shared runbook, not
   a per-runtime skill file, so it holds for Claude Code and Codex alike.

2. **The offer self-resolves the moment the intake is used.** The intake writes `docs/spec/index.md` as the
   first thing it authors, so once that anchor exists the operator has found and started the intake — the
   detector returns None and never nags a project that is mid-authoring or already has a description. It fires
   only for a project with no `docs/spec/` description at all — the true greenfield state.

A project that deliberately wants no formal spec (a small script) would otherwise see the offer forever; that
is what the ledger's retire path is for (`boot_alarm_ledger`, class `greenfield_intake`) — the operator can
say "I'm not describing a spec" and boot stops offering, exactly as with the leftover-license offer.

The fingerprint is a constant (`"greenfield"`): the greenfield state is binary, so a stable value lets boot's
anti-habituation ledger collapse the offer to a terse reminder after the first full render, instead of
re-relaying it in full every session.

READ-ONLY and OFFLINE: it only checks whether two files exist; it never reads their contents, never writes,
never touches the network, and never raises (any error degrades to None — no offer — so it can never break
boot). It imports NO product_design code (a core detector must not depend on an optional module).

Contract: `detect_greenfield(cwd=None) -> dict | None` — a truthy `{"greenfield": True, "fingerprint": ...}`
when the nudge should be offered, else None. A `demo` subcommand runs a falsifiable self-check.
"""
from __future__ import annotations
import json
import os
import sys

# A pure leaf: the repo root computed from __file__, no sibling import. .engine/tools/greenfield_intake.py
# → up three (tools → .engine → root). This reads the booting worktree's tree (not, like license_health, the
# resolved main checkout): the anchors it checks are tracked files, so every worktree sees a consistent
# picture — the one benign edge is booting an OLD worktree branched before the spec was first authored, which
# re-offers until merged; harmless (a terse offer, self-corrects) and not worth a git resolution here.
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # .engine/tools for the sibling import below
import repo_identity  # noqa: E402  (is_home_repo — the home-repo identity seam; #323)

# The product-design intake runbook — present iff the optional product-design module is installed, so the
# `engine-design` command it drives actually exists. The module provides this path and nothing else does.
_INTAKE_REL = os.path.join(".engine", "operations", "product-intake.md")

# The intake writes this first; its presence means the intake has already been used → not greenfield.
_INDEX_REL = os.path.join("docs", "spec", "index.md")

# The greenfield state is binary, so the collapse fingerprint is a constant — a stable value across sessions
# lets the anti-habituation ledger collapse the offer to terse instead of re-relaying it in full.
_FINGERPRINT = "greenfield"


def detect_greenfield(cwd: "str | None" = None) -> "dict | None":
    """Offline, read-only. Returns `{"greenfield": True, "fingerprint": "greenfield"}` when the project has the
    intake installed but no `docs/spec/` description yet (so the first-engagement nudge should be offered),
    else None (the intake isn't installed, or a description already exists). Never raises."""
    root = cwd if cwd is not None else _ROOT
    try:
        if not os.path.isfile(os.path.join(root, _INTAKE_REL)):
            return None  # the intake isn't installed — never offer a command that doesn't exist
        # The engine's own home repo — its git origin equals its recorded home_repository. There a missing
        # product spec is legitimate (the repo builds the engine, not a product), so the nudge no-ops, exactly
        # as the leftover-license offer does. Fails TOWARD home when the origin can't be read, so an
        # unplaceable repo stays quiet; boot only ever offers, never acts.
        if repo_identity.is_home_repo(root):
            return None
        if os.path.isfile(os.path.join(root, _INDEX_REL)):
            return None  # the intake has already been run — self-resolved, never nag mid-authoring
        return {"greenfield": True, "fingerprint": _FINGERPRINT}
    except Exception:  # noqa: BLE001 — a detector fault degrades to "no offer", never breaks boot
        return None


def _demo() -> int:
    """Prove the detector on throwaway trees: it stays silent when the intake isn't installed; fires when the
    intake is installed but no description exists; and self-resolves the moment `docs/spec/index.md` exists.
    RETURNS NON-ZERO on any mismatch (the falsification can fail). Mutation-free — every case runs against a
    throwaway temp root, so the real tree is never touched."""
    import shutil
    import subprocess
    import tempfile

    HOME = "StarshipSuperjam/engine-template"

    def _seed(files: dict, *, origin: str) -> str:
        # The home-repo carve-out now keys on git origin == recorded home (repo_identity.is_home_repo), so each
        # case is a throwaway git checkout with a set origin and a recorded home — a real falsification of the
        # origin-keyed detector, not a marker string.
        d = tempfile.mkdtemp(prefix="engine-greenfield-demo-")
        for rel, body in files.items():
            path = os.path.join(d, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(body)
        os.makedirs(os.path.join(d, ".engine"), exist_ok=True)
        with open(os.path.join(d, ".engine", "engine.json"), "w", encoding="utf-8") as fh:
            json.dump({"home_repository": HOME}, fh)
        subprocess.run(["git", "-C", d, "init", "-q"], capture_output=True, check=False)
        if origin:
            subprocess.run(["git", "-C", d, "remote", "add", "origin", origin], capture_output=True, check=False)
        return d

    product = "https://github.com/adopter/their-product.git"   # origin != home -> a deployed repo
    home = f"https://github.com/{HOME}.git"                     # origin == home -> the engine's own home
    cases = [
        ("no-intake-installed", {}, product, lambda r: r is None),
        ("installed-and-greenfield", {_INTAKE_REL: "x\n"}, product, lambda r: r is not None
         and r.get("greenfield") is True and r.get("fingerprint") == _FINGERPRINT),
        ("installed-with-a-description", {_INTAKE_REL: "x\n", _INDEX_REL: "x\n"}, product, lambda r: r is None),
        ("engine-home-repo", {_INTAKE_REL: "x\n"}, home, lambda r: r is None),
    ]
    failures = []
    for label, rels, origin, ok in cases:
        root = _seed(rels, origin=origin)
        try:
            result = detect_greenfield(root)
        finally:
            shutil.rmtree(root, ignore_errors=True)
        if not ok(result):
            failures.append(f"{label}: invariant broken, got {json.dumps(result)}")

    if failures:
        print("DEMO FAILED — the greenfield-intake detector broke an invariant:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("DEMO PASSED — the greenfield-intake detector stays silent when the intake isn't installed, offers "
          "the nudge in a deployed repo (origin != home) with the intake installed but no description, "
          "self-resolves once a description is started, and no-ops in the engine's own home repo (origin == home).")
    return 0


def main(argv: list) -> int:
    if argv and argv[0] == "demo":
        return _demo()
    # Default: print the live detection as JSON — a debug view of what boot would relay.
    print(json.dumps(detect_greenfield()))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
