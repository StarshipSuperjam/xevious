"""test_census_completeness.py — the demo census-completeness recurrence guard (#424 U13c).

The guard catches a construction demo (`.engine/tools/demo_*.py`) that neither retires (walled in the first-run
retirement census) nor is reached by a surviving non-demo file — orphan drift that would ship into a generated
repo. It is construction-scoped, reads the census as plain data (never imports the retiring instantiator), and
fails CLOSED on an unreadable census. These tests drive `check()` against seeded mini-trees, monkeypatching the
construction-scope gate for determinism (the memory_pointer test idiom), plus one self-proving assertion over the
real tree.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import census_completeness_check as guard  # noqa: E402


def _seed(root, *, demos=(), non_demos=(), census_files=None, census_ok=True):
    """Build a mini-tree under `root`: `.engine/tools/<name>.py` for each demo (a docstring stub) and each
    non_demo (name, body), plus `.engine/provisioning/first-run-assets.json` listing `census_files`. When
    `census_ok` is False the manifest is written as invalid JSON (the fail-closed input)."""
    tools = os.path.join(root, ".engine", "tools")
    prov = os.path.join(root, ".engine", "provisioning")
    os.makedirs(tools)
    os.makedirs(prov)
    for name in demos:
        with open(os.path.join(tools, name), "w", encoding="utf-8") as fh:
            fh.write('"""fixture demo — never executed."""\n')
    for name, body in non_demos:
        with open(os.path.join(tools, name), "w", encoding="utf-8") as fh:
            fh.write(body)
    with open(os.path.join(prov, "first-run-assets.json"), "w", encoding="utf-8") as fh:
        if census_ok:
            json.dump({"description": "fixture", "files": list(census_files or []), "directories": []}, fh)
        else:
            fh.write("{ not valid json ")


class _Construction(unittest.TestCase):
    """Base: pin the construction-scope gate so scan behavior is tested deterministically wherever this runs."""
    gate = True

    def setUp(self):
        self._cons = guard._in_home_repo
        guard._in_home_repo = lambda: self.gate

    def tearDown(self):
        guard._in_home_repo = self._cons


class ScopeTests(_Construction):
    gate = False

    def test_no_op_outside_the_construction_repo(self):
        # In a generated/deployed repo the demos are already retired — the check does nothing, even on an orphan.
        with tempfile.TemporaryDirectory() as root:
            _seed(root, demos=["demo_orphan.py"], census_files=[])
            self.assertEqual(guard.check(root), [])


class OrphanTests(_Construction):
    def test_a_demo_neither_walled_nor_reached_is_flagged(self):
        with tempfile.TemporaryDirectory() as root:
            _seed(root, demos=["demo_orphan.py"], census_files=[])
            fs = guard.check(root)
            self.assertEqual(len(fs), 1)
            self.assertEqual(fs[0]["severity"], "hard")
            self.assertIn("leftover workshop clutter", fs[0]["message"])
            self.assertEqual(fs[0]["location"]["file"], ".engine/tools/demo_orphan.py")

    def test_a_walled_demo_is_accounted_for(self):
        with tempfile.TemporaryDirectory() as root:
            _seed(root, demos=["demo_walled.py"], census_files=[".engine/tools/demo_walled.py"])
            self.assertEqual(guard.check(root), [])

    def test_a_demo_reached_by_a_surviving_tool_is_accounted_for(self):
        with tempfile.TemporaryDirectory() as root:
            _seed(root, demos=["demo_used.py"], non_demos=[("some_tool.py", "import demo_used\n")],
                  census_files=[])
            self.assertEqual(guard.check(root), [])

    def test_a_demo_reached_by_a_traveling_test_is_accounted_for(self):
        # The real demo_actionlint / demo_secret_scan case: kept alive by a test that travels and imports it.
        with tempfile.TemporaryDirectory() as root:
            _seed(root, demos=["demo_used.py"], non_demos=[("test_used.py", "import demo_used\n")],
                  census_files=[])
            self.assertEqual(guard.check(root), [])

    def test_reach_inside_a_function_body_counts(self):
        # ast.walk must catch an import nested in a function — the real pr_reconcile.py:300 `_demo()` delegate.
        with tempfile.TemporaryDirectory() as root:
            _seed(root, demos=["demo_used.py"],
                  non_demos=[("some_tool.py", "def _demo():\n    import demo_used\n    return demo_used.main([])\n")],
                  census_files=[])
            self.assertEqual(guard.check(root), [])

    def test_an_aliased_import_counts(self):
        # `import demo_x as demo` — the real test_first_run_reference_closure case; keyed on the module name.
        with tempfile.TemporaryDirectory() as root:
            _seed(root, demos=["demo_used.py"], non_demos=[("t.py", "import demo_used as demo\n")],
                  census_files=[])
            self.assertEqual(guard.check(root), [])

    def test_a_dynamic_import_counts(self):
        # importlib.import_module("demo_x") / __import__("demo_x") — the dynamic legs; defensive today (no real
        # traveler uses them) but the scan claims to handle them, so a witness holds that claim honest.
        with tempfile.TemporaryDirectory() as root:
            _seed(root, demos=["demo_used.py"],
                  non_demos=[("t.py", 'import importlib\nimportlib.import_module("demo_used")\n')],
                  census_files=[])
            self.assertEqual(guard.check(root), [])

    def test_reach_only_from_a_retired_importer_does_not_count(self):
        # A demo imported only by a census-listed (retiring) file is still orphan drift — that importer is gone in
        # a generated repo, so the demo would dangle there.
        with tempfile.TemporaryDirectory() as root:
            _seed(root, demos=["demo_used.py"], non_demos=[("retiring_tool.py", "import demo_used\n")],
                  census_files=[".engine/tools/retiring_tool.py"])
            fs = guard.check(root)
            self.assertEqual([f["location"]["file"] for f in fs], [".engine/tools/demo_used.py"])

    def test_reach_only_from_another_demo_does_not_count(self):
        # A demo kept alive only by ANOTHER demo is itself orphan drift — demos are excluded as keepers, so both
        # are flagged in one pass (no per-iteration cascade needed for the demo-imports-demo case).
        with tempfile.TemporaryDirectory() as root:
            _seed(root, demos=["demo_a.py", "demo_b.py"], census_files=[])
            with open(os.path.join(root, ".engine", "tools", "demo_b.py"), "w", encoding="utf-8") as fh:
                fh.write("import demo_a\n")
            flagged = sorted(f["location"]["file"] for f in guard.check(root))
            self.assertEqual(flagged, [".engine/tools/demo_a.py", ".engine/tools/demo_b.py"])


class FailClosedTests(_Construction):
    def test_an_unreadable_census_fails_closed(self):
        with tempfile.TemporaryDirectory() as root:
            _seed(root, demos=["demo_x.py"], census_ok=False)
            fs = guard.check(root)
            self.assertEqual(len(fs), 1)
            self.assertEqual(fs[0]["severity"], "hard")
            self.assertIn("can't read", fs[0]["message"].lower())

    def test_a_missing_census_fails_closed(self):
        with tempfile.TemporaryDirectory() as root:
            os.makedirs(os.path.join(root, ".engine", "tools"))
            with open(os.path.join(root, ".engine", "tools", "demo_x.py"), "w", encoding="utf-8") as fh:
                fh.write('"""d"""\n')
            fs = guard.check(root)  # no provisioning manifest at all
            self.assertEqual(len(fs), 1)
            self.assertEqual(fs[0]["severity"], "hard")


class GateDelegatesToTheHomeRepoSeam(unittest.TestCase):
    """The re-key (#323): the scope gate reads the shared origin==home seam (repo_identity.is_home_repo) for
    THIS checkout, not a CLAUDE.md text marker — so promoting the root file to the deployed floor (which drops
    the marker) can never silently disable this census guard. check() still scans the seeded fixture root; only
    the scope gate is the seam."""

    def test_gate_returns_the_seams_verdict_for_this_checkout(self):
        seen = {}

        def fake_home(root=None):
            seen["root"] = root
            return True

        with mock.patch.object(guard.repo_identity, "is_home_repo", fake_home):
            self.assertTrue(guard._in_home_repo())
        self.assertEqual(seen["root"], guard.validate.ROOT, "the gate must judge THIS checkout's root")
        with mock.patch.object(guard.repo_identity, "is_home_repo", lambda root=None: False):
            self.assertFalse(guard._in_home_repo())


class LiveTreeTests(unittest.TestCase):
    def test_the_live_tree_is_complete(self):
        # The self-proving green: after #424 U13a walled the 9 orphans, every demo on the real tree is accounted
        # for (in the census or reached by a surviving non-demo file). Uses the real construction-scope gate:
        # in the construction repo it scans the real tree and must be clean; in a deployed repo it no-ops. Either
        # way the result is empty — a NEW orphan demo added later is exactly what turns this (and CI) red.
        self.assertEqual(guard.check(), [])


if __name__ == "__main__":
    unittest.main()
