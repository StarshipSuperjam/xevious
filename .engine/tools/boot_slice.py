#!/usr/bin/env python3
"""The gitignored boot-slice cache (StarshipSuperjam/engine-template#37) — boot's rung-1 fast cache for the orientation knowledge read.

The committed `.engine/knowledge/graph.json` is the source of truth; the SQLite query index
(`knowledge_index.py`) is the *query* path's rung-1 fast cache. This module is the index's SIBLING: the
*boot* path's own rung-1 (the two-rung-1 model is named in
`knowledge_index.py`'s docstring). It caches the focus-INDEPENDENT structural projection boot's orientation
read consumes — the path->entity map (`derive_focus`) and the bidirectional depth-1 adjacency
(`neighborhood_of` + the structural-neighbours walk) — so orientation reads a small JSON file instead of
consulting the SQLite index. It is a CACHE, never committed: delete it and it rebuilds on the next orientation.

Home: the same gitignored `.engine/knowledge/.cache/` as the index — the KNOWLEDGE cache, NOT
`.engine/boot/` (which is for boot's OWN assembled artifacts, not a knowledge-produced cache boot reads).
Pruned from the ownership walk (module_coherence.PRUNE_DIRS has `.cache`) and invisible to catalog-coverage
(`.engine/knowledge/` is infra) — regenerable, never an orphan, never committed.

Degrade chain — the BOOT path's four rungs: rung-1 = this
slice when fresh; rungs 2-4 are SHARED with the query path and reused LITERALLY here via
`knowledge_index._load_graph` — a missing/stale slice is rebuilt from the committed graph (rung 2); an absent
committed graph falls back to a LIVE WALK of the surfaces (rung 3, loudly degraded, self-healing); only if
that also fails is knowledge unavailable (rung 4, raised — never crashed, never blocking boot). `read()`
fail-opens to None on ANY error, so a broken slice can never block orientation.
"""
from __future__ import annotations
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import knowledge_gen     # noqa: E402
# The SQLite-index sibling. We deliberately REUSE its loader + fingerprint + edge vocabulary so the shared
# degrade rungs 2-4 are ONE implementation, not a drift-prone copy (knowledge_index.py:15-21 calls them
# shared). `_load_graph` is underscore-private by convention but is a DELIBERATE shared-rung API here — do not
# "privatize" it without giving boot_slice its own loader. LIVE_WALK_FINGERPRINT / graph_fingerprint /
# WALK_EDGE_KINDS / EDGE_KINDS / KnowledgeUnavailable / CACHE_DIR are likewise shared on purpose.
import knowledge_index   # noqa: E402

# Home: the index's own `.engine/knowledge/.cache/` dir (shared constant), with our own filename.
SLICE_PATH = os.path.join(knowledge_index.CACHE_DIR, "boot-slice.json")

# This cache's own shape version — INDEPENDENT of the SQLite index's INDEX_SCHEMA_VERSION (a different
# artifact, a different shape). `is_fresh` keys ONLY on (existence, schema_version, graph_fingerprint), so a
# change to the PROJECTION below (its content OR its order) WITHOUT bumping this would leave a
# fresh-looking-but-divergent cache rendering a confident, wrong orientation block. BUMP THIS on any
# projection change. The load-bearing parity test (test_boot_slice.py) re-proves slice == live walk over the
# real graph on every CI run, so such a divergence cannot merge — but the version bump is the first-line guard.
SLICE_SCHEMA_VERSION = "2"   # 1 -> 2: WALK_EDGE_KINDS widened (the code-dependency + wiring edges join the
#                              projected adjacency), so a v1 slice omits them and must be rebuilt.


# ---- the projection -------------------------------------------------------------------------

def _add(bucket: list, entry: dict) -> None:
    """Append an adjacency entry, de-duped within its source — matching the index CTE's DISTINCT."""
    if entry not in bucket:
        bucket.append(entry)


