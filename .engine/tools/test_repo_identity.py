#!/usr/bin/env python3
"""Tests for repo_identity — the one shared home-repo identity seam (#323 Slice 1).

The seam answers "is this checkout the engine's OWN home repo?" from a STRUCTURAL, non-inherited signal: the
checkout's on-disk git origin compared (slug-normalized) to the `home_repository` its manifest records. These
tests lock the contracts the scope detectors rely on, against throwaway offline git fixtures:

  - it reads the checkout's ON-DISK origin, NEVER this process's GITHUB_REPOSITORY env — so a fixture (or a
    nested deployed projection) is judged by the repo it IS, not an ambient env var;
  - it is MARKER-INDEPENDENT — a home repo whose CLAUDE.md carries no "construction governance" marker still
    reads as home (origin==home), and a downstream copy that happens to carry the marker still reads as a copy
    (origin!=home). This is the whole point of the re-key: the fragile text proxy is gone;
  - it fails TOWARD home — an unreadable origin, an absent/blank home, or a malformed manifest all read as home,
    the safe direction for the scope detectors that gate on it (a HARD safety check RUNS rather than silently
    no-opping).
"""
from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import repo_identity  # noqa: E402


def _mkdtemp(case: unittest.TestCase) -> str:
    """A throwaway dir that is removed when the test finishes (no accumulation in the system temp)."""
    d = tempfile.mkdtemp()
    case.addCleanup(lambda: shutil.rmtree(d, ignore_errors=True))
    return d

HOME = "StarshipSuperjam/engine-template"


def _git(root: str, *args: str) -> None:
    subprocess.run(["git", "-C", root, *args], capture_output=True, text=True, check=False)


def _repo(tmp: str, name: str, *, origin: "str | None", home: "str | None" = HOME) -> str:
    """A throwaway git checkout: an `origin` remote (omitted when None) and an `.engine/engine.json` recording
    `home` (omitted when None). Those two are the whole of what places a checkout — no file CONTENT is read,
    which is the point of the seam."""
    root = os.path.join(tmp, name)
    os.makedirs(os.path.join(root, ".engine"), exist_ok=True)
    _git(root, "init", "-q")
    if origin:
        _git(root, "remote", "add", "origin", origin)
    manifest: dict = {"engine_release": "0.0.0"}
    if home is not None:
        manifest["home_repository"] = home
    with open(os.path.join(root, ".engine", "engine.json"), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh)
    return root


class TestIsHomeRepo(unittest.TestCase):
    def setUp(self):
        self.tmp = _mkdtemp(self)

    def test_home_when_origin_equals_home(self):
        repo = _repo(self.tmp, "home", origin=f"https://github.com/{HOME}.git")
        self.assertTrue(repo_identity.is_home_repo(repo))

    def test_copy_when_origin_differs(self):
        repo = _repo(self.tmp, "copy", origin="https://github.com/adopter/their-product.git")
        self.assertFalse(repo_identity.is_home_repo(repo))

    def test_no_origin_fails_toward_home(self):
        repo = _repo(self.tmp, "noorigin", origin=None)
        self.assertTrue(repo_identity.is_home_repo(repo))

    def test_absent_home_fails_toward_home(self):
        repo = _repo(self.tmp, "nohome", origin="https://github.com/adopter/their-product.git", home=None)
        self.assertTrue(repo_identity.is_home_repo(repo))

    def test_malformed_manifest_fails_toward_home(self):
        repo = _repo(self.tmp, "corrupt", origin="https://github.com/adopter/their-product.git")
        with open(os.path.join(repo, ".engine", "engine.json"), "w", encoding="utf-8") as fh:
            fh.write("{ not valid json ")
        self.assertTrue(repo_identity.is_home_repo(repo))

    def test_origin_read_is_slug_normalized(self):
        # SSH transport, mixed case, trailing .git — still the home repo.
        repo = _repo(self.tmp, "skew", origin="git@github.com:starshipsuperjam/Engine-Template.git")
        self.assertTrue(repo_identity.is_home_repo(repo))

    def test_origin_read_ignores_github_repository_env(self):
        # is_home_repo must judge the checkout it is handed, not an ambient env var: a home checkout stays home
        # even when GITHUB_REPOSITORY names a different repo, and a copy stays a copy even when the env names home.
        home = _repo(self.tmp, "envhome", origin=f"https://github.com/{HOME}.git")
        copy = _repo(self.tmp, "envcopy", origin="https://github.com/adopter/their-product.git")
        with mock.patch.dict(os.environ, {"GITHUB_REPOSITORY": "adopter/their-product"}):
            self.assertTrue(repo_identity.is_home_repo(home))
        with mock.patch.dict(os.environ, {"GITHUB_REPOSITORY": HOME}):
            self.assertFalse(repo_identity.is_home_repo(copy))


