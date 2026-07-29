#!/usr/bin/env python3
"""Tests for the engine-home contribution classification (issue #556) — the safety core of the
external-contribution leak-check narrowing.

`travels_to_engine_home` decides which engine content may ride upstream in a contribution back to the engine's
OWN home. Getting it wrong in the under-flag direction is a leak (this deployment's identity/state/tuning
riding into the shared template), so these tests pin: the exact-full-slug home switch (a look-alike must never
open it), the product-travels / instance-state-flags partition over the REAL owned set (the completeness net,
so a future committed store can't silently start travelling), and the fail-closed accessor.
"""
from __future__ import annotations
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import module_coherence as mc  # noqa: E402
import repo_identity  # noqa: E402  (home_repository + its manifest reader live here now; mc re-exports them)


class TestSlugEq(unittest.TestCase):
    def test_exact_full_slug_matches_case_and_git_and_slash_insensitively(self):
        self.assertTrue(mc.slug_eq("StarshipSuperjam/engine-template", "starshipsuperjam/engine-template"))
        self.assertTrue(mc.slug_eq("Acme/Repo.git", "acme/repo"))
        self.assertTrue(mc.slug_eq("acme/repo/", "acme/repo"))

    def test_lookalikes_never_match(self):
        real = "StarshipSuperjam/engine-template"
        for other in ("attacker/engine-template",           # different owner
                      "StarshipSuperjam/engine-template-x",  # different name
                      "engine-template",                      # name only, no owner
                      "StarshipSuperjam/ENGINE",              # different name
                      "notStarshipSuperjam/engine-template"): # substring owner
            self.assertFalse(mc.slug_eq(real, other), other)

    def test_none_is_never_equal(self):
        self.assertFalse(mc.slug_eq(None, None))
        self.assertFalse(mc.slug_eq("acme/repo", None))
        self.assertFalse(mc.slug_eq(None, "acme/repo"))


class TestHomeRepositoryAccessor(unittest.TestCase):
    def test_reads_the_real_manifest_home(self):
        # This construction repo records its own home; the accessor returns a real owner/repo slug.
        home = mc.home_repository()
        self.assertIsInstance(home, str)
        self.assertIn("/", home)

    def test_malformed_manifest_raises_loud_not_silent_none(self):
        # A present-but-malformed manifest must RAISE loud — the fail-loud commitment overlay_disclosure and
        # release_cut rely on (a corrupt manifest must not read as "no home"). The submit flow degrades this to
        # its strict full check LOCALLY (proven in test_submit), not by silencing the accessor. home_repository
        # reads the manifest through repo_identity._manifest (mc re-exports the accessor), so patch there.
        with mock.patch.object(repo_identity, "_manifest", side_effect=ValueError("bad json")):
            with self.assertRaises(ValueError):
                mc.home_repository()

    def test_blank_or_absent_home_is_none(self):
        for manifest in ({}, {"home_repository": ""}, {"home_repository": "  "}, {"home_repository": 5}, None):
            with mock.patch.object(repo_identity, "_manifest", return_value=manifest):
                self.assertIsNone(mc.home_repository())


class TestHomeTravelClassification(unittest.TestCase):
    """The completeness net: over the REAL engine-owned set, product/source travels and the deployment's
    accreted state stays flagged — so a future committed store cannot silently start travelling."""

    def setUp(self):
        self.owned = set(mc.engine_owned_paths(mc.discover_manifests()))

    def test_known_product_travels(self):
        for p in (".engine/tools/boot.py", ".engine/check/upstream-clean.json", ".engine/schemas/state.v1.json",
                  ".engine/knowledge/graph.json", ".engine/self-map.md",  # CI-required indexes
                  ".engine/pyproject.toml", ".engine/uv.lock", "AGENTS.md", ".engine/conduct/defaults.md"):
            self.assertTrue(mc.travels_to_engine_home(p), f"{p} must travel to the engine's home")

    def test_known_instance_state_and_private_content_never_travels(self):
        for p in (".engine/engine.json", ".engine/state/state.json", ".engine/product-spec-matrix.json",
                  ".engine/memory-backup/pointer.json", ".engine/audits/concern-list.json",
                  ".engine/erasures/proposal.json",
                  ".engine/operator-overrides.json", ".engine/conduct/operator.md",       # operator tuning
                  ".engine/provisioning/conduct-seed.md",                                   # maintainer seed
                  ".engine/contracts/instance/acme-eADR-0001.md"):                          # deployment records
            self.assertFalse(mc.travels_to_engine_home(p), f"{p} must NEVER travel (instance/operator content)")

    def test_no_owned_path_is_left_ambiguous(self):
        # Every owned path either travels (product) or is a recognised per-instance store. A NEW owned data
        # path outside the known instance locations fails here — forcing an explicit classification rather than
        # a silent travel/flag. (Runtime already default-flags the unknown, so this guards the over-flag side.)
        INSTANCE_PREFIXES = (".engine/state/", ".engine/audits/", ".engine/erasures/", ".engine/memory-backup/",
                             ".engine/memory/", ".engine/projects-sync/", ".engine/contracts/instance/")
        INSTANCE_FILES = {".engine/engine.json", ".engine/product-spec-matrix.json"}
        for p in self.owned:
            travels = mc.travels_to_engine_home(p)
            is_known_instance = p.startswith(INSTANCE_PREFIXES) or p in INSTANCE_FILES or p in mc.OPERATOR_CONFIG
            if travels:
                self.assertFalse(is_known_instance, f"{p} is a per-instance store but was classified to travel")
            else:
                self.assertTrue(is_known_instance,
                                f"{p} does not travel but is not a recognised instance store — classify it "
                                f"(product should travel; a new per-instance store should be listed)")


if __name__ == "__main__":
    unittest.main()
