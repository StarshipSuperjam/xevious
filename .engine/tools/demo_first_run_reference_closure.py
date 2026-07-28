#!/usr/bin/env python3
"""Operator-runnable demonstration of the first-run reference-closure check (issue #150).

Run it:  uv run --directory .engine -- python tools/demo_first_run_reference_closure.py

It builds two throwaway example projects (nothing real is touched) and runs the REAL check against each, so you
can SEE — without reading code — that the check does what it claims: it goes RED when a leftover file points at
setup code the engine removes when a project is first set up, and it stays SILENT once that setup is finished.
Vary it yourself: change the leftover file's name or what it points at and re-run; the message follows.

This demo imports only the check itself (never the first-run installer), so it travels safely and never becomes
the kind of leftover it exists to catch.
"""
from __future__ import annotations
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import first_run_reference_closure_check as frc  # noqa: E402

BANNER = "=" * 78


def _example_project(root: str, *, setup_still_present: bool) -> None:
    """A throwaway 'generated project': a manifest naming one setup file the engine removes, that setup file
    present-or-already-removed, and one LEFTOVER file that still points at it (`import setup_installer`)."""
    os.makedirs(os.path.join(root, ".engine", "provisioning"))
    os.makedirs(os.path.join(root, ".engine", "tools"))
    with open(os.path.join(root, ".engine", "provisioning", "first-run-assets.json"), "w", encoding="utf-8") as fh:
        json.dump({"files": [".engine/tools/setup_installer.py"], "directories": []}, fh)
    if setup_still_present:
        open(os.path.join(root, ".engine", "tools", "setup_installer.py"), "w", encoding="utf-8").close()
    with open(os.path.join(root, ".engine", "tools", "leftover_helper.py"), "w", encoding="utf-8") as fh:
        fh.write("import setup_installer\n")


def main() -> int:
    print(BANNER)
    print("What this checks: when a project is first set up from this template, the engine removes its own")
    print("setup files. If a file that STAYS behind still points at one of those removed files, the new")
    print("project's very first automated check would stop before it starts — with a programmer error its")
    print("owner cannot read. This check catches that before it can ship.")
    print(BANNER)

    print("\n[1] A project where a leftover file still points at the removed setup code. Expect: caught (RED).")
    print("-" * 78)
    with tempfile.TemporaryDirectory() as d:
        _example_project(d, setup_still_present=True)
        findings = frc.check(d)
    ok1 = len(findings) == 1
    print(f"   findings: {len(findings)}   caught the leftover? {ok1}")
    if findings:
        print("   what the project's owner would be told:")
        print("     " + findings[0]["message"])

    print("\n[2] The same project AFTER setup finished — the setup files are gone. Expect: nothing to do (SILENT).")
    print("-" * 78)
    with tempfile.TemporaryDirectory() as d:
        _example_project(d, setup_still_present=False)
        findings = frc.check(d)
    ok2 = findings == []
    print(f"   findings: {len(findings)}   stayed silent once setup was done? {ok2}")

    print("\n" + BANNER)
    print("In plain words: the check goes red and names the leftover file (so it can be fixed before any new")
    print("project breaks), and it does nothing once a project's setup is finished. It reads the list of")
    print("removed files as plain data and never loads the setup code itself.")
    ok = ok1 and ok2
    print(f"DEMO {'OK' if ok else 'FAILED'}")
    print(BANNER)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
