#!/usr/bin/env python3
"""Self-tests for the uv-group-drift CI check (engine/check/uv-group-drift) and the
`sync-groups` fixer it points at.

Run: uv run --directory .engine --frozen -- python -m unittest discover -s tools -p 'test_*.py' -b

The drift detection runs the REAL check against a throwaway fixture engine (via module_manager's
_redirect_root + _build_fixture — the same fake-only-the-tree discipline the coherence tests use); the
real-repo path is asserted in-sync directly. The deliverable-gate cold review attests each test's assertion
matches its name.
"""
from __future__ import annotations
import contextlib
import io
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate              # noqa: E402
import module_manager        # noqa: E402
import uv_group_drift_check as drift  # noqa: E402


class TestUvGroupDriftCheck(unittest.TestCase):

    def test_real_repository_is_in_sync(self):
        # The committed [tool.uv] default-groups equals what the present module set derives -> no finding.
        self.assertIsNone(drift.check())

    def test_main_emits_a_json_array_and_exits_zero(self):
        # The custom/script contract: stdout is a JSON array (empty on the in-sync real repo), exit 0.
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = drift.main()
        self.assertEqual(rc, 0)
        parsed = json.loads(buf.getvalue())
        self.assertIsInstance(parsed, list)
        self.assertEqual(parsed, [])

    def test_drift_is_flagged_as_one_hard_finding_naming_both_lists(self):
        with module_manager.tempfile.TemporaryDirectory() as d:
            with module_manager._redirect_root(d):
                module_manager._build_fixture(d)
                self.assertIsNone(drift.check())                      # the fixture starts in sync
                # induce drift: drop optx from default-groups while optx is still installed
                py = module_manager._pyproject_path()
                text = validate.read(py).replace(
                    'default-groups = ["base", "optx"]', 'default-groups = ["base"]')
                with open(py, "w", encoding="utf-8") as fh:
                    fh.write(text)
                f = drift.check()
                self.assertIsNotNone(f)
                self.assertEqual(f["severity"], "hard")
                self.assertIn("base", f["message"])                  # the committed value
                self.assertIn("optx", f["message"])                  # the derived (correct) value
                self.assertIn("sync-groups", f["message"])           # the concrete fix command

    def test_sync_groups_fixer_re_canonicalizes_drift(self):
        with module_manager.tempfile.TemporaryDirectory() as d:
            with module_manager._redirect_root(d):
                module_manager._build_fixture(d)
                py = module_manager._pyproject_path()
                drifted = validate.read(py).replace(            # read BEFORE the truncating open(w)
                    'default-groups = ["base", "optx"]', 'default-groups = ["base"]')
                with open(py, "w", encoding="utf-8") as fh:
                    fh.write(drifted)
                self.assertIsNotNone(drift.check())                  # drifted
                res = module_manager.sync_groups()
                self.assertTrue(res["changed"])
                self.assertEqual(res["groups"], ["base", "optx"])    # derived, sorted
                self.assertIsNone(drift.check())                     # back in sync
                # a second sync is a no-op (already canonical)
                self.assertFalse(module_manager.sync_groups()["changed"])


if __name__ == "__main__":
    unittest.main()
