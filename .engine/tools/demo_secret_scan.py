#!/usr/bin/env python3
"""Operator-runnable demo: the committed secret-scan floor catches a planted secret, leaves a
clean file alone, and can only WARN — it never blocks your merge.

Run: uv run --directory .engine -- python tools/demo_secret_scan.py
     (needs the open-source `gitleaks` on your PATH — the same scanner the workflow runs;
      install: https://github.com/gitleaks/gitleaks. Without it, the live-scan blocks are
      skipped and only the never-blocks guarantee is checked.)

This exercises the REAL scanner the floor ships (`.github/workflows/secret-scan.yml`), not a
re-implementation: it runs the real `gitleaks` binary against a throwaway directory, and it reads
the REAL required-check list (`protection_guard.REQUIRED_CHECKS`) to prove the scan is advisory.
Read the three blocks by eye:
  [1] a pretend password planted in a file IS found (the scanner reports it; nonzero result),
  [2] a normal file with no secrets is left alone (nothing reported; zero result),
  [3] even when it finds something, the check is NOT in your required-check list, so it can only
      warn — your merge is never blocked.
The fake token is assembled at run time so this demo's own source carries no secret-shaped text
(the workflow scans this file too)."""
from __future__ import annotations
import os
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import protection_guard  # noqa: E402

# The job name the workflow declares (.github/workflows/secret-scan.yml). The advisory guarantee
# is exactly: this name is NOT one of the branch ruleset's required checks.
WORKFLOW_JOB_NAME = "secret-scan"

# A GitHub-personal-access-token-SHAPED fake ("ghp_" + 36 high-entropy chars), assembled at run time
# from short harmless fragments so no secret-shaped literal sits in this file — the secret-scan
# workflow scans this file too, and a low-entropy/sequential string is allowlisted (it would not
# demonstrate a catch). The assembled value is not a real credential.
_FAKE_TOKEN = "gh" + "p_" + "".join([
    "x7Qm2T", "p9Lz4R", "w8KdN3", "vB6yH1", "jF5gS0", "aE2cDq",
])


def _run_gitleaks(target_dir: str) -> tuple[int, str]:
    """Run the REAL gitleaks over a directory exactly as the workflow does. Returns (exit, output).
    Exit 0 = clean; nonzero = a finding (or an error). Never raises on a finding."""
    proc = subprocess.run(
        ["gitleaks", "dir", target_dir, "--no-banner", "--redact", "--exit-code", "1"],
        capture_output=True, text=True)
    return proc.returncode, (proc.stdout + proc.stderr)


def _scan_planted_secret() -> bool:
    """Plant the fake token in a throwaway file and confirm the scanner FINDS it (nonzero)."""
    with tempfile.TemporaryDirectory() as d:
        with open(os.path.join(d, "config.txt"), "w", encoding="utf-8") as fh:
            fh.write(f"github_token = {_FAKE_TOKEN}\n")
        code, _out = _run_gitleaks(d)
    print(f"   planted a pretend password in a file -> scanner exit {code} "
          f"({'FOUND it' if code != 0 else 'missed it'})")
    return code != 0


def _scan_clean_file() -> bool:
    """A normal file with no secrets must come back clean (exit 0)."""
    with tempfile.TemporaryDirectory() as d:
        with open(os.path.join(d, "notes.txt"), "w", encoding="utf-8") as fh:
            fh.write("Remember to water the plants.\nBuy milk.\n")
        code, _out = _run_gitleaks(d)
    print(f"   scanned a normal file with no passwords -> scanner exit {code} "
          f"({'clean' if code == 0 else 'unexpected finding'})")
    return code == 0


def _advisory_guarantee_holds() -> bool:
    """The check can only WARN: its job name is NOT in the required-check list."""
    required = protection_guard.REQUIRED_CHECKS
    is_advisory = WORKFLOW_JOB_NAME not in required
    print(f"   required checks that CAN block a merge: {required}")
    print(f"   the secret scan ('{WORKFLOW_JOB_NAME}') is in that list? "
          f"{'YES — it would block' if not is_advisory else 'NO — it can only warn'}")
    return is_advisory


def main() -> int:
    print("=" * 78)
    print("The secret-scan floor: it catches planted secrets, and it only WARNS")
    print("=" * 78)

    have_gitleaks = shutil.which("gitleaks") is not None
    if have_gitleaks:
        ver = subprocess.run(["gitleaks", "version"], capture_output=True, text=True)
        print(f"Using the real scanner on your machine: gitleaks {ver.stdout.strip() or ver.stderr.strip()}")
    else:
        print("NOTE: gitleaks is not installed, so the two live-scan blocks below are SKIPPED.")
        print("      Install it to watch the scan run: https://github.com/gitleaks/gitleaks")

    print("\n[1] A pretend password is planted in a file. Expect: the scanner FINDS it.")
    print("-" * 78)
    caught = _scan_planted_secret() if have_gitleaks else None
    if caught is None:
        print("   (skipped — gitleaks not installed)")

    print("\n[2] A normal file with no passwords. Expect: the scanner leaves it alone.")
    print("-" * 78)
    clean = _scan_clean_file() if have_gitleaks else None
    if clean is None:
        print("   (skipped — gitleaks not installed)")

    print("\n[3] Even when it finds something, the scan can only WARN — never block your merge.")
    print("-" * 78)
    advisory = _advisory_guarantee_holds()

    # Success: the never-blocks guarantee always holds; the live-scan blocks pass when run.
    live_ok = (caught is True and clean is True) if have_gitleaks else True
    ok = advisory and live_ok

    print("\n" + "=" * 78)
    print("In plain words: this check scans every pull request for passwords or keys left in the")
    print("code by accident. If it finds one it shows a WARNING — it cannot block your merge,")
    print("because it is not one of the checks your branch requires. You stay in control.")
    if have_gitleaks:
        print("DEMO OK" if ok else "DEMO FAILED — unexpected outcome")
    else:
        print("DEMO OK (never-blocks guarantee verified; install gitleaks to also see the live scan)"
              if ok else "DEMO FAILED — unexpected outcome")
    print("=" * 78)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
