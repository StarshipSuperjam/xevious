#!/usr/bin/env python3
"""Tests for mechanic_build — the engine-mechanic cross-repo build preflight (eADR-0026), the GUARDED,
fail-closed gate behind a live cross-repo write.

Lock the behaviours a non-engineer cannot read code to verify, and that the guardrail-ack protects:
  - the host-anchored belt PASSES only for a genuine github.com origin that matches the committed target, and
    DENIES a look-alike host (`notgithub.com`) — under subprocess-in-place a matched checkout's own tools run,
    so a look-alike pass would be local code execution;
  - `resolve_build_target` NEVER returns a path unless the belt AND the health check both passed — proven by the
    full, ordered refusal taxonomy and a focused invariant test;
  - the preflight CLI keeps STRICT channel discipline (verified env to stdout on success, plain reason to stderr
    on refusal, stdout empty on refusal) so `cd`-ing on its output can never consume a refusal string.

Non-vacuity: the product fixture uses a DISTINCT slug (`acme/product`), never this repo's own
`StarshipSuperjam/engine-template`, and the in-place proof reads origin via `git -C <checkout>` (never the
process cwd, which `boot.repo_slug` would read and which is this repo's own origin) — so no assertion can pass by
reading the ambient repository. Fixtures are throwaway git repos; the whole surface is proven offline.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest

import checkout_health
import mechanic_build

# The product the mechanic is configured to build — deliberately NOT this repo's own slug, so a resolution that
# accidentally read the ambient repository would return the WRONG value and the assertion would fail.
_TARGET = "acme/product"


def _git(root: str, *args: str) -> None:
    subprocess.run(["git", "-C", root, *args], capture_output=True, text=True, check=False)


def _repo(tmp: str, name: str) -> str:
    """A throwaway git checkout: engine files present, one commit on the default branch."""
    root = os.path.join(tmp, name)
    os.makedirs(os.path.join(root, ".claude"))
    os.makedirs(os.path.join(root, ".engine"))
    with open(os.path.join(root, ".claude", "settings.json"), "w") as fh:
        fh.write("{}")
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "-c", "user.email=e@x", "-c", "user.name=n", "commit", "-q", "-m", "seed", "--allow-empty")
    return root


def _mechanic(tmp: str, *, target: str | None = _TARGET) -> str:
    """A mechanic checkout: its manifest records `product_build_target` (or none, for the not-a-mechanic case)."""
    root = _repo(tmp, "mechanic")
    manifest = {"product_build_target": target} if target else {"engine_release": "1.0.0"}
    with open(os.path.join(root, ".engine", "engine.json"), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh)
    return root


def _product(tmp: str, *, origin: str | None, dirty: bool = False, detach: bool = False) -> str:
    """A product checkout with the given `origin` remote URL (None = no origin remote). `dirty` leaves an
    uncommitted change; `detach` leaves HEAD detached."""
    root = _repo(tmp, "product")
    if origin:
        _git(root, "remote", "add", "origin", origin)
    if dirty:
        with open(os.path.join(root, "work.txt"), "w") as fh:
            fh.write("uncommitted work")
    if detach:
        sha = subprocess.run(["git", "-C", root, "rev-parse", "HEAD"],
                             capture_output=True, text=True).stdout.strip()
        _git(root, "checkout", "-q", "--detach", sha)
    return root


@contextlib.contextmanager
def _env(**kw):
    saved = {k: os.environ.get(k) for k in kw}
    try:
        for k, v in kw.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
        yield
    finally:
        for k, v in saved.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)


def _fetchable_product(tmp, name: str = "product") -> str:
    """A product clone whose `origin` is a local bare 'remote', so `git fetch origin` works OFFLINE and
    `origin/HEAD` resolves to a confident default. Its origin URL is a local path (NOT github.com), so the
    host-anchored identity gate is stubbed in tests that exercise the git mechanics — identity itself is proven
    non-vacuously by the `resolve_build_target` taxonomy above."""
    seed = _repo(tmp, name + "-seed")
    bare = os.path.join(tmp, name + "-remote.git")
    subprocess.run(["git", "clone", "--quiet", "--bare", seed, bare], capture_output=True, text=True)
    prod = os.path.join(tmp, name)
    subprocess.run(["git", "clone", "--quiet", bare, prod], capture_output=True, text=True)
    return prod


@contextlib.contextmanager
def _stub_identity(result):
    """Force the shared identity gate to a fixed verdict, so a test can drive `create_worktree`'s git mechanics
    with a locally-fetchable product whose origin is not a real github.com URL."""
    orig = mechanic_build._resolve_verified_identity
    mechanic_build._resolve_verified_identity = lambda cwd=None: result
    try:
        yield
    finally:
        mechanic_build._resolve_verified_identity = orig


def _head_branch(repo: str) -> str:
    return subprocess.run(["git", "-C", repo, "rev-parse", "--abbrev-ref", "HEAD"],
                          capture_output=True, text=True).stdout.strip()


class TestWorktreeNameValidation(unittest.TestCase):
    """`<name>` flows into a filesystem path, a git ref, and the stdout channel — so traversal, a leading dash
    (a git option), and a newline (a forged env line) must all be refused before anything is created."""

    def test_accepts_ordinary_build_names(self):
        for good in ("902-worktree-rule", "665", "a_b.c", "issue-42-fix"):
            self.assertTrue(mechanic_build._valid_worktree_name(good), good)

    def test_rejects_traversal_options_separators_and_newlines(self):
        for bad in ("", "../evil", "..", "a/b", "a\\b", "-b", "-force", ".hidden", "a..b",
                    "a\nb", "a\tb", "a b", "évil", "x" * 200):
            self.assertFalse(mechanic_build._valid_worktree_name(bad), repr(bad))

    def test_bad_name_refuses_before_touching_identity_or_git(self):
        # No fixtures, no stub: the name gate is first, so a hostile name can never reach resolution or git.
        self.assertEqual(mechanic_build.create_worktree("../evil"), (None, None, None, "bad-name"))


class TestCreateWorktree(unittest.TestCase):
    """The `worktree` verb: an isolated worktree cut from origin/<default>, homed in the mechanic's own state
    area, that NEVER disturbs the shared checkout — even when a peer has left it dirty and off-default."""

    def test_cuts_an_isolated_worktree_homed_in_the_mechanic_state_area(self):
        with tempfile.TemporaryDirectory() as tmp:
            m = _mechanic(tmp)
            p = _fetchable_product(tmp)
            with _stub_identity((p, _TARGET, None)):
                path, slug, base, refusal = mechanic_build.create_worktree("902-x", cwd=m)
            self.assertIsNone(refusal)
            self.assertEqual(slug, _TARGET)
            self.assertTrue(base.startswith("origin/"))        # the base ref it was cut from, for diffing
            expected = os.path.join(m, ".engine", "mechanic", "worktrees", "902-x")
            self.assertEqual(os.path.realpath(path), os.path.realpath(expected))
            self.assertTrue(os.path.isdir(path))
            # a LINKED worktree of the product: its .git is a file pointing back into the product clone
            self.assertTrue(os.path.isfile(os.path.join(path, ".git")))
            # the branch was created in the product clone
            self.assertIsNotNone(mechanic_build._run(
                ["git", "-C", p, "rev-parse", "--verify", "--quiet", "refs/heads/claude/902-x"]))

    def test_shared_checkout_head_and_tree_untouched_even_when_dirty_and_off_default(self):
        # The whole point of dropping the cleanliness leg: a peer mid-build (dirty tree, foreign branch) is a
        # legitimate state, and cutting a worktree must leave that peer exactly as it was.
        with tempfile.TemporaryDirectory() as tmp:
            m = _mechanic(tmp)
            p = _fetchable_product(tmp)
            subprocess.run(["git", "-C", p, "checkout", "-q", "-b", "peer-wip"], capture_output=True, text=True)
            with open(os.path.join(p, "peer.txt"), "w") as fh:
                fh.write("a peer's uncommitted work")
            with _stub_identity((p, _TARGET, None)):
                path, _slug, _base, refusal = mechanic_build.create_worktree("902-y", cwd=m)
            self.assertIsNone(refusal)
            self.assertTrue(os.path.isdir(path))
            self.assertEqual(_head_branch(p), "peer-wip")                     # HEAD not moved
            status = subprocess.run(["git", "-C", p, "status", "--porcelain"],
                                    capture_output=True, text=True).stdout
            self.assertIn("peer.txt", status)                                # the peer's WIP still there

    def test_not_a_mechanic_when_run_from_a_tree_with_no_build_target(self):
        # Safety property (invoked from inside a product worktree, whose manifest records no product_build_target):
        # the REAL identity gate refuses, so the verb can never recurse product-worktrees-inside-product-worktrees.
        with tempfile.TemporaryDirectory() as tmp:
            notmech = _mechanic(tmp, target=None)
            with _env(ENGINE_PRODUCT_CHECKOUT=None):
                self.assertEqual(mechanic_build.create_worktree("x", cwd=notmech),
                                 (None, None, None, "not-a-mechanic"))

    def test_worktree_exists_refuses(self):
        with tempfile.TemporaryDirectory() as tmp:
            m = _mechanic(tmp)
            p = _fetchable_product(tmp)
            dest = os.path.join(m, ".engine", "mechanic", "worktrees", "dup")
            os.makedirs(dest)
            with _stub_identity((p, _TARGET, None)):
                self.assertEqual(mechanic_build.create_worktree("dup", cwd=m),
                                 (None, None, None, "worktree-exists"))

    def test_branch_exists_refuses(self):
        with tempfile.TemporaryDirectory() as tmp:
            m = _mechanic(tmp)
            p = _fetchable_product(tmp)
            subprocess.run(["git", "-C", p, "branch", "claude/taken"], capture_output=True, text=True)
            with _stub_identity((p, _TARGET, None)):
                self.assertEqual(mechanic_build.create_worktree("taken", cwd=m),
                                 (None, None, None, "branch-exists"))

    def test_default_unresolved_refuses_rather_than_guess_a_base(self):
        with tempfile.TemporaryDirectory() as tmp:
            m = _mechanic(tmp)
            p = _product(tmp, origin="git@github.com:acme/product.git")   # a remote, but no origin/HEAD
            with _stub_identity((p, _TARGET, None)):
                self.assertEqual(mechanic_build.create_worktree("no-default", cwd=m),
                                 (None, None, None, "default-unresolved"))

    def test_prune_clears_stale_registration_but_a_lingering_branch_stays_fail_closed(self):
        # Crash recovery, done safely. After a worktree directory vanishes, the phantom registration must not
        # block a re-cut — but the branch it left behind might carry unpushed commits, so the verb refuses
        # (branch-exists) rather than silently deleting it. Deleting the branch is the operator's deliberate act.
        with tempfile.TemporaryDirectory() as tmp:
            m = _mechanic(tmp)
            p = _fetchable_product(tmp)
            with _stub_identity((p, _TARGET, None)):
                path, _s, _b, refusal = mechanic_build.create_worktree("reuse", cwd=m)
                self.assertIsNone(refusal)
                shutil.rmtree(path)                          # the directory vanishes; the registration is stale
                # Re-cut the same name: prune clears the phantom registration, so the ONLY block is the branch —
                # never a silent clobber of possible unpushed work.
                self.assertEqual(mechanic_build.create_worktree("reuse", cwd=m),
                                 (None, None, None, "branch-exists"))
                # With the registration pruned, the lingering branch can now be deleted deliberately, and the
                # re-cut succeeds — proving prune did clear the stale registration.
                self.assertIsNotNone(mechanic_build._run(["git", "-C", p, "branch", "-D", "claude/reuse"]))
                path3, _s3, _b3, refusal3 = mechanic_build.create_worktree("reuse", cwd=m)
            self.assertIsNone(refusal3)
            self.assertTrue(os.path.isdir(path3))

    def test_engine_root_unresolved_refuses(self):
        with tempfile.TemporaryDirectory() as tmp:
            m = _mechanic(tmp)
            p = _fetchable_product(tmp)
            orig = checkout_health.engine_common_checkout
            checkout_health.engine_common_checkout = lambda cwd=None: None
            try:
                with _stub_identity((p, _TARGET, None)):
                    self.assertEqual(mechanic_build.create_worktree("x", cwd=m),
                                     (None, None, None, "engine-root-unresolved"))
            finally:
                checkout_health.engine_common_checkout = orig

    def test_fetch_failed_refuses_when_fetch_never_succeeds(self):
        with tempfile.TemporaryDirectory() as tmp:
            m = _mechanic(tmp)
            p = _fetchable_product(tmp)
            orig = mechanic_build._fetch_origin_with_retry
            mechanic_build._fetch_origin_with_retry = lambda product_path: False
            try:
                with _stub_identity((p, _TARGET, None)):
                    self.assertEqual(mechanic_build.create_worktree("902-f", cwd=m),
                                     (None, None, None, "fetch-failed"))
            finally:
                mechanic_build._fetch_origin_with_retry = orig

    def test_origin_moved_refuses_when_origin_repoints_mid_operation(self):
        # The verify-then-write guard: origin is captured before the fetch and re-read before the cut; a change
        # between the two must refuse rather than write against a repository nobody verified.
        with tempfile.TemporaryDirectory() as tmp:
            m = _mechanic(tmp)
            p = _fetchable_product(tmp)
            seq = iter(["git@github.com:acme/product.git", "git@github.com:acme/MOVED.git"])
            orig_url = mechanic_build._git_origin_url
            orig_fetch = mechanic_build._fetch_origin_with_retry
            mechanic_build._git_origin_url = lambda path: next(seq)
            mechanic_build._fetch_origin_with_retry = lambda product_path: True
            try:
                with _stub_identity((p, _TARGET, None)):
                    self.assertEqual(mechanic_build.create_worktree("902-o", cwd=m),
                                     (None, None, None, "origin-moved"))
            finally:
                mechanic_build._git_origin_url = orig_url
                mechanic_build._fetch_origin_with_retry = orig_fetch

    def test_worktree_add_failure_refuses(self):
        with tempfile.TemporaryDirectory() as tmp:
            m = _mechanic(tmp)
            p = _fetchable_product(tmp)
            orig_run = mechanic_build._run

            def fake_run(cmd, cwd=None, timeout=30):
                if "worktree" in cmd and "add" in cmd:
                    return None                          # only the cut fails; every other git call is real
                return orig_run(cmd, cwd=cwd, timeout=timeout)

            mechanic_build._run = fake_run
            try:
                with _stub_identity((p, _TARGET, None)):
                    self.assertEqual(mechanic_build.create_worktree("902-a", cwd=m),
                                     (None, None, None, "worktree-add-failed"))
            finally:
                mechanic_build._run = orig_run


class TestFetchRetry(unittest.TestCase):
    """The bounded fetch retry that absorbs the transient shared-.git lock two concurrent cuts can cause."""

    def test_retries_up_to_the_cap_then_reports_failure(self):
        calls = []
        orig_run, orig_backoff = mechanic_build._run, mechanic_build._FETCH_BACKOFF_SEC
        mechanic_build._run = lambda cmd, cwd=None, timeout=30: calls.append(1) and None
        mechanic_build._FETCH_BACKOFF_SEC = 0
        try:
            self.assertFalse(mechanic_build._fetch_origin_with_retry("/x"))
            self.assertEqual(len(calls), mechanic_build._FETCH_ATTEMPTS)   # exhausted the cap, no more
        finally:
            mechanic_build._run, mechanic_build._FETCH_BACKOFF_SEC = orig_run, orig_backoff

    def test_returns_on_first_success_without_retrying(self):
        calls = []
        orig_run = mechanic_build._run
        mechanic_build._run = lambda cmd, cwd=None, timeout=30: (calls.append(1), "ok")[1]
        try:
            self.assertTrue(mechanic_build._fetch_origin_with_retry("/x"))
            self.assertEqual(len(calls), 1)                                # first attempt succeeded, no retry
        finally:
            mechanic_build._run = orig_run


class TestWorktreeCLI(unittest.TestCase):
    """Channel discipline for the creating verb: the DISTINCT `ENGINE_PRODUCT_WORKTREE` name to stdout on
    success (never overwriting the durable `ENGINE_PRODUCT_CHECKOUT` pointer), plain reason to stderr on refusal
    with stdout empty."""

    def _run_cli(self, monkeypatched_result):
        orig = mechanic_build.create_worktree
        mechanic_build.create_worktree = lambda name, cwd=None: monkeypatched_result
        out, err = io.StringIO(), io.StringIO()
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = mechanic_build.main(["worktree", "902-x"])
        finally:
            mechanic_build.create_worktree = orig
        return rc, out.getvalue(), err.getvalue()

    def test_success_emits_the_distinct_worktree_env_var_and_base(self):
        rc, out, err = self._run_cli(
            ("/home/me/eng/.engine/mechanic/worktrees/902-x", _TARGET, "origin/main", None))
        self.assertEqual(rc, 0)
        self.assertEqual(out, "ENGINE_PRODUCT_WORKTREE=/home/me/eng/.engine/mechanic/worktrees/902-x\n"
                              "ENGINE_PRODUCT_BASE=origin/main\n"
                              f"GITHUB_REPOSITORY={_TARGET}\n")
        self.assertNotIn("ENGINE_PRODUCT_CHECKOUT", out)   # never overwrites the durable pointer
        self.assertEqual(err, "")

    def test_refusal_goes_to_stderr_with_empty_stdout_and_nonzero_exit(self):
        rc, out, err = self._run_cli((None, None, None, "branch-exists"))
        self.assertNotEqual(rc, 0)
        self.assertEqual(out, "")
        self.assertIn("branch", err.lower())
        self.assertNotIn("branch-exists", err)             # prose, never the raw token

    def test_bad_name_refuses_through_the_real_verb_end_to_end(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = mechanic_build.main(["worktree", "../evil"])
        self.assertNotEqual(rc, 0)
        self.assertEqual(out.getvalue(), "")
        self.assertIn("not allowed", err.getvalue().lower())


class TestBeltHostAnchor(unittest.TestCase):
    """The fail-closed, host-anchored belt (moved from checkout_health): the last line of defence behind the
    guardrail-ack. It must DENY on any doubt — and MUST reject a look-alike host that merely CONTAINS github.com."""

    def test_belt_true_on_matching_genuine_github_origin(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = _product(tmp, origin="git@github.com:acme/product.git")
            self.assertTrue(mechanic_build.product_checkout_matches(_TARGET, p))
            # slug_eq normalizes case / .git — an SSH-vs-HTTPS-vs-case skew still matches
            self.assertTrue(mechanic_build.product_checkout_matches("ACME/Product", p))

    def test_belt_true_on_a_mixed_case_genuine_github_host(self):
        # Host names are case-insensitive by specification, so `GitHub.com` IS a genuine github.com origin: a
        # checkout on it that matches the target must classify `ok`, never `untrusted-host` (#625). The belt was
        # over-refusing a legitimate build. The look-alike rejection stays case-independent (structural anchor).
        self.assertEqual(mechanic_build._github_slug("https://GitHub.com/acme/product.git"), _TARGET)
        self.assertEqual(mechanic_build._github_slug("git@GitHub.com:acme/product.git"), _TARGET)
        self.assertIsNone(mechanic_build._github_slug("https://notGitHub.com/acme/product.git"))
        # U+0130 folds to ASCII `i` under Unicode case-folding: `re.ASCII` on the belt's flag must keep this
        # homograph host out, else it would authorize a cross-repo write against a look-alike origin (#625).
        self.assertIsNone(mechanic_build._github_slug("https://gİthub.com/acme/product.git"))
        with tempfile.TemporaryDirectory() as tmp:
            p = _product(tmp, origin="https://GitHub.com/acme/product.git")
            self.assertTrue(mechanic_build.product_checkout_matches(_TARGET, p))
            subprocess.run(["rm", "-rf", p], check=False)
            bad = _product(tmp, origin="https://notGitHub.com/acme/product.git")
            self.assertIs(mechanic_build.product_checkout_matches(_TARGET, bad), False)

    def test_belt_denies_look_alike_host(self):
        # BLOCKING-2 regression: notgithub.com CONTAINS "github.com" as a substring; an unanchored parse would
        # extract acme/product and the belt would PASS an attacker-controlled host — under subprocess-in-place
        # that is local code execution. The host anchor must DENY it.
        with tempfile.TemporaryDirectory() as tmp:
            for bad in ("https://notgithub.com/acme/product.git",
                        "git@evilgithub.com:acme/product.git",
                        "https://github.com.evil.com/acme/product.git"):
                p = _product(tmp, origin=bad)
                self.assertIs(mechanic_build.product_checkout_matches(_TARGET, p), False, bad)
                # a fresh product dir each time so the remote does not accumulate
                subprocess.run(["rm", "-rf", p], check=False)

    def test_belt_false_on_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = _product(tmp, origin="https://github.com/acme/other.git")
            self.assertFalse(mechanic_build.product_checkout_matches(_TARGET, p))

    def test_belt_fails_closed_on_missing_inputs(self):
        # assertIs(..., False), NOT assertFalse: the load-bearing invariant is "False, NEVER None" — a None return
        # would flip the belt fail-OPEN. assertFalse(None) passes, so it would not catch that regression.
        with tempfile.TemporaryDirectory() as tmp:
            p = _product(tmp, origin=None)   # no origin remote configured
            self.assertIs(mechanic_build.product_checkout_matches(_TARGET, p), False)    # unreadable origin
            self.assertIs(mechanic_build.product_checkout_matches("", p), False)         # blank slug
            self.assertIs(mechanic_build.product_checkout_matches(None, p), False)       # None slug
            self.assertIs(mechanic_build.product_checkout_matches(_TARGET, ""), False)   # blank path
            self.assertIs(mechanic_build.product_checkout_matches(_TARGET, os.path.join(tmp, "nope")), False)


class TestResolveBuildTarget(unittest.TestCase):
    """The ordered, mutually-exclusive refusal taxonomy, and the verified path — the whole authorization."""

    def test_verified_returns_path_and_target_slug(self):
        with tempfile.TemporaryDirectory() as tmp:
            m = _mechanic(tmp)
            p = _product(tmp, origin="git@github.com:acme/product.git")
            with _env(ENGINE_PRODUCT_CHECKOUT=p):
                path, slug, refusal = mechanic_build.resolve_build_target(cwd=m)
            self.assertIsNone(refusal)
            self.assertEqual(path, p)
            self.assertEqual(slug, _TARGET)

    def test_in_place_origin_resolves_via_git_C_not_process_cwd(self):
        # Non-vacuous in-place proof: from the verified checkout PATH, origin resolves to the DISTINCT product
        # slug via `git -C` — never the process cwd (which is this repo's own StarshipSuperjam/engine-template).
        # This is why the mechanic can run the checkout's own tools + gh in-place with no GITHUB_REPOSITORY leak.
        with tempfile.TemporaryDirectory() as tmp:
            m = _mechanic(tmp)
            p = _product(tmp, origin="git@github.com:acme/product.git")
            with _env(ENGINE_PRODUCT_CHECKOUT=p):
                path, _slug, _r = mechanic_build.resolve_build_target(cwd=m)
            self.assertEqual(mechanic_build._github_slug(mechanic_build._git_origin_url(path)), _TARGET)

    def test_refuse_not_a_mechanic(self):
        with tempfile.TemporaryDirectory() as tmp:
            m = _mechanic(tmp, target=None)
            self.assertEqual(mechanic_build.resolve_build_target(cwd=m), (None, None, "not-a-mechanic"))

    def test_refuse_path_unset(self):
        with tempfile.TemporaryDirectory() as tmp:
            m = _mechanic(tmp)
            with _env(ENGINE_PRODUCT_CHECKOUT=None):   # no env, no fallback file
                self.assertEqual(mechanic_build.resolve_build_target(cwd=m), (None, None, "path-unset"))

    def test_refuse_checkout_unreadable(self):
        with tempfile.TemporaryDirectory() as tmp:
            m = _mechanic(tmp)
            p = _product(tmp, origin=None)   # a real dir but no origin remote
            with _env(ENGINE_PRODUCT_CHECKOUT=p):
                self.assertEqual(mechanic_build.resolve_build_target(cwd=m), (None, None, "checkout-unreadable"))

    def test_refuse_origin_untrusted_host(self):
        with tempfile.TemporaryDirectory() as tmp:
            m = _mechanic(tmp)
            p = _product(tmp, origin="https://notgithub.com/acme/product.git")
            with _env(ENGINE_PRODUCT_CHECKOUT=p):
                self.assertEqual(mechanic_build.resolve_build_target(cwd=m),
                                 (None, None, "origin-untrusted-host"))

    def test_refuse_origin_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            m = _mechanic(tmp)
            p = _product(tmp, origin="git@github.com:acme/other.git")
            with _env(ENGINE_PRODUCT_CHECKOUT=p):
                self.assertEqual(mechanic_build.resolve_build_target(cwd=m), (None, None, "origin-mismatch"))

    def test_refuse_checkout_unhealthy_when_dirty(self):
        with tempfile.TemporaryDirectory() as tmp:
            m = _mechanic(tmp)
            p = _product(tmp, origin="git@github.com:acme/product.git", dirty=True)
            with _env(ENGINE_PRODUCT_CHECKOUT=p):
                self.assertEqual(mechanic_build.resolve_build_target(cwd=m), (None, None, "checkout-unhealthy"))

    def test_refuse_checkout_unhealthy_when_detached(self):
        with tempfile.TemporaryDirectory() as tmp:
            m = _mechanic(tmp)
            p = _product(tmp, origin="git@github.com:acme/product.git", detach=True)
            with _env(ENGINE_PRODUCT_CHECKOUT=p):
                self.assertEqual(mechanic_build.resolve_build_target(cwd=m), (None, None, "checkout-unhealthy"))

    def test_never_returns_a_path_without_belt_and_health(self):
        # The pinned invariant: a path comes back ONLY through the belt AND the health check. Every refusal above
        # returns path None; the verified case returns a path for which BOTH gates independently pass.
        with tempfile.TemporaryDirectory() as tmp:
            m = _mechanic(tmp)
            p = _product(tmp, origin="git@github.com:acme/product.git")
            with _env(ENGINE_PRODUCT_CHECKOUT=p):
                path, slug, refusal = mechanic_build.resolve_build_target(cwd=m)
            self.assertIsNotNone(path)
            self.assertTrue(mechanic_build.product_checkout_matches(slug, path))   # belt independently passes
            self.assertEqual(checkout_health.checkout_lossless(path)[0], True)     # health independently passes


class TestPreflightCLI(unittest.TestCase):
    """Channel discipline (SERIOUS-3): verified env to stdout on success; plain reason to stderr on refusal;
    stdout EMPTY on refusal — so `cd "$(… preflight)"` can never consume a refusal string."""

    def _run_cli(self, monkeypatched_result):
        orig = mechanic_build.resolve_build_target
        mechanic_build.resolve_build_target = lambda cwd=None: monkeypatched_result
        out, err = io.StringIO(), io.StringIO()
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = mechanic_build.main(["preflight"])
        finally:
            mechanic_build.resolve_build_target = orig
        return rc, out.getvalue(), err.getvalue()

    def test_success_emits_only_env_to_stdout(self):
        rc, out, err = self._run_cli(("/home/me/product", _TARGET, None))
        self.assertEqual(rc, 0)
        self.assertEqual(out, f"ENGINE_PRODUCT_CHECKOUT=/home/me/product\nGITHUB_REPOSITORY={_TARGET}\n")
        self.assertEqual(err, "")

    def test_refusal_goes_to_stderr_with_empty_stdout_and_nonzero_exit(self):
        rc, out, err = self._run_cli((None, None, "origin-untrusted-host"))
        self.assertNotEqual(rc, 0)
        self.assertEqual(out, "")                                   # stdout MUST be empty on refusal
        self.assertIn("github.com", err)                            # the plain reason + remedy, not a raw token
        self.assertNotIn("origin-untrusted-host", err)              # the operator sees prose, never the token


if __name__ == "__main__":
    unittest.main()
