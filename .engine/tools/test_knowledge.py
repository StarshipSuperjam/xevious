#!/usr/bin/env python3
"""Self-tests for the knowledge graph: the knowledge.v1 schema, the generic
catalog-driven generator, the committed graph, and the coverage/fingerprint relay gate.

Run: uv run --directory .engine --frozen -- python -m unittest discover -s tools -p 'test_*.py' -b

These lock the load-bearing teeth: the pure derivation is deterministic and produces well-shaped,
schema-conforming, referentially-intact entities (every predicate target resolves to an entity, ids
are unique, id == type:slug); coverage is TOTAL (every owned engine file under a catalogued surface
has an entity) and the derived-observational graph.json is NOT itself an entity (no recursion); the
conformance strips hold (no debt/finding/session types, no hand-authored predicates); generate->check
round-trips and a hand-edit/absence is REFUSED as a hard finding; the committed graph is in sync with
the live sources (so a forgotten regen fails this suite, not only CI); and the committed
`coverage`/`mode:fingerprint` rule, via validate.kind_coverage, passes the real graph, bites on a
stale/missing graph, fails closed on a broken generator, and the unknown-mode tail is intact.
"""
from __future__ import annotations
import ast
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate          # noqa: E402
import knowledge_gen     # noqa: E402
import hooks             # noqa: E402  (the run_hook harness the commit-boundary regen rides)

KNOWLEDGE_SCHEMA = validate.load_json(os.path.join(validate.SCHEMAS_DIR, "knowledge.v1.json"))
RULE_PATH = os.path.join(validate.CHECK_DIR, "knowledge-coverage.json")
ID_RE = r"^(contract|policy|conduct|schema|check|tool|operation|skill|agent|interface|doc|module):[A-Za-z0-9._-]+$"


def _errors(schema, instance):
    return list(validate.Draft202012Validator(schema).iter_errors(instance))


def _live_entities():
    return knowledge_gen.derive_entities(*knowledge_gen.load_sources())


class TestHelpers(unittest.TestCase):
    def test_slug_is_the_filename_stem(self):
        self.assertEqual(knowledge_gen._slug(".engine/check/state-cursor.json"), "state-cursor")
        self.assertEqual(knowledge_gen._slug(".engine/schemas/check.v1.json"), "check.v1")
        self.assertEqual(knowledge_gen._slug(".engine/tools/validate.py"), "validate")

    def test_instance_slug_uses_the_skill_directory_not_the_filename(self):
        # a skill IS its directory (the file is always SKILL.md, so the stem would collide every skill
        # onto 'SKILL'); every other surface keeps the filename stem.
        self.assertEqual(knowledge_gen._instance_slug("skill", ".claude/skills/engine-help/SKILL.md"),
                         "engine-help")
        self.assertEqual(knowledge_gen._instance_slug("agent", ".claude/agents/reviewer.md"), "reviewer")
        self.assertEqual(knowledge_gen._instance_slug("check", ".engine/check/state-cursor.json"),
                         "state-cursor")
        # a tool PACKAGE marker is always __init__.py, so the bare stem would collide every package onto
        # '__init__'; it is qualified by its package directory so two tool packages stay distinct.
        self.assertEqual(knowledge_gen._instance_slug("tool", ".engine/tools/memory/__init__.py"),
                         "memory.__init__")
        self.assertEqual(knowledge_gen._instance_slug("tool", ".engine/tools/projects_sync/__init__.py"),
                         "projects_sync.__init__")

    def test_surface_for_longest_prefix(self):
        surfaces = {"check": {"location": ".engine/check/"}, "schema": {"location": ".engine/schemas/"}}
        self.assertEqual(knowledge_gen._surface_for(".engine/check/x.json", surfaces), "check")
        self.assertEqual(knowledge_gen._surface_for(".engine/schemas/x.json", surfaces), "schema")
        self.assertIsNone(knowledge_gen._surface_for(".engine/engine.json", surfaces))
        self.assertIsNone(knowledge_gen._surface_for(".engine/knowledge/graph.json", surfaces))


class TestSchema(unittest.TestCase):
    def test_schema_is_well_formed(self):
        validate.Draft202012Validator.check_schema(KNOWLEDGE_SCHEMA)


