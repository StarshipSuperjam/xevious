#!/usr/bin/env python3
"""The gitignored SQLite query index derived from the committed knowledge graph.

The committed `.engine/knowledge/graph.json` is the source of truth and the offline
cold-start readout; it stores OUTGOING edges only. This module derives a gitignored SQLite index
under `.engine/knowledge/.cache/` that the graph-query op-set (knowledge_query.py) reads — adding the
reverse-edge traversal and the indexed selectors the committed file does not give cheaply at scale.

The index is a CACHE, never committed: delete it and it rebuilds from the committed graph on the next
query (degrade-to-git-native). It is regenerated on demand — rebuilt when missing or when the
committed graph's fingerprint moves. SQLite (stdlib `sqlite3`) is the end-state floor: it scales to a
real adopter's repo and serves neighbors(depth) / relate(path) / find(selector) via indexed SQL and
recursive CTEs, with no third-party dependency.

Degrade chain — these are the QUERY path's four rungs (rung-1 fast cache = this
SQLite index; the boot path's rung-1 is its own boot-slice cache, and rungs 2-4 are shared): a fresh
index answers; a missing/stale index is rebuilt from the committed graph; if the committed graph is
ABSENT — or PRESENT BUT UNREADABLE (corrupt: merge markers, a truncated regen) — the index is rebuilt
from a LIVE WALK of the surfaces (knowledge_gen.canonical_graph() — loudly degraded, so a fresh worktree,
the upgrade-overlay window, or a damaged file still answers); and only if that live walk also fails
is knowledge reported unavailable (the query layer reports it in plain language, never a crash, never
blocking boot). The absent and corrupt cases carry distinct degrade sources ('live' vs 'live-corrupt') so
the operator-facing signal names a missing file and a damaged one differently.
"""
from __future__ import annotations
import hashlib
import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate          # noqa: E402
import knowledge_gen     # noqa: E402

# The gitignored derivative dir alongside the committed graph. `.cache` is pruned from the engine
# file inventory (module_coherence.PRUNE_DIRS) so the index is never a false ownership orphan, and
# is gitignored via core's wires:gitignore fence — it is regenerable, never committed.
CACHE_DIR = os.path.join(knowledge_gen.KNOWLEDGE_DIR, ".cache")
INDEX_PATH = os.path.join(CACHE_DIR, "index.sqlite")

# The FULL valid edge vocabulary — every predicate that may appear in the store and be requested via a
# neighbors edge_filter / pulled by relate. `supersedes` is a deliberate PULL edge: valid here, but excluded
# from WALK_EDGE_KINDS below so the cold-start adjacency walk never traverses it.
EDGE_KINDS = ("provided_by", "governed_by", "targets", "depends_on",
              "imports", "tests", "enforced_by", "wires_hook", "implemented_by", "supersedes")

# The cold-start adjacency walk's traversal set — the containment edges (provided_by / governed_by / targets /
# depends_on) PLUS the code-dependency and wiring edges (imports / tests / enforced_by / wires_hook /
# implemented_by), so a cold session's orientation push answers "what depends on / exercises / wires the part
# I'm changing", not merely "what owns it". `supersedes` alone stays OFF the walk (a pull-only lineage edge).
# The orientation budget is held flat NOT by keeping this vocabulary small but by the render's per-relationship
# SAMPLE CAP with honest totals (attention.NEIGHBORHOOD_SAMPLE_CAP: a 162-importer hub renders as one line,
# "showing 4 of 162"), so a richer map cannot bloat the block. The invariant that is pinned here is WHICH kinds
# ride the walk (a build-spec decision), not a count; its conceptual home is the attention policy's `## Scope`
# (the surface that owns budget allocation/trim), a fixed rule rather than an operator-tunable dial, so it is
# pinned in code rather than the policy's overridable `values:` block. This is also the default neighbors()
# traverses when no edge_filter is given, and attention's cold-start caller passes it explicitly.
WALK_EDGE_KINDS = ("provided_by", "governed_by", "targets", "depends_on",
                   "imports", "tests", "enforced_by", "wires_hook", "implemented_by")

