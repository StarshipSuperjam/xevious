#!/usr/bin/env python3
"""The knowledge-retrieval op-set, the conforming fallback floor.

Realizes the knowledge-retrieval interface (.engine/interfaces/knowledge-retrieval.json) over the
gitignored SQLite index (knowledge_index.py): the four operations every conforming implementation
must answer —

  get-entity(id)                          one entity + its declared edges, or null
  find(type?, path_glob?, owner?)         the entities matching a selector
  neighbors(id, edge_filter?, direction?, depth?)   adjacency (out / in / both; multi-hop), the
                                          REVERSE traversal the committed graph cannot give cheaply
  relate(id_a, id_b)                      the shortest edge path between two entities, or null

Traversal is pushed into the indexed store (recursive CTEs over the dst-indexed edge table), so it
scales to a real adopter's repo. Degrade-to-git-native: every op ensures the
index first, rebuilding it from the committed graph if it is missing or stale; if the committed graph
is ABSENT it rebuilds from a LIVE WALK of the surfaces (loudly degraded), and only if that live walk
also fails is knowledge reported unavailable in plain language, never a crash. The graph-query MCP server
(knowledge_mcp_server.py) is a thin transport over this library; the CLI here is the operator's
no-Claude-Desktop demo path.
"""
from __future__ import annotations
import collections
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate            # noqa: E402
import knowledge_gen       # noqa: E402
import knowledge_index     # noqa: E402
from knowledge_index import KnowledgeUnavailable  # noqa: E402

EDGE_KINDS = knowledge_index.EDGE_KINDS            # the full valid edge vocabulary (incl. supersedes)
WALK_EDGE_KINDS = knowledge_index.WALK_EDGE_KINDS  # the cold-start default: the walk edges (all but supersedes)


def _decode_attributes(row) -> dict:
    """The entity's declared attributes (status/tier/title/discriminators) from the JSON column,
    or {} when absent. Tolerant of an OLD-shape index row that predates the column (defence-in-depth — the
    INDEX_SCHEMA_VERSION freshness leg already forces such an index to rebuild before it is read)."""
    if "attributes" not in row.keys():
        return {}
    raw = row["attributes"]
    return json.loads(raw) if raw else {}


# ---- pure query logic over an open connection (fixture-testable) ----------------------------

def _entity_row_to_dict(conn, row) -> dict:
    """Rebuild a knowledge.v1-shaped entity dict (core fields + declared attributes + outgoing predicates)
    from an entities row. The declared attributes ride back in from the `attributes` JSON column."""
    preds: dict = {}
    for er in conn.execute(
            "SELECT predicate, dst_id FROM edges WHERE src_id=? ORDER BY predicate, dst_id", (row["id"],)):
        preds.setdefault(er["predicate"], []).append(er["dst_id"])
    out = {
        "id": row["id"], "type": row["type"], "name": row["name"], "slug": row["slug"],
        "source": {"path": row["source_path"], "fingerprint": row["fingerprint"]},
        "owner": row["owner"],
    }
    out.update(_decode_attributes(row))                # status/tier/title/discriminators, when present
    out["predicates"] = preds
    return out


def _get_entity(conn, entity_id: str):
    row = conn.execute(
        "SELECT id,type,name,slug,source_path,fingerprint,owner,attributes FROM entities WHERE id=?",
        (entity_id,)).fetchone()
    return _entity_row_to_dict(conn, row) if row else None


def _find(conn, type=None, path_glob=None, owner=None) -> list:
    # SELECTORS stay core-scalar (type / path_glob / owner) — there is NO attribute selector, so no
    # find(attribute) canon back-door. The declared attributes are RETURNED (merged from the JSON
    # column) for parity with get-entity, but are never a WHERE dimension.
    where, params = [], []
    if type is not None:
        where.append("type=?"); params.append(type)
    if owner is not None:
        where.append("owner=?"); params.append(owner)
    if path_glob is not None:
        where.append("source_path GLOB ?"); params.append(path_glob)
    sql = "SELECT id,type,name,slug,source_path,owner,attributes FROM entities"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id"
    out = []
    for r in conn.execute(sql, params):
        rec = {"id": r["id"], "type": r["type"], "name": r["name"], "slug": r["slug"],
               "source_path": r["source_path"], "owner": r["owner"]}
        rec.update(_decode_attributes(r))
        out.append(rec)
    return out


