#!/usr/bin/env python3
"""Self-tests for the knowledge-retrieval op-set (knowledge_query.py), the SQLite index
(knowledge_index.py), and the graph-query MCP server (knowledge_mcp_server.py).

Run: uv run --directory .engine --frozen -- python -m unittest discover -s tools -p 'test_*.py' -b

These lock the load-bearing teeth over a controlled FIXTURE graph (so assertions are exact and
independent of the evolving real graph): get-entity returns the entity + edges (or None); find selects
by type/glob/owner; neighbors traverses out / in (the REVERSE edges the committed graph cannot give) /
both, honours an edge filter and multi-hop depth; relate finds the shortest undirected path (or null).
Then the four-rung degrade cascade: a missing index rebuilds from the committed
graph; a stale index rebuilds; an ABSENT committed graph rebuilds from a live walk of the surfaces and
still answers; only if that live walk also fails is KnowledgeUnavailable raised (never a crash). Finally
the MCP server, headless (no Claude Desktop): tools/list is exactly the four declared ops, and tools/call
delegates to the op-set.
"""
from __future__ import annotations
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import knowledge_index as ki      # noqa: E402
import knowledge_query as kq      # noqa: E402
import knowledge_gen as kg        # noqa: E402

D116_OPS = {"health", "get-entity", "find", "neighbors", "relate"}


def _entity(eid, etype, owner, src, preds):
    return {"id": eid, "type": etype, "name": src, "slug": eid.split(":", 1)[1],
            "source": {"path": src, "fingerprint": "sha256:" + "0" * 64},
            "owner": owner, "predicates": preds}


def _fixture_graph() -> dict:
    """A small controlled graph: checks governed by schemas + targeting an interface, all provided by core,
    a 2-hop chain (check:c1 -> interface:x -> schema:s2), and an isolated doc:orphan."""
    return {"schema_version": 1, "entities": [
        _entity("module:core", "module", "core", ".engine/modules/core/manifest.json", {}),
        _entity("schema:s1", "schema", "core", ".engine/schemas/s1.json",
                {"provided_by": ["module:core"]}),
        _entity("schema:s2", "schema", "core", ".engine/schemas/s2.json",
                {"provided_by": ["module:core"]}),
        _entity("interface:x", "interface", "core", ".engine/interfaces/x.json",
                {"provided_by": ["module:core"], "governed_by": ["schema:s2"]}),
        _entity("check:c1", "check", "core", ".engine/check/c1.json",
                {"provided_by": ["module:core"], "governed_by": ["schema:s1"], "targets": ["interface:x"]}),
        _entity("check:c2", "check", "core", ".engine/check/c2.json",
                {"provided_by": ["module:core"], "governed_by": ["schema:s1"]}),
        _entity("doc:orphan", "doc", "core", ".engine/docs/orphan.md", {}),
    ]}