# The index's own shape version. Bumped whenever the SQLite schema or the columns build_index writes
# change, so an OLD-shape cached index on disk is deemed stale and rebuilt (is_fresh checks it alongside
# the graph fingerprint) — a graph that did not move still cannot serve a row missing a new column.
# 2 -> 3: EDGE_KINDS widened (the code-dependency + wiring edges join the store), so an index built against
# the old 5-kind vocabulary would silently omit the new edges and must rebuild.
INDEX_SCHEMA_VERSION = "3"

# The staleness key an index built from a LIVE WALK records in its `meta` (rung 3). It is never equal
# to a real "sha256:" graph fingerprint, and never equal to the None that graph_fingerprint() returns
# while the committed graph is absent — so a live-walk index is never deemed fresh: it re-walks on every
# query while degraded, and self-heals to a committed rebuild the moment graph.json returns.
LIVE_WALK_FINGERPRINT = "live-walk"

# The entity keys stored as their own columns (or, for `source`, split into source_path/fingerprint) —
# everything ELSE on an entity is a declared attribute that rides the `attributes` JSON column.
_CORE_ENTITY_KEYS = frozenset({"id", "type", "name", "slug", "source", "owner", "predicates"})

SCHEMA_SQL = """
CREATE TABLE entities (
  id          TEXT PRIMARY KEY,
  type        TEXT NOT NULL,
  name        TEXT NOT NULL,
  slug        TEXT NOT NULL,
  source_path TEXT NOT NULL,
  fingerprint TEXT NOT NULL,
  owner       TEXT NOT NULL,
  attributes  TEXT
);
CREATE TABLE edges (
  src_id    TEXT NOT NULL,
  predicate TEXT NOT NULL,
  dst_id    TEXT NOT NULL
);
CREATE INDEX edges_src ON edges(src_id);
CREATE INDEX edges_dst ON edges(dst_id);
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


class KnowledgeUnavailable(Exception):
    """Knowledge cannot be read: the committed graph is absent AND a live walk of the surfaces also
    failed — the last two rungs of the degrade chain both gave out. Raised (never crashed-through) so
    the query layer surfaces it in plain language, without blocking boot."""


def graph_fingerprint(graph_path: str | None = None) -> str | None:
    """A sha256 of the committed graph.json raw bytes (prefixed 'sha256:'), or None if it is absent.
    The index's staleness key: the index records which graph it was built from."""
    graph_path = knowledge_gen.GRAPH_PATH if graph_path is None else graph_path
    if not os.path.isfile(graph_path):
        return None
    with open(graph_path, "rb") as fh:
        return "sha256:" + hashlib.sha256(fh.read()).hexdigest()


