#!/usr/bin/env python3
"""Tests for pr_reconcile — the stranded-PR conflict detector + lossless-or-refuse recovery (#136).

Lock the behaviours a non-engineer cannot read code to verify: a conflicting pull request is DETECTED (and an
async-uncomputed merge state degrades QUIETLY, never a false "all clear"); a conflict confined to the two derived-committed
index files classifies FIXABLE while any authored conflict (or a tree with no engine files) classifies
NEEDS-MANUAL; the recovery reconciles LOSSLESSLY (both pieces of work survive); and on ANY failure it RESTORES
the branch to exactly where it was and refuses — never losing work, never side-picking, never claiming a
success it didn't earn. The executor's control flow is tested with a fake regenerator over light throwaway git
repos; the REAL regenerator is exercised end-to-end by demo_pr_reconcile.py.
"""
from __future__ import annotations

import os
import re
import subprocess
import tempfile
import unittest
from unittest import mock

import pr_reconcile

GRAPH = ".engine/knowledge/graph.json"
SELFMAP = ".engine/self-map.md"


def _git(root: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", root, *args], capture_output=True, text=True, check=False)


def _commit(root: str, message: str) -> None:
    _git(root, "add", "-A")
    _git(root, "-c", "user.email=e@x", "-c", "user.name=n", "commit", "-q", "-m", message)


def _write(root: str, rel: str, content: str) -> None:
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)


def _repo(holder: str, *, members: bool = True) -> tuple[str, str]:
    """A bare 'origin' + a working clone on `main`, seeded with the two derived-committed member files (as plain text so they
    can be made to clash) plus an authored seed file. Returns (origin, work)."""
    origin, work = os.path.join(holder, "origin.git"), os.path.join(holder, "work")
    subprocess.run(["git", "init", "-q", "--bare", origin], check=False)
    os.makedirs(work)
    _git(work, "init", "-q", "-b", "main")
    _git(work, "remote", "add", "origin", origin)
    if members:
        _write(work, GRAPH, "base\n")
        _write(work, SELFMAP, "base\n")
    _write(work, ".engine/tools/seed.py", '"""seed"""\n')
    _commit(work, "seed")
    _git(work, "push", "-q", "origin", "main")
    _git(work, "remote", "set-head", "origin", "main")
    return origin, work


def _diverge(work: str, *, feature_member: str = GRAPH, main_member: str = GRAPH,
             feature_authored: str = "feat_a", main_authored: str = "feat_b",
             shared_authored: str | None = None) -> None:
    """Build a feature branch and an advanced main that clash. By default each side rewrites graph.json (a
    member clash) and adds its own distinct authored tool (no authored clash) -> fixable. Pass shared_authored
    to make BOTH sides edit the same authored file -> a real authored clash (needs-manual)."""
    _git(work, "checkout", "-q", "-b", "feature")
    _write(work, feature_member, "feature-side\n")
    if shared_authored:
        _write(work, f".engine/tools/{shared_authored}.py", '"""feature version"""\n')
    else:
        _write(work, f".engine/tools/{feature_authored}.py", '"""feature work"""\n')
    _commit(work, "feature work")
    _git(work, "push", "-q", "origin", "feature")

    _git(work, "checkout", "-q", "main")
    _write(work, main_member, "main-side\n")
    if shared_authored:
        _write(work, f".engine/tools/{shared_authored}.py", '"""main version"""\n')
    else:
        _write(work, f".engine/tools/{main_authored}.py", '"""main work"""\n')
    _commit(work, "main work (landed first)")
    _git(work, "push", "-q", "origin", "main")
    _git(work, "checkout", "-q", "feature")


# ---- detect_conflict: a fake GitHub serving the (method, path, body) transport contract ------

class _FakeGH:
    def __init__(self, repo="you/proj", *, pulls=None, prs=None, fail=False):
        self.repo = repo
        self._pulls = pulls if pulls is not None else [{"number": 7}]
        self._prs = prs or {}
        self._fail = fail

    def _transport(self, method, path, body):
        if self._fail:
            return 500, None
        if "/pulls?" in path:
            return 200, self._pulls
        m = re.search(r"/pulls/(\d+)$", path)
        if m:
            return 200, self._prs.get(int(m.group(1)))
        return 404, None


