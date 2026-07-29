#!/usr/bin/env python3
"""Tests for license_seeds — the template-LICENSE recognizer and its append-only seed set (#471).

Lock the behaviours a non-engineer cannot read code to verify: the engine recognizes its OWN shipped
template license (so the standing detector and the first-run clear can offer to clear it), while an adopter's
own license — a different license, or the same text with the copyright renamed to themselves — is PRESERVED,
never matched. Cosmetic-only variance (line endings, blank lines, a BOM, a missing trailing newline) still
matches; any substantive difference does not.
"""
from __future__ import annotations

import unittest

import license_seeds


class TestSeedSetShape(unittest.TestCase):
    def test_current_seed_is_the_tail(self):
        self.assertEqual(license_seeds.CURRENT_SEED, license_seeds.HISTORICAL_SEEDS[-1])

    def test_seed_set_is_a_nonempty_append_only_tuple(self):
        # A tuple (not a list) signals the append-only, order-bearing contract at the type level.
        self.assertIsInstance(license_seeds.HISTORICAL_SEEDS, tuple)
        self.assertGreaterEqual(len(license_seeds.HISTORICAL_SEEDS), 1)

    def test_carries_exactly_one_member(self):
        # The set carries one member (maintainer decision, #471): the current Apache-2.0 + Commons Clause seed
        # only. A relicense APPENDS a new seed — updating this count is the deliberate act that records the new era.
        self.assertEqual(len(license_seeds.HISTORICAL_SEEDS), 1)


class TestRecognizeMatches(unittest.TestCase):
    def test_matches_every_historical_seed(self):
        for i, seed in enumerate(license_seeds.HISTORICAL_SEEDS):
            self.assertTrue(license_seeds.recognize(seed), f"seed #{i} must self-recognize")

    def test_matches_current_seed(self):
        self.assertTrue(license_seeds.recognize(license_seeds.CURRENT_SEED))


class TestRecognizeNormalization(unittest.TestCase):
    """Cosmetic-only variance still matches (a traveled copy saved on another OS is still the engine's seed)."""

    def setUp(self):
        self.base = license_seeds.CURRENT_SEED

    def test_crlf_line_endings(self):
        self.assertTrue(license_seeds.recognize(self.base.replace("\n", "\r\n")), "CRLF (Windows-saved copy)")

    def test_cr_line_endings(self):
        self.assertTrue(license_seeds.recognize(self.base.replace("\n", "\r")), "bare CR")

    def test_missing_trailing_newline(self):
        self.assertTrue(license_seeds.recognize(self.base.rstrip("\n")))

    def test_leading_byte_order_mark(self):
        self.assertTrue(license_seeds.recognize("﻿" + self.base))

    def test_extra_blank_lines(self):
        self.assertTrue(license_seeds.recognize(self.base.replace("\n\n", "\n\n\n")))


class TestRecognizePreservesOnDoubt(unittest.TestCase):
    """Preserve-on-doubt: anything substantively different from a shipped seed is NOT matched."""

    def test_renamed_licensor_is_preserved(self):
        # An adopter who kept the engine's exact text but put THEIR name on it — never touched.
        mine = license_seeds.CURRENT_SEED.replace("StarshipSuperjam", "Acme Corp")
        self.assertFalse(license_seeds.recognize(mine))

    def test_plain_apache_without_commons_clause_is_preserved(self):
        # An adopter who independently chose plain Apache-2.0: the Commons Clause header is absent, so the
        # full-text match fails (the reason the recognizer is not a body-only match).
        seed = license_seeds.CURRENT_SEED
        marker = "---------------------------------------------------------------------------"
        apache_only = seed.split(marker, 1)[1].lstrip("\n") if marker in seed else seed
        self.assertFalse(license_seeds.recognize(apache_only))

    def test_extra_appended_term_is_preserved(self):
        self.assertFalse(license_seeds.recognize(license_seeds.CURRENT_SEED + "\n\nExtra adopter term.\n"))

    def test_empty_is_preserved(self):
        self.assertFalse(license_seeds.recognize(""))

    def test_none_is_preserved(self):
        self.assertFalse(license_seeds.recognize(None))

    def test_unrelated_text_is_preserved(self):
        self.assertFalse(license_seeds.recognize("MIT License\n\nPermission is hereby granted, free of charge...\n"))


if __name__ == "__main__":
    unittest.main()
