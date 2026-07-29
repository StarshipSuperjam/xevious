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
    """`_upgrade_from` reads the practice-upgrade result and blocks on refusal, non-application, a hard gate
    finding, a driver crash, OR a missing practice-path note (a silent network fetch of a real release)."""

    def _drive(self, result_obj=None, rc=0, stdout=None, stderr=""):
        out = stdout if stdout is not None else ("GATE_RESULT:" + json.dumps(result_obj))
        with mock.patch.object(rg, "_archive_baseline", return_value="/tmp/proj"), \
             mock.patch.object(rg, "_project_to_deployed", return_value=[]), \
             mock.patch.object(rg, "_assert_isolated", return_value=None), \
             mock.patch.object(rg, "_run", return_value=_proc(rc, out, stderr)):
            return rg._upgrade_from("v9.9.9", "/tmp/candidate")

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
    PR-context checks (pr-body-completeness, disposition-issue-resolution) misfire and block the first live
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
