from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path
import shutil
import sys
import tempfile
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
        # `air` is settled as of slice 8; `ground` is still a draft spec, so a leaf under it
        # must stay provisional until its own slice settles the description.
        changed = copy.deepcopy(self.manifest)
        leaf = next(item for item in changed["leaves"] if item["key"] == "ground.barra")
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
        # `ground` is still a draft spec, so its leaves render "Executable now: no" —
        # `air.toroid` became executable when slice 8 settled the aerial-enemies description.
        parent = next(item for item in self.manifest["parents"] if item["key"] == "ground")
        leaf = next(item for item in self.manifest["leaves"] if item["key"] == "ground.barra")
        body = roadmap.leaf_body(leaf, parent)
        self.assertIn("<!-- roadmap-key: ground.barra -->", body)
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

    def test_missing_title_and_slice_dependency_cycle_are_rejected(self) -> None:
        changed = copy.deepcopy(self.manifest)
        changed["parents"][0]["title"] = ""
        changed["slice_dependencies"]["8"] = ["9"]
        changed["slice_dependencies"]["9"] = ["8"]
        failures = roadmap.validate_manifest(changed)
        self.assertTrue(any("parent title is required" in item for item in failures))
        self.assertTrue(any("slice dependency cycle" in item for item in failures))

    def test_late_slices_declare_the_build_plan_prerequisites(self) -> None:
        dependencies = self.manifest["slice_dependencies"]
        self.assertIn("7", dependencies["8"])
        self.assertIn("10", dependencies["11"])
        self.assertEqual({"1", "2", "2a", *map(str, range(3, 21))}, set(dependencies["21"]))

    def test_fresh_and_version_one_snapshot_view_shapes_are_read(self) -> None:
        view = {"id": "view", "name": "Existing"}
        fresh = {"snapshot": {"project_views": {"data": {"node": {"views": {"nodes": [view]}}}}}}
        old = {"snapshot": {"project_views": {"data": {"viewer": {"projectV2": {"views": {"nodes": [view]}}}}}}}
        self.assertEqual([view], roadmap.snapshotted_views(fresh))
        self.assertEqual([view], roadmap.snapshotted_views(old))


class ClosureGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.manifest = {
            "repository": "StarshipSuperjam/xevious",
            "parents": [{"key": "cap"}],
            "leaves": [
                {"key": "done", "status": "history", "proof": "historical"},
                {"key": "ready", "status": "planned", "proof": "playable", "blocked_by": ["done"], "records": ["SYS-01"], "criteria": ["SYS-01.ready"]},
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
    @mock.patch.object(closures, "added_test_lines", return_value="# roadmap-evidence: SYS-01 success\n# roadmap-evidence: SYS-01 failure")
    @mock.patch.object(closures, "changed_files", return_value=["docs/mechanics/099-test.md", "tests/test_feature.py"])
    def test_exact_commit_playtest_record_allows_ready_leaf(self, _changed, _added, _content) -> None:
        comment = [{"user": {"login": "StarshipSuperjam"}, "body": f"<!-- xevious-playtest:v1 commit={'b' * 40} -->"}]
        with mock.patch.dict("os.environ", {"ROADMAP_COMMENTS_JSON": json.dumps(comment)}, clear=False):
            self.assertEqual([], closures.validate_pr(self.pr, self.manifest, self.migration))

    @mock.patch.dict("os.environ", {"ROADMAP_CLOSURES_JSON": "[12]", "ROADMAP_ISSUE_STATES_JSON": '{"11":"closed"}', "ROADMAP_COMMENTS_JSON": "[]"}, clear=False)
    @mock.patch.object(closures, "file_at_revision", return_value="Mechanic: SYS-01\n")
    @mock.patch.object(closures, "added_test_lines", return_value="# roadmap-evidence: SYS-01 success\n# roadmap-evidence: SYS-01 failure")
    @mock.patch.object(closures, "changed_files", return_value=["docs/mechanics/099-test.md", "tests/test_feature.py"])
    def test_label_without_exact_commit_record_is_rejected(self, _changed, _added, _content) -> None:
        failures = closures.validate_pr(self.pr, self.manifest, self.migration)
        self.assertTrue(any("exact tested head commit" in item for item in failures))

    @mock.patch.dict("os.environ", {"ROADMAP_CLOSURES_JSON": "[13]"}, clear=False)
    @mock.patch.object(closures, "added_test_lines", return_value="")
    @mock.patch.object(closures, "changed_files", return_value=[])
    def test_provisional_leaf_is_rejected(self, _changed, _added) -> None:
        failures = closures.validate_pr(self.pr, self.manifest, self.migration)
        self.assertTrue(any("provisional" in item for item in failures))

    @mock.patch.dict("os.environ", {"ROADMAP_CLOSURES_JSON": "[10]"}, clear=False)
    @mock.patch.object(closures, "added_test_lines", return_value="")
    @mock.patch.object(closures, "changed_files", return_value=[])
    def test_capability_parent_is_rejected(self, _changed, _added) -> None:
        failures = closures.validate_pr(self.pr, self.manifest, self.migration)
        self.assertTrue(any("capability parent" in item for item in failures))

    @mock.patch.dict("os.environ", {"ROADMAP_CLOSURES_JSON": "[12]", "ROADMAP_ISSUE_STATES_JSON": '{"11":"open"}', "ROADMAP_COMMENTS_JSON": "[]"}, clear=False)
    @mock.patch.object(closures, "file_at_revision", return_value="Mechanic: SYS-01\n")
    @mock.patch.object(closures, "added_test_lines", return_value="# roadmap-evidence: SYS-01 success\n# roadmap-evidence: SYS-01 failure")
    @mock.patch.object(closures, "changed_files", return_value=["docs/mechanics/099-test.md", "tests/test_feature.py"])
    def test_open_blocker_is_rejected(self, _changed, _added, _content) -> None:
        failures = closures.validate_pr(self.pr, self.manifest, self.migration)
        self.assertTrue(any("blocked by open" in item for item in failures))

    @mock.patch.dict("os.environ", {"ROADMAP_CLOSURES_JSON": "[12]", "ROADMAP_ISSUE_STATES_JSON": '{"11":"closed"}', "ROADMAP_COMMENTS_JSON": "[]"}, clear=False)
    @mock.patch.object(closures, "file_at_revision", return_value="Mechanic: OTHER-99\n")
    @mock.patch.object(closures, "added_test_lines", return_value="# roadmap-evidence: SYS-01 success\n# roadmap-evidence: SYS-01 failure")
    @mock.patch.object(closures, "changed_files", return_value=["docs/mechanics/099-unrelated.md", "tests/test_feature.py"])
    def test_unrelated_mechanics_record_is_rejected(self, _changed, _added, _content) -> None:
        failures = closures.validate_pr(self.pr, self.manifest, self.migration)
        self.assertTrue(any("mechanics evidence for SYS-01" in item for item in failures))

    @mock.patch.dict("os.environ", {"ROADMAP_CLOSURES_JSON": "[12]", "ROADMAP_ISSUE_STATES_JSON": '{"11":"closed"}', "ROADMAP_COMMENTS_JSON": "[]"}, clear=False)
    @mock.patch.object(closures, "file_at_revision", return_value="Mechanic: SYS-01\n")
    @mock.patch.object(closures, "added_test_lines", return_value="")
    @mock.patch.object(closures, "changed_files", return_value=["docs/mechanics/099-test.md"])
    def test_missing_automated_evidence_is_rejected(self, _changed, _added, _content) -> None:
        failures = closures.validate_pr(self.pr, self.manifest, self.migration)
        self.assertTrue(any("success and failure evidence markers" in item for item in failures))

    @mock.patch.dict("os.environ", {"ROADMAP_CLOSURES_JSON": "[12]", "ROADMAP_ISSUE_STATES_JSON": '{"11":"closed"}', "ROADMAP_COMMENTS_JSON": "[]"}, clear=False)
    @mock.patch.object(closures, "changed_files", return_value=["docs/mechanics/099-test.md", "tests/test_unrelated.py"])
    @mock.patch.object(closures, "added_test_lines", return_value="# unrelated pre-existing SYS-01 mention")
    def test_unrelated_automated_evidence_is_rejected(self, _added, _changed) -> None:
        with mock.patch.object(
            closures,
            "file_at_revision",
            side_effect=lambda _sha, path: "Mechanic: SYS-01\n" if path.startswith("docs/mechanics/") else "test OTHER-99\n",
        ):
            failures = closures.validate_pr(self.pr, self.manifest, self.migration)
        self.assertTrue(any("success and failure evidence markers for SYS-01" in item for item in failures))

    @mock.patch.object(closures, "source_pr_for_closed_issue", return_value=20)
    @mock.patch.object(closures, "load_pr")
    def test_manual_closure_requires_merged_pr_that_closes_the_leaf(self, load_pr, _source) -> None:
        load_pr.return_value = {**self.pr, "merged_at": None}
        event = {"issue": {"number": 12}}
        failures = closures.validate_issue_event(event, self.manifest, self.migration)
        self.assertTrue(any("merged delivering" in item for item in failures))

    def test_main_enforces_even_when_the_journal_has_no_phase(self) -> None:
        # The old phase gate returned 0 (a silent pass) whenever the journal `phase`
        # was not applied/reconciled. With it removed the check always enforces, so a
        # journal with no `phase` at all still reaches validate_pr and returns its verdict.
        event = {"pull_request": {"number": 20}}

        def fake_read_json(path):
            if path == closures.MANIFEST:
                return {"repository": "StarshipSuperjam/xevious", "parents": [], "leaves": []}
            if path == closures.MIGRATION:
                return {"parents": {}, "leaves": {}}  # no `phase` key
            return event

        with mock.patch.dict("os.environ", {"ROADMAP_CLOSURES_JSON": "[]"}, clear=False), \
             mock.patch.object(closures, "read_json", side_effect=fake_read_json), \
             mock.patch.object(closures, "load_pr", return_value={"number": 20}), \
             mock.patch.object(closures, "validate_pr", return_value=["boom"]) as validate:
            self.assertEqual(1, closures.main(["pr"]))
            validate.assert_called_once()


class DeliveryRecordingTests(unittest.TestCase):
    """The closure check requires a PR to record its own delivery in the manifest."""

    def setUp(self) -> None:
        self.repo = "StarshipSuperjam/xevious"
        self.pr = {"number": 20, "base": {"sha": "a" * 40}, "head": {"sha": "b" * 40}}
        self.leaves_by_number = {12: {"key": "ready"}}

    def _revisions(self, base_leaf: dict | None, head_leaf: dict | None):
        """A manifest_at_revision side effect: base sha → base manifest, head sha → head manifest."""
        base = {"leaves": [base_leaf] if base_leaf else []}
        head = {"leaves": [head_leaf] if head_leaf else []}
        return lambda sha: base if sha == self.pr["base"]["sha"] else head

    def _run(self, base_leaf, head_leaf):
        with mock.patch.dict("os.environ", {"ROADMAP_CLOSURES_JSON": "[12]"}, clear=False), \
             mock.patch.object(closures, "manifest_at_revision", side_effect=self._revisions(base_leaf, head_leaf)):
            return closures.delivery_recording_failures(self.repo, self.pr, self.leaves_by_number)

    def test_planned_to_history_with_this_pr_is_allowed(self) -> None:
        failures = self._run(
            {"key": "ready", "status": "planned"},
            {"key": "ready", "status": "history", "delivered_by": 20},
        )
        self.assertEqual([], failures)

    def test_head_still_planned_is_refused(self) -> None:
        failures = self._run({"key": "ready", "status": "planned"}, {"key": "ready", "status": "planned"})
        self.assertTrue(any("does not record it delivered" in item for item in failures))

    def test_delivered_by_another_pr_is_refused(self) -> None:
        failures = self._run(
            {"key": "ready", "status": "planned"},
            {"key": "ready", "status": "history", "delivered_by": 19},
        )
        self.assertTrue(any("does not record it delivered" in item for item in failures))

    def test_provisional_at_base_cannot_be_promoted(self) -> None:
        failures = self._run(
            {"key": "ready", "status": "provisional"},
            {"key": "ready", "status": "history", "delivered_by": 20},
        )
        self.assertTrue(any("not planned" in item for item in failures))

    def test_already_history_at_base_is_skipped(self) -> None:
        # A later PR that merely lists an already-delivered leaf must not be blocked.
        failures = self._run(
            {"key": "ready", "status": "history", "delivered_by": 8},
            {"key": "ready", "status": "history", "delivered_by": 8},
        )
        self.assertEqual([], failures)

    def test_leaf_absent_at_base_is_named_not_a_crash(self) -> None:
        failures = self._run(None, {"key": "ready", "status": "history", "delivered_by": 20})
        self.assertTrue(any("not a leaf in the manifest at its base" in item for item in failures))

    def test_unreadable_head_manifest_fails_closed(self) -> None:
        def side_effect(sha):
            if sha == self.pr["base"]["sha"]:
                return {"leaves": [{"key": "ready", "status": "planned"}]}
            raise closures.ClosureError("could not read manifest at head")

        with mock.patch.dict("os.environ", {"ROADMAP_CLOSURES_JSON": "[12]"}, clear=False), \
             mock.patch.object(closures, "manifest_at_revision", side_effect=side_effect):
            with self.assertRaises(closures.ClosureError):
                closures.delivery_recording_failures(self.repo, self.pr, self.leaves_by_number)

    def test_pr_closing_no_leaf_is_vacuous(self) -> None:
        with mock.patch.dict("os.environ", {"ROADMAP_CLOSURES_JSON": "[]"}, clear=False), \
             mock.patch.object(closures, "manifest_at_revision", side_effect=AssertionError("must not read a revision")):
            self.assertEqual([], closures.delivery_recording_failures(self.repo, self.pr, self.leaves_by_number))


class BoardArchiveTests(unittest.TestCase):
    """board_items sees archived cards; project_card_failures tolerates archived Done cards only."""

    def _page(self, nodes, has_next=False, cursor=None):
        return {"data": {"node": {"items": {"pageInfo": {"hasNextPage": has_next, "endCursor": cursor}, "nodes": nodes}}}}

    def _node(self, url, archived=False, fields=None):
        values = [{"name": val, "field": {"name": name}} for name, val in (fields or {}).items()]
        return {"id": f"card-{url}", "isArchived": archived, "content": {"url": url}, "fieldValues": {"nodes": values}}

    def test_board_items_paginates_and_flattens_including_archived(self) -> None:
        journal = {"project": {"node_id": "P"}}
        page1 = self._page([self._node("u1", fields={"Status": "Done", "Roadmap role": "Imported history"})], has_next=True, cursor="C")
        page2 = self._page([self._node("u2", archived=True, fields={"Status": "Backlog"})])
        with mock.patch.object(roadmap, "gh", side_effect=[page1, page2]):
            items = roadmap.board_items(journal)
        self.assertEqual(["u1", "u2"], sorted(items))
        self.assertEqual("Done", items["u1"][0]["status"])
        self.assertEqual("Imported history", items["u1"][0]["roadmap role"])
        self.assertFalse(items["u1"][0]["isArchived"])
        self.assertTrue(items["u2"][0]["isArchived"])

    def test_board_items_keeps_duplicate_cards_as_a_list(self) -> None:
        with mock.patch.object(roadmap, "gh", side_effect=[self._page([self._node("u1"), self._node("u1")])]):
            items = roadmap.board_items({"project": {"node_id": "P"}})
        self.assertEqual(2, len(items["u1"]))

    def test_archived_done_card_is_accepted(self) -> None:
        by_url = {"u": [{"id": "c", "isArchived": True, "status": "Done", "roadmap role": "Imported history"}]}
        self.assertEqual([], roadmap.project_card_failures("k", "u", {"status": "Done", "roadmap role": "Imported history"}, by_url))

    def test_archived_planned_card_is_a_failure(self) -> None:
        by_url = {"u": [{"id": "c", "isArchived": True, "status": "Backlog"}]}
        fails = roadmap.project_card_failures("k", "u", {"status": "Backlog"}, by_url)
        self.assertTrue(any("archived but the leaf is not delivered" in f for f in fails))

    def test_missing_and_duplicate_cards_fail(self) -> None:
        self.assertTrue(any("found 0" in f for f in roadmap.project_card_failures("k", "u", {"status": "Done"}, {})))
        by_url = {"u": [{"isArchived": False}, {"isArchived": False}]}
        self.assertTrue(any("found 2" in f for f in roadmap.project_card_failures("k", "u", {"status": "Done"}, by_url)))

    def test_field_mismatch_fails(self) -> None:
        by_url = {"u": [{"isArchived": False, "status": "Backlog"}]}
        fails = roadmap.project_card_failures("k", "u", {"status": "Done"}, by_url)
        self.assertTrue(any("Project status is" in f for f in fails))


class ApplyConvergenceTests(unittest.TestCase):
    """A converged apply writes nothing: issue skips match reconcile, board fields diff."""

    def test_issue_up_to_date_matches_reconcile_comparisons(self) -> None:
        issue = {"state": "open", "title": "T", "body": "B", "labels": [{"name": "x"}, {"name": "y"}], "milestone": {"number": 3}}
        self.assertTrue(roadmap.issue_up_to_date(issue, title="T", body="B", labels=["y", "x"], milestone=3, state="open"))
        self.assertFalse(roadmap.issue_up_to_date(issue, title="T", body="DIFF", labels=["y", "x"], milestone=3, state="open"))
        self.assertFalse(roadmap.issue_up_to_date(issue, title="T", body="B", labels=["y", "x"], milestone=3, state="closed"))
        self.assertFalse(roadmap.issue_up_to_date(issue, title="T", body="B", labels=["y", "x"], milestone=9, state="open"))
        self.assertFalse(roadmap.issue_up_to_date(issue, title="T", body="B", labels=["y"], milestone=3, state="open"))
        self.assertFalse(roadmap.issue_up_to_date(None, title="T", body="B", labels=[], milestone=None, state="open"))

    def _journal(self):
        select = lambda fid, opts: {"id": fid, "type": "ProjectV2SingleSelectField", "options": [{"name": n, "id": i} for n, i in opts]}
        return {
            "project": {"node_id": "P"},
            "project_fields": {
                "Roadmap role": select("f1", [("Leaf", "o1"), ("Imported history", "o2"), ("Parent", "o3")]),
                "Delivery slice": select("f2", [("8", "s8")]),
                "Proof level": select("f3", [("Playable", "p1")]),
                "Work type": select("f4", [("Feature", "w1")]),
                "Status": select("f5", [("Backlog", "b1"), ("Done", "d1")]),
            },
            "parents": {},
            "leaves": {"air.toroid": {"url": "u1", "node_id": "N1"}},
        }

    _MANIFEST = {"parents": [], "leaves": [{"key": "air.toroid", "slice": "8", "proof": "playable", "status": "planned"}]}

    def _sync(self, cards):
        calls: list[list] = []
        with mock.patch.object(roadmap, "board_items", return_value=cards), \
             mock.patch.object(roadmap, "graphql_batch", side_effect=lambda ops, **k: calls.append(ops)), \
             mock.patch.object(roadmap, "write_json"):
            written = roadmap.sync_project(self._MANIFEST, self._journal())
        return written, [op for ops in calls for op in ops]

    def test_sync_writes_nothing_when_the_card_already_matches(self) -> None:
        cards = {"u1": [{"id": "c1", "isArchived": False, "roadmap role": "Leaf", "delivery slice": "8",
                         "proof level": "Playable", "work type": "Feature", "status": "Backlog"}]}
        written, ops = self._sync(cards)
        self.assertEqual(0, written)
        self.assertEqual([], ops)

    def test_sync_unarchives_then_updates_only_the_differing_field(self) -> None:
        # Archived card whose Status is wrong (Done, should be Backlog for a planned leaf).
        cards = {"u1": [{"id": "c1", "isArchived": True, "roadmap role": "Leaf", "delivery slice": "8",
                         "proof level": "Playable", "work type": "Feature", "status": "Done"}]}
        written, ops = self._sync(cards)
        self.assertEqual(1, written)  # only Status differs
        self.assertTrue(any("unarchiveProjectV2Item" in op for op in ops))
        self.assertEqual(1, sum("updateProjectV2ItemFieldValue" in op for op in ops))


class DeliverTests(unittest.TestCase):
    """`roadmap deliver --pr N` records a merged PR's leaves as delivered in the manifest."""

    # A tiny manifest in the real hand-authored one-line-per-leaf style; validation is mocked so it
    # need not carry the full schema, only be valid JSON with locatable leaf lines.
    MANIFEST_TEXT = (
        "{\n"
        '  "version": 1,\n'
        '  "leaves": [\n'
        '    {"key":"done","status":"history","proof":"historical","delivered_by":8},\n'
        '    {"key":"ready","status":"planned","proof":"playable"},\n'
        '    {"key":"other","status":"planned","proof":"playable"}\n'
        "  ]\n"
        "}\n"
    )

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        self.manifest_path = Path(self.tmp) / "manifest.json"
        self.manifest_path.write_text(self.MANIFEST_TEXT, encoding="utf-8")
        # The journal holds the issue-number mapping; manifest leaves carry no issue number.
        self.journal = {
            "parents": {"cap": {"number": 10}},
            "leaves": {"done": {"number": 11}, "ready": {"number": 12}, "other": {"number": 14}},
        }

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _merged(self, *numbers: int) -> dict:
        return {"state": "MERGED", "closingIssuesReferences": [{"number": n} for n in numbers]}

    def _leaves(self) -> dict:
        return {l["key"]: l for l in json.loads(self.manifest_path.read_text())["leaves"]}

    def test_flips_only_the_prs_planned_leaves(self) -> None:
        with mock.patch.object(roadmap, "gh", return_value=self._merged(12)), \
             mock.patch.object(roadmap, "validate_manifest", return_value=[]):
            flipped = roadmap.deliver(self.journal, 20, manifest_path=self.manifest_path)
        self.assertEqual(["ready"], flipped)
        leaves = self._leaves()
        self.assertEqual("history", leaves["ready"]["status"])
        self.assertEqual(20, leaves["ready"]["delivered_by"])
        # An unrelated leaf the PR did not close stays planned and untouched.
        self.assertEqual("planned", leaves["other"]["status"])
        self.assertNotIn("delivered_by", leaves["other"])
        # The edit stays a minimal, per-line change — `other` and `done` lines are byte-identical.
        text = self.manifest_path.read_text()
        self.assertIn('{"key":"other","status":"planned","proof":"playable"}', text)
        self.assertIn('{"key":"ready","status":"history","delivered_by":20,"proof":"playable"}', text)

    def test_skips_parents_and_already_delivered(self) -> None:
        with mock.patch.object(roadmap, "gh", return_value=self._merged(10, 11, 12)), \
             mock.patch.object(roadmap, "validate_manifest", return_value=[]):
            flipped = roadmap.deliver(self.journal, 20, manifest_path=self.manifest_path)
        self.assertEqual(["ready"], flipped)  # #10 is a parent, #11 already history
        self.assertEqual(8, self._leaves()["done"]["delivered_by"])  # untouched

    def _open(self, *numbers: int) -> dict:
        return {"state": "OPEN", "closingIssuesReferences": [{"number": n} for n in numbers]}

    def test_records_delivery_on_an_open_pr(self) -> None:
        # The author runs deliver on their own open PR so the manifest edit rides the same PR;
        # the closure check then proves it is present before the PR may merge.
        with mock.patch.object(roadmap, "gh", return_value=self._open(12)), \
             mock.patch.object(roadmap, "validate_manifest", return_value=[]):
            flipped = roadmap.deliver(self.journal, 20, manifest_path=self.manifest_path)
        self.assertEqual(["ready"], flipped)
        self.assertEqual("history", self._leaves()["ready"]["status"])

    def test_refuses_a_closed_unmerged_pr(self) -> None:
        with mock.patch.object(roadmap, "gh", return_value={"state": "CLOSED", "closingIssuesReferences": [{"number": 12}]}):
            with self.assertRaises(roadmap.RoadmapError):
                roadmap.deliver(self.journal, 20, manifest_path=self.manifest_path)
        self.assertEqual(self.MANIFEST_TEXT, self.manifest_path.read_text())  # unchanged

    def test_refuses_when_nothing_planned_to_record(self) -> None:
        with mock.patch.object(roadmap, "gh", return_value=self._merged(11)):
            with self.assertRaises(roadmap.RoadmapError):
                roadmap.deliver(self.journal, 20, manifest_path=self.manifest_path)
        self.assertEqual(self.MANIFEST_TEXT, self.manifest_path.read_text())  # unchanged

    def test_an_invalid_result_aborts_the_write(self) -> None:
        with mock.patch.object(roadmap, "gh", return_value=self._merged(12)), \
             mock.patch.object(roadmap, "validate_manifest", return_value=["boom"]):
            with self.assertRaises(roadmap.RoadmapError):
                roadmap.deliver(self.journal, 20, manifest_path=self.manifest_path)
        self.assertEqual(self.MANIFEST_TEXT, self.manifest_path.read_text())  # unchanged


if __name__ == "__main__":
    unittest.main()