def _load_graph(graph_path: str):
    """The graph to index, as (graph_dict, source) — the degrade chain's middle rungs:
      'committed'    — the committed graph is present AND parses → rebuild the index from it (rung 2).
      'live'         — the committed graph is ABSENT → fall back to a LIVE WALK of the surfaces
                       (knowledge_gen.canonical_graph(), rung 3), so a fresh worktree or the
                       upgrade-overlay window still answers (loudly degraded).
      'live-corrupt' — the committed graph is PRESENT but UNREADABLE (merge markers, a truncated regen) →
                       fall back to the LIVE WALK exactly as absence does, but tagged distinctly so the
                       degrade signal names a DAMAGED file, not a missing one: the repair differs and the
                       operator relies on an honest signal (eADR-0004 'name what is reduced').
    A corrupt committed graph USED to raise a raw JSONDecodeError here (its validity deferred to the CI
    knowledge-coverage gate) — but that gate only fires at merge, while a reader hitting a mid-write
    truncation between commit and gate must not crash the query/boot path ('report unavailable,
    without blocking boot'). So corrupt now degrades like absence rather than raising. The producer of that
    corruption is closed in the same change: knowledge_gen.write_graph now writes graph.json atomically.
    Raises KnowledgeUnavailable only when the LIVE WALK itself also fails (rung 4) — reported, never
    crashed, so the read path never blocks boot."""
    text = knowledge_gen.read_committed(graph_path)
    corrupt = False
    if text is not None:
        try:
            return json.loads(text), "committed"
        except (json.JSONDecodeError, ValueError):
            corrupt = True                             # present but unreadable → degrade like absence
    try:
        return json.loads(knowledge_gen.canonical_graph()), ("live-corrupt" if corrupt else "live")
    except Exception as exc:                            # rung 4: the read path must never block boot
        state = "present but unreadable" if corrupt else "absent"
        raise KnowledgeUnavailable(
            f"the committed knowledge graph ({knowledge_gen._display(graph_path)}) is {state} and a "
            f"live walk of the surfaces also failed: {exc}") from exc


def build_index(index_path: str | None = None, graph_path: str | None = None):
    """(Re)build the SQLite index; return (index_path, source) — source is 'committed' (built from the
    committed graph, rung 2), 'live' (built from a LIVE WALK because the committed graph is absent, rung 3),
    or 'live-corrupt' (a LIVE WALK because the committed graph is present but unreadable). Builds into a
    temp file then atomically replaces, so a reader never sees a half-built index. Raises
    KnowledgeUnavailable only when the live walk itself also fails (rung 4)."""
    index_path = INDEX_PATH if index_path is None else index_path
    graph_path = knowledge_gen.GRAPH_PATH if graph_path is None else graph_path
    graph, source = _load_graph(graph_path)
    # The staleness key: the committed graph's byte-fingerprint, or the live-walk sentinel for BOTH live
    # rungs ('live' and 'live-corrupt'). A corrupt committed file still has a real byte-fingerprint, so the
    # sentinel (not that fingerprint) is what keeps a corrupt-sourced index from ever being deemed fresh —
    # it re-walks each query and self-heals the moment graph.json is regenerated.
    fp = graph_fingerprint(graph_path) if source == "committed" else LIVE_WALK_FINGERPRINT
    os.makedirs(os.path.dirname(index_path) or ".", exist_ok=True)
    # A process-unique temp name so two concurrent rebuilds of the same index (e.g. parallel test
    # runners sharing one worktree) cannot collide on the in-progress file; the final os.replace is
    # atomic, so the last writer wins with valid content.
    tmp = f"{index_path}.building.{os.getpid()}"
    if os.path.exists(tmp):
        os.remove(tmp)
    conn = sqlite3.connect(tmp)
    try:
        conn.executescript(SCHEMA_SQL)
        for e in graph.get("entities", []):
            src = e.get("source") or {}
            # The non-core declared attributes (status/tier/title/discriminators) ride one JSON
            # column, so a new attribute needs no migration and `find` stays core-scalar (no attribute
            # SELECTOR — no find(attribute) canon back-door); get-entity/find merge them back on read.
            attrs = {k: v for k, v in e.items() if k not in _CORE_ENTITY_KEYS}
            conn.execute(
                "INSERT INTO entities(id,type,name,slug,source_path,fingerprint,owner,attributes) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (e["id"], e["type"], e["name"], e["slug"],
                 src.get("path", ""), src.get("fingerprint", ""), e["owner"],
                 json.dumps(attrs, sort_keys=True) if attrs else None))
            for pred, dsts in (e.get("predicates") or {}).items():
                if pred not in EDGE_KINDS:             # allowlist: a stray predicate never enters the store
                    continue
                for dst in dsts:
                    conn.execute("INSERT INTO edges(src_id,predicate,dst_id) VALUES (?,?,?)",
                                 (e["id"], pred, dst))
        conn.execute("INSERT INTO meta(key,value) VALUES ('graph_fingerprint', ?)", (fp,))
        conn.execute("INSERT INTO meta(key,value) VALUES ('index_schema_version', ?)",
                     (INDEX_SCHEMA_VERSION,))
        conn.commit()
    finally:
        conn.close()
    os.replace(tmp, index_path)
    return index_path, source


