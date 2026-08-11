#!/usr/bin/env python3
"""Regression coverage for hooks_path_health (issues #707, #708; part of #690).

Exercises BOTH paths the acceptance calls for: detection (a set-and-missing core.hooksPath fires; unset or a
value pointing at a real directory stays quiet) and repair (removal-only, conservative-complete, lossless).
Throwaway `git init` repos in a TemporaryDirectory, git identity injected per-repo, CLI driven in-process.
"""
from __future__ import annotations

import contextlib
import io
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hooks_path_health as hp  # noqa: E402


def _git(root: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", root, *args], capture_output=True, text=True, check=False)


def _repo(tmp: str, name: str, *, worktree_config: bool = True) -> str:
    """A throwaway committed git checkout; `extensions.worktreeConfig` on by default (the target repo's state,
    and what makes `--worktree` a real, separate scope)."""
    root = os.path.join(tmp, name)
    os.makedirs(root, exist_ok=True)
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    if worktree_config:
        _git(root, "config", "extensions.worktreeConfig", "true")
    _git(root, "commit", "-qm", "seed", "--allow-empty")
    return root


def _set(root: str, value: str, *, scope: str = "local") -> None:
    _git(root, "config", f"--{scope}", "core.hooksPath", value)


def _get(root: str, *, scope: str = "local") -> str | None:
    out = _git(root, "config", f"--{scope}", "--get", "core.hooksPath")
    return out.stdout.rstrip("\n") if out.returncode == 0 else None


def _worktree(main: str, name: str) -> str:
    path = os.path.join(os.path.dirname(main), name)
    _git(main, "worktree", "add", "-q", path, "-b", f"wt-{name}", "HEAD")
    return path


class TestDetect(unittest.TestCase):
    def test_unset_is_healthy(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(hp.detect_broken_hooks_path(cwd=_repo(tmp, "r")))

    def test_empty_value_is_healthy(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = _repo(tmp, "r")
            _set(r, "")
            self.assertIsNone(hp.detect_broken_hooks_path(cwd=r))

    def test_absolute_existing_is_healthy(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = _repo(tmp, "r")
            good = os.path.join(tmp, "good-hooks")
            os.makedirs(good)
            _set(r, good)
            self.assertIsNone(hp.detect_broken_hooks_path(cwd=r))

    def test_absolute_missing_fires_fixable(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = _repo(tmp, "r")
            _set(r, os.path.join(tmp, "gone"))
            d = hp.detect_broken_hooks_path(cwd=r)
            self.assertIsNotNone(d)
            self.assertEqual(d["plan_kind"], "fixable")
            self.assertTrue(d["local_broken"] and d["local_absolute"])
            self.assertFalse(d["local_relative"])

    def test_relative_existing_resolves_against_toplevel(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = _repo(tmp, "r")
            os.makedirs(os.path.join(r, "myhooks"))
            _set(r, "myhooks")  # relative -> resolves against the worktree top, where it exists
            self.assertIsNone(hp.detect_broken_hooks_path(cwd=r))

    def test_relative_missing_fires_manual(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = _repo(tmp, "r")
            _set(r, "no-such-hooks")
            d = hp.detect_broken_hooks_path(cwd=r)
            self.assertIsNotNone(d)
            self.assertTrue(d["local_relative"])
            self.assertEqual(d["plan_kind"], "manual")  # a relative shared value is never auto-unset

    def test_tilde_missing_fires(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = os.path.join(tmp, "home")
            os.makedirs(home)
            r = _repo(tmp, "r")
            _set(r, "~/definitely-not-here")
            with mock.patch.dict(os.environ, {"HOME": home}):
                self.assertIsNotNone(hp.detect_broken_hooks_path(cwd=r))

    def test_worktree_override_missing_fires(self):
        with tempfile.TemporaryDirectory() as tmp:
            main = _repo(tmp, "main")
            wt = _worktree(main, "wt")
            _git(wt, "config", "--worktree", "core.hooksPath", os.path.join(tmp, "gone"))
            d = hp.detect_broken_hooks_path(cwd=wt)
            self.assertIsNotNone(d)
            self.assertTrue(d["worktree_broken"])
            self.assertEqual(d["effective_scope"], "worktree")
            self.assertEqual(d["plan_kind"], "fixable")

    def test_broken_shared_fires_even_when_worktree_override_is_valid(self):
        # #707/#690: latent shared breakage that would infect new worktrees must be caught from an existing
        # worktree whose OWN override is valid — not discovered only after a new worktree is born broken.
        with tempfile.TemporaryDirectory() as tmp:
            main = _repo(tmp, "main")
            _set(main, os.path.join(tmp, "gone-shared"))  # shared broken (absolute)
            wt = _worktree(main, "wt")
            good = os.path.join(tmp, "good")
            os.makedirs(good)
            _git(wt, "config", "--worktree", "core.hooksPath", good)  # valid override masks the shared value
            d = hp.detect_broken_hooks_path(cwd=wt)
            self.assertIsNotNone(d)
            self.assertTrue(d["local_broken"] and d["local_absolute"])
            self.assertEqual(d["plan_kind"], "fixable")

    def test_non_repo_is_quiet(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(hp.detect_broken_hooks_path(cwd=tmp))  # not a git repo -> fail-soft None

    def test_tilde_existing_is_healthy(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = os.path.join(tmp, "home")
            os.makedirs(os.path.join(home, "myhooks"))
            r = _repo(tmp, "r")
            _set(r, "~/myhooks")
            with mock.patch.dict(os.environ, {"HOME": home}):
                self.assertIsNone(hp.detect_broken_hooks_path(cwd=r))  # ~ expands to an existing dir -> healthy

    def test_global_missing_is_needs_manual(self):
        # a broken value in GLOBAL config the removal-only repair cannot address -> fires as manual. Isolate the
        # global/system config to temp files so the developer's real ~/.gitconfig is never read or written.
        with tempfile.TemporaryDirectory() as tmp:
            r = _repo(tmp, "r")
            gcfg = os.path.join(tmp, "gitconfig-global")
            with mock.patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": gcfg, "GIT_CONFIG_SYSTEM": os.devnull}):
                subprocess.run(["git", "config", "--global", "core.hooksPath", os.path.join(tmp, "gone")],
                               capture_output=True, check=False)
                d = hp.detect_broken_hooks_path(cwd=r)
                self.assertIsNotNone(d)
                self.assertTrue(d["external_broken"])
                self.assertEqual(d["effective_scope"], "external")
                self.assertEqual(d["plan_kind"], "manual")
                self.assertEqual(hp.repair(cwd=r, apply=True)["status"], "needs-manual")  # never touches global


class TestRepair(unittest.TestCase):
    def test_dry_run_mutates_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = _repo(tmp, "r")
            broken = os.path.join(tmp, "gone")
            _set(r, broken)
            res = hp.repair(cwd=r, apply=False)
            self.assertEqual(res["status"], "fixable")
            self.assertFalse(res["applied"])
            self.assertEqual(_get(r), broken)  # still set

    def test_apply_unsets_shared_absolute(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = _repo(tmp, "r")
            _set(r, os.path.join(tmp, "gone"))
            res = hp.repair(cwd=r, apply=True)
            self.assertEqual(res["status"], "fixed")
            self.assertIn("unset-local", res["did"])
            self.assertIsNone(_get(r))
            self.assertIsNone(hp.detect_broken_hooks_path(cwd=r))

    def test_apply_unsets_only_worktree_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            main = _repo(tmp, "main")
            wt = _worktree(main, "wt")
            _git(wt, "config", "--worktree", "core.hooksPath", os.path.join(tmp, "gone"))
            res = hp.repair(cwd=wt, apply=True)
            self.assertEqual(res["status"], "fixed")
            self.assertEqual(res["did"], ["unset-worktree"])
            self.assertIsNone(_get(wt, scope="worktree"))
            self.assertIsNone(_get(main))  # shared config untouched

    def test_relative_shared_is_needs_manual_no_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = _repo(tmp, "r")
            _set(r, "rel-hooks")  # relative -> could be valid in a peer -> never auto-unset
            res = hp.repair(cwd=r, apply=True)
            self.assertEqual(res["status"], "needs-manual")
            self.assertFalse(res["applied"])
            self.assertEqual(_get(r), "rel-hooks")

    def test_refuses_to_unset_a_valid_value(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = _repo(tmp, "r")
            good = os.path.join(tmp, "good")
            os.makedirs(good)
            _set(r, good)
            res = hp.repair(cwd=r, apply=True)
            self.assertEqual(res["status"], "healthy")
            self.assertFalse(res["applied"])
            self.assertEqual(_get(r), good)  # a working hooks path is never removed

    def test_apply_time_recheck_leaves_a_reappeared_dir(self):
        # TOCTOU guard: the resolved dir exists by apply time -> the value is NOT unset (the one path by which
        # this repair could disable a WORKING hook is closed).
        with tempfile.TemporaryDirectory() as tmp:
            r = _repo(tmp, "r")
            target = os.path.join(tmp, "reappears")
            _set(r, target)
            self.assertIsNotNone(hp.detect_broken_hooks_path(cwd=r))  # broken now
            os.makedirs(target)                                       # dir reappears before apply
            res = hp.repair(cwd=r, apply=True)
            self.assertEqual(res["status"], "healthy")
            self.assertEqual(_get(r), target)  # untouched

    def test_peer_worktree_overrides_are_not_swept(self):
        with tempfile.TemporaryDirectory() as tmp:
            main = _repo(tmp, "main")
            a = _worktree(main, "a")
            b = _worktree(main, "b")
            _git(a, "config", "--worktree", "core.hooksPath", os.path.join(tmp, "gone-a"))
            _git(b, "config", "--worktree", "core.hooksPath", os.path.join(tmp, "gone-b"))
            hp.repair(cwd=a, apply=True)
            self.assertIsNone(_get(a, scope="worktree"))                       # A fixed
            self.assertEqual(_get(b, scope="worktree"), os.path.join(tmp, "gone-b"))  # B untouched

    def test_new_worktree_inherits_clean_after_shared_repair(self):
        with tempfile.TemporaryDirectory() as tmp:
            main = _repo(tmp, "main")
            _set(main, os.path.join(tmp, "gone-shared"))  # absolute-missing shared value
            hp.repair(cwd=main, apply=True)               # unsets shared
            fresh = _worktree(main, "fresh")              # a worktree created AFTER the repair
            self.assertIsNone(hp.detect_broken_hooks_path(cwd=fresh))

    def test_existing_peer_self_heals_on_its_own_boot(self):
        # #707 "existing worktrees resolve correctly" — each peer's OWN broken override is caught (and fixable)
        # when detection runs in THAT worktree, i.e. on its own next boot.
        with tempfile.TemporaryDirectory() as tmp:
            main = _repo(tmp, "main")
            peer = _worktree(main, "peer")
            _git(peer, "config", "--worktree", "core.hooksPath", os.path.join(tmp, "gone"))
            self.assertIsNotNone(hp.detect_broken_hooks_path(cwd=peer))
            self.assertEqual(hp.repair(cwd=peer, apply=True)["status"], "fixed")

    def test_unset_of_absent_key_returns_exit_5(self):
        # git returns 5 for unsetting an already-absent key (a lost race under concurrent worktrees); repair
        # treats rc in (0, 5) as done, so this exit-code contract is what backs that.
        with tempfile.TemporaryDirectory() as tmp:
            r = _repo(tmp, "r")
            rc = hp._status(["git", "-C", r, "config", "--local", "--unset", "core.hooksPath"])
            self.assertEqual(rc, 5)

    def test_mixed_worktree_and_relative_shared_reports_needs_manual(self):
        # A worktree with its OWN broken absolute override AND a broken RELATIVE shared value: the auto-repair
        # clears the override (correct, safe) but the residual relative-shared value is still broken -> the
        # repair must report needs-manual (a hook is still disabled), never a false "fixed".
        with tempfile.TemporaryDirectory() as tmp:
            main = _repo(tmp, "main")
            _set(main, "rel-gone")  # shared, relative, missing (never auto-unset)
            wt = _worktree(main, "wt")
            _git(wt, "config", "--worktree", "core.hooksPath", os.path.join(tmp, "abs-gone"))
            res = hp.repair(cwd=wt, apply=True)
            self.assertEqual(res["did"], ["unset-worktree"])
            self.assertEqual(res["status"], "needs-manual")
            self.assertIsNotNone(hp.detect_broken_hooks_path(cwd=wt))  # still disabled

    def test_local_then_global_reports_needs_manual(self):
        # A broken absolute LOCAL value masking a broken GLOBAL value: unsetting local is correct, but the now
        # effective global value is still broken -> needs-manual, not "fixed".
        with tempfile.TemporaryDirectory() as tmp:
            r = _repo(tmp, "r")
            gcfg = os.path.join(tmp, "gitconfig-global")
            with mock.patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": gcfg, "GIT_CONFIG_SYSTEM": os.devnull}):
                subprocess.run(["git", "config", "--global", "core.hooksPath", os.path.join(tmp, "g-gone")],
                               capture_output=True, check=False)
                _set(r, os.path.join(tmp, "l-gone"))  # local, absolute, missing (auto-fixable)
                res = hp.repair(cwd=r, apply=True)
                self.assertEqual(res["did"], ["unset-local"])
                self.assertEqual(res["status"], "needs-manual")

    def test_apply_time_isabs_guard_skips_a_relative_shared_value(self):
        # SG re-check: if the live --local value is relative at apply time (a concurrent absolute->relative flip),
        # the unset-local step is skipped — a relative shared value could be valid in a peer worktree.
        with tempfile.TemporaryDirectory() as tmp:
            r = _repo(tmp, "r")
            _set(r, "rel-gone")  # relative, missing
            top = hp._toplevel(r)
            stale_plan = {"status": "fixable", "plan": ["unset-local"], "top": top, "detail": None}
            with mock.patch.object(hp, "assess", return_value=stale_plan):
                res = hp.repair(cwd=r, apply=True)
            self.assertIn("unset-local", res["skipped"])
            self.assertEqual(_get(r), "rel-gone")  # the relative shared value survives


class TestCLI(unittest.TestCase):
    @contextlib.contextmanager
    def _in(self, root: str):
        prev = os.getcwd()
        os.chdir(root)
        try:
            yield
        finally:
            os.chdir(prev)

    def test_default_reports_healthy_or_finding(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = _repo(tmp, "r")
            with self._in(r), contextlib.redirect_stdout(io.StringIO()) as out:
                hp.main([])
            self.assertIn("healthy", out.getvalue())
            _set(r, os.path.join(tmp, "gone"))
            with self._in(r), contextlib.redirect_stdout(io.StringIO()) as out:
                hp.main([])
            self.assertIn("missing directory", out.getvalue())

    def test_repair_dry_run_then_apply(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = _repo(tmp, "r")
            _set(r, os.path.join(tmp, "gone"))
            with self._in(r), contextlib.redirect_stdout(io.StringIO()) as out:
                hp.main(["repair"])
            self.assertIn("dry-run", out.getvalue())
            self.assertIsNotNone(_get(r))  # dry-run changed nothing
            with self._in(r), contextlib.redirect_stdout(io.StringIO()):
                hp.main(["repair", "--apply"])
            self.assertIsNone(_get(r))

    def test_demo_self_check_passes(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(hp.main(["demo"]), 0)


class TestNoDestructiveTokens(unittest.TestCase):
    def test_source_names_no_destructive_git_tokens(self):
        # defense-in-depth: the repair must never reach for a force/destructive git operation. Scan the QUOTED
        # command tokens (a backtick/prose mention like `pre-push` is not a false positive).
        with open(hooks_path_health := hp.__file__, encoding="utf-8") as fh:
            src = fh.read()
        for token in ('"reset"', '"clean"', '"-f"', '"--force"', '"--hard"',
                      '"drop"', '"clear"', '"push"', '"checkout"'):
            self.assertNotIn(token, src, f"the hooksPath repair must never use the git token {token}")


if __name__ == "__main__":
    unittest.main()