class TestDetectConflict(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = os.path.join(self._tmp.name, "r")
        os.makedirs(self.root)
        _git(self.root, "init", "-q", "-b", "feature")
        _commit_empty = _git(self.root, "-c", "user.email=e@x", "-c", "user.name=n",
                             "commit", "-q", "--allow-empty", "-m", "seed")

    def tearDown(self):
        self._tmp.cleanup()

    def test_none_without_github(self):
        self.assertIsNone(pr_reconcile.detect_conflict(None, root=self.root))

    def test_conflict_when_mergeable_false(self):
        gh = _FakeGH(prs={7: {"number": 7, "title": "My PR", "mergeable": False, "mergeable_state": "dirty"}})
        self.assertEqual(pr_reconcile.detect_conflict(gh, root=self.root), {"pr": 7, "title": "My PR"})

    def test_clean_returns_none(self):
        gh = _FakeGH(prs={7: {"number": 7, "title": "x", "mergeable": True, "mergeable_state": "clean"}})
        self.assertIsNone(pr_reconcile.detect_conflict(gh, root=self.root))

    def test_unknown_async_state_degrades_quietly_never_false_all_clear(self):
        # GitHub computes `mergeable` asynchronously; null must NOT read as conflict OR as a confident clear.
        gh = _FakeGH(prs={7: {"number": 7, "title": "x", "mergeable": None, "mergeable_state": "unknown"}})
        self.assertIsNone(pr_reconcile.detect_conflict(gh, root=self.root))

    def test_no_open_pr_returns_none(self):
        self.assertIsNone(pr_reconcile.detect_conflict(_FakeGH(pulls=[]), root=self.root))

    def test_github_failure_degrades_to_none(self):
        self.assertIsNone(pr_reconcile.detect_conflict(_FakeGH(fail=True), root=self.root))

    def test_detached_head_returns_none(self):
        sha = _git(self.root, "rev-parse", "HEAD").stdout.strip()
        _git(self.root, "checkout", "-q", "--detach", sha)
        gh = _FakeGH(prs={7: {"number": 7, "title": "x", "mergeable": False}})
        self.assertIsNone(pr_reconcile.detect_conflict(gh, root=self.root))


# ---- assess: the working-tree-free classifier ------------------------------------------------

class TestAssess(unittest.TestCase):
    def test_member_only_conflict_is_fixable(self):
        with tempfile.TemporaryDirectory() as holder:
            _origin, work = _repo(holder)
            _diverge(work)                                  # both rewrite graph.json; distinct authored files
            a = pr_reconcile.assess(root=work, default="main")
            self.assertEqual(a["status"], "fixable")
            self.assertEqual(set(a["conflicted"]), {GRAPH})

    def test_authored_conflict_is_needs_manual(self):
        with tempfile.TemporaryDirectory() as holder:
            _origin, work = _repo(holder)
            _diverge(work, shared_authored="shared")        # both edit the same authored file
            a = pr_reconcile.assess(root=work, default="main")
            self.assertEqual(a["status"], "needs-manual")
            self.assertEqual(a["reason"], "authored-conflict")

    def test_no_engine_members_is_needs_manual(self):
        with tempfile.TemporaryDirectory() as holder:
            _origin, work = _repo(holder, members=False)     # an external-contribution / fork-main tree
            _git(work, "checkout", "-q", "-b", "feature")
            _write(work, ".engine/tools/feat.py", '"""x"""\n')
            _commit(work, "feature")
            a = pr_reconcile.assess(root=work, default="main")
            self.assertEqual(a["status"], "needs-manual")
            self.assertEqual(a["reason"], "no-engine-members")

    def test_clean_is_healthy(self):
        with tempfile.TemporaryDirectory() as holder:
            _origin, work = _repo(holder)
            _git(work, "checkout", "-q", "-b", "feature")    # no divergence -> nothing to reconcile
            _write(work, ".engine/tools/feat.py", '"""x"""\n')
            _commit(work, "feature")
            self.assertEqual(pr_reconcile.assess(root=work, default="main")["status"], "healthy")


# ---- reconcile: lossless-or-refuse (control flow over a fake regenerator) ---------------------

def _fake_regen(root: str) -> bool:
    """Stand in for the real generators: write clean, deterministic content into the two members (resolving any
    conflict markers the merge left). The REAL regenerators are exercised by demo_pr_reconcile.py."""
    _write(root, GRAPH, "reconciled-graph\n")
    _write(root, SELFMAP, "reconciled-self-map\n")
    return True


class TestReconcile(unittest.TestCase):
    def test_lossless_recovery_keeps_both_pieces_of_work(self):
        with tempfile.TemporaryDirectory() as holder, \
                mock.patch.object(pr_reconcile, "_regen_members", _fake_regen):
            _origin, work = _repo(holder)
            _diverge(work)                                  # feature adds feat_a; main added feat_b
            r = pr_reconcile.reconcile(apply=True, root=work, default="main")
            self.assertEqual(r["status"], "reconciled")
            # both authored contributions are present in the reconciled branch
            self.assertTrue(os.path.isfile(os.path.join(work, ".engine/tools/feat_a.py")))
            self.assertTrue(os.path.isfile(os.path.join(work, ".engine/tools/feat_b.py")))
            # the members were regenerated (no conflict markers survive)
            with open(os.path.join(work, GRAPH), encoding="utf-8") as fh:
                self.assertEqual(fh.read(), "reconciled-graph\n")
            # the branch now merges cleanly into main (reconcile-before-merge) and the push landed
            self.assertEqual(pr_reconcile._merge_tree(r["base"], work), ("clean", []))
            self.assertEqual(_git(work, "rev-parse", "feature").stdout,
                             _git(work, "rev-parse", "origin/feature").stdout)

    def test_dry_run_mutates_nothing(self):
        with tempfile.TemporaryDirectory() as holder:
            _origin, work = _repo(holder)
            _diverge(work)
            before = _git(work, "rev-parse", "HEAD").stdout.strip()
            r = pr_reconcile.reconcile(apply=False, root=work, default="main")
            self.assertEqual(r["status"], "fixable")
            self.assertFalse(r["applied"])
            self.assertEqual(_git(work, "rev-parse", "HEAD").stdout.strip(), before)

    def test_authored_conflict_refused_untouched(self):
        with tempfile.TemporaryDirectory() as holder:
            _origin, work = _repo(holder)
            _diverge(work, shared_authored="shared")
            before = _git(work, "rev-parse", "HEAD").stdout.strip()
            r = pr_reconcile.reconcile(apply=True, root=work, default="main")
            self.assertEqual(r["status"], "needs-manual")
            self.assertEqual(r["reason"], "authored-conflict")
            self.assertEqual(_git(work, "rev-parse", "HEAD").stdout.strip(), before)
            self.assertFalse(_git(work, "status", "--porcelain").stdout.strip())

    def test_dirty_tree_refused(self):
        with tempfile.TemporaryDirectory() as holder, \
                mock.patch.object(pr_reconcile, "_regen_members", _fake_regen):
            _origin, work = _repo(holder)
            _diverge(work)
            _write(work, ".engine/tools/uncommitted.py", '"""dirty"""\n')   # an uncommitted change
            r = pr_reconcile.reconcile(apply=True, root=work, default="main")
            self.assertEqual(r["status"], "needs-manual")
            self.assertEqual(r["reason"], "dirty-tree")

    def test_regen_failure_restores_in_progress_merge(self):
        # The merge conflicts on the members; regeneration then fails -> abort the in-progress merge + restore.
        with tempfile.TemporaryDirectory() as holder, \
                mock.patch.object(pr_reconcile, "_regen_members", lambda root: False):
            _origin, work = _repo(holder)
            _diverge(work)
            before = _git(work, "rev-parse", "HEAD").stdout.strip()
            r = pr_reconcile.reconcile(apply=True, root=work, default="main")
            self.assertEqual(r["status"], "needs-manual")
            self.assertEqual(r["reason"], "regen-failed")
            self.assertEqual(_git(work, "rev-parse", "HEAD").stdout.strip(), before)   # universal restore
            self.assertFalse(_git(work, "status", "--porcelain").stdout.strip())

    def test_push_failure_restores_after_completed_merge(self):
        # The merge commits successfully, then the push is rejected -> `git merge --abort` is invalid (already
        # committed), so the UNIVERSAL `reset --hard <pre>` restores the branch (the A1 lesson).
        with tempfile.TemporaryDirectory() as holder, \
                mock.patch.object(pr_reconcile, "_regen_members", _fake_regen):
            _origin, work = _repo(holder)
            _diverge(work)
            before = _git(work, "rev-parse", "HEAD").stdout.strip()
            real_ok = pr_reconcile._ok

            def _ok_but_push_fails(args, root, timeout=120):
                if args and args[0] == "push":
                    return False
                return real_ok(args, root, timeout)

            with mock.patch.object(pr_reconcile, "_ok", _ok_but_push_fails):
                r = pr_reconcile.reconcile(apply=True, root=work, default="main")
            self.assertEqual(r["status"], "needs-manual")
            self.assertEqual(r["reason"], "push-rejected")
            self.assertEqual(_git(work, "rev-parse", "HEAD").stdout.strip(), before)   # reset --hard restored
            self.assertFalse(_git(work, "status", "--porcelain").stdout.strip())


if __name__ == "__main__":
    unittest.main()
