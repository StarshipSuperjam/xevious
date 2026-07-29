"""The actionlint workflow-grammar floor: a committed advisory workflow that lints every
.github/workflows/ file with the pinned open-source actionlint binary. These tests attest the
load-bearing facts a non-engineer cannot read off the YAML: the file exists, is engine-owned (travels
on upgrade + renders into CODEOWNERS), declares the lint job, and is ADVISORY (never a required check,
so a finding can never block a merge). The live-linter behavior is covered by demo_actionlint against
the real actionlint binary, skipped where it is not installed. Mirrors test_secret_scan.py."""
import os
import shutil
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate            # noqa: E402
import module_coherence    # noqa: E402
import module_manager      # noqa: E402
import protection_guard    # noqa: E402
import demo_actionlint     # noqa: E402
import quiet_call          # noqa: E402  (capture the demo walkthrough so it can't bury the summary)

WORKFLOW_REL = ".github/workflows/actionlint.yml"


class TestActionlintIsAnEngineOwnedTraveler(unittest.TestCase):
    """The workflow is a FOUNDATION_INFRA member, so it travels on upgrade (FOUNDATION_CODE) and is
    owned in CODEOWNERS (foundation_infra_paths) — the same treatment as the other engine workflows."""

    def test_workflow_is_present_in_the_tree(self):
        self.assertTrue(os.path.isfile(os.path.join(validate.ROOT, WORKFLOW_REL)),
                        f"{WORKFLOW_REL} must exist")

    def test_is_a_foundation_infra_member(self):
        self.assertIn(WORKFLOW_REL, module_coherence.FOUNDATION_INFRA)

    def test_travels_on_upgrade_via_foundation_code(self):
        # FOUNDATION_CODE (the upgrade overlay-replace set) derives from FOUNDATION_INFRA, so the
        # engine-owned lint workflow is refreshed wholesale on upgrade — never an operator-edited surface.
        self.assertIn(WORKFLOW_REL, module_manager.FOUNDATION_CODE)

    def test_renders_into_codeowners_via_foundation_infra_paths(self):
        owned = module_coherence.foundation_infra_paths()
        self.assertIn(WORKFLOW_REL, owned)
        self.assertFalse(any("*" in p for p in owned), "paths are concrete, never bare globs")


class TestActionlintIsAdvisory(unittest.TestCase):
    """The locked invariant: a workflow-lint finding is advisory — it never gates a merge."""

    def test_workflow_declares_the_actionlint_job_on_every_pr(self):
        text = validate.read(os.path.join(validate.ROOT, WORKFLOW_REL))
        self.assertIn("name: actionlint", text)
        self.assertIn("pull_request", text, "the floor lints every pull request")

    def test_actionlint_is_not_a_required_check(self):
        # protection_guard.REQUIRED_CHECKS is the SINGLE home of the required-check list. The advisory
        # guarantee is exactly: the actionlint job name is absent from it.
        self.assertNotIn("actionlint", protection_guard.REQUIRED_CHECKS)
        self.assertEqual(protection_guard.REQUIRED_CHECKS, ["engine-ci", "engine-guard"],
                         "the frozen required-check set is unchanged — adding the lint only warns")

    def test_demo_job_name_matches_the_workflow(self):
        self.assertEqual(demo_actionlint.WORKFLOW_JOB_NAME, "actionlint")
        text = validate.read(os.path.join(validate.ROOT, WORKFLOW_REL))
        self.assertIn(f"name: {demo_actionlint.WORKFLOW_JOB_NAME}", text)


class TestActionlintDemo(unittest.TestCase):
    """The demo exercises the real linter; here we pin its always-true and skip-if-absent legs."""

    def test_advisory_block_holds_without_a_linter(self):
        self.assertTrue(quiet_call.run(demo_actionlint._advisory_guarantee_holds))

    def test_demo_main_exits_zero(self):
        self.assertEqual(quiet_call.run(demo_actionlint.main), 0)

    @unittest.skipUnless(shutil.which("actionlint"), "the real actionlint binary is not installed")
    def test_real_linter_catches_broken_workflow_and_passes_clean(self):
        self.assertTrue(quiet_call.run(demo_actionlint._lint_broken_workflow),
                        "a workflow that waits on an undefined job must be caught by the real linter")
        self.assertTrue(quiet_call.run(demo_actionlint._lint_clean_workflow),
                        "a correct workflow must come back clean")


if __name__ == "__main__":
    unittest.main()