def _project(graph: dict) -> dict:
    """The focus-INDEPENDENT structure boot's orientation read consumes, derived from the committed graph:

      by_path   — {source.path: id} for every entity (derive_focus maps each changed file -> its owning entity).
      adjacency — {id: [{"id","predicate","direction"}, ...]} — every entity's depth-1 neighbours over the
                  walk edges (WALK_EDGE_KINDS: the containment edges plus the code-dependency and wiring edges;
                  supersedes excluded) in BOTH directions, EXACTLY
                  reproducing knowledge_query.neighbors(direction="both"). graph.json stores OUTGOING edges
                  only, so each `A --pred--> B` is recorded as {B,pred,"out"} under A AND {A,pred,"in"} under B;
                  a self-edge (A==B) is skipped and each (id,predicate,direction) is de-duped within a source —
                  matching the CTE's `nid<>?` + DISTINCT. Lists are NOT ordered here; the read-shim orders on
                  read to match the CTE's `ORDER BY nid,pred,dir`, so the cache file stays canonical regardless.
    """
    by_path: dict = {}
    adjacency: dict = {}
    walk = set(knowledge_index.WALK_EDGE_KINDS)
    for e in graph.get("entities", []):                  # pass 1: register every entity (a node may be the
        eid = e.get("id")                                # TARGET of a reverse edge before we iterate it)
        if not eid:
            continue
        adjacency.setdefault(eid, [])
        path = (e.get("source") or {}).get("path")
        if path:
            by_path[path] = eid
    for e in graph.get("entities", []):                  # pass 2: the bidirectional adjacency
        a = e.get("id")
        if not a:
            continue
        for pred, dsts in (e.get("predicates") or {}).items():
            if pred not in walk:                         # only the structural walk edges are cached
                continue
            for b in dsts:
                if b == a:                               # self-edge: the CTE excludes nid<>entity
                    continue
                _add(adjacency[a], {"id": b, "predicate": pred, "direction": "out"})
                _add(adjacency.setdefault(b, []), {"id": a, "predicate": pred, "direction": "in"})
    return {"by_path": by_path, "adjacency": adjacency}


# ---- build / freshness (mirrors knowledge_index) --------------------------------------------

def build(slice_path: str | None = None, graph_path: str | None = None):
    """(Re)build the boot slice; return (slice_path, source) — 'committed' (rung 2) or 'live' (rung 3). Builds
    into a process-unique temp file then atomically replaces, so a reader never sees a half-built slice. Raises
    KnowledgeUnavailable only when the committed graph is absent AND the live walk also fails (rung 4)."""
    slice_path = SLICE_PATH if slice_path is None else slice_path
    graph_path = knowledge_gen.GRAPH_PATH if graph_path is None else graph_path
    graph, source = knowledge_index._load_graph(graph_path)   # deliberate shared-rung reuse (see import note)
    # The staleness key: the committed graph's content-fingerprint, or the live-walk sentinel (never a real fp,
    # never the None graph_fingerprint() gives while the committed graph is absent) — so a live-built slice is
    # never fresh and self-heals to a committed rebuild the moment graph.json returns.
    fp = (knowledge_index.graph_fingerprint(graph_path) if source == "committed"
          else knowledge_index.LIVE_WALK_FINGERPRINT)
    payload = {"schema_version": SLICE_SCHEMA_VERSION, "graph_fingerprint": fp, **_project(graph)}
    os.makedirs(os.path.dirname(slice_path) or ".", exist_ok=True)
    tmp = f"{slice_path}.building.{os.getpid()}"
    if os.path.exists(tmp):
        os.remove(tmp)
    with open(tmp, "w", encoding="utf-8") as fh:
        # Compact + key-sorted: this is a machine-read cache no human diffs (unlike the committed graph.json),
        # so halve the boot-path read; sort_keys keeps the bytes deterministic across rebuilds. Whitespace is
        # not the projection's SHAPE, so this is not a SLICE_SCHEMA_VERSION change.
        json.dump(payload, fh, sort_keys=True, separators=(",", ":"))
    os.replace(tmp, slice_path)
    return slice_path, source