def is_fresh(index_path: str | None = None, graph_path: str | None = None) -> bool:
    """True iff the index exists, was built by THIS index shape (INDEX_SCHEMA_VERSION), and from the
    current committed graph (fingerprints match). The version leg forces an OLD-shape index (missing a
    newly-added column) to rebuild even when the committed graph did not move."""
    index_path = INDEX_PATH if index_path is None else index_path
    graph_path = knowledge_gen.GRAPH_PATH if graph_path is None else graph_path
    if not os.path.isfile(index_path):
        return False
    try:
        conn = sqlite3.connect(index_path)
        try:
            meta = dict(conn.execute("SELECT key, value FROM meta").fetchall())
        finally:
            conn.close()
    except sqlite3.DatabaseError:
        return False                                   # a corrupt/partial index is not fresh
    return (meta.get("index_schema_version") == INDEX_SCHEMA_VERSION
            and meta.get("graph_fingerprint") == graph_fingerprint(graph_path))


def ensure_index(index_path: str | None = None, graph_path: str | None = None):
    """Return (index_path, source) — `source` is None when the index was already fresh; otherwise it
    names what this call rebuilt from: 'committed' (the committed graph, rung 2), 'live' (a LIVE WALK
    because the committed graph is absent, rung 3), or 'live-corrupt' (a LIVE WALK because it is present
    but unreadable), so the query layer can surface a degraded read. Raises KnowledgeUnavailable only when
    the live walk itself also fails (rung 4)."""
    index_path = INDEX_PATH if index_path is None else index_path
    graph_path = knowledge_gen.GRAPH_PATH if graph_path is None else graph_path
    if is_fresh(index_path, graph_path):
        return index_path, None
    return build_index(index_path, graph_path)


def connect(index_path: str | None = None, graph_path: str | None = None):
    """A read connection to a fresh index (ensuring it first). Returns (sqlite3.Connection, source) —
    source is None (already fresh) / 'committed' / 'live' / 'live-corrupt' (see ensure_index). The caller
    closes it."""
    path, source = ensure_index(index_path, graph_path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn, source


# ---- CLI ------------------------------------------------------------------------------------

def main(argv: list) -> int:
    cmd = argv[0] if argv else "status"
    try:
        if cmd == "build":
            path, source = build_index()
            whence = ("a live walk of the surfaces (the committed graph is absent — loudly degraded)"
                      if source == "live" else "the committed graph")
            print(f"Built the knowledge index ({knowledge_gen._display(path)}) from {whence}.")
            return 0
        if cmd == "status":
            fresh = is_fresh()
            exists = os.path.isfile(INDEX_PATH)
            where = knowledge_gen._display(INDEX_PATH)
            if fresh:
                print(f"The knowledge index ({where}) is present and fresh.")
            elif exists:
                print(f"The knowledge index ({where}) is stale — it will rebuild from the committed "
                      f"graph on the next query.")
            else:
                print(f"The knowledge index ({where}) is absent — it will be built from the committed "
                      f"graph on the next query.")
            return 0
        print(f"usage: knowledge_index.py {{build|status}}\nunknown command {cmd!r}", file=sys.stderr)
        return 2
    except KnowledgeUnavailable as exc:
        print(f"Knowledge is unavailable: {exc}. Restore or regenerate "
              f"{knowledge_gen._display(knowledge_gen.GRAPH_PATH)} "
              f"(`{knowledge_gen.REGEN_CMD}`).", file=sys.stderr)
        return 1
    except (OSError, ValueError) as exc:
        print(f"CONFIG ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
