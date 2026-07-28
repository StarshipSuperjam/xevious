"""Tests for the stale-operator-override check.

Verifies: with no override (the normal state) the check surfaces nothing; a saved value on a current,
eligible setting surfaces nothing; a saved key the policy no longer carries is surfaced as stale; a saved
value on a fixed (structural) setting is surfaced as refused; a whole slice for a policy that no longer
exists is surfaced; and the finding carries the plain `/engine-tune` fix guidance. The CLI emit + demo run.
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import policy_override_check as poc  # noqa: E402


class TestFindings(unittest.TestCase):
    def test_no_override_surfaces_nothing(self):
        self.assertEqual(poc.findings("hard", override={}), [])

    def test_valid_setting_surfaces_nothing(self):
        self.assertEqual(poc.findings("hard", override={"triage-threshold": {"persistence": 5}}), [])

    def test_stale_key_is_surfaced(self):
        fs = poc.findings("hard", override={"triage-threshold": {"a_setting_that_was_removed": 9}})
        self.assertEqual(len(fs), 1)
        self.assertEqual(fs[0]["severity"], "hard")
        self.assertIn("/engine-tune", fs[0]["message"], "the fix points the operator at the command")

    def test_structural_key_is_surfaced_in_plain_language(self):
        fs = poc.findings("hard", override={"attention": {"precedence_blocking_debt": 1}})
        self.assertEqual(len(fs), 1)
        self.assertIn("safety order", fs[0]["message"], "a fixed setting is named plainly")
        self.assertIn("/engine-tune", fs[0]["message"])

    def test_non_number_value_is_surfaced(self):
        # A hand-edited non-number on an eligible setting is caught (the engine-mediated command refuses it,
        # but a hand-edit could slip one in) — defense-in-depth flagged at the deliverable gate.
        fs = poc.findings("hard", override={"triage-threshold": {"persistence": "lots"}})
        self.assertEqual(len(fs), 1)
        self.assertIn("isn't a number", fs[0]["message"])

    def test_whole_slice_for_a_gone_policy_is_surfaced(self):
        fs = poc.findings("hard", override={"made-up-policy": {"x": 1, "y": 2}})
        self.assertEqual(len(fs), 2, "every key of a policy that no longer exists is stale")

    def test_valid_alongside_stale_surfaces_only_the_stale(self):
        fs = poc.findings("hard", override={"triage-threshold": {"persistence": 5, "gone": 9}})
        self.assertEqual(len(fs), 1, "the valid key applies; only the stale one is surfaced")


class TestCLI(unittest.TestCase):
    def _main_stdout(self, seeded_path):
        """Run the CLI against a SEEDED saved-settings file and return its stdout. Driven through the
        `ENGINE_OVERRIDE_PATH` seam rather than the default read: the default reads the HOST repository's
        own `.engine/operator-overrides.json`, so pinning the output would pin a fact about whichever repo
        the suite runs in — the class of coupling that reds a deployment's required self-tests for using a
        shipped capability. The seam keeps the assertion exact instead of weakening it to 'some list'."""
        buf = io.StringIO()
        prior = os.environ.get("ENGINE_OVERRIDE_PATH")
        os.environ["ENGINE_OVERRIDE_PATH"] = seeded_path
        try:
            with contextlib.redirect_stdout(buf):
                rc = poc.main([])
        finally:
            if prior is None:
                os.environ.pop("ENGINE_OVERRIDE_PATH", None)
            else:
                os.environ["ENGINE_OVERRIDE_PATH"] = prior
        self.assertEqual(rc, 0)
        return buf.getvalue().strip()

    def test_main_emits_an_empty_array_for_saved_settings_that_all_apply(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "operator-overrides.json")
            with open(p, "w", encoding="utf-8") as fh:
                json.dump({}, fh)
            self.assertEqual(self._main_stdout(p), "[]")

    def test_main_emits_the_finding_array_for_a_stale_saved_setting(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "operator-overrides.json")
            with open(p, "w", encoding="utf-8") as fh:
                json.dump({"made-up-policy": {"x": 1}}, fh)
            emitted = json.loads(self._main_stdout(p))
        self.assertEqual(len(emitted), 1, "one stale saved setting -> one finding on stdout")
        self.assertEqual(emitted[0]["severity"], "hard")

    def test_demo_runs(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = poc.main(["demo"])
        self.assertEqual(rc, 0)
        self.assertIn("no longer apply", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
