#!/usr/bin/env python3
"""Self-tests for the lens-consumption consumer (lens_consumption_check.py): the custom/script guard
that diffs the installed review lenses against the consumed set build orchestration records.

Run: uv run --directory .engine --frozen -- python -m unittest discover -s tools -p 'test_*.py' -b

These lock the CONSUMER's contract (the pure diff leg validate.dangling_lens_findings is locked in
test_agent.py): the fenced consumed-review-lenses block in build-orchestration.md parses to exactly the
nine installed lens tokens; a MISSING or EMPTY block fails CLOSED (raises → the custom/script kind's
hard finding) rather than passing an unjudged roster as "nothing dangling"; the live repository is clean
(every installed review is consumed); and the demo runs its real fail-then-pass.
"""
from __future__ import annotations
import contextlib
import io
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate  # noqa: E402
import lens_consumption_check as lc  # noqa: E402

EXPECTED = {"product-intent", "architecture", "feasibility", "risk-governance",
            "spec-conformance", "divergence-hunter", "usability", "technical-integrity", "security-governance"}

_GOOD_NOTES = (
    "Some prose with an unrelated `backtick` token.\n\n"
    "```text\n"
    "consumed-review-lenses:\n"
    "  plan-review gate: product-intent, architecture, feasibility, risk-governance\n"
    "  product-design spec-lock ceremony: product-intent, architecture, feasibility, risk-governance\n"
    "  pre-submission gate: spec-conformance, divergence-hunter, usability, technical-integrity, security-governance\n"
    "```\n")


class TestConsumedParse(unittest.TestCase):
    def test_fenced_block_yields_exactly_the_nine_tokens(self):
        self.assertEqual(lc._consumed_from_notes(_GOOD_NOTES), EXPECTED)

    def test_ignores_unrelated_backticked_prose(self):
        """The fence bounds the machine-readable data, so a stray inline `token` in the Notes prose
        never leaks into the consumed set."""
        self.assertNotIn("backtick", lc._consumed_from_notes(_GOOD_NOTES))

    def test_missing_block_fails_closed(self):
        with self.assertRaises(ValueError):
            lc._consumed_from_notes("Notes prose with a fenced block but no sentinel.\n\n```text\nhello\n```\n")

    def test_empty_block_fails_closed(self):
        with self.assertRaises(ValueError):
            lc._consumed_from_notes("```text\nconsumed-review-lenses:\n```\n")


class TestLiveRepository(unittest.TestCase):
    def test_consumed_lenses_reads_the_committed_record(self):
        self.assertEqual(lc.consumed_lenses(), EXPECTED)

    def test_check_is_green_on_the_live_roster(self):
        """Every installed review lens is consumed today, so the check emits an empty finding array."""
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = lc.main([])
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(buf.getvalue()), [])

    def test_demo_runs_its_real_fail_then_pass(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = lc.main(["demo"])
        self.assertEqual(rc, 0)
        self.assertIn("all clear", buf.getvalue())
        self.assertIn("turns RED", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