class TestQueryOps(unittest.TestCase):
    """The pure op logic over a fixture index built into a temp dir."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.graph_path = os.path.join(self._tmp.name, "graph.json")
        self.index_path = os.path.join(self._tmp.name, "index.sqlite")
        with open(self.graph_path, "w", encoding="utf-8") as fh:
            json.dump(_fixture_graph(), fh)
        ki.build_index(self.index_path, self.graph_path)
        self.conn = __import__("sqlite3").connect(self.index_path)
        self.conn.row_factory = __import__("sqlite3").Row

    def tearDown(self):
        self.conn.close()
        self._tmp.cleanup()

    def _ids(self, rows):
        return sorted(r["id"] for r in rows)

    def test_get_entity_returns_entity_with_edges(self):
        e = kq._get_entity(self.conn, "check:c1")
        self.assertEqual(e["id"], "check:c1")
        self.assertEqual(e["predicates"]["governed_by"], ["schema:s1"])
        self.assertEqual(e["predicates"]["targets"], ["interface:x"])
        self.assertEqual(e["predicates"]["provided_by"], ["module:core"])

    def test_get_entity_unknown_is_none(self):
        self.assertIsNone(kq._get_entity(self.conn, "check:does-not-exist"))

    def test_find_by_type(self):
        self.assertEqual(self._ids(kq._find(self.conn, type="check")), ["check:c1", "check:c2"])

    def test_find_by_glob(self):
        self.assertEqual(self._ids(kq._find(self.conn, path_glob=".engine/check/*")),
                         ["check:c1", "check:c2"])

    def test_find_empty_selector_matches_all(self):
        self.assertEqual(len(kq._find(self.conn)), 7)

    def test_neighbors_out(self):
        got = {n["id"] for n in kq._neighbors(self.conn, "check:c1", direction="out")}
        self.assertEqual(got, {"schema:s1", "interface:x", "module:core"})

    def test_neighbors_in_is_reverse_traversal(self):
        # who is governed_by schema:s1 — the checks point AT it (the reverse edge the index exists for)
        got = {n["id"] for n in kq._neighbors(self.conn, "schema:s1", direction="in")}
        self.assertEqual(got, {"check:c1", "check:c2"})
        for n in kq._neighbors(self.conn, "schema:s1", direction="in"):
            self.assertEqual(n["direction"], "in")
            self.assertEqual(n["predicate"], "governed_by")

    def test_neighbors_both_unions_forward_and_reverse(self):
        # The cold-start orientation walk: `direction="both"` is the union of out and in, deduped.
        # schema:s1 is forward-poor (out -> only its module) but reverse-rich (the checks it governs point AT
        # it) — exactly the connective tissue a forward-only walk starves. both() must surface both halves.
        out = {n["id"] for n in kq._neighbors(self.conn, "schema:s1", direction="out")}
        inn = {n["id"] for n in kq._neighbors(self.conn, "schema:s1", direction="in")}
        both = {n["id"] for n in kq._neighbors(self.conn, "schema:s1", direction="both")}
        self.assertEqual(out, {"module:core"})                       # forward-only collapses to the module
        self.assertEqual(both, out | inn)                            # both is the deduped union
        self.assertEqual(both, {"module:core", "check:c1", "check:c2"})

    def test_neighbors_edge_filter(self):
        got = {n["id"] for n in kq._neighbors(self.conn, "check:c1", edge_filter=["governed_by"],
                                              direction="out")}
        self.assertEqual(got, {"schema:s1"})

    def test_neighbors_depth_is_transitive(self):
        d1 = {n["id"] for n in kq._neighbors(self.conn, "check:c1", direction="out", depth=1)}
        d2 = {n["id"] for n in kq._neighbors(self.conn, "check:c1", direction="out", depth=2)}
        self.assertNotIn("schema:s2", d1)
        self.assertIn("schema:s2", d2)          # reached via check:c1 -> interface:x -> schema:s2

    def test_neighbors_rejects_bad_args(self):
        with self.assertRaises(ValueError):
            kq._neighbors(self.conn, "check:c1", direction="sideways")
        with self.assertRaises(ValueError):
            kq._neighbors(self.conn, "check:c1", depth=0)
        with self.assertRaises(ValueError):
            kq._neighbors(self.conn, "check:c1", edge_filter=["not_a_real_edge"])

    def test_relate_direct(self):
        self.assertEqual(kq._relate(self.conn, "check:c1", "schema:s1"), ["check:c1", "schema:s1"])

    def test_relate_multi_hop(self):
        path = kq._relate(self.conn, "check:c1", "check:c2")
        self.assertIsNotNone(path)
        self.assertEqual(path[0], "check:c1")
        self.assertEqual(path[-1], "check:c2")
        self.assertEqual(len(path), 3)          # c1 - (schema:s1 | module:core) - c2

    def test_relate_unconnected_is_none(self):
        self.assertIsNone(kq._relate(self.conn, "check:c1", "doc:orphan"))

    def test_relate_same_node(self):
        self.assertEqual(kq._relate(self.conn, "check:c1", "check:c1"), ["check:c1"])


class TestRelateBfs(unittest.TestCase):
    """relate is a genuine node-visited BFS. The prior path-materializing recursive CTE enumerated
    every simple path and hung combinatorially through a high-degree hub; a BFS with a per-node visited set
    returns a shortest path in O(V+E). This fixture — a hub cross-linked to many leaves that also link both
    endpoints — has factorially-many simple paths (which would swamp the old query) but a trivial BFS."""

    def _hub_graph(self, leaves=60):
        # a and b each connect to the hub AND to every leaf; every leaf connects to the hub: many alternate
        # routes, shortest a-hub-b / a-leaf_i-b are all length 3. relate must return length 3, fast.
        ents = [_entity("module:core", "module", "core", ".engine/modules/core/manifest.json", {}),
                _entity("check:a", "check", "core", ".engine/check/a.json",
                        {"provided_by": ["module:core"]}),
                _entity("check:b", "check", "core", ".engine/check/b.json",
                        {"provided_by": ["module:core"]})]
        for i in range(leaves):
            # each leaf targets a, b and the hub — undirected, these become the cross-links that explode
            # the old all-simple-paths walk.
            ents.append(_entity(f"schema:l{i}", "schema", "core", f".engine/schemas/l{i}.json",
                                {"provided_by": ["module:core"], "targets": ["check:a", "check:b"]}))
        return {"schema_version": 1, "entities": ents}

    def test_relate_through_high_degree_hub_returns_a_shortest_path(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        gpath = os.path.join(tmp.name, "graph.json"); ipath = os.path.join(tmp.name, "index.sqlite")
        with open(gpath, "w", encoding="utf-8") as fh:
            json.dump(self._hub_graph(), fh)
        ki.build_index(ipath, gpath)
        conn = __import__("sqlite3").connect(ipath); conn.row_factory = __import__("sqlite3").Row
        self.addCleanup(conn.close)
        path = kq._relate(conn, "check:a", "check:b")
        self.assertIsNotNone(path)
        self.assertEqual(path[0], "check:a")
        self.assertEqual(path[-1], "check:b")
        self.assertEqual(len(path), 3)                    # a - (a leaf or the hub) - b, the shortest
        # deterministic (sorted neighbours): the same query yields the same path across runs
        self.assertEqual(kq._relate(conn, "check:a", "check:b"), path)


class TestDegradeToGitNative(unittest.TestCase):
    """The four-rung degrade cascade: a fresh index answers; a missing/stale
    index rebuilds from the committed graph (rung 2); an ABSENT committed graph rebuilds from a LIVE WALK
    of the surfaces (rung 3); only if that live walk also fails is knowledge unavailable (rung 4)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.graph_path = os.path.join(self._tmp.name, "graph.json")
        self.index_path = os.path.join(self._tmp.name, "index.sqlite")

    def tearDown(self):
        self._tmp.cleanup()

    def _write_graph(self, graph):
        with open(self.graph_path, "w", encoding="utf-8") as fh:
            json.dump(graph, fh)

    def test_missing_index_is_rebuilt_from_committed_graph(self):
        self._write_graph(_fixture_graph())
        self.assertFalse(os.path.exists(self.index_path))
        path, source = ki.ensure_index(self.index_path, self.graph_path)
        self.assertEqual(source, "committed")           # rung 2
        self.assertTrue(os.path.isfile(path))
        # the answer is correct off the rebuilt index, and a second ensure is a no-op
        e = kq.get_entity("check:c1", index_path=self.index_path, graph_path=self.graph_path)
        self.assertEqual(e["id"], "check:c1")
        _p, source2 = ki.ensure_index(self.index_path, self.graph_path)
        self.assertIsNone(source2)                       # already fresh -> no rebuild

    def test_stale_index_is_rebuilt(self):
        self._write_graph(_fixture_graph())
        ki.build_index(self.index_path, self.graph_path)
        self.assertTrue(ki.is_fresh(self.index_path, self.graph_path))
        # change the committed graph (drop an entity) -> the index is now stale -> rebuilt
        smaller = _fixture_graph()
        smaller["entities"] = [e for e in smaller["entities"] if e["id"] != "doc:orphan"]
        self._write_graph(smaller)
        self.assertFalse(ki.is_fresh(self.index_path, self.graph_path))
        _p, source = ki.ensure_index(self.index_path, self.graph_path)
        self.assertEqual(source, "committed")            # rebuilt from the (changed) committed graph

    def test_missing_committed_graph_falls_back_to_live_walk(self):
        # rung 3: no committed graph at the temp path, but the real surfaces ARE present -> the index is
        # rebuilt from a LIVE WALK (knowledge_gen.canonical_graph()) and still answers (loudly degraded).
        self.assertFalse(os.path.exists(self.graph_path))
        path, source = ki.ensure_index(self.index_path, self.graph_path)
        self.assertEqual(source, "live")
        self.assertTrue(os.path.isfile(path))
        # module:core is always derived from the core manifest, so a real live walk must surface it
        e = kq.get_entity("module:core", index_path=self.index_path, graph_path=self.graph_path)
        self.assertIsNotNone(e)
        self.assertEqual(e["id"], "module:core")
        # while the committed graph stays absent, every ensure re-walks (never wrongly deemed fresh)
        _p, source2 = ki.ensure_index(self.index_path, self.graph_path)
        self.assertEqual(source2, "live")

    def test_live_walk_failure_reports_unavailable(self):
        # rung 4: committed graph absent AND the live walk also fails -> KnowledgeUnavailable (reported,
        # not crashed). Fake only the boundary (the live walk); the real cascade logic runs.
        def _boom():
            raise RuntimeError("simulated live-walk failure")
        with mock.patch.object(kg, "canonical_graph", _boom):
            with self.assertRaises(ki.KnowledgeUnavailable) as cm:
                ki.ensure_index(self.index_path, self.graph_path)
        # Pin that it failed AT the live-walk rung (3->4), not earlier: the message names the live walk
        # and the chained cause is the simulated failure. This makes the test revert-proof on its own —
        # the old 3-rung code raised on absence with neither signal, so these asserts would fail on it.
        self.assertIn("live walk", str(cm.exception))
        self.assertIsInstance(cm.exception.__cause__, RuntimeError)
        self.assertIn("simulated live-walk failure", str(cm.exception.__cause__))

    def test_corrupt_committed_graph_degrades_like_absence(self):
        # a PRESENT but unreadable committed graph (merge markers / a truncated regen) must degrade to
        # the LIVE WALK exactly as absence does — never a raw JSONDecodeError crash — but tagged distinctly
        # ('live-corrupt') so the operator signal names a DAMAGED file, not a missing one.
        with open(self.graph_path, "w", encoding="utf-8") as fh:
            fh.write('{"schema_version": 1, "entities": [\n<<<<<<< HEAD truncated merge marker\n')
        path, source = ki.ensure_index(self.index_path, self.graph_path)
        self.assertEqual(source, "live-corrupt")
        self.assertTrue(os.path.isfile(path))
        # still answers off the live walk (module:core is always derived from the core manifest)
        e = kq.get_entity("module:core", index_path=self.index_path, graph_path=self.graph_path)
        self.assertEqual(e["id"], "module:core")
        # while the committed graph stays corrupt, every ensure re-walks (never wrongly deemed fresh)
        _p, source2 = ki.ensure_index(self.index_path, self.graph_path)
        self.assertEqual(source2, "live-corrupt")

    def test_corrupt_and_live_walk_failure_reports_unavailable(self):
        # rung 4 for the corrupt case: committed graph present-but-unreadable AND the live walk also fails
        # -> KnowledgeUnavailable naming 'present but unreadable' (reported, not crashed).
        with open(self.graph_path, "w", encoding="utf-8") as fh:
            fh.write("not json at all")
        def _boom():
            raise RuntimeError("simulated live-walk failure")
        with mock.patch.object(kg, "canonical_graph", _boom):
            with self.assertRaises(ki.KnowledgeUnavailable) as cm:
                ki.ensure_index(self.index_path, self.graph_path)
        self.assertIn("present but unreadable", str(cm.exception))
        self.assertIsInstance(cm.exception.__cause__, RuntimeError)


