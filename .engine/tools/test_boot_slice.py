#!/usr/bin/env python3
"""Tests for boot_slice — boot's rung-1 gitignored knowledge cache (#37).

The load-bearing test is PARITY: the cached read-shim must reproduce the live knowledge walk EXACTLY (same
neighbours, same order, same path->entity map) over the REAL committed graph, so boot's orientation block is
byte-identical whether read from the cache or the live walk. The rest pins the sibling-of-the-SQLite-index
lifecycle: fingerprint freshness (content, not mtime), the shared degrade rungs (committed -> live -> none),
and the fail-open read that never blocks boot.
"""
from __future__ import annotations
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import boot_slice          # noqa: E402
import quiet_call          # noqa: E402  (capture a CLI walkthrough's stdout so it can't bury the suite summary)
import knowledge_query     # noqa: E402
import knowledge_index     # noqa: E402
import knowledge_gen       # noqa: E402
import attention           # noqa: E402
import boot                # noqa: E402

WALK = list(knowledge_index.WALK_EDGE_KINDS)


def _tmp_slice(self) -> str:
    d = tempfile.mkdtemp()
    self.addCleanup(lambda: __import__("shutil").rmtree(d, ignore_errors=True))
    return os.path.join(d, "boot-slice.json")


class TestParityWithTheLiveWalk(unittest.TestCase):
    """The cache is a FAITHFUL reprojection of the index walk — proven over EVERY entity in the real committed
    graph, as an ORDERED sequence (the render samples the first N, so order is load-bearing, not just the set)."""

    @classmethod
    def setUpClass(cls):
        cls.sp = os.path.join(tempfile.mkdtemp(), "boot-slice.json")
        boot_slice.build(slice_path=cls.sp)              # from the real committed graph
        cls.shim = boot_slice.read(slice_path=cls.sp)
        cls.ids = [e["id"] for e in knowledge_query.find()]

    def test_a_slice_was_built_and_read(self):
        self.assertIsInstance(self.shim, boot_slice.Slice)
        self.assertTrue(self.ids, "the real graph should have entities to project")

    def test_neighbors_match_the_live_walk_for_every_entity_in_order(self):
        mismatches = []
        for eid in self.ids:
            cached = self.shim.neighbors(eid, edge_filter=WALK, direction="both")
            live = knowledge_query.neighbors(eid, edge_filter=WALK, depth=1, direction="both")
            if cached != live:                           # ORDERED equality, not a set compare
                mismatches.append(eid)
        self.assertEqual(mismatches, [], f"{len(mismatches)} entities diverge from the live walk")

    def test_find_reproduces_the_path_to_entity_map(self):
        cached = {e["source_path"]: e["id"] for e in self.shim.find()}
        live = {e["source_path"]: e["id"] for e in knowledge_query.find() if e.get("source_path")}
        self.assertEqual(cached, live)

    def test_find_is_ordered_by_id(self):
        ids = [e["id"] for e in self.shim.find()]
        self.assertEqual(ids, sorted(ids))


class TestRenderIsByteIdentical(unittest.TestCase):
    """End-to-end: the rendered orientation block is byte-identical whether neighborhood_of read the slice or
    the live walk — including a hub focus whose neighbourhood truncates (the case the honest-count render
    exists for). This is the guarantee behind "identical to today; only the source changes"."""

    @classmethod
    def setUpClass(cls):
        cls.sp = os.path.join(tempfile.mkdtemp(), "boot-slice.json")
        boot_slice.build(slice_path=cls.sp)
        cls.shim = boot_slice.read(slice_path=cls.sp)

    def _render(self, focus, source):
        nb = attention.neighborhood_of(focus, source=source)
        if nb is None:
            return None
        nb["focus_total"] = len(focus)
        return boot.render_neighborhood(nb)

    def test_render_matches_for_a_hub_a_policy_and_a_set(self):
        for focus in (["module:core"], ["policy:attention"], ["tool:attention", "policy:attention"]):
            live = self._render(focus, None)
            cached = self._render(focus, self.shim)
            self.assertIsNotNone(live, f"{focus} should have a neighbourhood")
            self.assertEqual(cached, live, f"slice render diverged from live for {focus}")

    def test_a_hub_render_discloses_its_true_count(self):
        # the case the cache must not silently mis-sample: a hub shows "provides N (showing 4 ...)"
        block = "\n".join(self._render(["module:core"], self.shim))
        self.assertIn("provides", block)
        self.assertIn("(showing ", block)


