#!/usr/bin/env python3
"""Tests for the instance guarded-paths declaration check (#532).

Covers the two tiers the check must keep separate: HARD on a malformed/degenerate declaration (fail closed at
the merge gate) and SOFT on a well-formed entry that names a path not present in the tree (a typo that silently
protects nothing). Absent is silent. The `_demo` self-check is exercised so its failure path is real."""
from __future__ import annotations
import contextlib
import io
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import operator_guarded_paths_check as check  # noqa: E402


def _sev(fs):
    return [f["severity"] for f in fs]


class TestOperatorGuardedPathsFindings(unittest.TestCase):
    def setUp(self):
        # A throwaway tree so the SOFT existence check has real files to resolve against.
        self.root = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.root, "scanners"))
        with open(os.path.join(self.root, "scanners", "contain.py"), "w") as fh:
            fh.write("# a real scanner\n")

    def test_absent_declaration_is_silent(self):
        self.assertEqual(check.findings("hard", check._ABSENT, root=self.root), [])

    def test_malformed_json_is_hard(self):
        self.assertEqual(_sev(check.findings("hard", check._MALFORMED, root=self.root)), ["hard"])

    def test_non_object_top_level_is_hard(self):
        self.assertEqual(_sev(check.findings("hard", ["not", "an", "object"], root=self.root)), ["hard"])

    def test_unknown_top_level_key_is_hard(self):
        # Load-bearing for the shrink detector: an unrecognised key is an inert place to re-add a removed path
        # (the decoy the deliverable gate caught). Forbidding it at the shape gate closes that bypass.
        decl = {"guarded_paths": ["scanners/contain.py"], "_moved": "scanners/contain.py"}
        self.assertEqual(_sev(check.findings("hard", decl, root=self.root)), ["hard"])

    def test_non_list_field_is_hard(self):
        self.assertEqual(_sev(check.findings("hard", {"guarded_paths": "scanners/contain.py"},
                                             root=self.root)), ["hard"])

    def test_degenerate_prefix_is_hard(self):
        # The footgun: a prefix that startswith() would make match EVERY file. Each degenerate form is hard.
        for bad in (".", "/", "", "./", "scanners"):  # incl. a no-trailing-slash prefix
            fs = check.findings("hard", {"guarded_prefixes": [bad]}, root=self.root)
            self.assertIn("hard", _sev(fs), bad)

    def test_bad_and_absolute_path_are_hard(self):
        self.assertEqual(_sev(check.findings("hard", {"guarded_paths": [123]}, root=self.root)), ["hard"])
        self.assertEqual(_sev(check.findings("hard", {"guarded_paths": ["/abs/x"]}, root=self.root)), ["hard"])

    def test_missing_path_is_soft_not_hard(self):
        fs = check.findings("hard", {"guarded_paths": ["scanners/typo.py"]}, root=self.root)
        self.assertEqual(_sev(fs), ["soft"])

    def test_missing_prefix_is_soft(self):
        fs = check.findings("hard", {"guarded_prefixes": ["nope/"]}, root=self.root)
        self.assertEqual(_sev(fs), ["soft"])

    def test_valid_declaration_is_silent(self):
        decl = {"guarded_paths": ["scanners/contain.py"], "guarded_prefixes": ["scanners/"]}
        self.assertEqual(check.findings("hard", decl, root=self.root), [])

    def test_load_declaration_distinguishes_absent_malformed_and_object(self):
        self.assertIs(check.load_declaration(os.path.join(self.root, "nope.json")), check._ABSENT)
        bad = os.path.join(self.root, "bad.json")
        with open(bad, "w") as fh:
            fh.write("{not json")
        self.assertIs(check.load_declaration(bad), check._MALFORMED)
        good = os.path.join(self.root, "good.json")
        with open(good, "w") as fh:
            fh.write('{"guarded_paths": []}')
        self.assertEqual(check.load_declaration(good), {"guarded_paths": []})


class TestDemoSelfCheck(unittest.TestCase):
    def test_demo_passes_and_has_a_real_failure_path(self):
        # The demo self-checks (one hard + one soft) and returns 0; and its assertion CAN fail — proving the
        # in-tool demo has a real failure path (the in-tool-demo-failure-path floor).
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(check._demo(), 0)
        # A construction that breaks the invariant (findings that are not 1 hard + 1 soft) must make it non-zero.
        original = check.findings
        try:
            check.findings = lambda *a, **k: []  # no findings -> the demo's self-check must fail
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(check._demo(), 1)
        finally:
            check.findings = original


if __name__ == "__main__":
    unittest.main()