class TestLiveDerivation(unittest.TestCase):
    """derive_entities over the real catalog/inventory/manifests/claims."""

    def setUp(self):
        self.entities = _live_entities()
        self.by_id = {e["id"]: e for e in self.entities}

    def test_canonical_graph_conforms_to_its_schema(self):
        graph = json.loads(knowledge_gen.canonical_graph())
        self.assertEqual(_errors(KNOWLEDGE_SCHEMA, graph), [])

    def test_ids_are_unique(self):
        ids = [e["id"] for e in self.entities]
        self.assertEqual(len(ids), len(set(ids)), "entity ids must be unique")

    def test_id_equals_type_colon_slug(self):
        for e in self.entities:
            self.assertEqual(e["id"], f'{e["type"]}:{e["slug"]}', e["id"])

    def test_referential_integrity_every_edge_target_resolves(self):
        """The gate cannot catch a consistently-wrong edge (committed == re-derived); this can."""
        ids = set(self.by_id)
        for e in self.entities:
            for kind, targets in (e.get("predicates") or {}).items():
                for t in targets:
                    self.assertIn(t, ids, f"{e['id']} {kind} -> dangling target {t}")

    def test_total_coverage_every_owned_surface_file_has_an_entity(self):
        catalog, manifests, inventory, claims, deployment_contracts = knowledge_gen.load_sources()
        surfaces = catalog.get("surfaces", {})
        expected = {rel for rel in inventory
                    if knowledge_gen._surface_for(rel, surfaces) and claims.get(rel)}
        # The deployment-authored contract stream is entitized too (Pass 1b) though it is deliberately
        # in no module's provides — issue #530: without this union the test fails the moment a deployed
        # repo records its first instance decision under .engine/contracts/instance/.
        expected |= set(deployment_contracts)
        got = {e["source"]["path"] for e in self.entities if e["type"] != "module"}
        self.assertEqual(got, expected)

    def test_surface_instance_inventory_spans_claude_and_excludes_placeholders(self):
        # issue #131: the graph's inventory is the catalog-location walk — owned files under a catalogued
        # surface across .engine/ AND .claude/, with .gitkeep placeholders dropped.
        catalog, _m, inventory, claims, _dc = knowledge_gen.load_sources()
        surfaces = catalog.get("surfaces", {})
        self.assertFalse(any(p.endswith("/.gitkeep") for p in inventory), "no placeholder in the inventory")
        self.assertTrue(any(p.startswith(".claude/skills/") for p in inventory),
                        "engine skills under .claude/ must be in the inventory")
        for p in inventory:                                   # every entry is owned and under a surface
            self.assertTrue(claims.get(p), p)
            self.assertIsNotNone(knowledge_gen._surface_for(p, surfaces), p)

    def test_source_fingerprints_are_sha256(self):
        import re
        for e in self.entities:
            self.assertRegex(e["source"]["fingerprint"], r"^sha256:[0-9a-f]{64}$", e["id"])

    def test_graph_json_is_not_itself_an_entity(self):
        for e in self.entities:
            self.assertNotEqual(e["source"]["path"], ".engine/knowledge/graph.json")

    def test_no_stripped_entity_types_or_predicates(self):
        for e in self.entities:
            self.assertNotIn(e["type"], {"integration-debt", "audit-finding", "session-claim", "feature"})
            self.assertNotIn("pushback_drawers", e.get("predicates") or {})

    def test_known_edges_for_the_state_cursor_check(self):
        # the committed state-cursor check is governed by check.v1 and owned by validators-core (one of
        # the corpus rules). Its target — the state cursor — is a FOUNDATION, not a catalogued surface
        # (issue #24), so the cursor is not a knowledge entity and the check carries no `targets` edge.
        chk = self.by_id.get("check:state-cursor")
        self.assertIsNotNone(chk, "expected a check:state-cursor entity")
        self.assertEqual(chk["predicates"].get("governed_by"), ["schema:check.v1"])
        self.assertEqual(chk["predicates"].get("provided_by"), ["module:validators-core"])
        self.assertNotIn("targets", chk["predicates"])  # the cursor is a foundation, not a surface entity
        # no state:state surface entity exists (state left the catalog); its schema state.v1.json
        # remains a schema-surface entity, governed by the external dialect (no governed_by edge)
        self.assertNotIn("state:state", self.by_id)
        self.assertIn("schema:state.v1", self.by_id)
        # both module entities exist (validators-core was stood up alongside core)
        self.assertIn("module:core", self.by_id)
        self.assertIn("module:validators-core", self.by_id)

    def test_catalog_coverage_rule_and_provisioned_surface_homes(self):
        # issue #30: the catalog-coverage gate is a validators-core corpus rule; the provisioned .engine/
        # surface homes appear as core-owned entities. The `doc` surface gained its grammar
        # (governing_schema null -> doc.v1) and landed its first instance, so its .gitkeep was removed and
        # the doc home is now the governed orientation instance; the `operation` surface gained its grammar,
        # so operation:.gitkeep is now governed by schema:operation.v1.
        cov = self.by_id.get("check:catalog-coverage")
        self.assertIsNotNone(cov, "expected a check:catalog-coverage entity")
        self.assertEqual(cov["predicates"].get("provided_by"), ["module:validators-core"])
        # the doc grammar flip: the placeholder doc:.gitkeep is gone (the first operator doc
        # landed), and the doc home is now the core-provided orientation instance, governed by doc.v1.
        self.assertNotIn("doc:.gitkeep", self.by_id)
        doc_home = self.by_id.get("doc:getting-started")
        self.assertIsNotNone(doc_home, "expected a doc:getting-started entity")
        self.assertEqual(doc_home["predicates"].get("provided_by"), ["module:core"])
        self.assertEqual(doc_home["predicates"].get("governed_by"), ["schema:doc.v1"])
        # the operation surface (its landed instances: boot's SessionStart pack, the modes operation, and
        # close's turn-close operation): the placeholder
        # operation:.gitkeep is gone, and every core-provided lifecycle operation is governed by operation.v1.
        self.assertNotIn("operation:.gitkeep", self.by_id)
        for op_id in ("operation:boot-session-start", "operation:operating-modes", "operation:close-turn"):
            op_home = self.by_id.get(op_id)
            self.assertIsNotNone(op_home, f"expected an {op_id} entity")
            self.assertEqual(op_home["predicates"].get("provided_by"), ["module:core"], op_id)
            self.assertEqual(op_home["predicates"].get("governed_by"), ["schema:operation.v1"], op_id)

    def test_known_edges_for_the_interfaces_and_their_declaration_check(self):
        # each interface declaration is governed by interface.v1 (the catalog
        # governing_schema flip) and provided by core; the interface-declaration check targets BOTH.
        # The non-fingerprint correlate for edge correctness — the fingerprint gate proves the graph
        # MATCHES the surfaces, never that the derived edges are right; this asserts the edges.
        for iid in ("interface:knowledge-retrieval", "interface:search"):
            iface = self.by_id.get(iid)
            self.assertIsNotNone(iface, f"expected an {iid} entity")
            self.assertEqual(iface["predicates"].get("governed_by"), ["schema:interface.v1"], iid)
            self.assertEqual(iface["predicates"].get("provided_by"), ["module:core"], iid)
        self.assertIn("schema:interface.v1", self.by_id)
        chk = self.by_id.get("check:interface-declaration")
        self.assertIsNotNone(chk, "expected a check:interface-declaration entity")
        # the check globs .engine/interfaces/*.json, so its derived `targets` are EVERY declaration
        # (sorted) — each new one widens this edge, the graph delta the operator eyeballs. `memory-control`
        # is deliberately its own declaration rather than more operations on `search`: recall's contract says
        # it never changes what is stored, so the writes could not be declared there without falsifying it.
        self.assertEqual(chk["predicates"].get("targets"),
                         ["interface:knowledge-retrieval", "interface:memory-control", "interface:search"])
        self.assertEqual(chk["predicates"].get("governed_by"), ["schema:check.v1"])

    def test_schema_surface_files_have_no_governed_by(self):
        """A schema file's governing authority is the external 2020-12 dialect (a URI), not an in-repo
        schema entity, so it carries no governed_by edge — by design, not a gap."""
        for e in self.entities:
            if e["type"] == "schema":
                self.assertNotIn("governed_by", e.get("predicates") or {}, e["id"])

    def test_catalog_data_files_are_schema_entities(self):
        """A deliberate, pinned choice: the catalog data file and its meta-schema live under the
        schema surface location, so they appear as schema entities (mechanical/total coverage),
        not silently excluded."""
        self.assertIn("schema:surface-catalog", self.by_id)
        self.assertIn("schema:surface-catalog.schema", self.by_id)

    def test_expected_entities_are_present(self):
        """Concrete spot-checks, independent of the _surface_for oracle the total-coverage test
        reuses — so a classification bug cannot pass both tests."""
        for eid in ("tool:validate", "tool:knowledge_gen", "tool:modes", "tool:close", "schema:check.v1",
                    "schema:knowledge.v1",
                    "check:knowledge-coverage", "check:catalog-coverage", "module:core"):
            self.assertIn(eid, self.by_id, eid)