def is_fresh(slice_path: str | None = None, graph_path: str | None = None) -> bool:
    """True iff the slice exists, was built by THIS shape (SLICE_SCHEMA_VERSION), and from the current committed
    graph (content fingerprints match — so a mere `touch` of graph.json, which leaves the content unchanged,
    does NOT invalidate; only a content change does). A corrupt/partial slice is not fresh."""
    slice_path = SLICE_PATH if slice_path is None else slice_path
    graph_path = knowledge_gen.GRAPH_PATH if graph_path is None else graph_path
    if not os.path.isfile(slice_path):
        return False
    try:
        with open(slice_path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (ValueError, OSError):
        return False
    return (data.get("schema_version") == SLICE_SCHEMA_VERSION
            and data.get("graph_fingerprint") == knowledge_index.graph_fingerprint(graph_path))


def ensure(slice_path: str | None = None, graph_path: str | None = None):
    """Return (slice_path, source) — source is None when the slice was already fresh; else 'committed'/'live'
    (what this call rebuilt from). Raises KnowledgeUnavailable only at rung 4."""
    slice_path = SLICE_PATH if slice_path is None else slice_path
    graph_path = knowledge_gen.GRAPH_PATH if graph_path is None else graph_path
    if is_fresh(slice_path, graph_path):
        return slice_path, None
    return build(slice_path, graph_path)


# ---- the read-shim boot consumes ------------------------------------------------------------

class Slice:
    """A read-shim over the cached projection, exposing the SAME two methods attention's orientation reads call
    on the `knowledge_query` module — same signatures, same return shapes, same ORDER — so a consumer reads the
    cache through ONE code path, byte-identical to the live walk. Also carries WALK_EDGE_KINDS/EDGE_KINDS so a
    consumer can read the edge vocabulary from its source without depending on the knowledge_query module.

    Serves the cold-start depth-1 bidirectional walk over the walk edges (all boot's orientation asks);
    a depth>1 or an off-walk (e.g. supersedes) request raises, because the cache does not hold it — those
    stay on the on-demand query path (the SQLite index), by design."""

    def __init__(self, data: dict):
        self._by_path = data.get("by_path") or {}
        self._adjacency = data.get("adjacency") or {}
        self.WALK_EDGE_KINDS = knowledge_index.WALK_EDGE_KINDS
        self.EDGE_KINDS = knowledge_index.EDGE_KINDS
        # Provenance for boot's render: True iff this slice was rebuilt from a LIVE surface-walk rather than
        # from the committed map (rung 3, "loudly degraded"). The map is still reachable (orientation works),
        # so this is NOT a "couldn't reach" degrade — boot surfaces it as a distinct heads-up. The two live
        # rungs are carried separately so boot names the cause: `from_live` = the committed graph.json was
        # ABSENT (regenerate); `from_corrupt` = it was PRESENT but DAMAGED (regenerate to replace it). `read()`
        # sets them from the build source; a directly-constructed Slice defaults both to False (committed).
        # See the boot_slice module docstring's degrade chain.
        self.from_live = False
        self.from_corrupt = False

    def find(self):
        """knowledge_query.find()-shaped, ordered by id (its `ORDER BY id`). Carries the two fields derive_focus
        reads (source_path, id) — the cache holds no other find() dimension, which boot's focus map never asks."""
        return [{"id": i, "source_path": p}
                for p, i in sorted(self._by_path.items(), key=lambda kv: (kv[1], kv[0]))]

    def neighbors(self, entity_id, edge_filter=None, direction="out", depth=1):
        """knowledge_query.neighbors()-shaped for the cold-start walk: same arg order/defaults, the same
        `ORDER BY nid,pred,dir` + `nid<>?` self-exclusion (enforced at build time), filtered by
        edge_filter/direction. Returns [{"id","predicate","direction"}, ...]. Validation MIRRORS the library
        for the walk edges but is deliberately STRICTER on the edge_filter: a non-structural edge (e.g.
        supersedes) is rejected here rather than returned empty, because the cache holds only the structural
        walk edges — boot's orientation only ever passes those, so its reads are identical either way."""
        if direction not in ("out", "in", "both"):
            raise ValueError(f"direction must be out/in/both, got {direction!r}")
        if not isinstance(depth, int) or depth < 1:
            raise ValueError(f"depth must be an integer >= 1, got {depth!r}")
        if depth != 1:
            raise ValueError("the boot slice caches the depth-1 cold-start walk only")
        preds = tuple(edge_filter) if edge_filter else knowledge_index.WALK_EDGE_KINDS
        bad = [p for p in preds if p not in knowledge_index.WALK_EDGE_KINDS]
        if bad:
            raise ValueError(f"the boot slice caches only the structural walk edges "
                             f"{list(knowledge_index.WALK_EDGE_KINDS)}; got {bad}")
        predset, want = set(preds), ({"out", "in"} if direction == "both" else {direction})
        rows = [n for n in self._adjacency.get(entity_id, [])
                if n.get("predicate") in predset and n.get("direction") in want]
        rows.sort(key=lambda n: (n["id"], n["predicate"], n["direction"]))
        return [{"id": n["id"], "predicate": n["predicate"], "direction": n["direction"]} for n in rows]


def read(slice_path: str | None = None, graph_path: str | None = None):
    """Boot's rung-1 read: ensure a fresh slice, then return a `Slice` read-shim — or None on ANY failure
    (fail-open; mirrors the index; NEVER raises into boot). When None, boot's reads fall back to the
    knowledge_query path (which itself runs the shared rungs), or boot orients without the block."""
    try:
        path, source = ensure(slice_path, graph_path)
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        s = Slice(data)
        # `ensure` returns 'committed'/'live'/'live-corrupt' (what it rebuilt from) or None (the slice was
        # already fresh — only a committed slice is ever fresh, a live one never is, line 105-109). Both live
        # rungs mean orientation ran on the rebuilt fallback; they are carried SEPARATELY so boot names the
        # cause honestly: 'live' = committed map ABSENT (regenerate), 'live-corrupt' = present but DAMAGED
        # (regenerate to replace it) — a distinct repair the operator relies on (eADR-0004 'name what is reduced').
        s.from_live = (source == "live")
        s.from_corrupt = (source == "live-corrupt")
        return s
    except Exception:
        return None


# ---- CLI (mirrors knowledge_index.py:219-254) -----------------------------------------------

def main(argv: list) -> int:
    cmd = argv[0] if argv else "status"
    try:
        if cmd == "build":
            path, source = build()
            whence = ("a live walk of the surfaces (the committed graph is absent — loudly degraded)"
                      if source == "live" else "the committed graph")
            print(f"Built the boot slice ({knowledge_gen._display(path)}) from {whence}.")
            return 0
        if cmd == "status":
            fresh = is_fresh()
            exists = os.path.isfile(SLICE_PATH)
            where = knowledge_gen._display(SLICE_PATH)
            if fresh:
                print(f"The boot slice ({where}) is present and fresh.")
            elif exists:
                print(f"The boot slice ({where}) is stale — it will rebuild from the committed graph "
                      f"on the next orientation.")
            else:
                print(f"The boot slice ({where}) is absent — it will be built from the committed graph "
                      f"on the next orientation.")
            return 0
        print(f"usage: boot_slice.py {{build|status}}\nunknown command {cmd!r}", file=sys.stderr)
        return 2
    except knowledge_index.KnowledgeUnavailable as exc:
        print(f"Knowledge is unavailable: {exc}. Restore or regenerate "
              f"{knowledge_gen._display(knowledge_gen.GRAPH_PATH)} (`{knowledge_gen.REGEN_CMD}`).",
              file=sys.stderr)
        return 1
    except (OSError, ValueError) as exc:
        print(f"CONFIG ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
