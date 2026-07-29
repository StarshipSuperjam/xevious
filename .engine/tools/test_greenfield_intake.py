#!/usr/bin/env python3
"""Tests for the greenfield-intake detector (.engine/tools/greenfield_intake.py).

Drives the real `detect_greenfield()` over throwaway trees (mutation-free temp roots), so the two guards
(the intake must be installed; it self-resolves once a description exists), the construction-repo carve-out,
and the fail-soft degrade are exercised against the shipped logic. A dispatch class confirms the demo/CLI
contract.
"""
from __future__ import annotations
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import greenfield_intake as gi  # noqa: E402


class DetectGreenfieldTests(unittest.TestCase):
    # The home-repo carve-out delegates to the repo_identity.is_home_repo seam (git origin == recorded home);
    # the seam itself is proven in test_repo_identity, so these tests patch it to exercise detect_greenfield's
    # own orchestration (intake-installed → not-home → no-description). Default: a deployed repo (not home).
    def _seed(self, files: dict) -> str:
        d = tempfile.mkdtemp(prefix="engine-greenfield-test-")
        self.addCleanup(shutil.rmtree, d, True)
        for rel, body in files.items():
            path = os.path.join(d, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(body)
        return d

    def _not_home(self):
        return mock.patch.object(gi.repo_identity, "is_home_repo", return_value=False)

    def test_silent_when_the_intake_is_not_installed(self):
        # No product-intake runbook -> the engine-design command doesn't exist -> never offer it.
        root = self._seed({"README.md": "hi\n"})
        with self._not_home():
            self.assertIsNone(gi.detect_greenfield(root))

    def test_fires_when_installed_and_no_description_exists(self):
        root = self._seed({gi._INTAKE_REL: "runbook\n"})
        with self._not_home():
            result = gi.detect_greenfield(root)
        self.assertIsNotNone(result)
        self.assertTrue(result["greenfield"])
        self.assertEqual(result["fingerprint"], gi._FINGERPRINT)

    def test_self_resolves_once_a_description_exists(self):
        # The intake writes docs/spec/index.md first; its presence means the intake has been used.
        root = self._seed({gi._INTAKE_REL: "runbook\n", gi._INDEX_REL: "# Product spec\n"})
        with self._not_home():
            self.assertIsNone(gi.detect_greenfield(root))

    def test_no_op_in_the_engine_home_repo(self):
        # origin == recorded home -> is_home_repo True -> the nudge no-ops (a missing product spec is legitimate
        # in the engine's own home). Also asserts detect_greenfield delegates the EXAMINED root to the seam.
        root = self._seed({gi._INTAKE_REL: "runbook\n"})
        with mock.patch.object(gi.repo_identity, "is_home_repo", return_value=True) as h:
            self.assertIsNone(gi.detect_greenfield(root))
        h.assert_called_once_with(root)

    def test_fingerprint_is_stable_across_calls(self):
        # A constant fingerprint is what lets the anti-nag ledger collapse the offer.
        root = self._seed({gi._INTAKE_REL: "runbook\n"})
        with self._not_home():
            self.assertEqual(gi.detect_greenfield(root)["fingerprint"],
                             gi.detect_greenfield(root)["fingerprint"])


class DispatchTests(unittest.TestCase):
    def test_demo_passes(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(gi._demo(), 0)

    def test_default_main_prints_json(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = gi.main([])
        self.assertEqual(rc, 0)
        json.loads(buf.getvalue())  # a valid JSON value (dict or null)

    def test_main_routes_demo(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(gi.main(["demo"]), 0)


if __name__ == "__main__":
    unittest.main()
