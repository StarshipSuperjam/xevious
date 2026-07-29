"""Tests for the operator-override file reader + its ownership carve-out.

Verifies: the reader degrades a missing, malformed, or non-object file to `{}` (never raises), drops a
non-object policy slice, and returns a single policy's slice; and the ownership carve-out — a committed
`.engine/operator-overrides.json` is exempt from the orphan leg via `module_coherence.OPERATOR_CONFIG`
(without the carve-out the same unclaimed file WOULD be flagged a hard orphan — the control that proves the
carve-out is load-bearing). The override file is operator config preserved across an engine update, claimed
by no module — so it must be exempt, not added to the overlay-replaced foundation set.
"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import operator_overrides as oo  # noqa: E402
import validate                  # noqa: E402
import module_coherence          # noqa: E402

_OVERRIDE_REL = ".engine/operator-overrides.json"


def _write(path, text):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


class TestLoad(unittest.TestCase):
    def test_absent_file_returns_empty(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(oo.load(os.path.join(d, "nope.json")), {})

    def test_malformed_returns_empty(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "o.json")
            _write(p, "{ not valid json")
            self.assertEqual(oo.load(p), {}, "a damaged file narrows to defaults, never raises")

    def test_non_object_returns_empty(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "o.json")
            _write(p, "[1, 2, 3]")
            self.assertEqual(oo.load(p), {})

    def test_valid_loads_and_drops_a_non_object_slice(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "o.json")
            _write(p, json.dumps({"triage-threshold": {"persistence": 5}, "bogus": 5}))
            self.assertEqual(oo.load(p), {"triage-threshold": {"persistence": 5}},
                             "a non-object policy slice is dropped so it cannot poison the rest")

    def test_slice_for_returns_one_policy_or_empty(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "o.json")
            _write(p, json.dumps({"attention": {"budget_orientation": 0.4}}))
            self.assertEqual(oo.slice_for("attention", p), {"budget_orientation": 0.4})
            self.assertEqual(oo.slice_for("triage-threshold", p), {})


class TestOwnershipCarveOut(unittest.TestCase):
    def test_override_path_is_in_operator_config(self):
        self.assertIn(_OVERRIDE_REL, module_coherence.OPERATOR_CONFIG)

    def test_override_file_is_exempt_from_orphan(self):
        findings = validate.ownership_findings(
            [_OVERRIDE_REL], {}, module_coherence.OPERATOR_CONFIG, "hard", "msg")
        self.assertEqual(findings, [], "the operator-override file is carved out of the orphan leg")

    def test_without_carveout_the_same_file_would_orphan(self):
        # The control: absent the carve-out, the unclaimed override file IS a hard orphan — so the carve-out
        # is what makes it clean, not some other exemption.
        findings = validate.ownership_findings([_OVERRIDE_REL], {}, set(), "hard", "msg")
        self.assertEqual(len(findings), 1)
        self.assertIn("orphan", findings[0]["message"].lower())

    def test_check_coherence_threads_the_carveout(self):
        # Pin the WIRING, not just ownership_findings in isolation: with the override file in the inventory,
        # the full check_coherence must report no orphan for it — i.e. OPERATOR_CONFIG is actually threaded
        # into the exempt set it builds (deliverable-gate correctness hardening).
        from unittest import mock
        with mock.patch.object(module_coherence, "engine_file_inventory", return_value=[_OVERRIDE_REL]):
            fs = module_coherence.check_coherence("hard")
        orphans = [f for f in fs if "operator-overrides" in f.get("message", "")]
        self.assertEqual(orphans, [], "check_coherence threads OPERATOR_CONFIG into its exempt set")


if __name__ == "__main__":
    unittest.main()
