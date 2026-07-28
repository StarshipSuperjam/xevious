"""The security floor (issue #124): the traveling git-native files — a committed advisory
secret-scan workflow + dependabot.yml, engine-owned so they travel to every generated repo. These
tests attest the load-bearing facts a non-engineer cannot read off the YAML: the files exist, are
engine-owned (travel + render into CODEOWNERS), and the scan is ADVISORY (never a required check, so a
finding can never block a merge). The live-scanner behavior is covered by demo_secret_scan against the
real gitleaks binary, skipped where it is not installed."""
import os
import shutil
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate            # noqa: E402
import module_coherence    # noqa: E402
import module_manager      # noqa: E402
import protection_guard    # noqa: E402
import demo_secret_scan    # noqa: E402
import quiet_call          # noqa: E402  (capture the demo walkthrough so it can't bury the summary)

WORKFLOW_REL = ".github/workflows/secret-scan.yml"
DEPENDABOT_REL = ".github/dependabot.yml"


class TestSecretScanFloorFilesAreEngineOwnedTravelers(unittest.TestCase):
    """Both files are FOUNDATION_INFRA members, so they travel on upgrade (FOUNDATION_CODE) and are
    owned in CODEOWNERS (foundation_infra_paths) — the same treatment as engine-ci.yml."""

    def test_both_files_are_present_in_the_tree(self):
        for rel in (WORKFLOW_REL, DEPENDABOT_REL):
            self.assertTrue(os.path.isfile(os.path.join(validate.ROOT, rel)), f"{rel} must exist")

    def test_both_are_foundation_infra_members(self):
        self.assertIn(WORKFLOW_REL, module_coherence.FOUNDATION_INFRA)
        self.assertIn(DEPENDABOT_REL, module_coherence.FOUNDATION_INFRA)

    def test_both_travel_on_upgrade_via_foundation_code(self):
        # FOUNDATION_CODE (the upgrade overlay-replace set) derives from FOUNDATION_INFRA, so engine-
        # owned travelers are refreshed wholesale on upgrade — never an operator-edited surface.
        self.assertIn(WORKFLOW_REL, module_manager.FOUNDATION_CODE)
        self.assertIn(DEPENDABOT_REL, module_manager.FOUNDATION_CODE)

    def test_both_render_into_codeowners_via_foundation_infra_paths(self):
        owned = module_coherence.foundation_infra_paths()
        self.assertIn(WORKFLOW_REL, owned)
        self.assertIn(DEPENDABOT_REL, owned)
        self.assertFalse(any("*" in p for p in owned), "paths are concrete, never bare globs")


class TestSecretScanIsAdvisory(unittest.TestCase):
    """The locked invariant: code/secret-scan alerts are advisory — a finding never gates a merge."""

    def test_workflow_declares_the_secret_scan_job(self):
        text = validate.read(os.path.join(validate.ROOT, WORKFLOW_REL))
        self.assertIn("name: secret-scan", text)
        self.assertIn("pull_request", text, "the floor scans every pull request")

    def test_secret_scan_is_not_a_required_check(self):
        # protection_guard.REQUIRED_CHECKS is the SINGLE home of the required-check list. The advisory
        # guarantee is exactly: the secret-scan job name is absent from it.
        self.assertNotIn("secret-scan", protection_guard.REQUIRED_CHECKS)
        self.assertEqual(protection_guard.REQUIRED_CHECKS, ["engine-ci", "engine-guard"],
                         "the frozen required-check set is unchanged — adding the scan only warns")

    def test_demo_job_name_matches_the_workflow(self):
        self.assertEqual(demo_secret_scan.WORKFLOW_JOB_NAME, "secret-scan")
        text = validate.read(os.path.join(validate.ROOT, WORKFLOW_REL))
        self.assertIn(f"name: {demo_secret_scan.WORKFLOW_JOB_NAME}", text)


class TestDependabotConfig(unittest.TestCase):
    """The dependency floor travels with the engine's own runtime + the pinned actions."""

    def test_declares_the_engine_runtime_and_actions_ecosystems(self):
        text = validate.read(os.path.join(validate.ROOT, DEPENDABOT_REL))
        self.assertIn("version: 2", text)
        self.assertIn('package-ecosystem: "uv"', text)
        self.assertIn('package-ecosystem: "github-actions"', text)
        self.assertIn('directory: "/.engine"', text)


class TestSecretScanDemo(unittest.TestCase):
    """The demo exercises the real scanner; here we pin its always-true and skip-if-absent legs."""

    def test_advisory_block_holds_without_a_scanner(self):
        self.assertTrue(quiet_call.run(demo_secret_scan._advisory_guarantee_holds))

    def test_demo_main_exits_zero(self):
        self.assertEqual(quiet_call.run(demo_secret_scan.main), 0)

    @unittest.skipUnless(shutil.which("gitleaks"), "the real gitleaks binary is not installed")
    def test_real_scanner_catches_planted_secret_and_passes_clean(self):
        self.assertTrue(quiet_call.run(demo_secret_scan._scan_planted_secret),
                        "the planted fake token must be found by the real scanner")
        self.assertTrue(quiet_call.run(demo_secret_scan._scan_clean_file),
                        "a file with no secrets must come back clean")


if __name__ == "__main__":
    unittest.main()