class TestOriginSlug(unittest.TestCase):
    def setUp(self):
        self.tmp = _mkdtemp(self)

    def test_reads_the_on_disk_remote(self):
        repo = _repo(self.tmp, "r", origin="https://github.com/owner/name.git")
        self.assertEqual(repo_identity.origin_slug(repo), "owner/name")

    def test_none_when_no_remote(self):
        repo = _repo(self.tmp, "r", origin=None)
        self.assertIsNone(repo_identity.origin_slug(repo))

    def test_none_on_a_non_github_remote(self):
        repo = _repo(self.tmp, "r", origin="https://gitlab.com/owner/name.git")
        self.assertIsNone(repo_identity.origin_slug(repo))

    def test_rejects_look_alike_hosts(self):
        # The host is anchored to the scheme/userinfo boundary: a look-alike host that merely ENDS in or
        # CONTAINS "github.com" must not mis-parse into a slug slug_eq would then read as home.
        for i, url in enumerate((
            "https://notgithub.com/evil/repo.git",
            "https://evilgithub.com/StarshipSuperjam/engine-template.git",
            "https://gitlab.com/github.com/foo/bar.git",  # github.com as a path segment under another host
        )):
            repo = _repo(self.tmp, f"la{i}", origin=url)
            self.assertIsNone(repo_identity.origin_slug(repo), f"{url} must not parse to a slug")

    def test_accepts_the_real_transports(self):
        for i, (url, want) in enumerate((
            ("https://github.com/owner/name.git", "owner/name"),
            ("git@github.com:owner/name.git", "owner/name"),
            ("ssh://git@github.com/owner/name", "owner/name"),
        )):
            repo = _repo(self.tmp, f"ok{i}", origin=url)
            self.assertEqual(repo_identity.origin_slug(repo), want, url)

    def test_accepts_a_mixed_case_host(self):
        # Host names are case-insensitive by specification, so `GitHub.com` IS the real host and must parse like
        # `github.com` across every transport (#625). A mixed-case LOOK-ALIKE still rejects: IGNORECASE folds
        # only the literal `github.com`, never the structural anchors that reject a look-alike.
        for i, url in enumerate((
            "https://GitHub.com/owner/name.git",
            "git@GitHub.com:owner/name.git",
            "ssh://git@GitHub.COM/owner/name",
        )):
            repo = _repo(self.tmp, f"mc{i}", origin=url)
            self.assertEqual(repo_identity.origin_slug(repo), "owner/name", url)
        for i, url in enumerate((
            "https://notGitHub.com/evil/repo.git",
            "https://EvilGitHub.com/StarshipSuperjam/engine-template.git",
            # U+0130 folds to ASCII `i` under Unicode case-folding — `re.ASCII` on the flag must keep this
            # homograph host out, else it would read as the engine's home (#625).
            "https://gİthub.com/StarshipSuperjam/engine-template.git",
        )):
            repo = _repo(self.tmp, f"mcla{i}", origin=url)
            self.assertIsNone(repo_identity.origin_slug(repo), f"{url} must not parse to a slug")


