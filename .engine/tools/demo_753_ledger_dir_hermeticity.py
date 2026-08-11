#!/usr/bin/env python3
"""Behavioral FALSIFICATION for engine-template #753 — the git-unavailable fallback of the two
ledger-directory resolvers must NOT double a `.engine`-rooted cwd into `.engine/.engine/...`.

The bug: every engine tool launches via `uv run --directory .engine`, so a self-test's working directory is
`<root>/.engine`. When a test stubs git out (so `_git_common_root` returns None), each `ledger_dir()` fell
back to treating that cwd AS the clone root and appended its own `.engine/...` subdir — producing
`<root>/.engine/.engine/memory/.capture.lock` and `<root>/.engine/.engine/boot/.cache/standing-alarms.json`
INSIDE the real checkout (#753; the #176 doubled-path regression class). The fix peels a trailing `.engine`
in that fallback so it names the true clone root.

FAIL-THEN-PASS, one hermetic fixture, no faking of the code under test:
  * POSITIVE (the fix): call the REAL `memory.ledger.ledger_dir` and `boot_alarm_ledger.ledger_dir` with
    cwd=`<tmp>/.engine` and git genuinely unavailable, and assert neither resolves to a doubled
    `.engine/.engine` path — they must land at `<tmp>/.engine/memory` and `<tmp>/.engine/boot/.cache`.
    Revert the peel and this arm regresses: the resolvers double and the assertions fail.
  * NEGATIVE CONTROL (the bug): reconstruct the pre-fix cwd-as-root join in-process to show the exact
    doubling the fix prevents — so the demo can only pass while genuinely exercising #753.

Hermetic by construction: a throwaway temp tree under the SYSTEM temp dir (so `git rev-parse` honestly finds
no repository), a git ceiling so it cannot walk up to an ancestor repo, and the two `ENGINE_*_DIR` overrides
cleared so the git-unavailable FALLBACK — not the env path — is what runs. Nothing real is touched; the demo
asserts on the resolved directory STRINGS and writes no ledger.

Run:  uv run --directory .engine --frozen -- python tools/demo_753_ledger_dir_hermeticity.py
Its companion test (`test_selftest_hermeticity.TestLedgerDirHermeticity.test_the_753_falsification_demo_passes`)
runs it, so it travels with the engine as a permanent regression guard in every generated repo.
"""
from __future__ import annotations
import os
import shutil
import sys
import tempfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from memory import ledger        # noqa: E402  (memory ledger-dir resolver under test)
import boot_alarm_ledger         # noqa: E402  (boot standing-alarm ledger-dir resolver under test)

_DOUBLED = os.path.join(".engine", ".engine")


def _doubles(path: str) -> bool:
    return _DOUBLED in path


def main() -> int:
    failures = []
    print("=" * 78)
    print("DEMO #753 — the git-unavailable ledger-dir fallback must not double `.engine` -> `.engine/.engine`.")
    print("=" * 78)

    root = tempfile.mkdtemp(prefix="demo753_")
    try:
        engine_dir = os.path.join(root, ".engine")
        os.makedirs(engine_dir, exist_ok=True)

        # Hermeticity: no override env active, and git genuinely unavailable at `engine_dir` (the temp tree
        # has no `.git`; the ceiling stops `git rev-parse` from walking up to an ancestor repo).
        with mock.patch.dict(os.environ, {"GIT_CEILING_DIRECTORIES": os.path.dirname(root)}, clear=False):
            os.environ.pop("ENGINE_MEMORY_DIR", None)
            os.environ.pop("ENGINE_BOOT_CACHE_DIR", None)

            # Precondition: the fallback IS the branch under test — else the demo would pass vacuously.
            mem_root = ledger._git_common_root(engine_dir)
            boot_root = boot_alarm_ledger._git_common_root(engine_dir)
            if mem_root is not None or boot_root is not None:
                failures.append(
                    f"precondition: git resolved a root under the temp tree (mem={mem_root!r}, "
                    f"boot={boot_root!r}); the git-unavailable fallback is not being exercised")
            else:
                mem_dir = ledger.ledger_dir(engine_dir)
                boot_dir = boot_alarm_ledger.ledger_dir(engine_dir)
                expected_mem = os.path.join(root, ".engine", "memory")
                expected_boot = os.path.join(root, ".engine", "boot", ".cache")

                print("\n[POSITIVE — the real resolvers, cwd=<tmp>/.engine, git unavailable]")
                print(f"  memory ledger dir : {mem_dir}")
                print(f"  boot   ledger dir : {boot_dir}")
                if _doubles(mem_dir):
                    failures.append(f"POSITIVE: memory ledger_dir doubled into `.engine/.engine`: {mem_dir}")
                if _doubles(boot_dir):
                    failures.append(f"POSITIVE: boot ledger_dir doubled into `.engine/.engine`: {boot_dir}")
                if mem_dir != expected_mem:
                    failures.append(f"POSITIVE: memory ledger_dir = {mem_dir}, expected {expected_mem}")
                if boot_dir != expected_boot:
                    failures.append(f"POSITIVE: boot ledger_dir = {boot_dir}, expected {expected_boot}")

                # NEGATIVE CONTROL — the pre-fix cwd-as-root join; it MUST double, or the demo is not
                # exercising #753 at all.
                pre_fix_mem = os.path.join(engine_dir, ledger.DATA_SUBDIR)
                pre_fix_boot = os.path.join(engine_dir, boot_alarm_ledger.CACHE_SUBDIR)
                print("\n[NEGATIVE CONTROL — the pre-fix cwd-as-root join (reproduces #753)]")
                print(f"  would write memory to : {pre_fix_mem}")
                print(f"  would write boot   to : {pre_fix_boot}")
                if not _doubles(pre_fix_mem) or not _doubles(pre_fix_boot):
                    failures.append(
                        "NEGATIVE CONTROL did not reproduce the doubling — the demo is not exercising #753")
    finally:
        shutil.rmtree(root, ignore_errors=True)

    print("\n" + "=" * 78)
    if failures:
        print("DEMO #753 FAILED:")
        for f in failures:
            print(f"  - {f}")
        print("=" * 78)
        return 1
    print("DEMO #753 PASSED — the fallback resolves to a single `.engine`; no doubling.")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(main())