def _neighbors(conn, entity_id: str, edge_filter=None, direction="out", depth=1) -> list:
    if direction not in ("out", "in", "both"):
        raise ValueError(f"direction must be out/in/both, got {direction!r}")
    if not isinstance(depth, int) or depth < 1:
        raise ValueError(f"depth must be an integer >= 1, got {depth!r}")
    # No edge_filter == the COLD-START default: the walk edges (WALK_EDGE_KINDS — every kind but supersedes),
    # so a focus walk never traverses supersedes. An explicit edge_filter may name ANY valid edge (EDGE_KINDS),
    # so supersedes is a deliberate PULL via neighbors(edge_filter=["supersedes"]).
    preds = tuple(edge_filter) if edge_filter else WALK_EDGE_KINDS
    bad = [p for p in preds if p not in EDGE_KINDS]
    if bad:
        raise ValueError(f"unknown edge kind(s) {bad}; valid: {list(EDGE_KINDS)}")
    ph = ",".join("?" for _ in preds)
    want_out = 1 if direction in ("out", "both") else 0
    want_in = 1 if direction in ("in", "both") else 0
    # dedges: each edge projected as a directed step honouring the direction + edge filter; walk: a
    # single recursive term following dedges from the root, bounded by depth, UNION-dedup as the cycle guard.
    sql = f"""
    WITH RECURSIVE
      dedges(a, b, pred, dir) AS (
        SELECT src_id, dst_id, predicate, 'out' FROM edges WHERE ?=1 AND predicate IN ({ph})
        UNION ALL
        SELECT dst_id, src_id, predicate, 'in'  FROM edges WHERE ?=1 AND predicate IN ({ph})
      ),
      walk(nid, pred, dir, d) AS (
        SELECT b, pred, dir, 1 FROM dedges WHERE a=?
        UNION
        SELECT de.b, de.pred, de.dir, w.d+1 FROM walk w JOIN dedges de ON de.a=w.nid WHERE w.d < ?
      )
    SELECT DISTINCT nid, pred, dir FROM walk WHERE nid<>? ORDER BY nid, pred, dir
    """
    params = [want_out, *preds, want_in, *preds, entity_id, depth, entity_id]
    return [{"id": r["nid"], "predicate": r["pred"], "direction": r["dir"]}
            for r in conn.execute(sql, params)]


def _relate(conn, id_a: str, id_b: str):
    """The shortest undirected edge path id_a..id_b as a list of ids (inclusive), or None.

    A genuine breadth-first search with a per-NODE visited set (the committed-JSON BFS floor): each
    node is expanded at most once, so the walk is O(V+E) and returns promptly even through a high-degree
    hub (module:core, in-degree ~250) — where the previous path-materializing recursive CTE enumerated
    every simple path as its own row and hung combinatorially. relate is the deliberate PULL that
    traverses ALL edge kinds including `supersedes`: the adjacency is built from every edge row,
    unfiltered — only the cold-start `neighbors` walk is pinned to WALK_EDGE_KINDS. Deterministic:
    neighbours are visited in sorted id order, so among equal-length shortest paths the chosen one is
    stable. Returns None when either endpoint is unknown or the two are unconnected; [id] for
    id_a==id_b iff that entity exists."""
    if id_a == id_b:
        return [id_a] if _get_entity(conn, id_a) else None
    if _get_entity(conn, id_a) is None or _get_entity(conn, id_b) is None:
        return None                                    # an unknown endpoint cannot be connected
    # Undirected adjacency, loaded once from EVERY edge row (all kinds — relate PULLs supersedes).
    adjacency: dict = {}
    for src, dst in conn.execute("SELECT src_id, dst_id FROM edges"):
        adjacency.setdefault(src, set()).add(dst)
        adjacency.setdefault(dst, set()).add(src)
    # BFS: the per-node `parent` map doubles as the visited set (a node is enqueued at most once, so the
    # walk is bounded by |V|), and reconstructs the shortest path on first reaching id_b.
    parent = {id_a: None}
    frontier = collections.deque([id_a])
    while frontier:
        node = frontier.popleft()
        if node == id_b:
            path = []
            while node is not None:
                path.append(node)
                node = parent[node]
            return path[::-1]
        for nxt in sorted(adjacency.get(node, ())):
            if nxt not in parent:
                parent[nxt] = node
                frontier.append(nxt)
    return None


# ---- public ops: open a fresh index, query, close (returns data only) -----------------------

def _with_conn(fn, index_path, graph_path):
    """Run fn over a fresh index connection; return (result, source), where source is the degrade rung the
    read came through — None (index was fresh) / 'committed' / 'live' (committed graph absent) /
    'live-corrupt' (committed graph present but unreadable). The public ops below unpack and return DATA
    ONLY (their callers — attention, the boot slice — consume plain results). The degrade `source` is
    carried here so the boundaries that WANT it can surface it: the CLI (`_note_degrade`) and the MCP
    transport (`with_degrade`)."""
    conn, source = knowledge_index.connect(index_path, graph_path)
    try:
        return fn(conn), source
    finally:
        conn.close()


def with_degrade(fn, *, index_path=None, graph_path=None):
    """The degrade-aware boundary the MCP transport uses: returns (result, degrade_note_or_None) — the note
    is the operator-facing plain-language line for a degraded read, None when the read was fully
    fresh/committed. Mirrors knowledge_index.connect's stable (payload, source) shape, confining the
    degrade-aware return to the one boundary that surfaces it rather than threading a variant return
    through every public op."""
    result, source = _with_conn(fn, index_path, graph_path)
    return result, degrade_message(source)


def get_entity(entity_id, *, index_path=None, graph_path=None):
    result, _source = _with_conn(lambda c: _get_entity(c, entity_id), index_path, graph_path)
    return result