class TestSlugPrimitives(unittest.TestCase):
    def test_normalize_casefolds_and_strips_git_suffix_and_slash(self):
        self.assertEqual(repo_identity.normalize_slug("StarshipSuperjam/Engine-Template.git/"),
                         "starshipsuperjam/engine-template")

    def test_normalize_blank_is_none(self):
        self.assertIsNone(repo_identity.normalize_slug("   "))
        self.assertIsNone(repo_identity.normalize_slug(None))

    def test_slug_eq_is_exact_full_slug_not_name_only(self):
        self.assertTrue(repo_identity.slug_eq("Owner/Repo", "owner/repo.git"))
        self.assertFalse(repo_identity.slug_eq("owner/repo", "someone-else/repo"))
        self.assertFalse(repo_identity.slug_eq("owner/repo", None))

    def test_is_downstream_copy_defaults_and_safe_direction(self):
        self.assertTrue(repo_identity.is_downstream_copy("adopter/product", HOME))
        self.assertFalse(repo_identity.is_downstream_copy(HOME, HOME))
        self.assertFalse(repo_identity.is_downstream_copy(None, HOME))
        self.assertFalse(repo_identity.is_downstream_copy("adopter/product", None))

    def test_home_repository_reads_the_manifest(self):
        tmp = _mkdtemp(self)
        repo = _repo(tmp, "r", origin=None, home="owner/name")
        self.assertEqual(repo_identity.home_repository(repo), "owner/name")

    def test_home_repository_raises_on_a_malformed_manifest(self):
        # The fail-LOUD contract the update path (module_manager/overlay_disclosure/release_cut) relies on.
        tmp = _mkdtemp(self)
        repo = _repo(tmp, "r", origin=None)
        with open(os.path.join(repo, ".engine", "engine.json"), "w", encoding="utf-8") as fh:
            fh.write("{ not valid json ")
        with self.assertRaises(Exception):
            repo_identity.home_repository(repo)


class TestIsDownstreamCopyStrict(unittest.TestCase):
    """The fail-LOUD complement of `is_home_repo`, and the CONTRAST between the two — pinned here, in the
    module that owns both, so a refactor that widens `is_downstream_copy`'s `try` (or collapses the strict
    form to `not is_home_repo`) breaks a test beside the contract instead of silently degrading a consumer two
    modules away into a reassuring, wrong silence."""

    def setUp(self):
        self.tmp = _mkdtemp(self)

    def test_agrees_with_is_home_repo_on_every_readable_case(self):
        cases = [("home", f"https://github.com/{HOME}.git", HOME, False),
                 ("copy", "https://github.com/acme/product.git", HOME, True),
                 ("nohome", "https://github.com/acme/product.git", None, False),
                 ("noorigin", None, HOME, False)]
        for name, origin, home, expected in cases:
            with self.subTest(case=name):
                repo = _repo(self.tmp, name, origin=origin, home=home)
                self.assertIs(repo_identity.is_downstream_copy_strict(repo), expected)
                self.assertIs(repo_identity.is_home_repo(repo), not expected)

    def test_a_malformed_manifest_raises_here_but_is_swallowed_by_is_home_repo(self):
        repo = _repo(self.tmp, "corrupt", origin="https://github.com/acme/product.git")
        with open(os.path.join(repo, ".engine", "engine.json"), "w", encoding="utf-8") as fh:
            fh.write("{ not valid json ")
        with self.assertRaises(Exception):
            repo_identity.is_downstream_copy_strict(repo)
        self.assertTrue(repo_identity.is_home_repo(repo),
                        "is_home_repo must keep failing TOWARD home — the two fail-directions are deliberate")


