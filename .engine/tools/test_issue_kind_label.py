"""Tests for issue_kind_label — the on:issues applicator that keeps the GitHub-native kind labels consistent.

These lock the load-bearing behaviours: that the workflow is an engine-owned traveler (FOUNDATION_INFRA →
CODEOWNERS + upgrade overlay, the same treatment as the other engine workflows); that the title→native-label
derivation is exactly the intended mapping and refuses to guess on an unmappable title; that the applicator is
apply-only (it SKIPS a native label the repo owner deleted, never minting one); that it is idempotent (no
redundant add when the label is already present) and orthogonal to the `engine` label (it acts on ANY issue);
and that out-of-scope / unactionable inputs no-op while a genuine API failure surfaces (the safety-net fail
contract). The label value is a fixed enum, never raw title text.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate                 # noqa: E402
import module_coherence         # noqa: E402
import module_manager           # noqa: E402
import issue_label_client       # noqa: E402
import issue_kind_label as k    # noqa: E402
import quiet_call               # noqa: E402  (capture a CLI walkthrough's stdout so it can't bury the suite summary)

WORKFLOW_REL = ".github/workflows/engine-issue-kind-label.yml"


class TestWorkflowIsEngineOwnedTraveler(unittest.TestCase):
    """The workflow is a FOUNDATION_INFRA member, so it travels on upgrade (FOUNDATION_CODE) and is owned in
    CODEOWNERS (foundation_infra_paths) — the same treatment as the other engine workflows. No generic check
    catches an omission here, so these assertions ARE the guard."""

    def test_workflow_is_present_in_the_tree(self):
        self.assertTrue(os.path.isfile(os.path.join(validate.ROOT, WORKFLOW_REL)),
                        f"{WORKFLOW_REL} must exist")

    def test_is_a_foundation_infra_member(self):
        self.assertIn(WORKFLOW_REL, module_coherence.FOUNDATION_INFRA)

    def test_travels_on_upgrade_via_foundation_code(self):
        self.assertIn(WORKFLOW_REL, module_manager.FOUNDATION_CODE)

    def test_renders_into_codeowners_via_foundation_infra_paths(self):
        owned = module_coherence.foundation_infra_paths()
        self.assertIn(WORKFLOW_REL, owned)
        self.assertFalse(any("*" in p for p in owned), "paths are concrete, never bare globs")


class TestKindDerivation(unittest.TestCase):
    """native_label_for_title is the single source both the applicator and any one-time backfill call."""

    def test_each_kind_maps_to_its_native_label(self):
        cases = {
            "Bug: broke": "bug", "Fix: it": "bug", "Engine fault: x": "bug", "Defect: y": "bug",
            "Security: z": "bug", "Feature: new": "enhancement", "Improvement: better": "enhancement",
            "Docs: note": "documentation", "Documentation: full": "documentation", "Question: ?": "question",
        }
        for title, expected in cases.items():
            self.assertEqual(k.native_label_for_title(title), expected, title)

    def test_case_and_whitespace_insensitive(self):
        self.assertEqual(k.native_label_for_title("fix: lower"), "bug")
        self.assertEqual(k.native_label_for_title("  Feature: leading space"), "enhancement")
        self.assertEqual(k.native_label_for_title("Engine fault : spaced colon"), "bug")

    def test_unmappable_titles_get_no_label_never_a_guess(self):
        for title in ("Maintenance: upkeep", "Migration M3: rename", "Delivery wave 2", "no prefix", "", None,
                      "Provisioning: detect", "Log the decision", "documentationless prose"):
            self.assertIsNone(k.native_label_for_title(title), repr(title))

    def test_docs_prefix_does_not_shadow_documentation(self):
        self.assertEqual(k.native_label_for_title("Documentation: x"), "documentation")

    def test_the_value_range_is_only_the_four_github_natives(self):
        self.assertEqual(set(k.NATIVE_KIND_LABELS), {"bug", "enhancement", "documentation", "question"})


class TestApplyIsApplyOnlyAndIdempotent(unittest.TestCase):
    def _client(self, gh):
        return issue_label_client.IssueLabelClient("o/r", "t", user_agent=k.USER_AGENT, transport=gh)

    def test_mappable_title_present_on_repo_absent_on_issue_gets_one_add(self):
        gh = k._FakeGitHub(label_exists=True)
        action = k.apply_kind_label({"number": 1, "title": "Fix: x", "labels": []}, self._client(gh))
        self.assertEqual(action, "labelled")
        self.assertEqual(len(gh.issue_label_adds()), 1)

    def test_repo_absent_native_label_is_skipped_never_minted(self):
        gh = k._FakeGitHub(label_exists=False)
        action = k.apply_kind_label({"number": 2, "title": "Feature: x", "labels": []}, self._client(gh))
        self.assertEqual(action, "absent")
        self.assertEqual(gh.issue_label_adds(), [])
        # no POST to /repos/o/r/labels (label creation) ever happened
        self.assertFalse(any(m == "POST" and p.endswith("/repos/o/r/labels") for m, p, _ in gh.calls))

    def test_label_already_present_is_a_no_op(self):
        gh = k._FakeGitHub(label_exists=True)
        action = k.apply_kind_label(
            {"number": 3, "title": "Improvement: x", "labels": [{"name": "enhancement"}]}, self._client(gh))
        self.assertEqual(action, "already")
        self.assertEqual(gh.issue_label_adds(), [])

    def test_unmappable_title_makes_no_github_calls(self):
        gh = k._FakeGitHub()
        action = k.apply_kind_label({"number": 4, "title": "Maintenance: x", "labels": []}, self._client(gh))
        self.assertEqual(action, "no-kind")
        self.assertEqual(gh.calls, [])

    def test_acts_on_any_issue_not_only_engine_labelled(self):
        # The kind axis is orthogonal to the engine label — a non-engine issue is still labelled.
        gh = k._FakeGitHub(label_exists=True)
        action = k.apply_kind_label(
            {"number": 5, "title": "Bug: x", "labels": [{"name": "some-product-label"}]}, self._client(gh))
        self.assertEqual(action, "labelled")

    def test_api_failure_surfaces_as_degraded_write(self):
        def boom(method, path, body=None):
            if "/labels/" in path:      # the label-exists GET fails hard
                return 500, None
            return 200, None
        with self.assertRaises(issue_label_client.DegradedWriteError):
            k.apply_kind_label({"number": 6, "title": "Fix: x", "labels": []}, self._client(boom))


class TestScopeFilter(unittest.TestCase):
    def test_partial_or_non_issue_events_are_out_of_scope(self):
        self.assertIsNone(k._issue_or_none({"issue": None}))
        self.assertIsNone(k._issue_or_none({"issue": {"number": "nan"}}))
        self.assertIsNone(k._issue_or_none({}))
        self.assertIsNone(k._issue_or_none("not a dict"))


class TestRunFailContract(unittest.TestCase):
    """_run reads the event from $GITHUB_EVENT_PATH and applies the safety-net-not-a-gate fail contract
    (mirroring the conformance net's TestRunFailContract): no/partial/malformed event or an unmappable
    title → quiet exit 0; a mappable title with no token → exit 1 (the net's own breakage is visible).
    These paths reach no network."""

    def _env(self, **overrides):
        keys = ("GITHUB_EVENT_PATH", "GITHUB_TOKEN", "GITHUB_REPOSITORY")
        saved = {kk: os.environ.get(kk) for kk in keys}

        def restore():
            for kk, v in saved.items():
                if v is None:
                    os.environ.pop(kk, None)
                else:
                    os.environ[kk] = v
        self.addCleanup(restore)
        for kk in keys:
            os.environ.pop(kk, None)
        for kk, v in overrides.items():
            if v is not None:
                os.environ[kk] = v

    def _event_file(self, event) -> str:
        fd, path = tempfile.mkstemp(suffix=".json")
        self.addCleanup(os.remove, path)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            if isinstance(event, str):
                fh.write(event)            # raw text — the malformed-JSON case
            else:
                json.dump(event, fh)
        return path

    def test_no_event_exits_zero(self):
        self._env()  # GITHUB_EVENT_PATH unset
        self.assertEqual(quiet_call.run(k.main, []), 0)

    def test_malformed_event_json_exits_zero(self):
        path = self._event_file("{not json at all")
        self._env(GITHUB_EVENT_PATH=path, GITHUB_TOKEN="tok", GITHUB_REPOSITORY="o/r")
        self.assertEqual(quiet_call.run(k.main, []), 0)

    def test_partial_event_exits_zero(self):
        path = self._event_file({"issue": None})
        self._env(GITHUB_EVENT_PATH=path, GITHUB_TOKEN="tok", GITHUB_REPOSITORY="o/r")
        self.assertEqual(quiet_call.run(k.main, []), 0)

    def test_unmappable_title_exits_zero_without_network(self):
        # unmappable → no-op BEFORE the env check, so exit 0 even with no token (and no client built).
        path = self._event_file({"number": 1, "issue": {"number": 1, "title": "Delivery wave 2", "labels": []}})
        self._env(GITHUB_EVENT_PATH=path)
        self.assertEqual(quiet_call.run(k.main, []), 0)

    def test_mappable_title_without_token_exits_one(self):
        path = self._event_file({"issue": {"number": 1, "title": "Fix: broken thing", "labels": []}})
        self._env(GITHUB_EVENT_PATH=path)  # mappable but no token/repo → visible failure
        self.assertEqual(quiet_call.run(k.main, []), 1)


class TestImportLayering(unittest.TestCase):
    def test_hot_path_import_stays_lean(self):
        # The applicator runs per issue event; importing it must never drag the module-manager stack in.
        for heavy in ("release_cut", "module_manager", "module_coherence"):
            self.assertNotIn(heavy, getattr(k, "__dict__", {}),
                             f"issue_kind_label must not import {heavy} (per-issue CI hot path)")


class TestDemoSelfChecks(unittest.TestCase):
    def test_demo_passes(self):
        self.assertEqual(quiet_call.run(k._demo), 0)


if __name__ == "__main__":
    unittest.main()
