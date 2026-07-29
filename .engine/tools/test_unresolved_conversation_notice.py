#!/usr/bin/env python3
"""Tests for the passive unresolved-conversation pre-arm (#408)."""
import contextlib
import io
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import unresolved_conversation_notice as ucn  # noqa: E402


class TestRender(unittest.TestCase):
    def setUp(self):
        self.block = ucn.render()

    def test_conveys_why_the_merge_is_blocked(self):
        # The greyed-button-because-unresolved-conversation explanation, in plain words (no jargon).
        self.assertIn("unresolved", self.block)
        self.assertIn("grey", self.block.lower())
        self.assertIn("check", self.block.lower())  # "even when all the automated checks are green"

    def test_says_operator_may_clear_after_reading_and_accepting(self):
        low = self.block.lower()
        self.assertIn("read", low)
        self.assertIn("accepting", low)
        self.assertIn("resolve", low)

    def test_never_a_bare_one_click_fixes_it(self):
        # The load-bearing product-intent guard: copy that framed it as a formality click would train the
        # operator to dismiss real flagged concerns and gut the finding-disposition spine.
        self.assertNotIn("one click", self.block.lower())
        self.assertIn("only once you've read it", self.block.lower())

    def test_explains_how_to_reach_the_thread(self):
        self.assertIn("Resolve conversation", self.block)
        self.assertIn("Conversation", self.block)

    def test_covers_the_post_rebase_unreachable_case_keeping_read_then_accept(self):
        # The sharpest clause: guiding the operator to a thread hidden after a rebase must NOT degrade into
        # "resolve it anyway" — it keeps the read-then-accept binding ("read it there, and only then resolve it").
        low = self.block.lower()
        self.assertIn("outdated", low)
        self.assertTrue("rebase" in low or "force-push" in low)
        self.assertIn("only then resolve", low)

    def test_is_collapsed_by_default(self):
        # Anti-habituation (collapse, not suppress): a one-line summary with the full text behind a <details>.
        self.assertIn("<details>", self.block)
        self.assertIn("<summary>", self.block)
        self.assertIn("</details>", self.block)

    def test_no_maintainer_or_engine_jargon_leaks(self):
        low = self.block.lower()
        for banned in ("ruleset", "finding-disposition", "pre-arm", "review thread", "d-134", "spine"):
            self.assertNotIn(banned, low)


class TestDemo(unittest.TestCase):
    def test_demo_self_check_passes(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = ucn.main(["demo"])
        self.assertEqual(rc, 0, buf.getvalue())
        self.assertIn("OK", buf.getvalue())

    def test_plain_render_prints_the_block(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = ucn.main([])
        self.assertEqual(rc, 0)
        self.assertIn("<details>", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
