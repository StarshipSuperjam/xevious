#!/usr/bin/env python3
"""Self-tests for block_coherence_check — the custom/script entry for engine/check/block-coherence.

The pure leg (validate.block_budget_findings) is exercised in test_hooks.py; these lock the TOOL: it
assembles the live registry clean on the real repo, its ENGINE_BLOCK_FIXTURE seam lets a seeded bad
registry inject (so the hard-check-bite meta-check can prove it bites), and its demo narrates a
fail-then-pass. Run: uv run --directory .engine --frozen -- python -m unittest discover -s tools -p 'test_*.py' -b
"""
from __future__ import annotations
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import block_coherence_check as bcc  # noqa: E402
import module_coherence  # noqa: E402


class TestBlockCoherenceTool(unittest.TestCase):
    def test_registrations_returns_the_live_three_member_registry(self):
        # With no fixture env, the tool reads the real assembled registry (3 members, each with modes).
        regs = bcc.registrations()
        names = {b["name"] for b in regs}
        self.assertEqual(names, {"explore-write-gate", "engine-issue-conformance", "findings-disposition"})
        self.assertTrue(all(b.get("modes") for b in regs), "every member declares its modes")
        self.assertEqual(regs, module_coherence.block_eligible_registrations())

    def test_check_mode_emits_empty_array_clean_on_real_repo(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = bcc.main([])
        self.assertEqual(rc, 0)
        out = json.loads(buf.getvalue())
        self.assertEqual(out, [], "the live block registry is coherent")

    def test_fixture_seam_injects_a_bad_registry_and_the_check_bites(self):
        # The ENGINE_BLOCK_FIXTURE seam (the hard-check-bite injection path) points registrations() at a
        # seeded registry whose block omits its modes -> the check emits a hard finding.
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "blocks.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump([{"event": "PreToolUse", "name": "broken", "owner": "modes"}], fh)
            os.environ["ENGINE_BLOCK_FIXTURE"] = path
            try:
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    rc = bcc.main([])
            finally:
                del os.environ["ENGINE_BLOCK_FIXTURE"]
        self.assertEqual(rc, 0)
        out = json.loads(buf.getvalue())
        self.assertTrue(any("does not declare the modes it is active in" in f["message"] for f in out))

    def test_demo_runs_and_narrates(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = bcc.main(["demo"])
        self.assertEqual(rc, 0)
        text = buf.getvalue()
        self.assertIn("RED", text)
        self.assertIn("mode", text.lower())


if __name__ == "__main__":
    unittest.main()