class TestFreshness(unittest.TestCase):
    """is_fresh keys on (existence, schema_version, content fingerprint) — the same three legs as the index."""

    def test_a_freshly_built_slice_is_fresh_and_ensure_skips_rebuild(self):
        sp = _tmp_slice(self)
        boot_slice.build(slice_path=sp)
        self.assertTrue(boot_slice.is_fresh(slice_path=sp))
        path, source = boot_slice.ensure(slice_path=sp)               # already fresh -> no rebuild
        self.assertEqual((path, source), (sp, None))

    def test_a_missing_slice_is_not_fresh(self):
        self.assertFalse(boot_slice.is_fresh(slice_path=_tmp_slice(self)))

    def test_a_schema_version_bump_forces_rebuild(self):
        sp = _tmp_slice(self)
        boot_slice.build(slice_path=sp)
        with open(sp, encoding="utf-8") as fh:
            data = json.load(fh)
        data["schema_version"] = "0"                                 # an old-shape slice
        with open(sp, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        self.assertFalse(boot_slice.is_fresh(slice_path=sp))

    def test_a_graph_content_change_forces_rebuild_a_mere_touch_does_not(self):
        # the operator-facing point (a `touch` leaves content unchanged -> still fresh; only a byte change
        # moves the sha256 fingerprint). Build against an isolated COPY of the real graph, then mutate it.
        gdir = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(gdir, ignore_errors=True))
        gp = os.path.join(gdir, "graph.json")
        with open(knowledge_gen.GRAPH_PATH, encoding="utf-8") as fh:
            original = fh.read()
        with open(gp, "w", encoding="utf-8") as fh:
            fh.write(original)
        sp = _tmp_slice(self)
        boot_slice.build(slice_path=sp, graph_path=gp)
        self.assertTrue(boot_slice.is_fresh(slice_path=sp, graph_path=gp))
        os.utime(gp, None)                                           # a bare touch -> content unchanged
        self.assertTrue(boot_slice.is_fresh(slice_path=sp, graph_path=gp), "touch must NOT invalidate")
        with open(gp, "w", encoding="utf-8") as fh:                  # a real content change
            fh.write(original.replace("}", "} ", 1))
        self.assertFalse(boot_slice.is_fresh(slice_path=sp, graph_path=gp), "a content change MUST invalidate")

    def test_a_corrupt_slice_is_not_fresh_and_rebuilds(self):
        sp = _tmp_slice(self)
        with open(sp, "w", encoding="utf-8") as fh:
            fh.write("{ this is not valid json")
        self.assertFalse(boot_slice.is_fresh(slice_path=sp))
        shim = boot_slice.read(slice_path=sp)                        # ensure rebuilds from the real graph
        self.assertIsInstance(shim, boot_slice.Slice)
        self.assertTrue(boot_slice.is_fresh(slice_path=sp))


class TestDegradeChain(unittest.TestCase):
    """Rungs 2-4 are knowledge_index's, reused: committed -> live walk -> unavailable, never blocking boot."""

    def test_absent_committed_graph_builds_from_a_live_walk_and_is_never_fresh(self):
        # point at a non-existent graph -> _load_graph falls to canonical_graph() (a live walk of real surfaces)
        gp = os.path.join(tempfile.mkdtemp(), "absent-graph.json")
        sp = _tmp_slice(self)
        path, source = boot_slice.build(slice_path=sp, graph_path=gp)
        self.assertEqual(source, "live")
        with open(sp, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["graph_fingerprint"], knowledge_index.LIVE_WALK_FINGERPRINT)
        self.assertFalse(boot_slice.is_fresh(slice_path=sp, graph_path=gp))   # a live slice self-heals: re-walks

    def test_a_live_built_slice_still_answers_a_neighbourhood(self):
        # The live rung (committed graph absent -> a walk of the surfaces) must still yield a usable read.
        # We do NOT compare to the committed index here: a live walk reads the on-disk SURFACES, which can
        # legitimately differ from a stale committed graph.json (e.g. mid-PR before regen) — the strict
        # ordered parity (same source) is proven in TestParityWithTheLiveWalk. Here we only prove it answers.
        gp = os.path.join(tempfile.mkdtemp(), "absent-graph.json")
        sp = _tmp_slice(self)
        _path, source = boot_slice.build(slice_path=sp, graph_path=gp)
        self.assertEqual(source, "live")
        shim = boot_slice.read(slice_path=sp, graph_path=gp)
        rows = shim.neighbors("module:core", edge_filter=WALK, direction="both")
        self.assertTrue(rows, "the live-built slice should still surface module:core's neighbours")
        self.assertTrue(all({"id", "predicate", "direction"} <= set(r) for r in rows))   # well-formed
        self.assertEqual(rows, sorted(rows, key=lambda r: (r["id"], r["predicate"], r["direction"])))  # ordered

    def test_read_carries_from_live_provenance(self):
        # boot renders a distinct heads-up when the committed graph is absent and orientation runs on a live
        # rebuild — read() carries that as `from_live`. True for a live-built slice (committed graph absent),
        # False for a committed one. This is the signal boot reads to surface "running on a rebuilt map".
        gp_absent = os.path.join(tempfile.mkdtemp(), "absent-graph.json")
        live = boot_slice.read(slice_path=_tmp_slice(self), graph_path=gp_absent)
        self.assertTrue(live.from_live)                  # committed graph absent -> live rebuild -> flagged
        committed = boot_slice.read(slice_path=_tmp_slice(self))   # default graph_path = the real committed map
        self.assertFalse(committed.from_live)            # committed map present -> not a rebuild
        self.assertFalse(committed.from_corrupt)

    def test_read_carries_from_corrupt_provenance(self):
        # A committed map that is PRESENT but unreadable (damaged) rebuilds live too, but boot names it
        # differently from the absent case — read() carries `from_corrupt` (not `from_live`) so boot renders
        # "present but damaged" rather than "missing" (the repair differs; eADR-0004 'name what is reduced').
        gp = os.path.join(tempfile.mkdtemp(), "corrupt-graph.json")
        with open(gp, "w", encoding="utf-8") as fh:
            fh.write('{"schema_version": 1, "entities": [\n<<<<<<< truncated\n')
        shim = boot_slice.read(slice_path=_tmp_slice(self), graph_path=gp)
        self.assertTrue(shim.from_corrupt)               # present-but-damaged -> flagged as corrupt
        self.assertFalse(shim.from_live)                 # NOT the absent-map signal
        self.assertTrue(shim.neighbors("module:core", edge_filter=WALK, direction="both"))  # still answers

    def test_read_fails_open_to_none_when_the_loader_raises(self):
        sp = _tmp_slice(self)
        with mock.patch.object(knowledge_index, "_load_graph",
                               side_effect=knowledge_index.KnowledgeUnavailable("both rungs gave out")):
            self.assertIsNone(boot_slice.read(slice_path=sp))        # never raises into boot
        with mock.patch.object(knowledge_index, "_load_graph", side_effect=Exception("disk on fire")):
            self.assertIsNone(boot_slice.read(slice_path=sp))


