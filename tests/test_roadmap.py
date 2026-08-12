from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path
import sys
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


roadmap = load_module("roadmap", ROOT / "tools" / "roadmap.py")
closures = load_module("check_roadmap_closures", ROOT / "tools" / "check_roadmap_closures.py")


class RoadmapManifestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.manifest = json.loads((ROOT / "docs" / "roadmap" / "manifest.json").read_text())

    def test_committed_manifest_is_complete_and_acyclic(self) -> None:
        self.assertEqual([], roadmap.validate_manifest(self.manifest))

    def test_duplicate_atomic_criterion_is_rejected(self) -> None:
        changed = copy.deepcopy(self.manifest)
        changed["leaves"][1]["criteria"].append(changed["leaves"][0]["criteria"][0])
        self.assertTrue(any("assigned to both" in item for item in roadmap.validate_manifest(changed)))

    def test_unsettled_spec_cannot_gain_executable_leaf(self) -> None:
        changed = copy.deepcopy(self.manifest)
        leaf = next(item for item in changed["leaves"] if item["key"] == "air.toroid")
        leaf["status"] = "planned"
        self.assertTrue(any("must be provisional" in item for item in roadmap.validate_manifest(changed)))

    def test_blocker_cycle_is_rejected(self) -> None:
        changed = copy.deepcopy(self.manifest)
        first = next(item for item in changed["leaves"] if item["key"] == "air.toroid")
        second = next(item for item in changed["leaves"] if item["key"] == "air.standard-bullets")
        first["blocked_by"] = [second["key"]]
        second["blocked_by"] = [first["key"]]
        self.assertTrue(any("blocker cycle" in item for item in roadmap.validate_manifest(changed)))

    def test_issue_body_carries_stable_identity_and_closure_contract(self) -> None:
        parent = next(item for item in self.manifest["parents"] if item["key"] == "air")
        leaf = next(item for item in self.manifest["leaves"] if item["key"] == "air.toroid")
        body = roadmap.leaf_body(leaf, parent)
        self.assertIn("<!-- roadmap-key: air.toroid -->", body)
        self.assertIn("Executable now: **no**", body)
        self.assertIn("## Closure rule", body)

    def test_exact_criterion_roster_rejects_removed_or_bogus_obligation(self) -> None:
        for replacement in ([], ["SYS-01.nonsense"]):
            changed = copy.deepcopy(self.manifest)
            leaf = next(item for item in changed["leaves"] if item["key"] == "core.director-reset")
            leaf["criteria"] = replacement
            self.assertTrue(any("criterion roster differs" in item for item in roadmap.validate_manifest(changed)))

    def test_invalid_status_and_global_key_collision_are_rejected(self) -> None:
        changed = copy.deepcopy(self.manifest)
        changed["leaves"][0]["status"] = "ready-ish"
        changed["leaves"][1]["key"] = changed["parents"][0]["key"]
        failures = roadmap.validate_manifest(changed)
        self.assertTrue(any("invalid roadmap status" in item for item in failures))
        self.assertTrue(any("globally unique" in item for item in failures))

    def test_late_slices_declare_the_build_plan_prerequisites(self) -> None:
        dependencies = self.manifest["slice_dependencies"]
        self.assertIn("7", dependencies["8"])
        self.assertIn("10", dependencies["11"])
        self.assertEqual({"1", "2", "2a", *map(str, range(3, 21))}, set(dependencies["21"]))


class ClosureGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.manifest = {
            "repository": "StarshipSuperjam/xevious",
            "parents": [{"key": "cap"}],
            "leaves": [
                {"key": "done", "status": "history", "proof": "historical"},
                {"key": "ready", "status": "planned", "proof": "playable", "blocked_by": ["done"], "records": ["SYS-01"]},
                {"key": "future", "status": "provisional", "proof": "playable"},
            ],
        }
        self.migration = {
            "parents": {"cap": {"number": 10}},
            "leaves": {
                "done": {"number": 11},
                "ready": {"number": 12},
                "future": {"number": 13},
            },
        }
        self.pr = {
            "number": 20,
            "body": "Closes #12",
            "labels": [{"name": "playtest-approved"}],
            "base": {"sha": "a" * 40},
            "head": {"sha": "b" * 40},
        }

    @mock.patch.dict("os.environ", {"ROADMAP_CLOSURES_JSON": "[12]", "ROADMAP_ISSUE_STATES_JSON": '{"11":"closed"}'}, clear=False)
    @mock.patch.object(closures, "file_at_revision", return_value="Mechanic: SYS-01\n")
    @mock.patch.object(closures, "changed_files", return_value=["docs/mechanics/099-test.md", "tests/test_feature.py"])
    def test_exact_commit_playtest_record_allows_ready_leaf(self, _changed, _content) -> None:
        comment = [{"user": {"login": "StarshipSuperjam"}, "body": f"<!-- xevious-playtest:v1 commit={'b' * 40} -->"}]
        with mock.patch.dict("os.environ", {"ROADMAP_COMMENTS_JSON": json.dumps(comment)}, clear=False):
            self.assertEqual([], closures.validate_pr(self.pr, self.manifest, self.migration))

    @mock.patch.dict("os.environ", {"ROADMAP_CLOSURES_JSON": "[12]", "ROADMAP_ISSUE_STATES_JSON": '{"11":"closed"}', "ROADMAP_COMMENTS_JSON": "[]"}, clear=False)
    @mock.patch.object(closures, "file_at_revision", return_value="Mechanic: SYS-01\n")
    @mock.patch.object(closures, "changed_files", return_value=["docs/mechanics/099-test.md", "tests/test_feature.py"])
    def test_label_without_exact_commit_record_is_rejected(self, _changed, _content) -> None:
        failures = closures.validate_pr(self.pr, self.manifest, self.migration)
        self.assertTrue(any("exact tested head commit" in item for item in failures))

    @mock.patch.dict("os.environ", {"ROADMAP_CLOSURES_JSON": "[13]"}, clear=False)
    @mock.patch.object(closures, "changed_files", return_value=[])
    def test_provisional_leaf_is_rejected(self, _changed) -> None:
        failures = closures.validate_pr(self.pr, self.manifest, self.migration)
        self.assertTrue(any("provisional" in item for item in failures))

    @mock.patch.dict("os.environ", {"ROADMAP_CLOSURES_JSON": "[10]"}, clear=False)
    @mock.patch.object(closures, "changed_files", return_value=[])
    def test_capability_parent_is_rejected(self, _changed) -> None:
        failures = closures.validate_pr(self.pr, self.manifest, self.migration)
        self.assertTrue(any("capability parent" in item for item in failures))

    @mock.patch.dict("os.environ", {"ROADMAP_CLOSURES_JSON": "[12]", "ROADMAP_ISSUE_STATES_JSON": '{"11":"open"}', "ROADMAP_COMMENTS_JSON": "[]"}, clear=False)
    @mock.patch.object(closures, "file_at_revision", return_value="Mechanic: SYS-01\n")
    @mock.patch.object(closures, "changed_files", return_value=["docs/mechanics/099-test.md", "tests/test_feature.py"])
    def test_open_blocker_is_rejected(self, _changed, _content) -> None:
        failures = closures.validate_pr(self.pr, self.manifest, self.migration)
        self.assertTrue(any("blocked by open" in item for item in failures))

    @mock.patch.dict("os.environ", {"ROADMAP_CLOSURES_JSON": "[12]", "ROADMAP_ISSUE_STATES_JSON": '{"11":"closed"}', "ROADMAP_COMMENTS_JSON": "[]"}, clear=False)
    @mock.patch.object(closures, "file_at_revision", return_value="Mechanic: OTHER-99\n")
    @mock.patch.object(closures, "changed_files", return_value=["docs/mechanics/099-unrelated.md", "tests/test_feature.py"])
    def test_unrelated_mechanics_record_is_rejected(self, _changed, _content) -> None:
        failures = closures.validate_pr(self.pr, self.manifest, self.migration)
        self.assertTrue(any("mechanics evidence for SYS-01" in item for item in failures))

    @mock.patch.dict("os.environ", {"ROADMAP_CLOSURES_JSON": "[12]", "ROADMAP_ISSUE_STATES_JSON": '{"11":"closed"}', "ROADMAP_COMMENTS_JSON": "[]"}, clear=False)
    @mock.patch.object(closures, "file_at_revision", return_value="Mechanic: SYS-01\n")
    @mock.patch.object(closures, "changed_files", return_value=["docs/mechanics/099-test.md"])
    def test_missing_automated_evidence_is_rejected(self, _changed, _content) -> None:
        failures = closures.validate_pr(self.pr, self.manifest, self.migration)
        self.assertTrue(any("automated success/failure" in item for item in failures))

    @mock.patch.object(closures, "source_pr_for_closed_issue", return_value=20)
    @mock.patch.object(closures, "load_pr")
    def test_manual_closure_requires_merged_pr_that_closes_the_leaf(self, load_pr, _source) -> None:
        load_pr.return_value = {**self.pr, "merged_at": None}
        event = {"issue": {"number": 12}}
        failures = closures.validate_issue_event(event, self.manifest, self.migration)
        self.assertTrue(any("merged delivering" in item for item in failures))


if __name__ == "__main__":
    unittest.main()