class TestGithubHostParsersAgree(unittest.TestCase):
    """#625 was a DRIFT bug: several hand-copied `github.com` host parsers scattered across the tree disagreed on
    case — one carried `re.IGNORECASE`, the others did not — so on a mixed-case origin they reached opposite
    conclusions. This pins the SHARED contract across every origin parser so the same divergence cannot silently
    recur: each must read a mixed-case host and reject the same look-alikes. It asserts only the common
    transports; `repo_identity`'s form deliberately accepts a few extra shapes (e.g. a `git://` scheme URL, via
    its `//` host boundary) the anchored `^(?:https?|ssh)://` forms do not, which is outside this contract."""

    def _parsers(self):
        # Lazy imports keep this focused module's top-level surface light; every parser is reached through a
        # uniform `url -> slug|None` adapter so the battery below hits all of them identically.
        import boot
        import execution_environment
        import first_run_health
        import mechanic_build

        def _via_regex(rx):
            return lambda u: (rx.search(u).group(1) if rx.search(u) else None)

        def _via_boot(u):
            # boot.repo_slug reads GITHUB_REPOSITORY first, then git origin; clear the env and inject the URL so
            # the regex path is what runs. patch.dict restores the env afterward.
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("GITHUB_REPOSITORY", None)
                with mock.patch.object(boot, "_run", return_value=u):
                    return boot.repo_slug()

        return {
            "repo_identity": _via_regex(repo_identity._GITHUB_SLUG_RE),
            "execution_environment": _via_regex(execution_environment._SLUG_RE),
            "first_run_health": _via_regex(first_run_health._GITHUB_SLUG_RE),
            "mechanic_build": mechanic_build._github_slug,
            "boot": _via_boot,
        }

    def test_every_parser_reads_a_mixed_case_host(self):
        for url in ("https://GitHub.com/owner/name.git",
                    "git@GitHub.com:owner/name.git",
                    "ssh://git@GitHub.COM/owner/name"):
            for name, parse in self._parsers().items():
                self.assertEqual(parse(url), "owner/name", f"{name} must read {url}")

    def test_every_parser_rejects_the_same_look_alikes(self):
        for url in ("https://notGitHub.com/owner/name.git",
                    "https://EvilGitHub.com/owner/name.git",
                    "https://github.com.evil.com/owner/name.git",
                    # U+0130 (LATIN CAPITAL LETTER I WITH DOT ABOVE) folds to ASCII `i` under Unicode
                    # case-folding: a homograph host that `re.ASCII` on the flags must keep out (#625).
                    "https://gİthub.com/owner/name.git"):
            for name, parse in self._parsers().items():
                self.assertIsNone(parse(url), f"{name} must reject {url}")


@contextlib.contextmanager
def _no_branch_env():
    """Run with the two branch env vars absent, restored on exit — so a runner that happens to export
    PROTECTED_BRANCH / GITHUB_DEFAULT_BRANCH can't mask the recorded/git resolution these tests exercise."""
    with mock.patch.dict(os.environ, clear=False):
        os.environ.pop("PROTECTED_BRANCH", None)
        os.environ.pop("GITHUB_DEFAULT_BRANCH", None)
        yield


def _branch_repo(tmp: str, name: str, *, default_branch: "str | None" = None,
                 origin_head: "str | None" = None) -> str:
    """A throwaway git checkout recording `default_branch` in its manifest (omitted when None) and, when
    `origin_head` is given, an `origin/HEAD` pointing at it (set WITHOUT a real remote fetch — the offline
    equivalent of what a clone leaves behind)."""
    root = os.path.join(tmp, name)
    os.makedirs(os.path.join(root, ".engine"), exist_ok=True)
    _git(root, "init", "-q")
    manifest: dict = {"engine_release": "0.0.0"}
    if default_branch is not None:
        manifest["default_branch"] = default_branch
    with open(os.path.join(root, ".engine", "engine.json"), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh)
    if origin_head is not None:
        _git(root, "remote", "add", "origin", "https://github.com/x/y.git")
        _git(root, "symbolic-ref", "refs/remotes/origin/HEAD", f"refs/remotes/origin/{origin_head}")
    return root


