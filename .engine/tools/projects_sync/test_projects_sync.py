"""Unit tests for projects_sync.py — the github-projects-sync projection tool.

The one un-runnable step (a REAL GraphQL write to a live Projects v2 board) never runs in the construction
repo — there is no board — so it is the named inductive gap. Everything around it is exercised here against
an INJECTED fake transport + fake signals + an in-memory/temp config: the real resolve / add / field-write /
verify / degrade / debounce logic runs, only the network and clock are faked. The field-ownership invariant
(only the engine's own fields, only the engine's own labeled items, never Status/position) is asserted
directly, because it is the whole basis of the one-way contract.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import quiet_call  # noqa: E402  (capture a demo walkthrough's stdout so it can't bury the suite summary)
import validate  # noqa: E402
from projects_sync import projects_sync as ps  # noqa: E402


def _signals(**over):
    base = {"live_standing": {"milestone": "v1", "phase": "building the board"},
            "att_lines": ["carry the open pull request forward"],
            "finding_count": 4, "debt_count": 2, "state": None}
    base.update(over)
    return base


def _config(**over):
    cfg = {"schema_version": 1, "project": {"id": "PVT_test"},
           "fields": {ps.FIELD_BUILDING: "F_BUILD", ps.FIELD_NEXT: "F_NEXT", ps.FIELD_REVIEW: "F_REVIEW",
                      ps.FIELD_ISSUES: "F_ISSUES", ps.FIELD_SYNCED: "F_SYNCED"},
           "label": "engine"}
    cfg.update(over)
    return cfg


def _gql(**kw):
    rec = ps._RecordingGQL(**kw)
    return rec, ps.BoardGraphQL("tok", transport=rec.transport)


_NOW = datetime(2026, 1, 1, 14, 32, tzinfo=timezone.utc)


class _Base(unittest.TestCase):
    def setUp(self):
        self._root = tempfile.TemporaryDirectory()
        self._old_root = validate.ROOT
        validate.ROOT = self._root.name

    def tearDown(self):
        validate.ROOT = self._old_root
        self._root.cleanup()


class TestProjection(_Base):
    def test_maps_the_five_engine_fields(self):
        proj = ps.compute_projection(_signals(), "2026-01-01 14:32 UTC")
        self.assertEqual(proj[ps.FIELD_BUILDING], "building the board")
        self.assertEqual(proj[ps.FIELD_NEXT], "carry the open pull request forward")
        self.assertEqual(proj[ps.FIELD_REVIEW], "4")
        self.assertEqual(proj[ps.FIELD_ISSUES], "2")
        self.assertEqual(proj[ps.FIELD_SYNCED], "2026-01-01 14:32 UTC")

    def test_field_names_are_the_plain_language_set(self):
        # The board-face field names are exactly the five plain-language labels — the leak-guard guarantee at the
        # name level (the values come from boot's already-clean signals).
        self.assertEqual(set(ps.compute_projection(_signals(), "00:00")),
                         set(ps.ENGINE_FIELD_NAMES))

    def test_missing_signals_render_em_dash_not_a_guess(self):
        proj = ps.compute_projection({"live_standing": None, "att_lines": [], "finding_count": None,
                                      "debt_count": None, "state": None}, "09:00")
        self.assertEqual(proj[ps.FIELD_BUILDING], "—")
        self.assertEqual(proj[ps.FIELD_NEXT], "—")
        self.assertEqual(proj[ps.FIELD_REVIEW], "—")
        self.assertEqual(proj[ps.FIELD_ISSUES], "—")

    def test_state_cursor_phase_is_the_fallback_when_github_is_dark(self):
        proj = ps.compute_projection(_signals(live_standing=None, state={"phase": "offline phase"}), "10:00")
        self.assertEqual(proj[ps.FIELD_BUILDING], "offline phase")


class TestGraphQL(_Base):
    def test_run_returns_data(self):
        rec, gql = _gql()
        self.assertEqual(gql.run("query{viewer{login}}", {}), {})

    def test_errors_in_a_200_body_degrade_never_succeed(self):
        rec, gql = _gql(error_mode=True)
        with self.assertRaises(ps.DegradedReadError):
            gql.run("query{x}", {})

    def test_http_error_status_degrades(self):
        gql = ps.BoardGraphQL("tok", transport=lambda m, p, b: (404, None))
        with self.assertRaises(ps.DegradedReadError):
            gql.run("query{x}", {})

    def test_resolve_board_parses_fields_options_and_autoadd(self):
        node = {"id": "PVT_x", "number": 7, "url": "https://x",
                "fields": {"nodes": [{"id": "F1", "name": ps.FIELD_BUILDING},
                                     {"id": "F2", "name": "Stage",
                                      "options": [{"id": "O1", "name": "Todo"}]}]},
                "workflows": {"nodes": [{"id": "W1", "name": "Auto-add to project", "enabled": True}]}}
        gql = ps.BoardGraphQL("tok", transport=lambda m, p, b: (200, {"data": {"node": node}}))
        board = ps.resolve_board(gql, "PVT_x")
        self.assertEqual(board["fields"][ps.FIELD_BUILDING], "F1")
        self.assertEqual(board["options"]["Stage"]["Todo"], "O1")
        self.assertTrue(board["auto_add_enabled"])

    def test_resolve_board_missing_node_degrades(self):
        gql = ps.BoardGraphQL("tok", transport=lambda m, p, b: (200, {"data": {"node": None}}))
        with self.assertRaises(ps.DegradedReadError):
            ps.resolve_board(gql, "PVT_gone")


class TestSync(_Base):
    def test_never_configured_makes_no_network_call(self):
        # No config file in the temp ROOT -> NOT_CONFIGURED, and no GraphQL is ever constructed (the sync
        # itself is a pure no-op; the handler is what surfaces the plain-language setup next step).
        result = ps.sync(force=True, config=None, signals=_signals(), gql=None, items=[])
        self.assertEqual(result["status"], ps.NOT_CONFIGURED)
        self.assertIn("engine-board-setup", result["message"])

    def test_writes_only_engine_fields_on_engine_items(self):
        rec, gql = _gql()
        result = ps.sync(force=True, config=_config(), signals=_signals(), gql=gql,
                         items=["ISSUE_1", "ISSUE_2"], now=_NOW)
        self.assertEqual(result["status"], ps.SYNCED)
        own_fields = set(_config()["fields"].values())
        own_items = {"ITEM_ISSUE_1", "ITEM_ISSUE_2"}
        adds = sets = 0
        for _path, body in rec.calls:
            q = (body or {}).get("query", "")
            v = (body or {}).get("variables", {})
            if "addProjectV2ItemById" in q:
                adds += 1
                self.assertIn(v["contentId"], {"ISSUE_1", "ISSUE_2"})
            if "updateProjectV2ItemFieldValue" in q:
                sets += 1
                self.assertIn(v["fieldId"], own_fields)        # only the engine's own fields
                self.assertIn(v["itemId"], own_items)          # only on items the engine added
                self.assertNotIn("status", q.lower())          # never Status/column/position
        self.assertEqual((adds, sets), (2, 10))

    def test_idempotent_add_returns_existing_item(self):
        rec, gql = _gql()
        # The fake maps a content id to a stable item id, so a second sync writes onto the same items.
        ps.sync(force=True, config=_config(), signals=_signals(), gql=gql, items=["ISSUE_1"], now=_NOW)
        first_items = {(b or {}).get("variables", {}).get("itemId")
                       for _p, b in rec.calls if "updateProjectV2ItemFieldValue" in (b or {}).get("query", "")}
        self.assertEqual(first_items, {"ITEM_ISSUE_1"})

    def test_board_error_degrades_not_crashes(self):
        rec, gql = _gql(error_mode=True)
        result = ps.sync(force=True, config=_config(), signals=_signals(), gql=gql, items=["ISSUE_1"],
                         now=_NOW)
        self.assertEqual(result["status"], ps.DEGRADED)
        self.assertIn("issues and pull requests are unchanged", result["message"])

    def test_incomplete_config_degrades(self):
        rec, gql = _gql()
        result = ps.sync(force=True, config={"schema_version": 1, "project": {}}, signals=_signals(),
                         gql=gql, items=["ISSUE_1"], now=_NOW)
        self.assertEqual(result["status"], ps.DEGRADED)

    def test_debounce_skips_a_recent_sync(self):
        # Stamp a sync "now", then a non-forced sync within the window is skipped without touching GitHub.
        os.makedirs(ps._config_dir(), exist_ok=True)
        ps._stamp_sync(_NOW.timestamp())
        rec, gql = _gql()
        result = ps.sync(force=False, config=_config(), signals=_signals(), gql=gql, items=["ISSUE_1"],
                         now=_NOW)
        self.assertEqual(result["status"], ps.SKIPPED)
        self.assertEqual(rec.calls, [])


class TestDiscovery(_Base):
    def _disc_transport(self, issues, prs):
        def t(method, path, body):
            self.assertEqual(path, "/graphql")
            if "repository(" in body["query"]:
                return 200, {"data": {"repository": {
                    "issues": {"nodes": [{"id": i} for i in issues]},
                    "pullRequests": {"nodes": [{"id": p} for p in prs]}}}}
            return 200, {"data": {}}
        return t

    def test_collects_issue_and_pr_node_ids(self):
        gql = ps.BoardGraphQL("tok", transport=self._disc_transport(["I_1", "I_2"], ["PR_9"]))
        self.assertEqual(ps._engine_item_content_ids(gql, "acme/widgets", "engine"), ["I_1", "I_2", "PR_9"])

    def test_no_engine_work_is_empty_not_an_error(self):
        gql = ps.BoardGraphQL("tok", transport=self._disc_transport([], []))
        self.assertEqual(ps._engine_item_content_ids(gql, "acme/widgets", "engine"), [])

    def test_unreadable_repo_propagates_degrade(self):
        gql = ps.BoardGraphQL("tok", transport=lambda m, p, b: (200, {"errors": [{"message": "nope"}]}))
        with self.assertRaises(ps.DegradedReadError):
            ps._engine_item_content_ids(gql, "acme/widgets", "engine")

    def test_sync_discovers_and_adds_when_items_not_injected(self):
        # The full default path (auto-add off): discover via the live query (_RecordingGQL serves
        # ISSUE_1/ISSUE_2), add idempotently, write the engine fields. This is what populates the board.
        rec, gql = _gql()
        old = ps.boot.repo_slug
        ps.boot.repo_slug = lambda: "acme/widgets"
        try:
            result = ps.sync(force=True, config=_config(), signals=_signals(), gql=gql, items=None, now=_NOW)
        finally:
            ps.boot.repo_slug = old
        self.assertEqual(result["status"], ps.SYNCED)
        self.assertEqual(result["items"], 2)
        adds = sum(1 for _p, b in rec.calls if "addProjectV2ItemById" in (b or {}).get("query", ""))
        self.assertEqual(adds, 2)

    def test_sync_degrades_when_discovery_fails(self):
        # items=None -> sync discovers via gql; a board error there must DEGRADE, not falsely SYNC.
        rec, gql = _gql(error_mode=True)
        old = ps.boot.repo_slug
        ps.boot.repo_slug = lambda: "acme/widgets"
        try:
            result = ps.sync(force=True, config=_config(), signals=_signals(), gql=gql, items=None, now=_NOW)
        finally:
            ps.boot.repo_slug = old
        self.assertEqual(result["status"], ps.DEGRADED)


class TestConfigAndDebounce(_Base):
    def test_config_roundtrip(self):
        ps.save_config(_config())
        self.assertEqual(ps.load_config()["project"]["id"], "PVT_test")

    def test_unreadable_or_wrong_version_config_is_none(self):
        os.makedirs(ps._config_dir(), exist_ok=True)
        with open(ps._config_path(), "w", encoding="utf-8") as fh:
            fh.write('{"schema_version": 99, "project": {}}')
        self.assertIsNone(ps.load_config())

    def test_recently_synced_fails_open_without_a_stamp(self):
        self.assertFalse(ps._recently_synced(_NOW.timestamp()))


class TestHookHandler(_Base):
    def _handler(self, canned):
        old = ps.sync
        ps.sync = lambda **kw: canned
        try:
            return ps._session_start_handler({"session_id": "s1"})
        finally:
            ps.sync = old

    def test_degraded_injects_the_fix(self):
        out = self._handler({"status": ps.DEGRADED, "message": "do this one thing"})
        self.assertEqual(out.get("action"), "inject")
        self.assertIn("do this one thing", out.get("context", ""))

    def test_never_configured_surfaces_the_setup_next_step(self):
        # A never-configured board no-ops for a reason the operator can act on -> disclose the next step,
        # not stay silent (the board-absent case is owed a plain-language next step).
        out = self._handler({"status": ps.NOT_CONFIGURED, "message": "Run /engine-board-setup to create one"})
        self.assertEqual(out.get("action"), "inject")
        self.assertIn("engine-board-setup", out.get("context", ""))

    def test_clean_and_skipped_stay_silent(self):
        # A clean sync (the board is the surface) and a debounce skip surface nothing.
        for status in (ps.SYNCED, ps.SKIPPED):
            out = self._handler({"status": status, "message": "ignored"})
            self.assertEqual(out.get("action"), "proceed")


class TestDemo(_Base):
    def test_demo_passes_clean(self):
        self.assertEqual(quiet_call.run(ps._demo), 0)


if __name__ == "__main__":
    unittest.main()