class TestProjectionShape(unittest.TestCase):
    """The builder reproduces the CTE's `nid<>?` self-exclusion and DISTINCT, and stores both edge directions."""

    def test_self_edges_are_skipped_and_entries_deduped(self):
        graph = {"entities": [
            {"id": "module:core", "source": {"path": "m.json"},
             "predicates": {"depends_on": ["module:core", "module:core", "module:other"]}},
            {"id": "module:other", "source": {"path": "o.json"}, "predicates": {}}]}
        proj = boot_slice._project(graph)
        # the self-edge module:core->module:core is dropped; the doubled ->other is deduped to one
        self.assertEqual(proj["adjacency"]["module:core"],
                         [{"id": "module:other", "predicate": "depends_on", "direction": "out"}])
        # the reverse edge lands on the target as direction="in"
        self.assertEqual(proj["adjacency"]["module:other"],
                         [{"id": "module:core", "predicate": "depends_on", "direction": "in"}])
        self.assertEqual(proj["by_path"], {"m.json": "module:core", "o.json": "module:other"})

    def test_only_structural_walk_edges_are_cached(self):
        graph = {"entities": [
            {"id": "policy:a", "source": {"path": "a.md"},
             "predicates": {"supersedes": ["policy:b"], "governed_by": ["policy:c"]}},
            {"id": "policy:b", "source": {"path": "b.md"}, "predicates": {}},
            {"id": "policy:c", "source": {"path": "c.md"}, "predicates": {}}]}
        proj = boot_slice._project(graph)
        preds = {n["predicate"] for n in proj["adjacency"]["policy:a"]}
        self.assertEqual(preds, {"governed_by"})         # supersedes (a PULL edge) is never cached on the walk


class TestShimContract(unittest.TestCase):
    """The read-shim mirrors knowledge_query.neighbors's validation, and rejects what the cache can't answer."""

    def setUp(self):
        self.shim = boot_slice.Slice({"by_path": {}, "adjacency":
                                      {"x:1": [{"id": "x:2", "predicate": "depends_on", "direction": "out"}]}})

    def test_depth_beyond_one_is_rejected(self):
        with self.assertRaises(ValueError):
            self.shim.neighbors("x:1", edge_filter=WALK, depth=2, direction="both")

    def test_bad_direction_is_rejected(self):
        with self.assertRaises(ValueError):
            self.shim.neighbors("x:1", direction="sideways")

    def test_a_non_structural_edge_filter_is_rejected(self):
        with self.assertRaises(ValueError):
            self.shim.neighbors("x:1", edge_filter=["supersedes"], direction="both")

    def test_direction_filter_is_honoured(self):
        self.assertEqual(self.shim.neighbors("x:1", direction="out"),
                         [{"id": "x:2", "predicate": "depends_on", "direction": "out"}])
        self.assertEqual(self.shim.neighbors("x:1", direction="in"), [])     # the only edge is an "out"


class TestCli(unittest.TestCase):
    def test_status_and_unknown_command(self):
        self.assertEqual(quiet_call.run(boot_slice.main, ["status"]), 0)
        self.assertEqual(boot_slice.main(["bogus"]), 2)


if __name__ == "__main__":
    unittest.main()