class TestAttributeHarvesters(unittest.TestCase):
    """The Pass-1 declared-attribute harvesters — pure (IO-free), tested directly on parsed dicts so the
    'declared, not interpreted / structure, not belief' gates are locked independently of any source tree.
    (The Pass-4 `summary` attribute copies a docstring line VERBATIM — mechanical self-description, not an
    interpretation — a distinct carve-out within the same gate, tested in TestPass4Attributes below.)"""

    def test_status_is_modules_and_contracts_only_else_active(self):
        kg = knowledge_gen
        self.assertEqual(kg._status_for("module", {}, {"status": "required"}), "required")
        self.assertEqual(kg._status_for("module", {}, {"status": "experimental"}), "experimental")
        self.assertEqual(kg._status_for("contract", {"status": "superseded"}, None), "superseded")
        # every OTHER surface is 'active' ('else active'), even one that declares a status of its own
        for st in ("policy", "operation", "doc", "conduct", "interface", "check", "schema", "tool"):
            self.assertEqual(kg._status_for(st, {"status": "deprecated"}, None), "active", st)
        # a missing status on the two declaring surfaces degrades to 'active' (never a crash)
        self.assertEqual(kg._status_for("contract", {}, None), "active")   # the .gitkeep trap
        self.assertEqual(kg._status_for("module", {}, {}), "active")

    def test_tier_is_checks_only(self):
        kg = knowledge_gen
        self.assertEqual(kg._tier_for("check", {"tier": "hard"}), "hard")
        self.assertEqual(kg._tier_for("check", {"tier": "soft"}), "soft")
        self.assertIsNone(kg._tier_for("check", {}))                    # malformed check -> None
        self.assertIsNone(kg._tier_for("check", {"tier": "posture"}))   # not a check bite tier
        self.assertIsNone(kg._tier_for("policy", {"tier": "hard"}))     # never from a policy

    def test_title_identity_only_no_slug_fallback(self):
        kg = knowledge_gen
        self.assertEqual(kg._title_for("policy", {"title": "Contract threshold"}), "Contract threshold")
        self.assertEqual(kg._title_for("interface", {"title": "Memory recall"}), "Memory recall")
        self.assertEqual(kg._title_for("skill", {"name": "engine-start"}), "engine-start")
        # excluded surfaces never get a title even when they declare one (purpose/decision clauses)
        self.assertIsNone(kg._title_for("operation", {"title": "Boot the session"}))
        self.assertIsNone(kg._title_for("doc", {"title": "Getting started"}))
        self.assertIsNone(kg._title_for("contract", {"title": "A decision"}))
        # absent / empty -> omit (NO slug fallback)
        self.assertIsNone(kg._title_for("policy", {}))
        self.assertIsNone(kg._title_for("policy", {"title": "   "}))

    def test_title_shape_guard_rejects_purpose_clauses_and_imperatives(self):
        kg = knowledge_gen
        for bad in ("Operating modes — the session stance", "Knowledge graph - retrieval",
                    "Start building now", "Set up your project", "Do this. Then that",
                    "Scope: the worker role", "Shape how I work with you"):
            self.assertIsNone(kg._title_for("policy", {"title": bad}), bad)
        for ok in ("Attention", "Contract threshold", "Knowledge graph retrieval", "Finding disposition"):
            self.assertEqual(kg._title_for("policy", {"title": ok}), ok)

    def test_discriminators_per_surface_with_sorted_lists(self):
        kg = knowledge_gen
        self.assertEqual(kg._discriminators_for("check", {}, {"kind": "shape", "suites": ["pr", "CI"]}, None),
                         {"kind": "shape", "suites": ["CI", "pr"]})            # suites sorted
        self.assertEqual(kg._discriminators_for("interface", {},
                         {"operations": [{"name": "neighbors"}, {"name": "find"}],
                          "fallback": {"handle": "engine-x"}}, None),
                         {"operations": ["find", "neighbors"], "fallback": "engine-x"})  # op names sorted
        self.assertEqual(kg._discriminators_for("agent", {"role": "worker", "model-tier": "judgment"}, {}, None),
                         {"role": "worker", "model-tier": "judgment"})
        self.assertEqual(kg._discriminators_for("skill", {"invocation": "operator-typed"}, {}, None),
                         {"invocation": "operator-typed"})
        self.assertEqual(kg._discriminators_for("module", {}, {}, {"version": "1.4.0"}), {"version": "1.4.0"})
        self.assertEqual(kg._discriminators_for("policy", {"title": "x"}, {}, None), {})  # none for a policy


class TestImportResolver(unittest.TestCase):
    """The Pass-4 import resolver — pure over a SYNTHETIC module index, so the dangling-import loud-fail and
    the package / module / re-export resolution are locked independently of the live tree."""

    def setUp(self):
        self.kg = knowledge_gen
        # a synthetic tool tree: top-level module `top`; package `pkg` with submodules `sub`/`helper`, a
        # re-exported symbol `shared`, and a nested package `pkg.deep` with a module `leaf`.
        self.index = (
            {("pkg",), ("pkg", "deep")},                                             # packages
            {("top",), ("pkg", "sub"), ("pkg", "helper"), ("pkg", "deep", "leaf")},  # modules
            {("pkg",): frozenset({"shared"}), ("pkg", "deep"): frozenset()},         # init symbols
        )
        self.root = ".engine/tools"

    def _resolve(self, src, source_rel="t.py"):
        return self.kg._resolve_tool_imports(source_rel, ast.parse(src), self.index, self.root)

    def test_bare_module_and_package_imports(self):
        self.assertEqual(self._resolve("import top"), [".engine/tools/top.py"])
        self.assertEqual(self._resolve("import pkg"), [".engine/tools/pkg/__init__.py"])   # package -> __init__
        self.assertEqual(self._resolve("import pkg.sub"), [".engine/tools/pkg/sub.py"])
        self.assertEqual(self._resolve("import pkg.deep.leaf"), [".engine/tools/pkg/deep/leaf.py"])

    def test_from_package_import_submodule_resolves_to_the_submodule(self):
        self.assertEqual(self._resolve("from pkg import sub"), [".engine/tools/pkg/sub.py"])
        self.assertEqual(self._resolve("from pkg.deep import leaf"), [".engine/tools/pkg/deep/leaf.py"])

    def test_from_package_import_reexported_symbol_resolves_to_the_package(self):
        # `shared` is not a submodule but IS declared in pkg/__init__ -> edge to the package init.
        self.assertEqual(self._resolve("from pkg import shared"), [".engine/tools/pkg/__init__.py"])

    def test_from_module_import_names_edges_to_the_module(self):
        # `top` is a module file; its imported names are attributes -> one edge, no per-name probe.
        self.assertEqual(self._resolve("from top import a, b, c"), [".engine/tools/top.py"])

    def test_stdlib_and_external_are_dropped(self):
        self.assertEqual(self._resolve("import os\nimport json\nfrom collections import Counter\nimport numpy"), [])

    def test_lazy_in_function_import_is_counted(self):
        self.assertEqual(self._resolve("def f():\n    import top\n    return top"), [".engine/tools/top.py"])

    def test_relative_imports_resolve_against_the_source_package(self):
        # a relative import is resolved against the importing file's own package (no silent in-repo miss), not
        # skipped. Source lives in the `pkg` package.
        pkg_mod = ".engine/tools/pkg/mod.py"
        self.assertEqual(self._resolve("from . import sub", pkg_mod), [".engine/tools/pkg/sub.py"])
        self.assertEqual(self._resolve("from .sub import x", pkg_mod), [".engine/tools/pkg/sub.py"])
        self.assertEqual(self._resolve("from .deep import leaf", pkg_mod), [".engine/tools/pkg/deep/leaf.py"])
        self.assertEqual(self._resolve("from . import shared", pkg_mod), [".engine/tools/pkg/__init__.py"])
        # climbs above the tools root -> not an in-repo target, dropped (not raised)
        self.assertEqual(self._resolve("from ... import x", pkg_mod), [])
        # a relative import that resolves to nothing in-repo raises loud, like any dangling import — and the
        # message renders the spec cleanly (one dot, not two).
        with self.assertRaises(self.kg.DanglingImportError) as cm:
            self._resolve("from . import ghost", pkg_mod)
        self.assertIn(".ghost", str(cm.exception))
        self.assertNotIn("..ghost", str(cm.exception))

    def test_star_reexport_in_init_resolves_instead_of_raising(self):
        # a package whose __init__ does `from .x import *` may expose any name; an otherwise-unresolvable
        # `from p import anything` edges to the package rather than false-raising. (No tools package uses star
        # imports today; this guards the future.)
        index = ({("p",)}, {("p", "x")}, {("p",): frozenset({"*"})})
        out = self.kg._resolve_tool_imports("t.py", ast.parse("from p import anything"), index, ".engine/tools")
        self.assertEqual(out, [".engine/tools/p/__init__.py"])

    def test_dangling_from_name_raises_loud(self):
        with self.assertRaises(self.kg.DanglingImportError):
            self._resolve("from pkg import does_not_exist")

    def test_dangling_from_module_raises_loud(self):
        # head `pkg` is in-repo but `pkg.ghost` is neither a package nor a module.
        with self.assertRaises(self.kg.DanglingImportError):
            self._resolve("from pkg.ghost import x")

    def test_dangling_dotted_import_raises_loud(self):
        with self.assertRaises(self.kg.DanglingImportError):
            self._resolve("import pkg.ghost")

    def test_importing_a_submodule_of_a_plain_module_is_dangling(self):
        # `top` is a module, not a package, so `top.deeper` cannot exist.
        with self.assertRaises(self.kg.DanglingImportError):
            self._resolve("import top.deeper")

    def test_dangling_error_is_a_valueerror_and_names_the_file_and_import(self):
        # a ValueError subclass so the CLI + CI fingerprint gate catch it on their existing fail-closed paths.
        try:
            self._resolve("from pkg import ghost")
            self.fail("expected DanglingImportError")
        except self.kg.DanglingImportError as e:
            self.assertIsInstance(e, ValueError)
            self.assertIn("t.py", str(e))
            self.assertIn("pkg.ghost", str(e))


