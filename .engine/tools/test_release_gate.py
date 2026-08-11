"""Tests for `release_gate.py` — the cut-time deployment gate.

FAST + INJECTED by design. The real gate (a full self-test suite inside a projected deployment, and real
practice upgrades from released baselines) takes many minutes and runs at CUT TIME, never in this per-PR
suite — moving that cost out of every pull request is the whole point of the slice. So these exercise the
gate's ORCHESTRATION and its fail-CLOSED contract with the heavy arms stubbed; the genuine operate/upgrade
proofs are the cut-time gate run and the first-run-retired `demo_664_release_gate.py`.

Every case is guarded `@skipUnless(_CONSTRUCTION)` — the home repo AND not a nested run — so that when Arm A's
in-projection suite re-collects this file (it ships to deployed repos), these cases skip rather than recurse
into the gate or fail against a tag-less projected tree.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate                              # noqa: E402
import module_manager as mm                  # noqa: E402  (the shared PRACTICE_RUN_NOTE constant)
import release_gate as rg                    # noqa: E402

_CONSTRUCTION = rg._ccc._in_home_repo() and not os.environ.get(rg._NESTED_ENV)
_SKIP = "runs where a release is cut (the home repo, not a nested projection run)"


def _proc(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


@unittest.skipUnless(_CONSTRUCTION, _SKIP)
class TestBaselineSelection(unittest.TestCase):
    """`_upgrade_baselines` picks the released version tags at or above the clean-upgrade floor. The tag list
    is INJECTED so the test is independent of the checkout's actual tags — the CI self-test checkout is shallow
    and carries none, so asserting against the real `git tag` would fail there while passing locally."""

    def _baselines_for(self, tag_lines):
        with mock.patch.object(rg, "_run", return_value=_proc(0, tag_lines, "")):
            return rg._upgrade_baselines()

    def test_floor_filter_and_shape(self):
        floor = validate.load_json(os.path.join(validate.ROOT, ".engine", "engine.json"))["min_upgradeable_from"]
        baselines = self._baselines_for("v0.1.0\nv0.2.0\nv0.3.0\nv0.3.1\nv0.3.2\nv0.4.0\n"
                                        "merged-verified\nbackup-394-verified\n")
        self.assertTrue(baselines, "expected at least one in-range baseline from the injected tags")
        self.assertTrue(all(t.startswith("v") for t in baselines))                       # v-prefix preserved
        self.assertNotIn("merged-verified", baselines)                                   # non-version tags dropped
        self.assertIn("v" + floor, baselines)                                            # the floor tag is included
        self.assertNotIn("v0.1.0", baselines)                                            # a below-floor tag is not
        for t in baselines:                                                              # every kept tag is >= floor
            self.assertGreaterEqual(validate._ver_tuple(t[1:]), validate._ver_tuple(floor))
        self.assertEqual(baselines, sorted(set(baselines), key=lambda t: validate._ver_tuple(t[1:])))

    def test_v_prefix_strip_is_load_bearing(self):
        # a bare-vs-v-prefixed mismatch in the floor compare would silently drop EVERY baseline; the injected
        # in-range tags must survive, proving the `v` is stripped before the version compare.
        self.assertEqual(self._baselines_for("v0.3.2\nv0.4.0\n"), ["v0.3.2", "v0.4.0"])


@unittest.skipUnless(_CONSTRUCTION, _SKIP)
class TestIsolationGuard(unittest.TestCase):
    """The belt-and-suspenders half of the ROOT-isolation guarantee refuses a non-throwaway target."""

    def test_refuses_home_root(self):
        with self.assertRaises(rg.GateError):
            rg._assert_isolated(validate.ROOT)

    def test_refuses_path_outside_tempdir(self):
        with self.assertRaises(rg.GateError):
            rg._assert_isolated(os.path.dirname(validate.ROOT))

    def test_allows_throwaway_tempdir(self):
        with tempfile.TemporaryDirectory() as d:
            rg._assert_isolated(d)                          # must not raise


@unittest.skipUnless(_CONSTRUCTION, _SKIP)
class TestFailClosed(unittest.TestCase):
    """A gate that cannot run — a setup GateError or ANY unexpected error — BLOCKS the cut (exit nonzero),
    never waves it through."""

    def _main_json(self):
        import io
        import contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = rg.main(["--json"])
        return code, json.loads(buf.getvalue())

    def test_setup_error_blocks(self):
        with mock.patch.object(rg, "run_gate", side_effect=rg.GateError("projection could not be built")):
            code, out = self._main_json()
        self.assertEqual(code, 1)
        self.assertFalse(out["passed"])
        self.assertIn("projection could not be built", out["reason"])

    def test_unexpected_error_blocks(self):
        with mock.patch.object(rg, "run_gate", side_effect=ValueError("kaboom")):
            code, out = self._main_json()
        self.assertEqual(code, 1)
        self.assertFalse(out["passed"])
        self.assertIn("unexpected error", out["reason"])

    def test_red_result_blocks(self):
        with mock.patch.object(rg, "run_gate", return_value={"ran": True, "passed": False}):
            code, _ = self._main_json()
        self.assertEqual(code, 1)

    def test_green_result_passes(self):
        with mock.patch.object(rg, "run_gate", return_value={"ran": True, "passed": True}):
            code, _ = self._main_json()
        self.assertEqual(code, 0)


@unittest.skipUnless(_CONSTRUCTION, _SKIP)
class TestInertWhenDeployed(unittest.TestCase):
    """On a non-home checkout the gate is inert (ran=False) and passes — a deployed repo runs the suite in its
    own engine-ci. The workflow, not the tool, decides an engine cut must actually have run."""

    def test_not_home_repo_is_inert_pass(self):
        with mock.patch.object(rg._ccc, "_in_home_repo", return_value=False):
            result = rg.run_gate()
        self.assertFalse(result["ran"])
        self.assertTrue(result["passed"])


@unittest.skipUnless(_CONSTRUCTION, _SKIP)
class TestHomeTreeGuard(unittest.TestCase):
    """If the gate ever leaves a change in the home working tree the cut is about to commit, it BLOCKS."""

    def test_home_tree_mutation_blocks(self):
        with mock.patch.object(rg._ccc, "_in_home_repo", return_value=True), \
             mock.patch.object(rg, "_archive_candidate", return_value="/tmp/nowhere"), \
             mock.patch.object(rg, "_arm_operates", return_value={"passed": True, "failures": []}), \
             mock.patch.object(rg, "_arm_upgrades", return_value={"passed": True, "failures": []}), \
             mock.patch.object(rg, "_worktree_digest", side_effect=["BEFORE", "AFTER"]):
            result = rg.run_gate()
        self.assertTrue(result["ran"])
        self.assertFalse(result["passed"])
        self.assertTrue(result.get("home_tree_mutated"))


@unittest.skipUnless(_CONSTRUCTION, _SKIP)
class TestUpgradeArmReporting(unittest.TestCase):
    """The UPGRADE leg (`_upgrade_leg`) reads the practice-upgrade result and blocks on refusal, non-
    application, a hard gate finding, a driver crash, OR a missing practice-path note (a silent network fetch of
    a real release). It is exercised directly (not through `_upgrade_from`) so the rollback leg stays out of
    scope here; the composed transition is `TestTransitionComposition`."""

    def _drive(self, result_obj=None, rc=0, stdout=None, stderr=""):
        out = stdout if stdout is not None else ("GATE_RESULT:" + json.dumps(result_obj))
        with mock.patch.object(rg, "_run", return_value=_proc(rc, out, stderr)):
            return rg._upgrade_leg("/tmp/proj", "v9.9.9", "/tmp/candidate")

    def _clean(self, **over):
        base = {"refused": False, "applied": True, "reason": None, "findings": [],
                "notes": [mm.PRACTICE_RUN_NOTE]}      # the REAL constant, so a reword there breaks this test
        base.update(over)
        return base

    def test_clean_upgrade_passes(self):
        self.assertTrue(self._drive(self._clean())["passed"])

    def test_hard_finding_blocks(self):
        res = self._drive(self._clean(findings=[{"severity": "hard", "id": "engine/check/knowledge-coverage"}]))
        self.assertFalse(res["passed"])
        self.assertIn("blocking", res["detail"])

    def test_phase1_refusal_blocks(self):
        self.assertFalse(self._drive(self._clean(refused=True, reason="unreachable"))["passed"])

    def test_tail_refusal_reason_blocks(self):
        # The upgrade tail can refuse (reconcile / migration) with applied=True, a `reason`, and EMPTY findings
        # — it must NOT read as a pass just because `refused` is unset and no hard finding was recorded.
        res = self._drive(self._clean(applied=True, findings=[],
                                      reason="a stored-data update could not be completed"))
        self.assertFalse(res["passed"])
        self.assertIn("reconcile cleanly", res["detail"])

    def test_not_applied_blocks(self):
        self.assertFalse(self._drive(self._clean(applied=False, reason=None))["passed"])

    def test_missing_practice_note_blocks(self):
        # no "practice run" note => the upgrade may have fetched a real release instead of the candidate
        self.assertFalse(self._drive(self._clean(notes=[]))["passed"])

    def test_driver_crash_blocks(self):
        self.assertFalse(self._drive(rc=1, stdout="", stderr="boom")["passed"])

    def test_no_gate_result_marker_blocks(self):
        self.assertFalse(self._drive(stdout="garbage with no marker")["passed"])


@unittest.skipUnless(_CONSTRUCTION, _SKIP)
class TestRollbackLegReporting(unittest.TestCase):
    """The ROLLBACK leg (`_rollback_leg`) undoes the staged practice upgrade and asserts the PARSED result — a
    real staged undo with a recovery point — never the exit code. It blocks a vacuous `state:"none"` (nothing
    seen to undo), a refusal (the StarshipSuperjam/engine-template#599 foreign-work class), a partial undo, a
    missing recovery point, a resync failure, a memory reach a projection should never make, a dirty tree after
    the undo, a driver crash, or a missing result marker. The rollback child's stdout AND the trailing
    `git status --porcelain` are both `_run` calls, so cases inject an ordered two-element `side_effect`."""

    def _drive(self, result_obj=None, *, rc=0, stdout=None, stderr="", status_out="", status_rc=0):
        out = stdout if stdout is not None else ("ROLLBACK_RESULT:" + json.dumps(result_obj))
        # call 1 = the rollback driver child; call 2 = the `git status --porcelain` clean check
        with mock.patch.object(rg, "_run", side_effect=[_proc(rc, out, stderr),
                                                        _proc(status_rc, status_out, "")]):
            return rg._rollback_leg("/tmp/proj", "v9.9.9")

    def _clean(self, **over):
        base = {"state": "staged", "undone": True, "recovery_point": "engine-rescue/2026",
                "restored": False, "memory_note": "no saved-memory change to put back"}
        base.update(over)
        return base

    def test_clean_rollback_passes(self):
        self.assertTrue(self._drive(self._clean())["passed"])

    def test_vacuous_state_none_blocks(self):
        # exit 0 with nothing to undo (or an in-projection git failure degrading to none) must NOT pass
        res = self._drive(self._clean(state="none", undone=False, recovery_point=None))
        self.assertFalse(res["passed"])
        self.assertIn("vacuous", res["detail"])

    def test_refusal_blocks_and_names_the_599_class(self):
        res = self._drive(self._clean(refused=True, reason="you have unsaved work"))
        self.assertFalse(res["passed"])
        self.assertIn("StarshipSuperjam/engine-template#599", res["detail"])

    def test_partial_blocks(self):
        self.assertFalse(self._drive(self._clean(partial=True, reason="couldn't finish"))["passed"])

    def test_not_undone_blocks(self):
        self.assertFalse(self._drive(self._clean(undone=False))["passed"])

    def test_missing_recovery_point_blocks(self):
        self.assertFalse(self._drive(self._clean(recovery_point=""))["passed"])

    def test_resync_failure_blocks(self):
        self.assertFalse(self._drive(self._clean(resync_failed=True))["passed"])

    def test_memory_restore_in_projection_blocks(self):
        self.assertFalse(self._drive(self._clean(restored=True))["passed"])

    def test_memory_vault_reach_blocks(self):
        res = self._drive(self._clean(memory_note="couldn't reach your backup to put the copy back"))
        self.assertFalse(res["passed"])

    def test_dirty_tree_after_undo_blocks(self):
        res = self._drive(self._clean(), status_out=" M .engine/tools/module_manager.py\n")
        self.assertFalse(res["passed"])
        self.assertIn("left changes", res["detail"])

    def test_driver_crash_blocks(self):
        self.assertFalse(self._drive(rc=1, stdout="", stderr="boom")["passed"])

    def test_no_rollback_marker_blocks(self):
        self.assertFalse(self._drive(stdout="garbage with no marker")["passed"])


@unittest.skipUnless(_CONSTRUCTION, _SKIP)
class TestTransitionComposition(unittest.TestCase):
    """`_upgrade_from` composes the two legs into one transition record. The rollback leg runs ONLY if the
    upgrade leg passed (a rollback on a half-applied tree would obscure the real upgrade failure), and the
    rollback child is spawned AFTER the upgrade child (ordering is load-bearing — the overlay must land before
    the candidate's rollback code is imported)."""

    def _compose(self, upgrade_leg, rollback_leg):
        with mock.patch.object(rg, "_archive_baseline", return_value="/tmp/proj"), \
             mock.patch.object(rg, "_project_to_deployed", return_value=[]), \
             mock.patch.object(rg, "_assert_isolated", return_value=None), \
             mock.patch.object(rg, "_upgrade_leg", side_effect=upgrade_leg) as u, \
             mock.patch.object(rg, "_rollback_leg", side_effect=rollback_leg) as r:
            res = rg._upgrade_from("v9.9.9", "/tmp/candidate")
        return res, u, r

    def test_both_pass_is_a_passing_transition(self):
        res, u, r = self._compose([{"passed": True, "detail": ""}], [{"passed": True, "detail": ""}])
        self.assertTrue(res["passed"])
        self.assertEqual(res["baseline"], "v9.9.9")
        self.assertTrue(res["rollback"]["passed"])
        self.assertEqual(u.call_count, 1)
        self.assertEqual(r.call_count, 1)                       # rollback ran because the upgrade passed

    def test_upgrade_failure_skips_the_rollback_leg(self):
        def _boom(*a, **k):
            raise AssertionError("the rollback leg must not run when the upgrade failed")
        res, u, r = self._compose([{"passed": False, "detail": "upgrade/v9.9.9: red"}], _boom)
        self.assertFalse(res["passed"])
        self.assertIsNone(res["rollback"]["passed"])           # recorded as not-run, not as a failure
        self.assertIn("not run", res["rollback"]["detail"])
        self.assertEqual(r.call_count, 0)                      # the rollback leg was never called

    def test_rollback_failure_fails_the_transition(self):
        res, _u, _r = self._compose([{"passed": True, "detail": ""}],
                                    [{"passed": False, "detail": "rollback/v9.9.9: partial"}])
        self.assertFalse(res["passed"])
        self.assertFalse(res["rollback"]["passed"])

    def test_the_second_spawn_is_the_rollback_child(self):
        # Prove ordering through the REAL legs (not stubs): the first `_run` argv carries the upgrade driver
        # (module_manager.upgrade(...)), the second carries the rollback driver (module_manager.rollback(...)).
        calls = []

        def _record(cmd, *a, **k):
            calls.append(cmd)
            if "module_manager.upgrade(" in " ".join(cmd):
                return _proc(0, "GATE_RESULT:" + json.dumps(
                    {"refused": False, "applied": True, "reason": None, "findings": [],
                     "notes": [mm.PRACTICE_RUN_NOTE]}), "")
            if "module_manager.rollback(" in " ".join(cmd):
                return _proc(0, "ROLLBACK_RESULT:" + json.dumps(
                    {"state": "staged", "undone": True, "recovery_point": "engine-rescue/x",
                     "restored": False, "memory_note": "no saved-memory change to put back"}), "")
            return _proc(0, "", "")   # the trailing git status --porcelain clean check
        with mock.patch.object(rg, "_archive_baseline", return_value="/tmp/proj"), \
             mock.patch.object(rg, "_project_to_deployed", return_value=[]), \
             mock.patch.object(rg, "_assert_isolated", return_value=None), \
             mock.patch.object(rg, "_run", side_effect=_record):
            res = rg._upgrade_from("v9.9.9", "/tmp/candidate")
        self.assertTrue(res["passed"])
        drivers = [" ".join(c) for c in calls if "-c" in c]
        self.assertIn("module_manager.upgrade(", drivers[0])   # first driver spawn = the upgrade
        self.assertIn("module_manager.rollback(", drivers[1])  # second driver spawn = the rollback


@unittest.skipUnless(_CONSTRUCTION, _SKIP)
class TestBaselineSelectionExcluded(unittest.TestCase):
    """`_baseline_selection` records the below-floor version tags it excluded, so the evidence can show the
    matrix was not silently shrunk."""

    def test_below_floor_tags_are_recorded_as_excluded(self):
        floor = validate.load_json(os.path.join(validate.ROOT, ".engine", "engine.json"))["min_upgradeable_from"]
        with mock.patch.object(rg, "_run", return_value=_proc(0, "v0.1.0\nv0.2.0\nv" + floor + "\n", "")):
            sel = rg._baseline_selection()
        self.assertEqual(sel["floor"], floor)
        self.assertIn("v" + floor, sel["baselines"])
        self.assertIn("v0.1.0", sel["excluded"])               # a below-floor tag is recorded, not dropped
        self.assertNotIn("v0.1.0", sel["baselines"])
        for t in sel["excluded"]:
            self.assertLess(validate._ver_tuple(t[1:]), validate._ver_tuple(floor))


@unittest.skipUnless(_CONSTRUCTION, _SKIP)
class TestArmUpgradesShape(unittest.TestCase):
    """Arm B's result carries the transition matrix and its shape fields (floor / baselines / excluded), so the
    release evidence can state the matrix instance, not just a pass/fail."""

    def test_transitions_and_shape_fields_present(self):
        with mock.patch.object(rg, "_baseline_selection",
                               return_value={"floor": "0.3.2", "baselines": ["v0.3.2", "v0.4.0"],
                                             "excluded": ["v0.1.0"]}), \
             mock.patch.object(rg, "_upgrade_from",
                               side_effect=lambda tag, cand: {"baseline": tag,
                                                              "upgrade": {"passed": True, "detail": ""},
                                                              "rollback": {"passed": True, "detail": ""},
                                                              "passed": True}):
            arm = rg._arm_upgrades("/tmp/candidate")
        self.assertTrue(arm["passed"])
        self.assertEqual(arm["floor"], "0.3.2")
        self.assertEqual(arm["baselines"], ["v0.3.2", "v0.4.0"])
        self.assertEqual(arm["excluded"], ["v0.1.0"])
        self.assertEqual([t["baseline"] for t in arm["transitions"]], ["v0.3.2", "v0.4.0"])
        self.assertEqual(arm["failures"], [])

    def test_a_failing_transition_surfaces_leg_detail(self):
        with mock.patch.object(rg, "_baseline_selection",
                               return_value={"floor": "0.3.2", "baselines": ["v0.3.2"], "excluded": []}), \
             mock.patch.object(rg, "_upgrade_from",
                               side_effect=lambda tag, cand: {"baseline": tag,
                                                              "upgrade": {"passed": True, "detail": ""},
                                                              "rollback": {"passed": False,
                                                                           "detail": "rollback/v0.3.2: partial"},
                                                              "passed": False}):
            arm = rg._arm_upgrades("/tmp/candidate")
        self.assertFalse(arm["passed"])
        self.assertEqual(arm["failures"], ["rollback/v0.3.2: partial"])

    def test_an_upgrade_failure_does_not_pollute_failures_with_the_not_run_line(self):
        # when the upgrade leg fails, the rollback leg is recorded as not-run (passed:None) — that placeholder
        # must NOT appear in the operator-facing failures list, only the real upgrade-leg detail.
        with mock.patch.object(rg, "_baseline_selection",
                               return_value={"floor": "0.3.2", "baselines": ["v0.3.2"], "excluded": []}), \
             mock.patch.object(rg, "_upgrade_from",
                               side_effect=lambda tag, cand: {"baseline": tag,
                                                              "upgrade": {"passed": False,
                                                                          "detail": "upgrade/v0.3.2: red"},
                                                              "rollback": {"passed": None,
                                                                           "detail": "not run — the upgrade "
                                                                                     "did not complete"},
                                                              "passed": False}):
            arm = rg._arm_upgrades("/tmp/candidate")
        self.assertFalse(arm["passed"])
        self.assertEqual(arm["failures"], ["upgrade/v0.3.2: red"])
        self.assertFalse(any("not run" in f for f in arm["failures"]))


@unittest.skipUnless(_CONSTRUCTION, _SKIP)
class TestCandidateIdentity(unittest.TestCase):
    """`_candidate_tree_sha` computes a real git tree hash of the working tree (the identity stamped into the
    gate result). Exercised against real git — the run_gate/pr-body unit tests mock it, so this is the one place
    the actual git plumbing is checked."""

    def test_returns_a_git_tree_sha(self):
        sha = rg._candidate_tree_sha()
        self.assertIsInstance(sha, str)
        self.assertEqual(len(sha), 40)                         # a git object name (SHA-1 tree hash)
        self.assertTrue(all(c in "0123456789abcdef" for c in sha))


@unittest.skipUnless(_CONSTRUCTION, _SKIP)
class TestSummaryMarkdown(unittest.TestCase):
    """`_summary_md` renders the per-transition matrix for the step summary — structured fields only, never a
    raw `detail` string, and it states the floor and count so a shrunken matrix is visible."""

    def _result(self, transitions, **overupg):
        passed = all(t["passed"] for t in transitions)
        up = {"passed": passed, "floor": "0.3.2", "baselines": [t["baseline"] for t in transitions],
              "excluded": [], "transitions": transitions, "failures": []}
        up.update(overupg)
        return {"ran": True, "passed": passed, "upgrades": up}

    def test_renders_rows_floor_and_count(self):
        md = rg._summary_md(self._result([
            {"baseline": "v0.3.2", "upgrade": {"passed": True, "detail": "x"},
             "rollback": {"passed": True, "detail": "y"}, "passed": True}]))
        self.assertIn("| `v0.3.2` | pass | pass |", md)
        self.assertIn("floor `0.3.2`", md)
        self.assertIn("1 transition", md)
        self.assertNotIn("qualification", md.lower())

    def test_no_raw_detail_leaks(self):
        secret = "/Users/someone/secret/path/traceback"
        md = rg._summary_md(self._result([
            {"baseline": "v0.3.2", "upgrade": {"passed": False, "detail": secret},
             "rollback": {"passed": None, "detail": secret}, "passed": False}], passed=False))
        self.assertNotIn(secret, md)                           # detail strings never reach the summary
        self.assertIn("FAIL", md)
        self.assertIn("not run", md)

    def test_inert_result_is_plain(self):
        self.assertIn("not applicable", rg._summary_md({"ran": False, "passed": True}).lower())


@unittest.skipUnless(_CONSTRUCTION, _SKIP)
class TestRunGateIdentity(unittest.TestCase):
    """`run_gate` stamps the candidate identity (tree sha + a UTC timestamp) so the release-PR renderer can tie
    the transition matrix to the tree it was run against."""

    def test_result_carries_candidate_identity(self):
        with mock.patch.object(rg._ccc, "_in_home_repo", return_value=True), \
             mock.patch.object(rg, "_worktree_digest", return_value="SAME"), \
             mock.patch.object(rg, "_arm_operates", return_value={"passed": True, "failures": []}), \
             mock.patch.object(rg, "_archive_candidate", return_value="/tmp/candidate"), \
             mock.patch.object(rg, "_arm_upgrades", return_value={"passed": True, "failures": [],
                                                                  "transitions": []}), \
             mock.patch.object(rg, "_candidate_tree_sha", return_value="deadbeef"):
            result = rg.run_gate()
        self.assertTrue(result["passed"])
        self.assertEqual(result["candidate_tree"], "deadbeef")
        self.assertIn("generated_at", result)


@unittest.skipUnless(_CONSTRUCTION, _SKIP)
class TestNoBaselinesFailsClosed(unittest.TestCase):
    """A checkout with no in-range baseline (shallow / tag-less) BLOCKS rather than reporting a vacuous pass."""

    def test_empty_baselines_blocks(self):
        with mock.patch.object(rg, "_run", return_value=_proc(0, "", "")):   # `git tag` lists nothing
            with self.assertRaises(rg.GateError):
                rg._arm_upgrades("/tmp/candidate")


@unittest.skipUnless(_CONSTRUCTION, _SKIP)
class TestRenderCopy(unittest.TestCase):
    """The operator-facing copy is plain language — never a check id or an internal arm token."""

    def test_inert_copy(self):
        self.assertIn("inert", rg._render({"ran": False, "passed": True}).lower())

    def test_pass_copy(self):
        self.assertIn("passed", rg._render({"ran": True, "passed": True}).lower())

    def test_blocked_copy_is_plain(self):
        text = rg._render({"ran": True, "passed": False,
                           "operates": {"passed": False}, "upgrades": {"passed": True}})
        self.assertIn("would not work", text.lower())
        self.assertIn("nothing was changed", text.lower())
        self.assertIn("operate", text.lower())
        for jargon in ("engine/check", "knowledge-coverage", "Arm A", "Arm B", "_reconcile", "DanglingImport"):
            self.assertNotIn(jargon, text)


@unittest.skipUnless(_CONSTRUCTION, _SKIP)
class TestDeclineVocabulary(unittest.TestCase):
    """The declined arm declines every DECLINABLE module — both `default-on` (the #663 shape) and `optional`
    (the #646 shape). If either status literal is ever renamed, the declined projection would silently stop
    covering that half (the gate itself fails closed only when NOTHING is declinable, which a surviving
    default-on module masks). These pin both literals loudly at construction, where a maintainer sees it
    before a cut."""

    def _live_statuses(self):
        modules_dir = os.path.join(validate.ROOT, ".engine", "modules")
        return [validate.load_json(os.path.join(modules_dir, mid, "manifest.json")).get("status")
                for mid in sorted(os.listdir(modules_dir))
                if os.path.isfile(os.path.join(modules_dir, mid, "manifest.json"))]

    def test_at_least_one_default_on_module_exists(self):
        self.assertIn("default-on", self._live_statuses(),
                      "no module has status 'default-on' — the gate's declined (#663) arm would have nothing "
                      "to decline; update release_gate._decline_optional_modules for the new vocabulary")

    def test_at_least_one_optional_module_exists(self):
        # Without this, a rename of the 'optional' literal would leave the declined arm silently covering only
        # the #663 (default-on) half while staying green, since a default-on module keeps `declinable` non-empty.
        self.assertIn("optional", self._live_statuses(),
                      "no module has status 'optional' — the gate's declined (#646) arm would silently stop "
                      "covering the add-on-declined shape; update release_gate._decline_optional_modules")


@unittest.skipUnless(_CONSTRUCTION, _SKIP)
class TestNestedEnvScrub(unittest.TestCase):
    """Every process the gate spawns inside a projection runs with the release workflow's GitHub-Actions
    identity stripped. A projection has no real pull request, so leaking the ambient CI/PR env made the
    PR-context check (pr-body-completeness) misfire and block the first live
    cut; `_nested_env` restores the offline posture of a local run. `patch.dict` restores os.environ after."""

    _CI_VARS = {"CI": "true", "GITHUB_ACTIONS": "true", "GITHUB_EVENT_PATH": "/x/event.json",
                "GITHUB_REPOSITORY": "o/r", "GITHUB_TOKEN": "secret", "RUNNER_TEMP": "/t", "ACTIONS_ID": "1"}

    def test_strips_actions_ci_identity_keeps_the_rest(self):
        with mock.patch.dict(os.environ, {**self._CI_VARS, "PATH": "/usr/bin", "UV_CACHE_DIR": "/c"}, clear=False):
            env = rg._nested_env()
        for leaked in self._CI_VARS:
            self.assertNotIn(leaked, env, f"{leaked} leaked into a projection spawn")   # no PR/CI identity
        self.assertEqual(env.get(rg._NESTED_ENV), "1")                                  # the re-entry guard is set
        self.assertEqual(env.get("PATH"), "/usr/bin")                                   # a non-Actions var is kept
        self.assertEqual(env.get("UV_CACHE_DIR"), "/c")                                 # the tool-runtime var kept

    def test_extra_keys_carry_through_and_token_still_denied(self):
        with mock.patch.dict(os.environ, {"GITHUB_TOKEN": "secret"}, clear=False):
            env = rg._nested_env(**{rg._DRIVER_CANDIDATE: "/cand"})
        self.assertEqual(env[rg._DRIVER_CANDIDATE], "/cand")           # the driver's extra reaches the child
        self.assertNotIn("GITHUB_TOKEN", env)                          # ...and the token is still stripped

    def test_scrub_defeats_the_pr_context_leak_end_to_end(self):
        # The #676 incident proven through the REAL validate.get_pr_body in a spawned child, not just the
        # helper in isolation: a workflow_dispatch event (no pull_request) leaked into the gate's parent env
        # makes get_pr_body read "" — which pr-body-completeness evaluates as "sections missing" — but a child
        # launched through _nested_env() sees the CI identity stripped, so get_pr_body reads None and the
        # PR-context checks no-op. Guards the conjunction (leaked env -> wrong verdict; scrub -> fixed) against
        # a future regression to _nested_env's prefix list OR get_pr_body's None-vs-"" behaviour.
        tools_dir = os.path.dirname(os.path.abspath(validate.__file__))
        with tempfile.TemporaryDirectory() as d:
            event = os.path.join(d, "event.json")
            with open(event, "w", encoding="utf-8") as fh:
                json.dump({"action": "workflow_dispatch"}, fh)         # a dispatch event carries no pull_request
            leak = {"GITHUB_EVENT_PATH": event, "GITHUB_ACTIONS": "true", "CI": "true", "GITHUB_TOKEN": "x"}
            with mock.patch.dict(os.environ, leak, clear=False):
                self.assertEqual(validate.get_pr_body(None), "")       # the raw leak WOULD misfire ("" is not None)
                probe = ("import os, sys; sys.path.insert(0, os.getcwd()); import validate; "
                         "print(repr(validate.get_pr_body(None))); "
                         "print(any(k in os.environ for k in "
                         "('CI', 'GITHUB_ACTIONS', 'GITHUB_EVENT_PATH', 'GITHUB_TOKEN')))")
                r = subprocess.run([sys.executable, "-c", probe], cwd=tools_dir,
                                   env=rg._nested_env(), capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        lines = r.stdout.splitlines()
        self.assertEqual(lines[0], "None")                             # scrubbed child: get_pr_body -> None (no PR)
        self.assertEqual(lines[1], "False")                            # no CI identity reached the child


@unittest.skipUnless(_CONSTRUCTION, _SKIP)
class TestOperateArmReporting(unittest.TestCase):
    """`_validate_in` surfaces the validator's FAIL section as the failure reason. report() prints the verbose
    "notes (…)" section BEFORE the "FAIL (…)" one, so the reason must be read from the FAIL marker — reading the
    text before "notes (" yields an empty string, the blank-log bug that hid the first live cut's real red."""

    # a red laid out exactly as validate.report() emits it: the notes section first, then the hard-finding FAIL
    _RED = ("\nnotes (2):\n  - some disclosed no-op\n  - 1 check(s) not applicable here\n"
            "\nFAIL (2 hard finding(s)) [suite: CI] — blocks the merge:\n"
            "  - Required section '## Purpose' is missing from the pull-request body.\n"
            "  - Couldn't check the follow-up issues cited in this change's Review.\n")

    def test_fail_section_is_surfaced_not_the_empty_preamble(self):
        with mock.patch.object(rg, "_run", return_value=_proc(1, self._RED, "")):
            res = rg._validate_in("/tmp/proj", "operate/default")
        self.assertFalse(res["passed"])
        self.assertIn("operate/default: validator red", res["detail"])
        self.assertIn("Required section '## Purpose' is missing", res["detail"])   # the actual reason, not blank
        self.assertNotIn("some disclosed no-op", res["detail"])                    # the notes preamble is dropped

    def test_reason_without_fail_marker_falls_back_to_full_output(self):
        # a CONFIG ERROR / crash red carries no "FAIL (" section — the reason must still surface, never blank
        with mock.patch.object(rg, "_run", return_value=_proc(2, "", "CONFIG ERROR: cannot load the suite")):
            res = rg._validate_in("/tmp/proj", "operate/default")
        self.assertFalse(res["passed"])
        self.assertIn("CONFIG ERROR", res["detail"])

    def test_green_run_has_no_detail(self):
        with mock.patch.object(rg, "_run", return_value=_proc(0, "\nOK — suite 'CI' passed", "")):
            res = rg._validate_in("/tmp/proj", "operate/default")
        self.assertTrue(res["passed"])
        self.assertEqual(res["detail"], "")


if __name__ == "__main__":
    unittest.main()
