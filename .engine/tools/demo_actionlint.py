#!/usr/bin/env python3
"""Operator-runnable demo: the committed actionlint floor catches a broken workflow file, leaves a
valid one alone, and can only WARN — it never blocks your merge.

Run: uv run --directory .engine -- python tools/demo_actionlint.py
     (needs the open-source `actionlint` on your PATH — the same linter the workflow runs;
      install: https://github.com/rhysd/actionlint. Without it, the live-lint blocks are
      skipped and only the never-blocks guarantee is checked.)

This exercises the REAL linter the floor ships (`.github/workflows/actionlint.yml`), not a
re-implementation: it runs the real `actionlint` binary against throwaway workflow files, and it
reads the REAL required-check list (`protection_guard.REQUIRED_CHECKS`) to prove the lint is advisory.
Read the three blocks by eye:
  [1] a workflow with a real mistake (a job that waits on another job that does not exist) IS caught
      (the linter reports it; nonzero result),
  [2] a correct, ordinary workflow is left alone (nothing reported; zero result),
  [3] even when it finds something, the check is NOT in your required-check list, so it can only
      warn — your merge is never blocked.
"""
from __future__ import annotations
import os
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import protection_guard  # noqa: E402

# The job name the workflow declares (.github/workflows/actionlint.yml). The advisory guarantee is
# exactly: this name is NOT one of the branch ruleset's required checks.
WORKFLOW_JOB_NAME = "actionlint"

# A workflow with a real GitHub Actions mistake actionlint reliably catches: a job that `needs` a job
# that is not defined anywhere — the workflow would never run as written.
_BROKEN_WORKFLOW = """\
name: broken
on:
  pull_request:
    types: [opened]
permissions:
  contents: read
jobs:
  build:
    needs: [a-job-that-does-not-exist]
    runs-on: ubuntu-latest
    steps:
      - run: echo "this job waits on a job that was never defined"
"""

# A correct, ordinary workflow actionlint passes clean.
_CLEAN_WORKFLOW = """\
name: clean
on:
  pull_request:
    types: [opened]
permissions:
  contents: read
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - run: echo "all good"
"""


def _run_actionlint(workflow_text: str) -> tuple[int, str]:
    """Run the REAL actionlint over a throwaway workflow file laid out under .github/workflows/ (the
    layout actionlint expects). Returns (exit, output). Exit 0 = clean; nonzero = a finding (or an
    error). Never raises on a finding."""
    with tempfile.TemporaryDirectory() as d:
        wf_dir = os.path.join(d, ".github", "workflows")
        os.makedirs(wf_dir)
        path = os.path.join(wf_dir, "demo.yml")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(workflow_text)
        proc = subprocess.run(["actionlint", "-no-color", path],
                              capture_output=True, text=True)
    return proc.returncode, (proc.stdout + proc.stderr)


def _lint_broken_workflow() -> bool:
    """A workflow with a real mistake must be FOUND (nonzero)."""
    code, _out = _run_actionlint(_BROKEN_WORKFLOW)
    print(f"   linted a workflow with a real mistake -> linter exit {code} "
          f"({'FOUND it' if code != 0 else 'missed it'})")
    return code != 0


def _lint_clean_workflow() -> bool:
    """A correct workflow must come back clean (exit 0)."""
    code, out = _run_actionlint(_CLEAN_WORKFLOW)
    print(f"   linted a correct workflow -> linter exit {code} "
          f"({'clean' if code == 0 else 'unexpected finding'})")
    if code != 0:
        print(out)
    return code == 0


def _advisory_guarantee_holds() -> bool:
    """The check can only WARN: its job name is NOT in the required-check list."""
    required = protection_guard.REQUIRED_CHECKS
    is_advisory = WORKFLOW_JOB_NAME not in required
    print(f"   required checks that CAN block a merge: {required}")
    print(f"   the workflow lint ('{WORKFLOW_JOB_NAME}') is in that list? "
          f"{'YES — it would block' if not is_advisory else 'NO — it can only warn'}")
    return is_advisory


def main() -> int:
    print("=" * 78)
    print("The actionlint floor: it catches broken workflow files, and it only WARNS")
    print("=" * 78)

    have_actionlint = shutil.which("actionlint") is not None
    if have_actionlint:
        ver = subprocess.run(["actionlint", "--version"], capture_output=True, text=True)
        print(f"Using the real linter on your machine: actionlint {ver.stdout.strip().splitlines()[0] if ver.stdout.strip() else ver.stderr.strip()}")
    else:
        print("NOTE: actionlint is not installed, so the two live-lint blocks below are SKIPPED.")
        print("      Install it to watch the lint run: https://github.com/rhysd/actionlint")

    print("\n[1] A workflow with a real mistake (waits on a job that does not exist). Expect: FOUND.")
    print("-" * 78)
    caught = _lint_broken_workflow() if have_actionlint else None
    if caught is None:
        print("   (skipped — actionlint not installed)")

    print("\n[2] A correct, ordinary workflow. Expect: the linter leaves it alone.")
    print("-" * 78)
    clean = _lint_clean_workflow() if have_actionlint else None
    if clean is None:
        print("   (skipped — actionlint not installed)")

    print("\n[3] Even when it finds something, the lint can only WARN — never block your merge.")
    print("-" * 78)
    advisory = _advisory_guarantee_holds()

    # Success: the never-blocks guarantee always holds; the live-lint blocks pass when run.
    live_ok = (caught is True and clean is True) if have_actionlint else True
    ok = advisory and live_ok

    print("\n" + "=" * 78)
    print("In plain words: this check reads every workflow file on each pull request and points out")
    print("mistakes in how it is written, before a broken one can ship. A clean result means the file")
    print("is written correctly — not that the automation does what you intend. If it finds a mistake")
    print("it shows a WARNING — it cannot block your merge, because it is not one of the checks your")
    print("branch requires. You stay in control.")
    if have_actionlint:
        print("DEMO OK" if ok else "DEMO FAILED — unexpected outcome")
    else:
        print("DEMO OK (never-blocks guarantee verified; install actionlint to also see the live lint)"
              if ok else "DEMO FAILED — unexpected outcome")
    print("=" * 78)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