def find(type=None, path_glob=None, owner=None, *, index_path=None, graph_path=None):
    result, _source = _with_conn(lambda c: _find(c, type, path_glob, owner), index_path, graph_path)
    return result


def neighbors(entity_id, edge_filter=None, direction="out", depth=1, *, index_path=None, graph_path=None):
    result, _source = _with_conn(lambda c: _neighbors(c, entity_id, edge_filter, direction, depth),
                                 index_path, graph_path)
    return result


def relate(id_a, id_b, *, index_path=None, graph_path=None):
    result, _source = _with_conn(lambda c: _relate(c, id_a, id_b), index_path, graph_path)
    return result


# ---- CLI (the operator's no-Claude-Desktop demo path) ---------------------------------------

def degrade_message(source) -> str | None:
    """The operator-facing plain-language line for a DEGRADED read, or None when the read was fully
    fresh/committed. Shared by the CLI (`_note_degrade`) and the MCP transport (`with_degrade`) so the two
    channels never drift, and phrased in the SAME plain "project map" register boot uses (never engine
    shorthand like "live walk"/"graph") so one fault reads the same wherever the operator meets it. 'live' =
    the committed map is MISSING (benign in a fresh worktree — regenerate to restore it); 'live-corrupt' = it
    is PRESENT but could not be read (a bad write or overlay damaged it) — named distinctly because the
    repair differs (regenerate to REPLACE the damaged file, not to create a missing one; eADR-0004 'name
    what is reduced')."""
    m = knowledge_gen._display(knowledge_gen.GRAPH_PATH)
    if source == "live":
        return (f"your committed project map ({m}) is missing, so this answer came from a map I rebuilt from "
                f"your live project files — regenerate it with `{knowledge_gen.REGEN_CMD}` and commit the "
                f"result to restore your saved map.")
    if source == "live-corrupt":
        return (f"your committed project map ({m}) is present but damaged (I couldn't read it), so this "
                f"answer came from a map I rebuilt from your live project files — regenerate it with "
                f"`{knowledge_gen.REGEN_CMD}` and commit the result to replace the damaged file.")
    return None


def _note_degrade(source) -> None:
    """Surface a degraded read on stderr (the operator/CLI channel). None = the index was fresh (silent);
    'committed' = rebuilt from the committed graph (git-native, a quiet note); 'live'/'live-corrupt' = a
    LOUD live-walk fallback (the committed graph is absent / present-but-damaged)."""
    if source == "committed":
        print(f"(the fast lookup index was absent or stale, so this answer was rebuilt from your committed "
              f"project map — {knowledge_gen._display(knowledge_gen.GRAPH_PATH)}, the saved source of "
              f"truth)", file=sys.stderr)
        return
    msg = degrade_message(source)
    if msg:
        print(f"KNOWLEDGE DEGRADED: {msg}", file=sys.stderr)


def main(argv: list) -> int:
    if not argv:
        print("usage: knowledge_query.py {get-entity <id> | find [--type T] [--glob G] [--owner M] "
              "| neighbors <id> [--in|--out|--both] [--depth N] [--edge K] | relate <id-a> <id-b>}",
              file=sys.stderr)
        return 2
    cmd, rest = argv[0], argv[1:]
    try:
        conn, source = knowledge_index.connect()
    except KnowledgeUnavailable as exc:
        print(f"Knowledge is unavailable: {exc}. Restore or regenerate "
              f"{knowledge_gen._display(knowledge_gen.GRAPH_PATH)} "
              f"(`{knowledge_gen.REGEN_CMD}`), then try again.", file=sys.stderr)
        return 1
    try:
        _note_degrade(source)
        if cmd == "get-entity" and len(rest) == 1:
            result = _get_entity(conn, rest[0])
        elif cmd == "find":
            opts = _parse_opts(rest, {"--type": "type", "--glob": "path_glob", "--owner": "owner"})
            result = _find(conn, **opts)
        elif cmd == "neighbors" and rest:
            entity_id = rest[0]
            direction = "out"
            if "--in" in rest: direction = "in"
            if "--both" in rest: direction = "both"
            depth = int(_flag_value(rest, "--depth", "1"))
            edges = [v for v in _all_flag_values(rest, "--edge")] or None
            result = _neighbors(conn, entity_id, edge_filter=edges, direction=direction, depth=depth)
        elif cmd == "relate" and len(rest) == 2:
            result = _relate(conn, rest[0], rest[1])
        else:
            print(f"bad arguments for {cmd!r}", file=sys.stderr)
            return 2
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    except (ValueError, KeyError) as exc:
        print(f"bad query: {exc}", file=sys.stderr)
        return 2
    finally:
        conn.close()


def _flag_value(argv, flag, default):
    return argv[argv.index(flag) + 1] if flag in argv and argv.index(flag) + 1 < len(argv) else default


def _all_flag_values(argv, flag):
    return [argv[i + 1] for i, a in enumerate(argv) if a == flag and i + 1 < len(argv)]


def _parse_opts(argv, mapping):
    out = {}
    for flag, key in mapping.items():
        if flag in argv:
            out[key] = _flag_value(argv, flag, None)
    return out


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
