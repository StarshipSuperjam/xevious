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


class TestBeltHostAnchor(unittest.TestCase):
    """The fail-closed, host-anchored belt (moved from checkout_health): the last line of defence behind the
    guardrail-ack. It must DENY on any doubt — and MUST reject a look-alike host that merely CONTAINS github.com."""

    def test_belt_true_on_matching_genuine_github_origin(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = _product(tmp, origin="git@github.com:acme/product.git")
            self.assertTrue(mechanic_build.product_checkout_matches(_TARGET, p))
            # slug_eq normalizes case / .git — an SSH-vs-HTTPS-vs-case skew still matches
            self.assertTrue(mechanic_build.product_checkout_matches("ACME/Product", p))

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
