#!/usr/bin/env python3
"""Tests for first_run_health — the un-finished-first-run detector (issue #353).

Lock the behaviours a non-engineer cannot read code to verify: a fresh copy of the template (origin differs
from the recorded update home, the one-time setup tool still present) reads FIRES (offering to walk setup);
the workshop where the engine is built (origin == home), a finished project (setup tool retired), and any
repo whose origin/home can't be read all read as quiet no-ops; a setup interrupted partway (tool still
present, floor already swapped) still FIRES so setup can resume; the origin/home compare is slug-normalized
(SSH/.git/case skew never mis-reads the workshop as a copy); and the detector is strictly READ-ONLY (no
git-mutation verb in its source). Fixtures are throwaway git repos so the detection is proven offline and
deterministically.
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
from unittest import mock

import first_run_health
import module_coherence
import repo_identity  # the dependency-light home-repo identity seam is_downstream_copy now lives in

HOME = "StarshipSuperjam/engine-template"


def _git(root: str, *args: str) -> None:
    subprocess.run(["git", "-C", root, *args], capture_output=True, text=True, check=False)


def _repo(tmp: str, name: str, *, origin: str, home: str | None = HOME,
          tool_present: bool = True, floor_swapped: bool = False, commit: bool = True) -> str:
    """A throwaway committed git checkout with a set origin remote, an installed manifest recording `home`
    (omitted when None), optionally the one-time setup tool, and either the construction or deployed-floor root."""
    root = os.path.join(tmp, name)
    os.makedirs(os.path.join(root, ".engine", "tools"), exist_ok=True)
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    if origin:
        _git(root, "remote", "add", "origin", origin)
    manifest: dict = {"engine_release": "0.0.0"}
    if home is not None:
        manifest["home_repository"] = home
    with open(os.path.join(root, ".engine", "engine.json"), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh)
    if tool_present:
        with open(os.path.join(root, ".engine", "tools", "instantiator.py"), "w", encoding="utf-8") as fh:
            fh.write("# placeholder setup tool\n")
    with open(os.path.join(root, "CLAUDE.md"), "w", encoding="utf-8") as fh:
        fh.write("# Your project runs on an Engine\n" if floor_swapped
                 else "# a fresh copy of the engine template\n")   # content is incidental; keyed on origin + tool
    if commit:
        _git(root, "add", "-A")
        _git(root, "commit", "-qm", "seed")
    return root


class TestDetectFirstRunPending(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_fires_on_a_fresh_template_copy(self):
        repo = _repo(self.tmp, "fresh", origin="https://github.com/adopter/their-product.git")
        d = first_run_health.detect_first_run_pending(cwd=repo)
        self.assertIsNotNone(d)
        self.assertTrue(d["present"])
        self.assertEqual(d["own"], "adopter/their-product")
        self.assertEqual(d["home"], HOME)

    def test_quiet_in_the_workshop_where_origin_equals_home(self):
        repo = _repo(self.tmp, "workshop", origin=f"https://github.com/{HOME}.git")
        self.assertIsNone(first_run_health.detect_first_run_pending(cwd=repo))

    def test_quiet_in_a_finished_project_with_the_setup_tool_retired(self):
        repo = _repo(self.tmp, "finished", origin="https://github.com/adopter/their-product.git",
                     tool_present=False, floor_swapped=True)
        self.assertIsNone(first_run_health.detect_first_run_pending(cwd=repo))

    def test_fires_on_a_setup_interrupted_after_the_floor_swap(self):
        # The post-floor-swap interrupt window (#519 §1): floor already swapped but the setup tool not yet
        # retired -> still un-finished, same remedy (setup resumes idempotently). Keyed on the TOOL, not the floor.
        repo = _repo(self.tmp, "half", origin="https://github.com/adopter/their-product.git",
                     tool_present=True, floor_swapped=True)
        self.assertIsNotNone(first_run_health.detect_first_run_pending(cwd=repo))

    def test_quiet_when_origin_cannot_be_read(self):
        # Safe fail direction: no origin remote -> cannot place the repo -> no fire (never a false offer).
        repo = _repo(self.tmp, "noorigin", origin="")
        self.assertIsNone(first_run_health.detect_first_run_pending(cwd=repo))

    def test_quiet_when_home_is_absent(self):
        # A manifest with no home_repository -> cannot place -> no fire.
        repo = _repo(self.tmp, "nohome", origin="https://github.com/adopter/their-product.git", home=None)
        self.assertIsNone(first_run_health.detect_first_run_pending(cwd=repo))

    def test_quiet_on_a_corrupt_manifest(self):
        # Fail-soft: a malformed engine.json (unreadable home) cannot place the repo -> no fire, no crash.
        repo = _repo(self.tmp, "corrupt", origin="https://github.com/adopter/their-product.git")
        with open(os.path.join(repo, ".engine", "engine.json"), "w", encoding="utf-8") as fh:
            fh.write("{ this is not valid json ")
        self.assertIsNone(first_run_health.detect_first_run_pending(cwd=repo))

    def test_slug_comparison_is_normalized_against_case_and_git_suffix(self):
        # A case/.git-skewed origin that is REALLY the workshop must not mis-read as a downstream copy.
        repo = _repo(self.tmp, "skew", origin="git@github.com:starshipsuperjam/Engine-Template.git")
        self.assertIsNone(first_run_health.detect_first_run_pending(cwd=repo))

    def test_look_alike_host_origin_is_not_a_slug(self):
        # Host anchor (defense-in-depth): an origin on a look-alike host that merely CONTAINS "github.com"
        # (notgithub.com) must NOT parse to a real slug — else a copy there could be mis-placed as the home.
        repo = _repo(self.tmp, "lookalike", origin="https://notgithub.com/starshipsuperjam/engine-template.git")
        self.assertIsNone(first_run_health._origin_slug(repo))
        self.assertIsNone(first_run_health._origin_slug(_repo(self.tmp, "evil2",
                          origin="https://github.com.evil.com/starshipsuperjam/engine-template.git")))

    def test_resolves_the_main_checkout_from_a_linked_worktree(self):
        main = _repo(self.tmp, "main", origin="https://github.com/adopter/their-product.git")
        wt = os.path.join(self.tmp, "wt")
        _git(main, "worktree", "add", "-q", "-b", "side", wt)
        d = first_run_health.detect_first_run_pending(cwd=wt)
        self.assertIsNotNone(d)
        self.assertEqual(os.path.realpath(d["main"]), os.path.realpath(main))

    def test_unresolvable_checkout_degrades_quietly(self):
        plain = os.path.join(self.tmp, "notgit")
        os.makedirs(plain, exist_ok=True)
        self.assertIsNone(first_run_health.detect_first_run_pending(cwd=plain))


class TestDetectHomeWorkshop(unittest.TestCase):
    # The strict-positive complement (#323): fires ONLY on a confirmed origin==home match, so boot surfaces the
    # home-development grounding in the engine's own home and NOWHERE else. Marker-independent — it keys on git
    # origin vs recorded home, never on the root CLAUDE.md content.
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_fires_in_the_workshop_where_origin_equals_home(self):
        repo = _repo(self.tmp, "workshop", origin=f"https://github.com/{HOME}.git")
        d = first_run_health.detect_home_workshop(cwd=repo)
        self.assertIsNotNone(d)
        self.assertTrue(d["present"])
        self.assertEqual(d["own"], HOME)
        self.assertEqual(d["home"], HOME)

    def test_quiet_in_a_downstream_copy(self):
        repo = _repo(self.tmp, "copy", origin="https://github.com/adopter/their-product.git")
        self.assertIsNone(first_run_health.detect_home_workshop(cwd=repo))

    def test_quiet_when_origin_cannot_be_read(self):
        # STRICT positive (the deliberate opposite of is_home_repo's fail-toward-home): an unreadable origin is
        # NOT confirmably home, so the overlay stays quiet — it must never point a repo with no engine-development
        # runbook (a deployed copy) at that retired doc.
        repo = _repo(self.tmp, "noorigin", origin="")
        self.assertIsNone(first_run_health.detect_home_workshop(cwd=repo))

    def test_quiet_when_home_is_absent(self):
        repo = _repo(self.tmp, "nohome", origin=f"https://github.com/{HOME}.git", home=None)
        self.assertIsNone(first_run_health.detect_home_workshop(cwd=repo))

    def test_quiet_on_a_corrupt_manifest(self):
        repo = _repo(self.tmp, "corrupt", origin=f"https://github.com/{HOME}.git")
        with open(os.path.join(repo, ".engine", "engine.json"), "w", encoding="utf-8") as fh:
            fh.write("{ not valid json ")
        self.assertIsNone(first_run_health.detect_home_workshop(cwd=repo))

    def test_a_case_and_git_skewed_home_origin_still_fires(self):
        repo = _repo(self.tmp, "skew", origin="git@github.com:starshipsuperjam/Engine-Template.git")
        self.assertIsNotNone(first_run_health.detect_home_workshop(cwd=repo))

    def test_unresolvable_checkout_degrades_quietly(self):
        plain = os.path.join(self.tmp, "notgit")
        os.makedirs(plain, exist_ok=True)
        self.assertIsNone(first_run_health.detect_home_workshop(cwd=plain))

    def test_home_and_copy_are_mutually_exclusive(self):
        # A placed checkout is home XOR a downstream copy — the two detectors never both fire.
        home = _repo(self.tmp, "home2", origin=f"https://github.com/{HOME}.git")
        copy = _repo(self.tmp, "copy2", origin="https://github.com/adopter/their-product.git")
        self.assertIsNotNone(first_run_health.detect_home_workshop(cwd=home))
        self.assertIsNone(first_run_health.detect_first_run_pending(cwd=home))
        self.assertIsNone(first_run_health.detect_home_workshop(cwd=copy))
        self.assertIsNotNone(first_run_health.detect_first_run_pending(cwd=copy))


class TestIsDownstreamCopy(unittest.TestCase):
    # The shared, injectable, normalized predicate both callers (show branch + boot detector) run through.
    def test_none_own_slug_is_never_a_copy(self):
        self.assertFalse(module_coherence.is_downstream_copy(None, HOME))

    def test_different_origin_and_home_is_a_copy(self):
        self.assertTrue(module_coherence.is_downstream_copy("adopter/their-product", HOME))

    def test_equal_origin_and_home_is_not_a_copy_even_with_skew(self):
        # The workshop (or any non-copy): origin == home, tolerant of case / .git / SSH skew.
        self.assertFalse(module_coherence.is_downstream_copy("StarshipSuperjam/Engine-Template.git", HOME))

    def test_absent_home_is_not_a_copy(self):
        self.assertFalse(module_coherence.is_downstream_copy("adopter/their-product", None))

    def test_malformed_manifest_home_degrades_to_not_a_copy(self):
        # The fail-soft path: when home defaults (None passed) and home_repository() RAISES on a corrupt
        # manifest, the predicate returns False rather than crashing its read-only caller. is_downstream_copy
        # resolves home_repository in repo_identity's namespace (its single home), so the patch targets there.
        with mock.patch.object(repo_identity, "home_repository", side_effect=ValueError("corrupt")):
            self.assertFalse(module_coherence.is_downstream_copy("adopter/their-product"))


class TestForkedFromHome(unittest.TestCase):
    @staticmethod
    def _transport(status, body):
        def t(method, path, body_arg=None):
            return status, body
        return t

    def test_missing_repo_token_or_home_returns_none(self):
        # Best-effort online step: any missing input -> None (caller offers normally, never suppresses blindly).
        self.assertIsNone(first_run_health.forked_from_home(None, None, None))
        self.assertIsNone(first_run_health.forked_from_home("owner/repo", None, HOME))
        self.assertIsNone(first_run_health.forked_from_home("owner/repo", "tok", None))

    def test_a_non_fork_is_not_suppressed(self):
        # A "Use this template" copy AND a clone-and-push copy are both fork:false -> return False (keep
        # offering); the dead-on-arrival case they represent must never be silenced (#353 req #4).
        t = self._transport(200, {"fork": False})
        self.assertIs(first_run_health.forked_from_home("adopter/product", "tok", HOME, transport=t), False)

    def test_a_fork_of_home_is_suppressed(self):
        t = self._transport(200, {"fork": True, "parent": {"full_name": HOME}})
        self.assertIs(first_run_health.forked_from_home("adopter/product", "tok", HOME, transport=t), True)

    def test_a_fork_of_a_different_parent_is_not_suppressed(self):
        t = self._transport(200, {"fork": True, "parent": {"full_name": "someone/unrelated"}})
        self.assertIs(first_run_health.forked_from_home("adopter/product", "tok", HOME, transport=t), False)

    def test_an_api_error_degrades_to_none(self):
        t = self._transport(404, None)
        self.assertIsNone(first_run_health.forked_from_home("adopter/product", "tok", HOME, transport=t))


class TestReadOnly(unittest.TestCase):
    # The in-tool demo builds THROWAWAY tmp fixtures (init/add/commit on repos it owns) — legitimate scaffolding,
    # never the operator's tree. The read-only guarantee is about the DETECTOR, so the scan skips the demo helpers.
    _DEMO_SCAFFOLDING = {"_git", "_fixture", "_demo"}

    def test_detection_functions_issue_no_git_mutation_verb(self):
        with open(first_run_health.__file__, encoding="utf-8") as fh:
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
                                     f"first_run_health.{fn.name} must issue no git-mutation verb "
                                     f"(found {node.value!r})")
        self.assertGreater(scanned, 3, "the read-only scan must cover the detection functions")


class TestDemoSelfChecks(unittest.TestCase):
    def test_demo_runs_green(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(first_run_health.main(["demo"]), 0)


if __name__ == "__main__":
    unittest.main()