class TestPass4Attributes(unittest.TestCase):
    """The Pass-4 per-tool attribute + wiring harvesters (pure). `summary` copies the file's own module
    docstring first line VERBATIM — mechanical self-description, not an interpretation of what the code means,
    so it clears the same 'declared, not belief' gate `title` does."""

    def setUp(self):
        self.kg = knowledge_gen

    def test_summary_is_the_first_docstring_line_verbatim(self):
        t = ast.parse('"""First line of the summary.\nSecond line, ignored."""\nx = 1')
        self.assertEqual(self.kg._summary_for(t), "First line of the summary.")

    def test_summary_collapses_whitespace_and_strips_control_chars(self):
        # a tab is collapsed as whitespace; non-line-break C0 controls (bell, escape) are stripped. (NUL and
        # the line-break controls \x0b/\x0c can't appear inline in a docstring first line — the former is
        # illegal in Python source, the latter start a new line, which splitlines already ends the line at.)
        t = ast.parse('"""A\tsummary\x07 with  control\x1b chars and   spaces."""')
        self.assertEqual(self.kg._summary_for(t), "A summary with control chars and spaces.")

    def test_summary_strips_bidi_zerowidth_and_c1_controls(self):
        # the scrub drops format/control chars beyond C0 — a bidi override (RLO), a zero-width space, DEL, and
        # a C1 control — so none can ride invisible or direction-flipping text into the committed graph.
        t = ast.parse('"""safe\u202etext\u200b here\x7f and\x9b more."""')
        s = self.kg._summary_for(t)
        for bad in ("\u202e", "\u200b", "\x7f", "\x9b"):
            self.assertNotIn(bad, s)
        self.assertEqual(s, "safetext here and more.")

    def test_parse_tool_ast_returns_none_on_malformed_source(self):
        # the malformed-.py skip path: a broken tool parses to None (so it still entitizes with `guarded` but
        # harvests no imports/summary/entrypoint), a good one parses to a tree; a read error is NOT swallowed.
        with tempfile.TemporaryDirectory() as d:
            good, bad = os.path.join(d, "good.py"), os.path.join(d, "bad.py")
            with open(good, "w", encoding="utf-8") as fh:
                fh.write("x = 1\n")
            with open(bad, "w", encoding="utf-8") as fh:
                fh.write("def (:\n")
            self.assertIsNotNone(self.kg._parse_tool_ast(good))
            self.assertIsNone(self.kg._parse_tool_ast(bad))

    def test_summary_truncates_to_160_chars(self):
        t = ast.parse('"""' + ("word " * 60).strip() + '"""')
        self.assertLessEqual(len(self.kg._summary_for(t)), 160)

    def test_summary_none_without_a_docstring(self):
        self.assertIsNone(self.kg._summary_for(ast.parse("x = 1")))
        self.assertIsNone(self.kg._summary_for(ast.parse('"""   """')))

    def test_has_main_guard(self):
        self.assertTrue(self.kg._has_main_guard(ast.parse('if __name__ == "__main__":\n    pass')))
        self.assertFalse(self.kg._has_main_guard(ast.parse('def main():\n    pass')))

    def test_entrypoint_precedence(self):
        kg = self.kg
        hook, mcp, ci = {".engine/tools/boot.py"}, {".engine/tools/x_mcp.py"}, {".engine/tools/y_check.py"}
        empty, main = ast.parse(""), ast.parse('if __name__ == "__main__":\n    pass')
        self.assertEqual(kg._entrypoint_for(".engine/tools/test_x.py", empty, hook, mcp, ci), "test")
        self.assertEqual(kg._entrypoint_for(".engine/tools/demo_x.py", empty, hook, mcp, ci), "demo")
        self.assertEqual(kg._entrypoint_for(".engine/tools/boot.py", empty, hook, mcp, ci), "hook")
        self.assertEqual(kg._entrypoint_for(".engine/tools/x_mcp.py", empty, hook, mcp, ci), "mcp-server")
        self.assertEqual(kg._entrypoint_for(".engine/tools/y_check.py", empty, hook, mcp, ci), "ci")
        self.assertEqual(kg._entrypoint_for(".engine/tools/z.py", main, hook, mcp, ci), "cli")
        self.assertEqual(kg._entrypoint_for(".engine/tools/z.py", empty, hook, mcp, ci), "library")
        # a name convention outranks a wiring set (a test_ file wired as a hook is still 'test').
        self.assertEqual(kg._entrypoint_for(".engine/tools/test_boot.py", empty,
                                            {".engine/tools/test_boot.py"}, mcp, ci), "test")

    def test_hook_wired_tools_extracts_py_payload_and_excludes_the_launcher(self):
        m = {"id": "core", "wires": [
            {"type": "hook", "hook": {"command":
                'sh "/x/.engine/tools/hook-runner.sh" "/x/py" "/x/.engine/tools/boot.py" startup'}},
            {"type": "mcp", "hook": {"command": "/x/.engine/tools/should_not_count.py"}},
        ]}
        self.assertEqual(self.kg._hook_wired_tools([("core/manifest.json", m)]),
                         {"core": [".engine/tools/boot.py"]})

    def test_mcp_handle_to_tool_from_the_real_manifest(self):
        h2t = self.kg._mcp_handle_to_tool(os.path.join(validate.ROOT, ".mcp.json"))
        self.assertEqual(h2t.get("engine-knowledge-graph"), ".engine/tools/knowledge_mcp_server.py")

    def test_guard_canary_fails_loud_when_the_classifier_blankets(self):
        # if the guardrail classifier collapses to its blanket fail-safe (every tool path reads guarded),
        # derive_entities REFUSES rather than baking an all-true `guarded` into the committed graph.
        with mock.patch.object(knowledge_gen.weakening_guard, "is_guardrail", return_value=True):
            with self.assertRaises(ValueError):
                _live_entities()


