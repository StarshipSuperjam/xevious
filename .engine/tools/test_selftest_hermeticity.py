#!/usr/bin/env python3
"""Hermeticity guards for the deployed self-test suite (engine-template #753).

#753: the git-unavailable fallback of the two ledger-directory resolvers (`memory.ledger.ledger_dir` and
`boot_alarm_ledger.ledger_dir`) doubled a `.engine`-rooted cwd into `.engine/.engine/...`, writing runtime
state (a capture lock, the standing-alarm ledger) INSIDE the real checkout whenever a test stubbed git out
while cwd was `<root>/.engine` (every tool launches via `uv run --directory .engine`). The fix peels a
trailing `.engine` STRICTLY in that fallback. These tests lock the fix, its scope (a git-confirmed root is
never peeled; the env override still wins), and a tree-cleanliness tripwire that no ignore rule can blind.
"""
from __future__ import annotations
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import boot_alarm_ledger    # noqa: E402
import quiet_call           # noqa: E402
import validate             # noqa: E402
from memory import ledger   # noqa: E402


class TestLedgerDirHermeticity(unittest.TestCase):
    """The #753 doubled-path fix on both ledger-dir resolvers, plus the falsification demo."""

    def _clean_env(self):
        # Exercise the git-unavailable FALLBACK, not the env override. patch.dict snapshots the environ and
        # restores it (including anything we pop or add) at cleanup, so nothing leaks to sibling modules.
        patch = mock.patch.dict(os.environ, {}, clear=False)
        patch.start()
        self.addCleanup(patch.stop)
        os.environ.pop("ENGINE_MEMORY_DIR", None)
        os.environ.pop("ENGINE_BOOT_CACHE_DIR", None)

    def test_memory_ledger_dir_does_not_double_a_dot_engine_cwd(self):
        self._clean_env()
        with tempfile.TemporaryDirectory() as root:
            # A ceiling stops `git rev-parse` from walking up to an ancestor repo of the system temp dir.
            os.environ["GIT_CEILING_DIRECTORIES"] = os.path.dirname(root)
            engine_dir = os.path.join(root, ".engine")
            os.makedirs(engine_dir)
            self.assertIsNone(ledger._git_common_root(engine_dir))  # precondition: fallback is under test
            self.assertEqual(ledger.ledger_dir(engine_dir), os.path.join(root, ".engine", "memory"))

    def test_boot_alarm_ledger_dir_does_not_double_a_dot_engine_cwd(self):
        self._clean_env()
        with tempfile.TemporaryDirectory() as root:
            os.environ["GIT_CEILING_DIRECTORIES"] = os.path.dirname(root)
            engine_dir = os.path.join(root, ".engine")
            os.makedirs(engine_dir)
            self.assertIsNone(boot_alarm_ledger._git_common_root(engine_dir))  # precondition
            self.assertEqual(boot_alarm_ledger.ledger_dir(engine_dir),
                             os.path.join(root, ".engine", "boot", ".cache"))

    def test_a_trailing_slash_cwd_still_peels(self):
        # `os.path.basename("<root>/.engine/")` is "" — the peel must normalize the path first, or a
        # trailing-separator cwd would slip through unpeeled and re-double. Mirrors `_git_common_root`.
        self._clean_env()
        with tempfile.TemporaryDirectory() as root:
            os.environ["GIT_CEILING_DIRECTORIES"] = os.path.dirname(root)
            os.makedirs(os.path.join(root, ".engine"))
            engine_dir_slash = os.path.join(root, ".engine") + os.sep
            self.assertIsNone(ledger._git_common_root(engine_dir_slash))  # precondition
            self.assertEqual(ledger.ledger_dir(engine_dir_slash), os.path.join(root, ".engine", "memory"))
            self.assertEqual(boot_alarm_ledger.ledger_dir(engine_dir_slash),
                             os.path.join(root, ".engine", "boot", ".cache"))

    def test_a_non_engine_cwd_fallback_is_left_alone(self):
        # The peel fires only for a trailing `.engine`; any other cwd is used verbatim (still cwd-relative,
        # but never doubled — the #753 symptom is specifically the `.engine/.engine` doubling).
        self._clean_env()
        with tempfile.TemporaryDirectory() as root:
            os.environ["GIT_CEILING_DIRECTORIES"] = os.path.dirname(root)
            self.assertIsNone(ledger._git_common_root(root))
            self.assertEqual(ledger.ledger_dir(root), os.path.join(root, ".engine", "memory"))

    def test_a_git_confirmed_root_is_never_peeled(self):
        # The peel is STRICTLY the git-unavailable branch. When `_git_common_root` CONFIRMS a clone root it is
        # used verbatim (joined with the subdir constant) and the cwd is irrelevant — even a root that itself
        # ends in `.engine` (a repo cloned into a `.engine` dir, where the store genuinely lives at
        # `<root>/.engine/memory`) is NOT peeled. This locks the fix's scope: were the peel applied outside
        # the `root is None` branch, a confirmed `.engine`-named root would be wrongly shortened and this fails.
        self._clean_env()
        confirmed = os.path.join(os.sep, "tmp", "weird", ".engine")
        peelable_cwd = os.path.join(os.sep, "elsewhere", ".engine")  # would be peeled in the fallback branch
        with mock.patch.object(ledger, "_git_common_root", return_value=confirmed):
            self.assertEqual(ledger.ledger_dir(peelable_cwd), os.path.join(confirmed, ledger.DATA_SUBDIR))
        with mock.patch.object(boot_alarm_ledger, "_git_common_root", return_value=confirmed):
            self.assertEqual(boot_alarm_ledger.ledger_dir(peelable_cwd),
                             os.path.join(confirmed, boot_alarm_ledger.CACHE_SUBDIR))

    def test_env_override_still_wins_over_the_fallback(self):
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.dict(os.environ, {"ENGINE_MEMORY_DIR": d, "ENGINE_BOOT_CACHE_DIR": d}):
                dot_engine = os.path.join(os.sep, "x", ".engine")
                self.assertEqual(ledger.ledger_dir(dot_engine), os.path.abspath(d))
                self.assertEqual(boot_alarm_ledger.ledger_dir(dot_engine), os.path.abspath(d))

    def test_the_753_falsification_demo_passes(self):
        # Runs the standalone reproducer end-to-end (fail-then-pass on the real resolvers). Importing it here
        # is also what keeps the demo alive for the census reference-closure (like the #594/#599 companions).
        import demo_753_ledger_dir_hermeticity as demo
        self.assertEqual(quiet_call.run(demo.main), 0)


class TestRealCheckoutStaysClean(unittest.TestCase):
    """A tree-cleanliness tripwire: no suite run may leave a doubled `.engine/.engine/` under the real
    checkout root. Checks PHYSICAL existence (not `git status`), so no ignore rule can blind it."""

    def test_no_nested_dot_engine_under_the_real_root(self):
        nested = os.path.join(validate.ROOT, ".engine", ".engine")
        self.assertFalse(
            os.path.isdir(nested),
            f"a doubled runtime tree exists at {nested} — a self-test wrote into the real checkout (#753)")


if __name__ == "__main__":
    unittest.main()
