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

    def test_carries_two_members_after_the_apache_relicense(self):
        # Two members: the RETIRED Apache-2.0 + Commons Clause seed (index 0) and the CURRENT plain Apache-2.0
        # seed (the tail). The retired seed stays so any repo generated from the template before the relicense
        # still recognizes its lingering Commons Clause LICENSE. A future relicense APPENDS again — updating this
        # count is the deliberate act that records the new era (#471).
        self.assertEqual(len(license_seeds.HISTORICAL_SEEDS), 2)
        self.assertIn("Commons Clause", license_seeds.HISTORICAL_SEEDS[0])
        self.assertNotIn("Commons Clause", license_seeds.HISTORICAL_SEEDS[-1])

    def test_current_seed_is_plain_apache_without_commons_clause(self):
        # The relicense outcome: the shipped seed is plain Apache-2.0, no Commons Clause, and still self-recognized.
        self.assertNotIn("Commons Clause", license_seeds.CURRENT_SEED)
        self.assertIn("Apache License", license_seeds.CURRENT_SEED)
        self.assertTrue(license_seeds.recognize(license_seeds.CURRENT_SEED))


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

    def test_stock_apache_without_the_holder_line_is_preserved(self):
        # THE load-bearing guard (see the license_seeds module docstring). The current seed is plain Apache-2.0
        # whose ONLY distinguishing text is its leading holder line. Strip that line and what remains is stock
        # Apache-2.0 — byte-identical to what any adopter would independently choose — which MUST NOT be
        # recognized, so the first-run clear and the boot detector can never delete an adopter's own Apache
        # license. If a future edit "cleans up" the LICENSE to look standard by dropping the holder line, this
        # test goes red instead of silently arming the deleter against every Apache adopter.
        holder = license_seeds._APACHE_2_0_HOLDER
        stock_apache = license_seeds.CURRENT_SEED.replace(holder + "\n\n", "", 1)
        self.assertNotIn(holder, stock_apache)
        self.assertTrue(stock_apache.lstrip().startswith("Apache License"), "left with bare stock Apache body")
        self.assertFalse(license_seeds.recognize(stock_apache),
                         "stock Apache-2.0 without the engine's holder line must be preserved")

    def test_stock_apache_with_a_different_holder_is_preserved(self):
        # An adopter who independently chose plain Apache-2.0 and put their OWN copyright at the top — never touched.
        theirs = license_seeds.CURRENT_SEED.replace(license_seeds._APACHE_2_0_HOLDER,
                                                    "Copyright 2026 Acme Corp", 1)
        self.assertFalse(license_seeds.recognize(theirs))

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