class TestPass4LiveEdges(unittest.TestCase):
    """Pass-4 edges + attributes over the REAL graph — the non-fingerprint correlate that the DERIVED edges
    are correct (the fingerprint gate proves only that the graph matches its sources, never that its edges
    are right)."""

    def setUp(self):
        self.by_id = {e["id"]: e for e in _live_entities()}

    def test_imports_are_real_dependency_edges_no_self_edge(self):
        boot = self.by_id["tool:boot"]
        self.assertIn("tool:attention", boot["predicates"]["imports"])
        self.assertIn("tool:boot_slice", boot["predicates"]["imports"])
        self.assertNotIn("tool:boot", boot["predicates"].get("imports", []))

    def test_a_test_source_routes_to_tests_never_imports(self):
        tk = self.by_id["tool:test_knowledge"]
        self.assertIn("tool:knowledge_gen", tk["predicates"]["tests"])
        self.assertNotIn("imports", tk["predicates"])

    def test_enforced_by_resolves_a_script_check_to_its_tool(self):
        self.assertEqual(self.by_id["check:knowledge-vocabulary"]["predicates"]["enforced_by"],
                         ["tool:knowledge_vocabulary_check"])

    def test_wires_hook_names_payload_tools_not_the_launcher(self):
        wired = self.by_id["module:core"]["predicates"]["wires_hook"]
        self.assertIn("tool:boot", wired)
        self.assertNotIn("tool:hook-runner", wired)          # the shared .sh launcher is not a payload

    def test_implemented_by_joins_interface_to_its_fallback_tool(self):
        self.assertEqual(self.by_id["interface:knowledge-retrieval"]["predicates"]["implemented_by"],
                         ["tool:knowledge_mcp_server"])

    def test_guarded_is_a_bool_on_every_tool_including_non_py(self):
        for e in self.by_id.values():
            if e["type"] == "tool":
                self.assertIsInstance(e.get("guarded"), bool, e["id"])
        self.assertTrue(self.by_id["tool:hook-runner"]["guarded"])    # a floored .sh launcher
        self.assertFalse(self.by_id["tool:boot"]["guarded"])

    def test_summary_and_entrypoint_are_py_tool_only(self):
        boot = self.by_id["tool:boot"]
        self.assertTrue(boot["summary"].startswith("boot:"))
        self.assertEqual(boot["entrypoint"], "hook")
        sh = self.by_id["tool:hook-runner"]                  # a .sh tool: guarded only
        self.assertNotIn("summary", sh)
        self.assertNotIn("entrypoint", sh)


class TestSupersedesEdges(unittest.TestCase):
    """The supersedes edge guard (the canon invariant): contract->contract, DEPLOYMENT-STREAM
    only — no edge may EVER reach a canon eADR. Canon-ness is provides-membership (modelled as canon_ids)."""

    @staticmethod
    def _contract(eid):
        return {"id": eid, "type": "contract", "name": eid, "slug": eid.split(":", 1)[1],
                "source": {"path": f"x/{eid}.md", "fingerprint": "sha256:" + "0" * 64},
                "owner": "core", "predicates": {}}

    def _pair(self):
        a, b = self._contract("contract:eADR-0002"), self._contract("contract:eADR-0001")
        fm = {"contract:eADR-0002": {"id": "eADR-0002", "supersedes": "eADR-0001"},
              "contract:eADR-0001": {"id": "eADR-0001"}}
        return [a, b], fm

    def test_deployment_to_deployment_emits_the_edge(self):
        ents, fm = self._pair()
        self.assertEqual(knowledge_gen._supersedes_edges(ents, fm, canon_ids=set()),
                         {"contract:eADR-0002": ["contract:eADR-0001"]})

    def test_a_canon_target_is_never_reached(self):
        ents, fm = self._pair()
        self.assertEqual(knowledge_gen._supersedes_edges(ents, fm, canon_ids={"contract:eADR-0001"}), {})

    def test_a_canon_source_emits_nothing(self):
        ents, fm = self._pair()
        self.assertEqual(knowledge_gen._supersedes_edges(
            ents, fm, canon_ids={"contract:eADR-0001", "contract:eADR-0002"}), {})

    def test_dangling_or_self_target_emits_nothing(self):
        a = self._contract("contract:eADR-0002")
        self.assertEqual(knowledge_gen._supersedes_edges(
            [a], {"contract:eADR-0002": {"id": "eADR-0002", "supersedes": "eADR-9999"}}, set()), {})
        self.assertEqual(knowledge_gen._supersedes_edges(
            [a], {"contract:eADR-0002": {"id": "eADR-0002", "supersedes": "eADR-0002"}}, set()), {})