class TestDefaultBranch(unittest.TestCase):
    """The single recorded-value reader. UNLIKE `home_repository` it is fail-SOFT (see the malformed case)."""

    def setUp(self):
        self.tmp = _mkdtemp(self)

    def test_reads_the_recorded_default_branch(self):
        root = _branch_repo(self.tmp, "r", default_branch="master")
        self.assertEqual(repo_identity.default_branch(root), "master")

    def test_none_when_key_absent(self):
        root = _branch_repo(self.tmp, "r")
        self.assertIsNone(repo_identity.default_branch(root))

    def test_none_when_blank(self):
        root = _branch_repo(self.tmp, "r", default_branch="   ")
        self.assertIsNone(repo_identity.default_branch(root))

    def test_fail_soft_none_on_a_malformed_manifest(self):
        # The DELIBERATE CONTRAST with home_repository (which RAISES here): default_branch degrades to None so
        # boot — which reads it at IMPORT — can never crash the tree on a corrupt manifest, and every caller
        # falls through to its next source. `test_home_repository_raises_on_a_malformed_manifest` pins the other.
        root = _branch_repo(self.tmp, "r", default_branch="master")
        with open(os.path.join(root, ".engine", "engine.json"), "w", encoding="utf-8") as fh:
            fh.write("{ not valid json ")
        self.assertIsNone(repo_identity.default_branch(root))


class TestResolveDefaultBranch(unittest.TestCase):
    """The one shared resolver: env_var override -> recorded -> origin/HEAD -> 'main', always non-empty."""

    def setUp(self):
        self.tmp = _mkdtemp(self)

    def test_env_var_wins(self):
        root = _branch_repo(self.tmp, "r", default_branch="master")
        with mock.patch.dict(os.environ, {"PROTECTED_BRANCH": "from-env"}):
            self.assertEqual(repo_identity.resolve_default_branch(root), "from-env")

    def test_present_but_empty_env_falls_through(self):
        # The `or` idiom, NOT os.environ.get(k, default): a workflow expression that expanded to "" on a
        # payload-less trigger must fall through to the recorded value, never pin the gate to an empty branch.
        root = _branch_repo(self.tmp, "r", default_branch="master")
        with mock.patch.dict(os.environ, {"PROTECTED_BRANCH": ""}):
            self.assertEqual(repo_identity.resolve_default_branch(root), "master")

    def test_recorded_preferred_over_origin_head(self):
        root = _branch_repo(self.tmp, "r", default_branch="master", origin_head="trunk")
        with _no_branch_env():
            self.assertEqual(repo_identity.resolve_default_branch(root), "master")

    def test_origin_head_self_heals_a_pre_recorded_key_deployment(self):
        # A repo deployed BEFORE the recorded key existed: no manifest default, but git still knows it.
        root = _branch_repo(self.tmp, "r", default_branch=None, origin_head="master")
        with _no_branch_env():
            self.assertEqual(repo_identity.resolve_default_branch(root), "master")

    def test_main_is_the_last_resort(self):
        root = _branch_repo(self.tmp, "r")  # no recorded key, no origin/HEAD
        with _no_branch_env():
            self.assertEqual(repo_identity.resolve_default_branch(root), "main")

    def test_reads_the_named_env_var(self):
        root = _branch_repo(self.tmp, "r")
        with mock.patch.dict(os.environ, {"GITHUB_DEFAULT_BRANCH": "release"}):
            self.assertEqual(
                repo_identity.resolve_default_branch(root, env_var="GITHUB_DEFAULT_BRANCH"), "release")

    def test_always_returns_non_empty_even_on_a_malformed_manifest(self):
        root = _branch_repo(self.tmp, "r")
        with open(os.path.join(root, ".engine", "engine.json"), "w", encoding="utf-8") as fh:
            fh.write("{ not valid json ")
        with _no_branch_env():
            self.assertEqual(repo_identity.resolve_default_branch(root), "main")


if __name__ == "__main__":
    unittest.main()
