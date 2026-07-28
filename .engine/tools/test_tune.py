"""Tests for the `/engine-tune` tool.

Verifies: the effective value is the shipped default with the operator override merged per-key (and the
default alone when there is no override); eligibility excludes attention's structural keys and includes the
threshold policies' keys; validate_value refuses a fixed (structural) setting with the pinned plain sentence,
an unknown setting, and a non-number value (a bool is not a number); write_override creates the file, merges,
and preserves every other saved setting; set_value writes only after validation passes, opens a reviewed PR
through the INJECTED opener (faked — no real commit), saves-without-PR on request, and degrades when the
opener fails. The demo runs.
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tune  # noqa: E402


def _fake_opener(branch, title, body, paths):
    """A practice-run stand-in for the real git+PR boundary — records the call, opens nothing."""
    _fake_opener.calls.append({"branch": branch, "title": title, "body": body, "paths": paths})
    return {"number": 7, "html_url": "https://example.test/pull/7"}


_fake_opener.calls = []


class TestEffectiveAndEligibility(unittest.TestCase):
    def test_effective_without_override_is_the_default(self):
        self.assertEqual(tune.effective("triage-threshold"), tune.default_values("triage-threshold"))

    def test_effective_merges_the_override_per_key(self):
        eff = tune.effective("triage-threshold", {"persistence": 99})
        self.assertEqual(eff["persistence"], 99)
        self.assertEqual(eff["auto_resolve"], tune.default_values("triage-threshold")["auto_resolve"])

    def test_eligible_excludes_attention_structural_keys(self):
        eligible = tune.eligible_keys("attention")
        self.assertIn("budget_orientation", eligible)
        self.assertNotIn("precedence_blocking_debt", eligible, "precedence is a fixed structural law")
        self.assertNotIn("trim_orientation", eligible, "trim order is a fixed structural law")

    def test_threshold_policy_has_no_structural_keys(self):
        self.assertEqual(tune.structural_keys("triage-threshold"), set())
        self.assertEqual(sorted(tune.eligible_keys("triage-threshold")),
                         sorted(tune.default_values("triage-threshold")))


class TestValidateValue(unittest.TestCase):
    def test_structural_key_refused_with_pinned_sentence(self):
        ok, msg = tune.validate_value("attention", "precedence_blocking_debt", 1)
        self.assertFalse(ok)
        self.assertEqual(msg, tune._REFUSE_STRUCTURAL)

    def test_unknown_setting_refused(self):
        ok, msg = tune.validate_value("triage-threshold", "made_up_setting", 5)
        self.assertFalse(ok)
        self.assertIn("don't have a setting", msg)

    def test_non_number_refused(self):
        ok, msg = tune.validate_value("triage-threshold", "persistence", "lots")
        self.assertFalse(ok)
        self.assertIn("number", msg)

    def test_bool_is_not_a_number(self):
        ok, _msg = tune.validate_value("triage-threshold", "persistence", True)
        self.assertFalse(ok, "a bool must not pass as a number")

    def test_infinity_and_not_a_number_are_refused(self):
        # They survive float() and json.dumps (as the non-standard `Infinity`/`NaN` literals), so without this
        # they save cleanly and then quietly break the setting they tune. Concretely, on the debt-blocking bar:
        # an endless bar defers even the class that must never be deferred (a safety check that could not run),
        # and "not a number" compares false against everything, so it blocks what it should let past.
        for value in (float("inf"), float("-inf"), float("nan")):
            ok, msg = tune.validate_value("attention", "debt_blocking_threshold", value)
            self.assertFalse(ok, f"{value} was accepted as a dial the engine can measure against")
            self.assertIn("number", msg)

    def test_the_refusal_covers_every_setting_not_just_the_one_that_exposed_it(self):
        for policy, key in (("triage-threshold", "persistence"), ("attention", "weight_recency")):
            ok, _msg = tune.validate_value(policy, key, float("inf"))
            self.assertFalse(ok, f"{policy}.{key} accepted an endless value")

    def test_valid_value_accepted(self):
        ok, msg = tune.validate_value("triage-threshold", "persistence", 5)
        self.assertTrue(ok)
        self.assertEqual(msg, "")


class TestWriteOverride(unittest.TestCase):
    def test_write_creates_and_merges_preserving_others(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "operator-overrides.json")
            tune.write_override("triage-threshold", "persistence", 5, path=p)
            tune.write_override("triage-threshold", "auto_resolve", 1, path=p)
            tune.write_override("attention", "budget_orientation", 0.4, path=p)
            with open(p, encoding="utf-8") as fh:
                data = json.load(fh)
            self.assertEqual(data["triage-threshold"], {"persistence": 5, "auto_resolve": 1},
                             "the second write preserves the first")
            self.assertEqual(data["attention"], {"budget_orientation": 0.4},
                             "a different policy's slice is preserved alongside")


class TestSetValue(unittest.TestCase):
    def setUp(self):
        _fake_opener.calls = []

    def test_invalid_change_writes_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "operator-overrides.json")
            res = tune.set_value("attention", "precedence_blocking_debt", 1, override_path=p,
                                 opener=_fake_opener)
            self.assertFalse(res["ok"])
            self.assertEqual(res["message"], tune._REFUSE_STRUCTURAL)
            self.assertFalse(os.path.exists(p), "a refused change must never touch the file")
            self.assertEqual(_fake_opener.calls, [], "a refused change opens no pull request")

    def test_valid_change_writes_and_opens_a_pull_request(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "operator-overrides.json")
            res = tune.set_value("triage-threshold", "persistence", 5, override_path=p, opener=_fake_opener)
            self.assertTrue(res["ok"])
            self.assertEqual(res["message"], tune._CONFIRM)
            self.assertEqual(res["pr"]["number"], 7)
            self.assertTrue(os.path.exists(p), "the change is saved")
            self.assertEqual(len(_fake_opener.calls), 1, "exactly one pull request is opened")

    def test_no_pr_saves_without_opening(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "operator-overrides.json")
            res = tune.set_value("triage-threshold", "persistence", 5, override_path=p, open_pr=False)
            self.assertTrue(res["ok"])
            self.assertIsNone(res["pr"])
            self.assertTrue(os.path.exists(p))

    def test_opener_failure_degrades_to_saved_not_lost(self):
        def boom(branch, title, body, paths):
            raise RuntimeError("network down")
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "operator-overrides.json")
            res = tune.set_value("triage-threshold", "persistence", 5, override_path=p, opener=boom)
            self.assertTrue(res["ok"], "the value was saved even though the PR could not open")
            self.assertIsNone(res["pr"])
            self.assertIn("could not be opened", res["message"])
            self.assertTrue(os.path.exists(p))


class TestCLIAndDemo(unittest.TestCase):
    def test_show_prints_the_eligible_settings(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = tune.main(["show", "triage-threshold"])
        self.assertEqual(rc, 0)
        self.assertIn("persistence", buf.getvalue())

    def test_demo_runs_and_narrates(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = tune.main(["demo"])
        self.assertEqual(rc, 0)
        text = buf.getvalue()
        self.assertIn("pull request", text.lower())
        self.assertIn("budget_orientation", text, "the demo shows the live attention read change")


if __name__ == "__main__":
    unittest.main()