class TestDeploymentStreamEntitization(unittest.TestCase):
    """#422: `derive_entities` entitizes the deployment-owned per-instance eADR stream
    (`.engine/contracts/instance/*eADR-*.md` — bare or project-namespaced `<slug>-eADR-####` after #467, in NO
    module's `provides`) as NON-canon contracts — owner is
    the reserved token `deployment`, they carry NO `provided_by` edge, and that absence (not `owner`) is what
    canon detection reads — which makes the `supersedes` leg LIVE for a deployment's own decisions. Driven
    through the REAL generate -> schema-validate -> build_index path, because the construction repo carries no
    instance eADRs, so the live graph never contains one and a pure-function fixture cannot exercise the break
    the plan gate flagged (owner-required schema + a NOT-NULL / subscript index insert)."""

    _CATALOG = {"surfaces": {"contract": {"class": "prose", "location": ".engine/contracts/",
                                          "governing_schema": "contract.v1.json",
                                          "template": "../templates/contract.md"}}}

    @staticmethod
    def _write_eadr(root, rel, eid, supersedes=None):
        p = os.path.join(root, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        lines = ["---", f"id: {eid}", f"title: {eid} decision", "status: accepted", "date: 2026-07-12"]
        if supersedes:
            lines.append(f"supersedes: {supersedes}")
        lines += ["---", "", "## Decision", "", "A decision.", ""]
        with open(p, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines))
        return rel

    def _derive(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = tmp.name
        # a canon eADR (owned, via Pass 1) AND a deployment eADR with the SAME file stem — the collision the
        # id-qualify must survive (an unqualified id would silently overwrite one in the entities dict).
        canon = self._write_eadr(root, ".engine/contracts/eADR-0001-foo.md", "eADR-0001")
        self._write_eadr(root, ".engine/contracts/instance/eADR-0001-foo.md", "eADR-1001")
        newer = self._write_eadr(root, ".engine/contracts/instance/eADR-1002-newer.md", "eADR-1002",
                                 supersedes="eADR-1003")
        older = self._write_eadr(root, ".engine/contracts/instance/eADR-1003-older.md", "eADR-1003")
        # a README under instance/ must NOT be entitized (the walk is eADR-*; it is not in deployment_contracts)
        deployment = [".engine/contracts/instance/eADR-0001-foo.md", newer, older]
        with mock.patch.object(validate, "ROOT", root):
            ents = knowledge_gen.derive_entities(
                self._CATALOG, [], [canon], {canon: ["core"]}, deployment_contracts=deployment)
        return ents

    def _by_id(self):
        return {e["id"]: e for e in self._derive()}

    def test_deployment_eadrs_are_non_canon_entities_distinct_from_a_same_stem_canon(self):
        by_id = self._by_id()
        canon = by_id["contract:eADR-0001-foo"]                     # canon: owned, provided_by
        self.assertEqual(canon["owner"], "core")
        self.assertIn("provided_by", canon["predicates"])
        dep = by_id["contract:instance.eADR-0001-foo"]             # deployment same-stem: DISTINCT id
        self.assertEqual(dep["owner"], "deployment")               # the reserved non-module token
        self.assertNotIn("provided_by", dep["predicates"])         # non-canon signal
        self.assertIn("governed_by", dep["predicates"])            # still governed by contract.v1
        self.assertNotEqual(canon["source"]["path"], dep["source"]["path"])   # both survived, no overwrite

    def test_supersedes_leg_is_live_for_the_deployment_stream(self):
        by_id = self._by_id()
        self.assertEqual(by_id["contract:instance.eADR-1002-newer"]["predicates"].get("supersedes"),
                         ["contract:instance.eADR-1003-older"])
        self.assertNotIn("supersedes", by_id["contract:eADR-0001-foo"]["predicates"])   # canon never emits

    def test_the_derived_graph_conforms_to_the_schema_with_deployment_entities(self):
        graph = json.loads(knowledge_gen.render_graph(self._derive()))
        self.assertEqual(_errors(KNOWLEDGE_SCHEMA, graph), [],
                         "a deployment entity (owner='deployment', no provided_by) must be schema-valid")

    def test_the_query_index_builds_over_deployment_entities_and_lists_them(self):
        import knowledge_index
        import knowledge_query
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        graph_path = os.path.join(tmp.name, "graph.json")
        with open(graph_path, "w", encoding="utf-8") as fh:
            fh.write(knowledge_gen.render_graph(self._derive()))
        idx = os.path.join(tmp.name, "index.sqlite")
        # the owner-less-entity crash the plan gate flagged would surface HERE (NOT NULL / e["owner"] subscript)
        _ipath, source = knowledge_index.build_index(index_path=idx, graph_path=graph_path)
        self.assertEqual(source, "committed")
        # pass the same graph_path so the freshness check reads THIS index (not a rebuild from the real graph).
        found = knowledge_query.find(owner="deployment", index_path=idx, graph_path=graph_path)
        self.assertEqual(len(found), 3, "`find --owner deployment` lists a deployment's own eADRs")

    def test_absent_instance_dir_yields_no_deployment_entities(self):
        # fail-safe: a deployed repo may never create instance/; the walk must return [] not raise.
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        with mock.patch.object(validate, "ROOT", tmp.name):
            self.assertEqual(knowledge_gen.deployment_contract_inventory(), [])

    # ---- #467: a PROJECT-NAMESPACED deployment record (`<project-slug>-eADR-####`) entitizes and coexists.

    def test_the_widened_inventory_glob_matches_a_namespaced_record(self):
        # deployment_contract_inventory's glob widened `instance/eADR-*` -> `instance/*eADR-*` (#467), so it now
        # picks up the project-namespaced filename while still excluding the README (no `eADR` in its name).
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self._write_eadr(tmp.name, ".engine/contracts/instance/acme-eADR-0007-foo.md", "acme-eADR-0007")
        os.makedirs(os.path.join(tmp.name, ".engine", "contracts", "instance"), exist_ok=True)
        with open(os.path.join(tmp.name, ".engine/contracts/instance/README.md"), "w", encoding="utf-8") as fh:
            fh.write("# guide\n")
        with mock.patch.object(validate, "ROOT", tmp.name):
            inv = knowledge_gen.deployment_contract_inventory()
        self.assertIn(".engine/contracts/instance/acme-eADR-0007-foo.md", inv)
        self.assertNotIn(".engine/contracts/instance/README.md", inv)

    def test_a_namespaced_deployment_eadr_entitizes_as_non_canon(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        rel = self._write_eadr(tmp.name, ".engine/contracts/instance/acme-eADR-0007-foo.md", "acme-eADR-0007")
        with mock.patch.object(validate, "ROOT", tmp.name):
            ents = knowledge_gen.derive_entities(self._CATALOG, [], [], {}, deployment_contracts=[rel])
        dep = {e["id"]: e for e in ents}["contract:instance.acme-eADR-0007-foo"]   # folder-qualified id
        self.assertEqual(dep["owner"], "deployment")
        self.assertNotIn("provided_by", dep["predicates"])     # non-canon
        self.assertIn("governed_by", dep["predicates"])        # still governed by contract.v1

    def test_canon_and_deployment_same_number_coexist_without_collision(self):
        # #467 acceptance: a canon `eADR-0034` and a deployment `acme-eADR-0034` — the SAME number — coexist as
        # DISTINCT entities with no bare-token collision (distinct ids, distinct paths, deployment non-canon).
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        canon = self._write_eadr(tmp.name, ".engine/contracts/eADR-0034-x.md", "eADR-0034")
        dep = self._write_eadr(tmp.name, ".engine/contracts/instance/acme-eADR-0034-x.md", "acme-eADR-0034")
        with mock.patch.object(validate, "ROOT", tmp.name):
            ents = knowledge_gen.derive_entities(self._CATALOG, [], [canon], {canon: ["core"]},
                                                 deployment_contracts=[dep])
        by_id = {e["id"]: e for e in ents}
        c, d = by_id["contract:eADR-0034-x"], by_id["contract:instance.acme-eADR-0034-x"]
        self.assertIn("provided_by", c["predicates"])          # canon
        self.assertNotIn("provided_by", d["predicates"])       # deployment non-canon
        self.assertNotEqual(c["id"], d["id"])                  # distinct entity ids — no collision
        self.assertNotEqual(c["source"]["path"], d["source"]["path"])


class TestLiveDerivationAttributes(unittest.TestCase):
    """The declared attributes on the REAL derived graph — the non-fingerprint correlate that the harvest is
    RIGHT (the gate proves the committed graph MATCHES the sources, never that the values are correct)."""

    def setUp(self):
        self.by_id = {e["id"]: e for e in _live_entities()}

    def test_check_carries_tier_kind_suites_status_and_no_title(self):
        c = self.by_id["check:catalog-coverage"]
        self.assertEqual((c.get("tier"), c.get("kind"), c.get("suites"), c.get("status")),
                         ("hard", "coverage", ["CI"], "active"))
        self.assertNotIn("title", c)
        self.assertEqual(self.by_id["check:conduct-weakening-guard"].get("tier"), "soft")

    def test_policy_and_interface_carry_title_and_discriminators(self):
        self.assertEqual(self.by_id["policy:attention"].get("title"), "Attention")
        i = self.by_id["interface:knowledge-retrieval"]
        self.assertEqual(i.get("title"), "Knowledge graph retrieval")
        self.assertEqual(i.get("operations"), ["find", "get-entity", "health", "neighbors", "relate"])
        self.assertEqual(i.get("fallback"), "engine-knowledge-graph")

    def test_module_carries_status_and_version(self):
        m = self.by_id["module:core"]
        # The derived module entity carries the module's REAL declared version. Read the expected value from
        # the source of truth (core's manifest) rather than pinning the construction-time sentinel, so this
        # attribute test stays correct after a release cut bumps the version (it does not — the whole point of
        # a cut is that this version moves off 0.0.0-dev, and a hardcoded sentinel here would fail every cut).
        core_version = validate.load_json(
            os.path.join(validate.ROOT, ".engine/modules/core/manifest.json"))["version"]
        self.assertEqual((m.get("status"), m.get("version")), ("required", core_version))

    def test_every_entity_has_status_and_tier_and_title_are_well_scoped(self):
        for e in self.by_id.values():
            self.assertIn("status", e, e["id"])
            if "tier" in e:
                self.assertEqual(e["type"], "check", e["id"])
            if "title" in e:
                self.assertIn(e["type"], ("policy", "interface", "skill"), e["id"])  # skill.name is a title

    def test_canon_never_emits_supersedes_in_the_live_graph(self):
        # CANON never emits a supersedes edge — the structural invariant (`knowledge.v1.json` scopes the
        # predicate to the deployment stream; the derivation suppresses canon on both sides). Asserted over
        # the canon SLICE, not the whole graph: a deployment whose own records supersede one another emits
        # the edge legitimately, so asserting the graph carries none would assert a fact about whichever
        # repo the suite runs in and red a deployment's required self-tests for using the feature.
        canon = [e for e in self.by_id.values() if "provided_by" in e["predicates"]]
        self.assertTrue(canon, "the live graph must carry canon entities for this assertion to mean anything")
        self.assertFalse(any("supersedes" in e["predicates"] for e in canon),
                         "no canon entity emits a supersedes edge in the live graph")

    def test_no_placeholder_is_entitized(self):
        # issue #131: a .gitkeep is a directory placeholder, not a ratified instance — it must never appear
        # as an entity (the old graph wrongly carried contract:.gitkeep as the lone 'contract').
        leaked = [e["id"] for e in self.by_id.values() if e["source"]["path"].endswith("/.gitkeep")
                  or e["source"]["path"] == ".gitkeep"]
        self.assertEqual(leaked, [], f"placeholder entities leaked into the graph: {leaked}")
        self.assertNotIn("contract:.gitkeep", self.by_id)

    def test_engine_owned_skills_are_entitized_with_distinct_ids(self):
        # issue #131: the engine's own skills live under .claude/skills/ — a catalogued surface — and must
        # appear in the graph, one entity per skill directory (not collapsed onto a single skill:SKILL).
        # Derive the expected set from ownership so this does not rot when the next engine skill lands.
        _catalog, manifests, _inv, claims, _dc = knowledge_gen.load_sources()
        expected = {f"skill:{os.path.basename(os.path.dirname(rel))}"
                    for rel in claims
                    if rel.startswith(".claude/skills/") and rel.endswith("/SKILL.md")}
        got = {e["id"] for e in self.by_id.values() if e["type"] == "skill"}
        self.assertEqual(got, expected)
        self.assertGreaterEqual(len(got), 6, "the engine ships several engine-* skills")
        self.assertIn("skill:engine-help", got)


class TestCommittedGraph(unittest.TestCase):
    """The committed .engine/knowledge/graph.json — conforms to its schema and is in sync with the
    live sources (so a forgotten regenerate fails THIS suite, not only CI)."""

    def test_committed_graph_conforms(self):
        committed = knowledge_gen.read_committed(knowledge_gen.GRAPH_PATH)
        self.assertIsNotNone(committed, "the knowledge graph must be generated and committed")
        self.assertEqual(_errors(KNOWLEDGE_SCHEMA, json.loads(committed)), [])

    def test_committed_graph_is_in_sync_with_sources(self):
        f = knowledge_gen.check()
        self.assertEqual(f["severity"], "note",
                         "the committed graph is stale — run knowledge_gen.py generate and commit")


class TestGenerateCheckIO(unittest.TestCase):
    """Real sources + a redirected committed path in a temp dir."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self._tmp.name, "graph.json")

    def tearDown(self):
        self._tmp.cleanup()

    def test_write_graph_is_atomic_and_leaves_no_committable_orphan(self):
        # write_graph writes a temp then os.replace -> the target is byte-identical (newline=''
        # preserved) and no orphan temp is left beside it; the transient temp lives in the gitignored,
        # ownership-pruned .cache/ dir, never a sibling of graph.json that could be committed.
        text = knowledge_gen.canonical_graph()
        knowledge_gen.write_graph(text, self.path)
        with open(self.path, encoding="utf-8", newline="") as fh:
            self.assertEqual(fh.read(), text)             # byte-identical round-trip
        # the only entries in the dir are the target and the .cache/ scratch dir — no *.building.* orphan
        self.assertEqual(set(os.listdir(self._tmp.name)) - {"graph.json", ".cache"}, set())
        # a second write overwrites cleanly (proves os.replace, not append)
        knowledge_gen.write_graph(text, self.path)
        with open(self.path, encoding="utf-8", newline="") as fh:
            self.assertEqual(fh.read(), text)

    def test_generate_then_check_is_in_sync(self):
        # Freeze ONE live derivation so generate() and the following check() compare the same content
        # (check() re-derives canonical_graph() internally). Re-deriving per call made this depend on the
        # live source tree staying byte-stable across the two calls, which flaked under full-suite
        # discovery (#202); the two-derivations-agree property is the job of TestSourceDeterminismRoundTrip.
        snapshot = knowledge_gen.canonical_graph()
        with mock.patch.object(knowledge_gen, "canonical_graph", return_value=snapshot):
            g = knowledge_gen.generate(self.path)
            self.assertEqual(g["severity"], "note")
            self.assertIn("Wrote", g["message"])
            self.assertEqual(knowledge_gen.check(self.path)["severity"], "note")

    def test_generate_is_idempotent(self):
        # Freeze ONE live derivation so the two back-to-back generate() calls compare identical content.
        # This test is about generate()'s idempotency/round-trip logic (write -> re-read -> "no change"),
        # not about whether two independent live derivations agree — re-deriving canonical_graph() per call
        # made it flake under full-suite discovery (#202). The real write/read round-trip still runs on
        # both calls, so a lossy round-trip is still caught; the determinism property itself is covered by
        # TestSourceDeterminismRoundTrip (left untouched).
        snapshot = knowledge_gen.canonical_graph()
        with mock.patch.object(knowledge_gen, "canonical_graph", return_value=snapshot):
            first = knowledge_gen.generate(self.path)
            again = knowledge_gen.generate(self.path)
        self.assertIn("Wrote", first["message"])
        self.assertIn("already up to date", again["message"])

    def test_hand_edit_is_caught_as_drift(self):
        knowledge_gen.generate(self.path)
        with open(self.path, "a", encoding="utf-8", newline="") as fh:
            fh.write("junk a human typed\n")
        self.assertEqual(knowledge_gen.check(self.path)["severity"], "hard")

    def test_absent_graph_is_caught(self):
        self.assertEqual(knowledge_gen.check(os.path.join(self._tmp.name, "nope.json"))["severity"], "hard")

    def test_drift_tier_is_the_callers_tier(self):
        knowledge_gen.generate(self.path)
        with open(self.path, "a", encoding="utf-8", newline="") as fh:
            fh.write("junk\n")
        self.assertEqual(knowledge_gen.check(self.path, tier="soft")["severity"], "soft")


class TestCoverageFingerprintMode(unittest.TestCase):
    """The committed rule, dispatched through validate.kind_coverage (no _run_kind stub needed — the
    fingerprint mode ignores target_files and re-derives from the catalog)."""

    def _rule(self):
        return validate.load_json(RULE_PATH)

    def test_rule_is_well_formed_and_joins_ci(self):
        check_schema = validate.load_json(os.path.join(validate.SCHEMAS_DIR, "check.v1.json"))
        rule = self._rule()
        self.assertEqual(_errors(check_schema, rule), [])
        self.assertIn("CI", rule.get("suites", []))
        self.assertEqual(rule["params"]["mode"], "fingerprint")
        self.assertEqual(rule["target"], {"context": "knowledge-fingerprint"})

    def test_real_graph_passes_via_the_mode(self):
        passed, found = validate.kind_coverage(self._rule(), {})
        self.assertTrue(passed)
        self.assertEqual(found, [])

    def test_stale_committed_graph_is_hard(self):
        # mock.patch.object guarantees GRAPH_PATH is restored even on an unexpected path.
        with tempfile.TemporaryDirectory() as d:
            stale = os.path.join(d, "graph.json")
            knowledge_gen.write_graph('{"schema_version": 1, "entities": []}\n', stale)
            with mock.patch.object(knowledge_gen, "GRAPH_PATH", stale):
                passed, found = validate.kind_coverage(self._rule(), {})
        self.assertFalse(passed)
        self.assertTrue(any(f["severity"] == "hard" for f in found))
        self.assertTrue(any("out of date" in f["message"] for f in found))

    def test_missing_committed_graph_is_hard(self):
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(knowledge_gen, "GRAPH_PATH", os.path.join(d, "nope.json")):
                passed, found = validate.kind_coverage(self._rule(), {})
        self.assertFalse(passed)
        self.assertTrue(any(f["severity"] == "hard" for f in found))

    def test_broken_generator_fails_closed(self):
        def boom(*a, **k):
            raise RuntimeError("simulated generator failure")

        with mock.patch.object(knowledge_gen, "check", boom):
            passed, found = validate.kind_coverage(self._rule(), {})
        self.assertFalse(passed)
        self.assertTrue(any(f["severity"] == "hard" for f in found))

    def test_unrecognized_mode_still_fails_closed(self):
        passed, found = validate.kind_coverage(
            {"id": "x", "tier": "hard", "params": {"mode": "bogus"}}, {})
        self.assertFalse(passed)
        self.assertTrue(any(f["severity"] == "hard" for f in found))


class TestCommitBoundaryRegen(unittest.TestCase):
    """The PreToolUse commit-boundary regen hook. On a
    `git commit` it refreshes the graph best-effort and ALWAYS proceeds — it never blocks, never injects,
    fails open, and is reached via the `hook` verb (whose absence/mistyping would BLOCK the commit: the
    no-arg default is `show`, an stdout dump, and an unknown verb returns exit 2)."""

    _COMMIT = {"tool_name": "Bash", "tool_input": {"command": "git commit -m 'x'"}}
    _STATUS = {"tool_name": "Bash", "tool_input": {"command": "git status"}}

    # The `git commit` classifier moved to hooks._is_git_commit (shared with the other commit-boundary
    # hooks); its unit tests live in test_hooks.py. These tests exercise the regen handler's use of it.

    def test_handler_regenerates_on_commit_and_proceeds(self):
        with tempfile.TemporaryDirectory() as d:
            scratch = os.path.join(d, "graph.json")
            with mock.patch.object(knowledge_gen, "GRAPH_PATH", scratch), \
                    contextlib.redirect_stderr(io.StringIO()):
                decision = knowledge_gen._regen_handler(self._COMMIT)
            self.assertEqual(decision, hooks.proceed())             # ALWAYS proceed
            self.assertTrue(os.path.exists(scratch))                # the graph was refreshed...
            with open(scratch, encoding="utf-8") as fh:
                json.loads(fh.read())                               # ...and it is valid JSON

    def test_handler_does_not_regenerate_on_non_commit(self):
        with tempfile.TemporaryDirectory() as d:
            scratch = os.path.join(d, "graph.json")                 # never created
            with mock.patch.object(knowledge_gen, "GRAPH_PATH", scratch):
                decision = knowledge_gen._regen_handler(self._STATUS)
            self.assertEqual(decision, hooks.proceed())
            self.assertFalse(os.path.exists(scratch))               # no regen on a non-commit

    def test_handler_fails_open_on_generate_error(self):
        err = io.StringIO()
        with mock.patch.object(knowledge_gen, "generate", side_effect=OSError("boom")), \
                contextlib.redirect_stderr(err):
            decision = knowledge_gen._regen_handler(self._COMMIT)
        self.assertEqual(decision, hooks.proceed())                 # fail-open: proceed, never block
        self.assertIn("could not run", err.getvalue())              # not silent
        self.assertIn("commit was not affected", err.getvalue())

    def test_handler_returns_proceed_on_every_path(self):
        # PreToolUse is block-eligible and is NOT downgraded the way a forced Stop is, so a block/deny
        # return here would BLOCK the commit. The handler must return proceed on every path.
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(knowledge_gen, "GRAPH_PATH", os.path.join(d, "g.json")), \
                    contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(knowledge_gen._regen_handler(self._COMMIT), hooks.proceed())
            self.assertEqual(knowledge_gen._regen_handler(self._STATUS), hooks.proceed())
        with mock.patch.object(knowledge_gen, "generate", side_effect=ValueError("x")), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(knowledge_gen._regen_handler(self._COMMIT), hooks.proceed())

    def test_end_to_end_via_run_hook_proceeds_exit_zero_no_stdout(self):
        with tempfile.TemporaryDirectory() as d:
            scratch = os.path.join(d, "graph.json")
            out, err = io.StringIO(), io.StringIO()
            with mock.patch.object(knowledge_gen, "GRAPH_PATH", scratch):
                code = hooks.run_hook("PreToolUse", knowledge_gen._regen_handler,
                                      stdin=io.StringIO(json.dumps(self._COMMIT)), stdout=out, stderr=err)
            self.assertEqual(code, hooks.EXIT_PROCEED)              # exit 0 — the commit proceeds
            self.assertEqual(out.getvalue(), "")                   # no structured-output corruption
            self.assertTrue(os.path.exists(scratch))               # the graph was refreshed

    def test_main_hook_verb_routes_to_run_hook(self):
        # The `hook` verb MUST route to run_hook; its absence would fall through to the usage error
        # (exit 2 = a PreToolUse BLOCK) and the no-arg default `show` would dump the graph to stdout.
        with mock.patch.object(hooks, "run_hook", return_value=0) as run:
            self.assertEqual(knowledge_gen.main(["hook"]), 0)
        run.assert_called_once()
        self.assertEqual(run.call_args.args[0], "PreToolUse")
        self.assertIs(run.call_args.args[1], knowledge_gen._regen_handler)

    def test_wired_command_passes_the_hook_arg(self):
        # The footgun guard: the wired command MUST end in ` hook`. Dropping the arg re-enables the
        # no-arg `show` -> stdout dump on every PreToolUse. Assert it in both the manifest and settings.
        manifest = validate.load_json(os.path.join(validate.ROOT, ".engine/modules/core/manifest.json"))
        kg_wires = [w for w in manifest["wires"] if w.get("type") == "hook"
                    and "knowledge_gen.py" in w.get("hook", {}).get("command", "")]
        self.assertEqual(len(kg_wires), 1, "exactly one knowledge_gen hook wire")
        self.assertEqual(kg_wires[0]["event"], "PreToolUse")
        self.assertTrue(kg_wires[0]["hook"]["command"].rstrip().endswith(" hook"))
        settings = validate.load_json(os.path.join(validate.ROOT, ".claude", "settings.json"))
        kg_cmds = [h["command"] for grp in settings["hooks"].get("PreToolUse", [])
                   for h in grp.get("hooks", []) if "knowledge_gen.py" in h.get("command", "")]
        self.assertEqual(len(kg_cmds), 1)
        self.assertTrue(kg_cmds[0].rstrip().endswith(" hook"))


class TestSourceDeterminismRoundTrip(unittest.TestCase):
    """The enforcing correlate: regenerating the graph from the same committed source
    tree yields byte-identical output — including across a PROCESS BOUNDARY under a different
    PYTHONHASHSEED. This guards the source-determinism *property against a future regression* (e.g. a
    change that lets unsorted set/dict iteration leak into committed bytes); it does NOT by itself prove
    the generator nondeterminism-free — it passes trivially today because every collection is sorted
    before it reaches output (`render_graph` sort_keys + sorted `derive_entities`). The process/hash-seed
    axis is the only coverage this adds over the in-process determinism the rest of the suite exercises.
    """

    @staticmethod
    def _canonical_in_subprocess(seed: str) -> bytes:
        # Absolute tools dir on sys.path (NOT a cwd-relative "tools") so the subprocess resolves the
        # import regardless of the runner's working directory.
        tools_dir = os.path.dirname(os.path.abspath(knowledge_gen.__file__))
        code = (
            "import sys\n"
            f"sys.path.insert(0, {tools_dir!r})\n"
            "import knowledge_gen\n"
            "sys.stdout.buffer.write(knowledge_gen.canonical_graph().encode('utf-8'))\n"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code], capture_output=True,
            env={**os.environ, "PYTHONHASHSEED": seed}, check=True)
        return proc.stdout

    def test_regeneration_is_byte_identical_same_process(self):
        self.assertEqual(knowledge_gen.canonical_graph(), knowledge_gen.canonical_graph())

    def test_regeneration_is_byte_identical_across_hash_seeds(self):
        in_process = knowledge_gen.canonical_graph().encode("utf-8")
        a = self._canonical_in_subprocess("1")
        b = self._canonical_in_subprocess("2")
        self.assertEqual(a, b, "graph bytes differ across PYTHONHASHSEED — a nondeterministic generation path")
        self.assertEqual(a, in_process, "subprocess graph differs from in-process — nondeterminism leaked in")


if __name__ == "__main__":
    unittest.main()
