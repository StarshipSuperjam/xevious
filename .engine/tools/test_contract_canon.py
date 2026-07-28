#!/usr/bin/env python3
"""The foundational eADR canon's cold-start gate — the design-rationale transplant's own check.

The contract canon (the `eADR-####` records under `.engine/contracts/`) is a living cold-copy snapshot:
each record is revised in place to current truth and carries no supersession chain, so the canon stays
all-first-of-its-kind (a deployment's own records under instance/ stay append-only). These tests pin the
three permanence-critical properties that no other check enforces, reading the REAL committed graph and
running the REAL attention render — they fake nothing, so a regression in any of them fails here:

  - COVERAGE — every committed contract record derives exactly one knowledge entity (none dropped, none
    duplicated, the `.gitkeep` placeholder excluded).
  - NO FORWARD EDGE OUT OF THE CANON — the graph derives no `ratifies` edge, and a contract entity emits
    only the two ownership edges every owned surface file has (`provided_by` -> the core module,
    `governed_by` -> the contract schema). In particular the ~14 sibling cross-references in the record
    prose are NOT turned into graph edges, and no `supersedes` edge exists (the canon is all first-of-its-kind
    records). Adjacency INTO the canon does exist and is expected — the contract checks target it, the schema
    governs it, the core module provides it — so "no orientation bloat" is NOT guaranteed by edge-absence; it
    is guaranteed by the bounded render below.
  - NO ORIENTATION BLOAT — a clean-tree session (empty focus) surfaces none of the canon; and even when a
    hub the whole canon hangs off (the core module, the contract schema) lands in a session's focus, the
    orientation render surfaces the canon as a bounded, COUNTED sample (NEIGHBORHOOD_SAMPLE_CAP), never an
    inline dump of all N records. This is the property the manifest's "reached on demand, never pushed into
    the cold-start pack" design rests on, made falsifiable: a future change that let a hub dump the whole
    canon into orientation would fail here.
"""
from __future__ import annotations
import glob
import json
import os
import unittest

import attention
import boot_slice

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_GRAPH = os.path.join(_ROOT, ".engine", "knowledge", "graph.json")
_CONTRACTS = os.path.join(_ROOT, ".engine", "contracts")


def _graph() -> dict:
    with open(_GRAPH, encoding="utf-8") as fh:
        return json.load(fh)


def _committed_record_ids() -> set:
    """The eADR id of every committed contract file (the filename's `eADR-NNNN`), excluding placeholders."""
    out = set()
    for path in glob.glob(os.path.join(_CONTRACTS, "eADR-*.md")):
        base = os.path.basename(path)
        out.add(base[:9])          # 'eADR-0001-...md' -> 'eADR-0001'
    return out


def _source_over(graph: dict):
    """A read-shim over the committed graph, built directly (no cache) — the SAME projection boot consumes,
    so neighborhood_of runs its real bidirectional walk over the real edges."""
    return boot_slice.Slice(boot_slice._project(graph))


class ContractCanonCoverage(unittest.TestCase):
    def test_every_record_is_exactly_one_entity(self):
        graph = _graph()
        contract_ents = [e for e in graph["entities"] if e.get("type") == "contract"]
        ids = sorted(e["id"] for e in contract_ents)
        self.assertEqual(len(ids), len(set(ids)), "a contract record must derive at most one entity")
        # Canon-only coverage. A deployment's per-instance eADR is also a `contract` entity, but it carries NO
        # `provided_by` edge (it is in no module's provides) and its slug is `instance.<stem>` — so filter to
        # canon (provided_by present) before matching the `eADR-NNNN` filename ids. `_committed_record_ids`
        # already globs only the canon root (.engine/contracts/eADR-*.md), so both sides are canon. This keeps
        # teeth against the two regressions: a canon record that lost `provided_by` drops out of `canon_ents`
        # (→ mismatch → fail), and a deployment record that gained one has an `instance.<stem>` slug whose
        # `[:9]` is `instance.` (→ mismatch → fail).
        canon_ents = [e for e in contract_ents if (e.get("predicates") or {}).get("provided_by")]
        entity_eadr = {e["slug"][:9] for e in canon_ents}   # canon slug == 'eADR-0001-...'
        files = _committed_record_ids()
        self.assertTrue(files, "expected committed eADR records under .engine/contracts/")
        self.assertEqual(entity_eadr, files,
                         "every committed canon record must derive one entity and vice-versa")