class TestDegradeSurfacing(unittest.TestCase):
    """The degrade `source` is carried through _with_conn so the MCP/library boundary can surface it,
    while the four public ops stay DATA-ONLY (attention / the boot slice consume plain results in
    comprehensions — a tuple return would break them)."""

    def _paths(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        return (os.path.join(tmp.name, "graph.json"), os.path.join(tmp.name, "index.sqlite"))

    def test_degrade_message_distinguishes_absent_from_corrupt(self):
        self.assertIsNone(kq.degrade_message(None))
        self.assertIsNone(kq.degrade_message("committed"))
        absent, corrupt = kq.degrade_message("live"), kq.degrade_message("live-corrupt")
        self.assertIn("missing", absent)                 # absent -> "missing", restore
        self.assertIn("damaged", corrupt)                # corrupt -> "damaged", replace
        self.assertIn("replace the damaged file", corrupt)
        self.assertIn(kg.REGEN_CMD, absent)
        self.assertIn(kg.REGEN_CMD, corrupt)
        # plain "project map" register (boot's), never engine shorthand — one fault, one voice
        for msg in (absent, corrupt):
            self.assertIn("project map", msg)
            self.assertNotIn("LIVE WALK", msg)
            self.assertNotIn("knowledge graph", msg)

    def test_public_ops_return_data_only(self):
        gpath, ipath = self._paths()
        with open(gpath, "w", encoding="utf-8") as fh:
            json.dump(_fixture_graph(), fh)
        self.assertIsInstance(kq.get_entity("check:c1", index_path=ipath, graph_path=gpath), dict)
        self.assertIsInstance(kq.find(index_path=ipath, graph_path=gpath), list)
        self.assertIsInstance(kq.neighbors("check:c1", index_path=ipath, graph_path=gpath), list)
        self.assertIsInstance(kq.relate("check:c1", "schema:s1", index_path=ipath, graph_path=gpath), list)

    def test_with_degrade_carries_a_note_on_a_live_walk(self):
        # absent committed graph at the temp path -> live walk -> with_degrade returns a non-None note.
        gpath, ipath = self._paths()
        result, note = kq.with_degrade(lambda c: kq._get_entity(c, "module:core"),
                                       index_path=ipath, graph_path=gpath)
        self.assertEqual(result["id"], "module:core")
        self.assertIsNotNone(note)
        self.assertIn("rebuilt", note)                   # the absent-map degrade note, plain-language
        self.assertIn("missing", note)

    def test_with_degrade_is_silent_on_a_committed_read(self):
        gpath, ipath = self._paths()
        with open(gpath, "w", encoding="utf-8") as fh:
            json.dump(_fixture_graph(), fh)
        _r, note = kq.with_degrade(lambda c: kq._get_entity(c, "check:c1"),
                                   index_path=ipath, graph_path=gpath)
        self.assertIsNone(note)


class TestMcpServer(unittest.IsolatedAsyncioTestCase):
    """The graph-query MCP server, headless (in-process) — no Claude Desktop, no subprocess. The
    server's tools delegate to the op-set over the LIVE committed graph."""

    @staticmethod
    def _tool_result_json(res):
        content = res[0] if isinstance(res, tuple) else res
        return json.loads(content[0].text)

    async def test_tools_list_is_exactly_the_op_set(self):
        import knowledge_mcp_server as srv
        names = {t.name for t in await srv.server.list_tools()}
        self.assertEqual(names, D116_OPS)

    async def test_health_is_content_free_and_fixed_identity(self):
        import knowledge_mcp_server as srv
        with mock.patch.object(kq, "with_degrade", side_effect=AssertionError("health read graph")):
            data = self._tool_result_json(await srv.server.call_tool("health", {}))
        self.assertEqual(data, {"status": "ok", "server": "engine-knowledge-graph"})

    async def test_call_tool_get_entity_delegates(self):
        import knowledge_mcp_server as srv
        data = self._tool_result_json(await srv.server.call_tool("get-entity", {"id": "module:core"}))
        self.assertEqual(data["entity"]["id"], "module:core")

    async def test_call_tool_neighbors_matches_the_library(self):
        import knowledge_mcp_server as srv
        data = self._tool_result_json(
            await srv.server.call_tool("neighbors", {"id": "schema:check.v1", "direction": "in"}))
        expected = kq.neighbors("schema:check.v1", direction="in")
        self.assertEqual({n["id"] for n in data["neighbors"]}, {n["id"] for n in expected})
        self.assertTrue(len(data["neighbors"]) >= 1)

    async def test_tool_surfaces_a_degraded_key_when_the_read_is_degraded(self):
        # when with_degrade yields a note (a live-walk read), the tool response carries it under a
        # `degraded` key so the in-session caller relays it. Force the note at the boundary; the real _merge
        # wiring in the tool attaches it.
        import knowledge_mcp_server as srv
        real = kq.with_degrade
        def fake(fn, **kw):
            result, _ = real(fn, **kw)
            return result, "KNOWLEDGE DEGRADED: test note"
        with mock.patch.object(kq, "with_degrade", fake):
            data = self._tool_result_json(await srv.server.call_tool("get-entity", {"id": "module:core"}))
        self.assertEqual(data["entity"]["id"], "module:core")
        self.assertEqual(data["degraded"], "KNOWLEDGE DEGRADED: test note")

    async def test_tool_has_no_degraded_key_on_a_fresh_committed_read(self):
        # The real committed graph is present in this checkout, so a normal read carries no degrade note.
        import knowledge_mcp_server as srv
        data = self._tool_result_json(await srv.server.call_tool("get-entity", {"id": "module:core"}))
        self.assertNotIn("degraded", data)


class TestEnrichedEntities(unittest.TestCase):
    """Pull-path enrichment: the declared attributes ride through get-entity/find via the JSON
    attributes column; supersedes is a deliberate PULL (neighbors edge_filter) but stays OFF the
    cold-start default walk; build_index allowlists edge kinds."""

    def _build(self, graph):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        gpath = os.path.join(tmp.name, "graph.json")
        ipath = os.path.join(tmp.name, "index.sqlite")
        with open(gpath, "w", encoding="utf-8") as fh:
            json.dump(graph, fh)
        ki.build_index(ipath, gpath)
        conn = __import__("sqlite3").connect(ipath)
        conn.row_factory = __import__("sqlite3").Row
        self.addCleanup(conn.close)
        return conn

    def _enriched_graph(self):
        c = _entity("check:c1", "check", "core", ".engine/check/c1.json", {"provided_by": ["module:core"]})
        c.update({"status": "active", "tier": "hard", "kind": "shape", "suites": ["CI"]})
        p = _entity("policy:p1", "policy", "core", ".engine/policies/p1.md", {"provided_by": ["module:core"]})
        p["title"] = "Attention"
        a = _entity("contract:eADR-0002", "contract", "core", "x/a.md", {"supersedes": ["contract:eADR-0001"]})
        b = _entity("contract:eADR-0001", "contract", "core", "x/b.md", {})
        m = _entity("module:core", "module", "core", ".engine/modules/core/manifest.json", {})
        return {"schema_version": 1, "entities": [c, p, a, b, m]}

    def test_get_entity_carries_declared_attributes_and_keeps_edges(self):
        conn = self._build(self._enriched_graph())
        e = kq._get_entity(conn, "check:c1")
        self.assertEqual((e.get("status"), e.get("tier"), e.get("kind"), e.get("suites")),
                         ("active", "hard", "shape", ["CI"]))
        self.assertEqual(e["predicates"]["provided_by"], ["module:core"])      # edges still present
        self.assertEqual(kq._get_entity(conn, "policy:p1").get("title"), "Attention")

    def test_find_carries_attributes_but_selects_core_scalar_only(self):
        conn = self._build(self._enriched_graph())
        rows = {r["id"]: r for r in kq._find(conn, type="check")}
        self.assertEqual(rows["check:c1"].get("tier"), "hard")
        # NO attribute selector -> no find(attribute) canon back-door
        import inspect
        self.assertEqual(set(inspect.signature(kq._find).parameters) - {"conn"},
                         {"type", "path_glob", "owner"})

    def test_supersedes_is_pull_queryable_but_off_the_cold_start_default(self):
        conn = self._build(self._enriched_graph())
        pulled = {n["id"] for n in kq._neighbors(conn, "contract:eADR-0002", edge_filter=["supersedes"])}
        self.assertEqual(pulled, {"contract:eADR-0001"})            # deliberate pull
        default = {n["id"] for n in kq._neighbors(conn, "contract:eADR-0002")}
        self.assertEqual(default, set())                            # cold-start default never follows it

    def test_edge_sets_are_split(self):
        self.assertNotIn("supersedes", kq.WALK_EDGE_KINDS)
        self.assertIn("supersedes", kq.EDGE_KINDS)
        # every walk kind is a valid edge kind; the walk is EDGE_KINDS minus the pull-only supersedes.
        self.assertTrue(set(kq.WALK_EDGE_KINDS) <= set(kq.EDGE_KINDS))
        self.assertEqual(set(kq.EDGE_KINDS) - set(kq.WALK_EDGE_KINDS), {"supersedes"})

    def test_interface_edge_filter_enum_matches_edge_kinds(self):
        # the interface's edge_filter enum is a HAND-MAINTAINED duplicate of EDGE_KINDS. The knowledge
        # vocabulary check guards entity TYPES, not predicate kinds, so this pin is the only thing that catches
        # the interface enum and the code vocabulary drifting apart.
        iface = kg.validate.load_json(os.path.join(kg.validate.ENGINE_DIR, "interfaces",
                                                   "knowledge-retrieval.json"))
        neighbors = next(op for op in iface["operations"] if op["name"] == "neighbors")
        enum = neighbors["input_schema"]["properties"]["edge_filter"]["items"]["enum"]
        self.assertEqual(set(enum), set(kq.EDGE_KINDS))

    def test_build_index_allowlists_edge_kinds(self):
        c = _entity("check:c1", "check", "core", ".engine/check/c1.json",
                    {"provided_by": ["module:core"], "bogus_edge": ["module:core"]})
        conn = self._build({"schema_version": 1, "entities": [
            c, _entity("module:core", "module", "core", "m.json", {})]})
        preds = {row[0] for row in conn.execute("SELECT DISTINCT predicate FROM edges")}
        self.assertNotIn("bogus_edge", preds)
        self.assertIn("provided_by", preds)

    def test_old_shape_index_is_rebuilt(self):
        # an index built before the attributes column / version sentinel must be deemed stale and rebuilt
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        gpath = os.path.join(tmp.name, "graph.json")
        ipath = os.path.join(tmp.name, "index.sqlite")
        with open(gpath, "w", encoding="utf-8") as fh:
            json.dump(self._enriched_graph(), fh)
        ki.build_index(ipath, gpath)
        # simulate an OLD index: wipe the version sentinel
        conn = __import__("sqlite3").connect(ipath)
        conn.execute("DELETE FROM meta WHERE key='index_schema_version'"); conn.commit(); conn.close()
        self.assertFalse(ki.is_fresh(ipath, gpath))                 # version leg forces a rebuild
        ki.ensure_index(ipath, gpath)
        self.assertTrue(ki.is_fresh(ipath, gpath))


if __name__ == "__main__":
    unittest.main()
