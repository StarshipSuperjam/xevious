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


if __name__ == "__main__":
    unittest.main()