class NoForwardEdgeIntoTheCanon(unittest.TestCase):
    def test_no_ratifies_predicate_anywhere(self):
        graph = _graph()
        offenders = [e["id"] for e in graph["entities"] if "ratifies" in (e.get("predicates") or {})]
        self.assertEqual(offenders, [], "the graph must derive no `ratifies` edge (canon is reached on demand)")

    def test_contracts_emit_only_the_two_ownership_edges(self):
        """Each CANON contract entity emits exactly `provided_by` -> the core module and `governed_by` -> the
        contract schema, and nothing else — so the prose cross-references never became graph edges, and no
        `supersedes` (or any other forward edge) is derived out of the canon. Canon is identified by the
        `provided_by` edge: a deployment's per-instance eADR is a `contract` entity with NO `provided_by`, and
        any `supersedes` it carries lives inside its own stream (checked in test_knowledge.py, not here). The
        canon-entity count is cross-checked against the committed canon files, so a canon record that lost its
        `provided_by` edge (mis-reading as non-canon) fails here rather than passing silently."""
        graph = _graph()
        contracts = [e for e in graph["entities"] if e.get("type") == "contract"]
        self.assertTrue(contracts)
        canon = [e for e in contracts if (e.get("predicates") or {}).get("provided_by")]
        self.assertEqual(len(canon), len(_committed_record_ids()),
                         "every committed canon record must derive a canon (provided_by) entity")
        for e in canon:
            preds = e.get("predicates") or {}
            self.assertEqual(set(preds), {"provided_by", "governed_by"},
                             f"{e['id']} must emit only provided_by + governed_by, got {sorted(preds)}")
            self.assertEqual(preds["provided_by"], ["module:core"], e["id"])
            self.assertEqual(preds["governed_by"], ["schema:contract.v1"], e["id"])


class NoOrientationBloat(unittest.TestCase):
    def test_clean_tree_focus_surfaces_no_neighbourhood(self):
        """The steady state a deployed cold boot meets: an empty focus (clean working tree) renders no
        structural neighbourhood at all — so the canon is never pushed into the cold-start pack."""
        src = _source_over(_graph())
        self.assertIsNone(attention.neighborhood_of([], source=src))
        self.assertIsNone(attention.neighborhood_of(None, source=src))

    def test_schema_hub_bounds_the_whole_canon_to_a_counted_sample(self):
        """Touching the contract schema puts `schema:contract.v1` in focus; its incoming `governed_by`
        neighbours are EXACTLY the canon. The render must surface them as a counted sample, never inline."""
        graph = _graph()
        src = _source_over(graph)
        n_records = len([e for e in graph["entities"] if e.get("type") == "contract"])
        self.assertGreaterEqual(n_records, 1)
        nb = attention.neighborhood_of(["schema:contract.v1"], source=src)
        self.assertIsNotNone(nb, "the schema hub must have a neighbourhood")
        gov = [g for g in nb["groups"] if g["predicate"] == "governed_by" and g["direction"] == "in"]
        self.assertEqual(len(gov), 1, "the schema's governed-by-in group is the canon")
        group = gov[0]
        self.assertEqual(group["total"], n_records,
                         "the schema governs exactly the canon — the count is the whole canon")
        self.assertLessEqual(len(group["sample"]), attention.NEIGHBORHOOD_SAMPLE_CAP,
                             "the canon must render as a bounded sample, never an inline dump of all records")

    def test_core_hub_bounds_its_provided_set_to_a_counted_sample(self):
        """Touching the core module manifest puts `module:core` in focus; its provided set (which includes
        the whole canon) must likewise render as a counted sample, never a dump."""
        graph = _graph()
        src = _source_over(graph)
        n_records = len([e for e in graph["entities"] if e.get("type") == "contract"])
        nb = attention.neighborhood_of(["module:core"], source=src)
        self.assertIsNotNone(nb)
        prov = [g for g in nb["groups"] if g["predicate"] == "provided_by" and g["direction"] == "in"]
        self.assertEqual(len(prov), 1, "core's provided-by-in group")
        group = prov[0]
        self.assertGreaterEqual(group["total"], n_records,
                                "core provides at least the whole canon")
        self.assertLessEqual(len(group["sample"]), attention.NEIGHBORHOOD_SAMPLE_CAP,
                             "core's provided set must render as a bounded sample, never an inline dump")


if __name__ == "__main__":
    unittest.main()
