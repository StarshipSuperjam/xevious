#!/usr/bin/env python3
"""Tests for license_health — the standing foreign-template-LICENSE detector (issue #471).

Lock the behaviours a non-engineer cannot read code to verify: a generated repo still carrying the engine's own
template LICENSE reads FIRES (offering a reviewed cleanup); an adopter's own license, an absent license, or the
engine's own template repo read as no-op; the verdict tracks the COMMITTED HEAD, never a working-tree edit; and
the detector is strictly READ-ONLY (no git-mutation verb in its source). Fixtures are throwaway git repos so the
detection is proven offline and deterministically.
"""
from __future__ import annotations

import ast
import contextlib
import io
import json
import os
import subprocess
import tempfile
import unittest

import license_health
import license_seeds

SEED = license_seeds.CURRENT_SEED
HOME = "StarshipSuperjam/engine-template"
HOME_URL = f"https://github.com/{HOME}.git"
PRODUCT_URL = "https://github.com/adopter/their-product.git"


def _git(root: str, *args: str) -> None:
    subprocess.run(["git", "-C", root, *args], capture_output=True, text=True, check=False)


def _repo(tmp: str, name: str, *, license_text=None, origin: str = PRODUCT_URL, commit: bool = True) -> str:
    """A throwaway committed git checkout: an optional root LICENSE, a git origin, and a recorded home_repository.
    The engine==product carve-out now keys on git origin == recorded home (repo_identity.is_home_repo), NOT a
    CLAUDE.md marker: the default origin is a DEPLOYED repo (origin != home, so a leftover LICENSE fires); pass
    origin=HOME_URL for the engine's own home repo (the carve-out no-ops)."""
    root = os.path.join(tmp, name)
    os.makedirs(os.path.join(root, ".engine"), exist_ok=True)
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    if origin:
        _git(root, "remote", "add", "origin", origin)
    with open(os.path.join(root, ".engine", "engine.json"), "w", encoding="utf-8") as fh:
        json.dump({"home_repository": HOME}, fh)
    if license_text is not None:
        with open(os.path.join(root, "LICENSE"), "w", encoding="utf-8") as fh:
            fh.write(license_text)
    if commit:
        _git(root, "add", "-A")
        _git(root, "commit", "-qm", "seed")
    return root


class TestDetectForeignLicense(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_fires_on_a_leftover_template_license(self):
        repo = _repo(self.tmp, "leftover", license_text=SEED)
        d = license_health.detect_foreign_license(cwd=repo)
        self.assertIsNotNone(d)
        self.assertTrue(d["present"])
        self.assertTrue(d["fingerprint"])

    def test_preserves_an_adopters_renamed_license(self):
        repo = _repo(self.tmp, "renamed", license_text=SEED.replace("StarshipSuperjam", "Acme Corp"))
        self.assertIsNone(license_health.detect_foreign_license(cwd=repo))

    def test_preserves_a_repo_with_no_license(self):
        repo = _repo(self.tmp, "absent", license_text=None)
        self.assertIsNone(license_health.detect_foreign_license(cwd=repo))

    def test_no_ops_in_the_engines_own_home_repo(self):
        # The engine==product carve-out: the home repo's root LICENSE is legitimately the engine's, judged by
        # git origin == recorded home (repo_identity.is_home_repo), not a CLAUDE.md marker.
        repo = _repo(self.tmp, "home", license_text=SEED, origin=HOME_URL)
        self.assertIsNone(license_health.detect_foreign_license(cwd=repo))

    def test_verdict_tracks_committed_head_not_the_working_tree(self):
        # The committed LICENSE governs the product; an uncommitted working-tree edit must not change the verdict.
        repo = _repo(self.tmp, "dirty", license_text=SEED)
        with open(os.path.join(repo, "LICENSE"), "w", encoding="utf-8") as fh:
            fh.write("the adopter rewrote the working tree but never committed\n")
        d = license_health.detect_foreign_license(cwd=repo)
        self.assertIsNotNone(d, "must read HEAD:LICENSE, not the dirty working tree")

    def test_resolves_the_main_checkout_from_a_linked_worktree(self):
        # Running from a linked worktree still resolves the MAIN checkout and reads its committed LICENSE.
        main = _repo(self.tmp, "main", license_text=SEED)
        wt = os.path.join(self.tmp, "wt")
        _git(main, "worktree", "add", "-q", "-b", "side", wt)
        d = license_health.detect_foreign_license(cwd=wt)
        self.assertIsNotNone(d)
        self.assertEqual(os.path.realpath(d["main"]), os.path.realpath(main))

    def test_unresolvable_checkout_degrades_quietly(self):
        # A non-git directory: no crash, just None.
        plain = os.path.join(self.tmp, "notgit")
        os.makedirs(plain, exist_ok=True)
        self.assertIsNone(license_health.detect_foreign_license(cwd=plain))


class TestRemovalPrDedupe(unittest.TestCase):
    def test_no_repo_or_token_returns_none(self):
        # Best-effort online step: without credentials it cannot determine -> None (caller offers normally).
        self.assertIsNone(license_health.removal_pr_open(None, None))
        self.assertIsNone(license_health.removal_pr_open("owner/repo", None))
        self.assertIsNone(license_health.removal_pr_open(None, "tok"))


class TestReadOnly(unittest.TestCase):
    # The in-tool demo builds THROWAWAY tmp fixtures (init/add/commit on repos it owns) — legitimate scaffolding,
    # never the operator's tree. The read-only guarantee is about the DETECTOR, so the scan skips the demo helpers.
    _DEMO_SCAFFOLDING = {"_git", "_fixture", "_demo"}

    def test_detection_functions_issue_no_git_mutation_verb(self):
        # The detector and its dedupe are strictly read-only over the operator's repo: no git write anywhere in
        # their bodies (the checkout_health read-only discipline). Scans string literals per function.
        with open(license_health.__file__, encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        mutating = {"commit", "push", "add", "rm", "reset", "checkout", "merge", "rebase", "branch",
                    "stash", "clean", "restore", "cherry-pick", "revert", "apply", "init", "fetch"}
        scanned = 0
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.FunctionDef) or fn.name in self._DEMO_SCAFFOLDING:
                continue
            scanned += 1
            for node in ast.walk(fn):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    self.assertNotIn(node.value, mutating,
                                     f"license_health.{fn.name} must issue no git-mutation verb "
                                     f"(found {node.value!r})")
        self.assertGreater(scanned, 3, "the read-only scan must cover the detection functions")


class TestDemoSelfChecks(unittest.TestCase):
    def test_demo_runs_green(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(license_health.main(["demo"]), 0)


if __name__ == "__main__":
    unittest.main()
