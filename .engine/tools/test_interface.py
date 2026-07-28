#!/usr/bin/env python3
"""Self-tests for the interface surface (slices 11a + 11b): the interface.v1 declaration grammar, the
committed knowledge-retrieval (11a) and search (11b) declarations, the schema-kind validation rule, and
the single-active / conformance coherence leg (validate.interface_resolution_findings).

Run: uv run --directory .engine --frozen -- python -m unittest discover -s tools -p 'test_*.py' -b

These lock: interface.v1 is a well-formed schema with teeth (a malformed declaration is rejected);
the committed knowledge-retrieval declaration conforms and pins the op-set EXACTLY (a dropped or
renamed operation fails this suite, not only review); each operation's inline input/output schema is
itself a well-formed JSON Schema; the catalog-resolved schema-kind rule joins CI and passes on the real
declaration; and the coherence leg fires single-active (>1 non-default → hard) + conformance (missing
op → hard) and treats an absent named fallback as an expected-pending NOTE, never drift. The GENERIC
properties (conforms, id==filename stem, fallback shape, status-in-enum) are checked over EVERY
declaration so a new interface is auto-covered; the search declaration's op-set/signature/fallback
(engine-memory) and its live expected-pending note are pinned explicitly (11b).
"""
from __future__ import annotations
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate          # noqa: E402

INTERFACE_SCHEMA = validate.load_json(os.path.join(validate.SCHEMAS_DIR, "interface.v1.json"))
INTERFACES_DIR = os.path.join(validate.ENGINE_DIR, "interfaces")
KR_PATH = os.path.join(INTERFACES_DIR, "knowledge-retrieval.json")
KR = validate.load_json(KR_PATH)
SEARCH_PATH = os.path.join(INTERFACES_DIR, "search.json")
SEARCH = validate.load_json(SEARCH_PATH)
RULE_PATH = os.path.join(validate.CHECK_DIR, "interface-declaration.json")
D116_OPS = {"health", "get-entity", "find", "neighbors", "relate"}
STATUS_ENUM = INTERFACE_SCHEMA["properties"]["status"]["enum"]


def _all_declarations():
    """(path, declaration) for every committed interface declaration under .engine/interfaces/ — so the
    generic property tests auto-cover a newly-added interface without an edit here."""
    out = []
    for name in sorted(os.listdir(INTERFACES_DIR)):
        if name.endswith(".json"):
            p = os.path.join(INTERFACES_DIR, name)
            out.append((p, validate.load_json(p)))
    return out


def _errors(schema, instance):
    return list(validate.Draft202012Validator(schema).iter_errors(instance))


class TestSchema(unittest.TestCase):
    def test_interface_schema_is_well_formed(self):
        validate.Draft202012Validator.check_schema(INTERFACE_SCHEMA)

    def test_declaration_conforms(self):
        self.assertEqual(_errors(INTERFACE_SCHEMA, KR), [])

    def test_malformed_declaration_is_rejected(self):
        """The grammar has teeth: a declaration missing 'operations' or 'fallback' is refused."""
        for drop in ("operations", "fallback", "id"):
            bad = {k: v for k, v in KR.items() if k != drop}
            self.assertNotEqual(_errors(INTERFACE_SCHEMA, bad), [], f"dropping {drop} should fail")
        bad_op = json.loads(json.dumps(KR))
        bad_op["operations"][0].pop("input_schema")
        self.assertNotEqual(_errors(INTERFACE_SCHEMA, bad_op), [], "an op missing input_schema must fail")

    def test_each_operation_io_schema_is_well_formed(self):
        for op in KR["operations"]:
            validate.Draft202012Validator.check_schema(op["input_schema"])
            validate.Draft202012Validator.check_schema(op["output_schema"])

    def test_every_declaration_conforms(self):
        """Generic coverage: every interface declaration (knowledge-retrieval, search, and any future
        one) conforms to interface.v1 — a new interface file is auto-covered without editing this test."""
        for path, decl in _all_declarations():
            self.assertEqual(_errors(INTERFACE_SCHEMA, decl), [],
                             f"{os.path.basename(path)} must conform to interface.v1")

    def test_every_operation_io_schema_is_well_formed(self):
        for path, decl in _all_declarations():
            for op in decl["operations"]:
                validate.Draft202012Validator.check_schema(op["input_schema"])
                validate.Draft202012Validator.check_schema(op["output_schema"])


class TestDeclaration(unittest.TestCase):
    def test_id_matches_filename_stem(self):
        self.assertEqual(KR["id"], os.path.splitext(os.path.basename(KR_PATH))[0])

    def test_op_set_is_exactly_d116(self):
        """Pin the locked op-set: dropping/renaming/adding an operation is caught here, not only review."""
        self.assertEqual({op["name"] for op in KR["operations"]}, D116_OPS)

    def test_fallback_is_the_engine_prefixed_mcp_handle(self):
        self.assertEqual(KR["fallback"]["kind"], "mcp")
        self.assertEqual(KR["fallback"]["handle"], "engine-knowledge-graph")
        self.assertTrue(KR["fallback"]["handle"].startswith("engine-"))

    def test_status_active(self):
        self.assertEqual(KR["status"], "active")

    def test_every_id_matches_filename_stem(self):
        for path, decl in _all_declarations():
            self.assertEqual(decl["id"], os.path.splitext(os.path.basename(path))[0],
                             f"{os.path.basename(path)} id must match its filename stem")

    def test_every_fallback_is_engine_prefixed_mcp(self):
        for path, decl in _all_declarations():
            self.assertEqual(decl["fallback"]["kind"], "mcp", os.path.basename(path))
            self.assertTrue(decl["fallback"]["handle"].startswith("engine-"), os.path.basename(path))

    def test_every_status_is_in_the_enum(self):
        for path, decl in _all_declarations():
            self.assertIn(decl["status"], STATUS_ENUM, os.path.basename(path))

    def test_search_op_set_and_signature(self):
        """11b: the recall contract's content-free health probe plus read-only ops — ranked search, the
        transcript window that reads one named session back, and the meaning-based sibling. The window is a
        FETCH, so it adds no second ranking; declaring them here is what keeps a core-owned recall workflow
        from depending on a private detail of one implementation's server (a richer swap-in must answer all).

        `session` narrows the ranked search to ONE conversation. It is part of the SIGNATURE rather than a
        server's private convenience because it is the second move of a recall — a hit that carries no
        position is otherwise only reachable by reading its whole session — so a substitute implementation
        that could not answer it would leave a caller with no way in."""
        self.assertEqual({op["name"] for op in SEARCH["operations"]},
                         {"health", "search", "recall-window", "recall-by-meaning"})
        op = next(o for o in SEARCH["operations"] if o["name"] == "search")
        self.assertEqual(op["input_schema"]["required"], ["query"])
        self.assertEqual(set(op["input_schema"]["properties"]),
                         {"query", "tags", "session", "limit"})
        self.assertEqual(op["output_schema"]["required"], ["results"])

    def test_each_live_helper_health_signature_is_fixed_and_content_free(self):
        for declaration, server in ((SEARCH, "engine-memory"),
                                    (KR, "engine-knowledge-graph")):
            with self.subTest(interface=declaration["id"]):
                op = next(o for o in declaration["operations"] if o["name"] == "health")
                self.assertEqual(op["input_schema"]["properties"], {})
                self.assertEqual(op["input_schema"]["additionalProperties"], False)
                self.assertEqual(op["output_schema"]["required"], ["status", "server"])
                self.assertEqual(op["output_schema"]["properties"]["status"]["const"], "ok")
                self.assertEqual(op["output_schema"]["properties"]["server"]["const"], server)

    def test_recall_window_op_signature(self):
        """11b: the window op pins the session boundary — one named session in, its own turns out."""
        op = next(o for o in SEARCH["operations"] if o["name"] == "recall-window")
        self.assertEqual(op["input_schema"]["required"], ["session_id"])
        self.assertEqual(set(op["input_schema"]["properties"]),
                         {"session_id", "anchor_seq", "radius", "max_turns"})
        self.assertEqual(op["output_schema"]["required"], ["session_id", "turns"])

    def test_search_fallback_is_engine_memory(self):
        """11b: the frozen cross-slice handle the memory-substrate slice must register its server under."""
        self.assertEqual(SEARCH["fallback"], {"kind": "mcp", "handle": "engine-memory"})

    def test_search_status_active(self):
        self.assertEqual(SEARCH["status"], "active")


class TestCheckRule(unittest.TestCase):
    def test_rule_is_well_formed_and_joins_ci(self):
        check_schema = validate.load_json(os.path.join(validate.SCHEMAS_DIR, "check.v1.json"))
        rule = validate.load_json(RULE_PATH)
        self.assertEqual(_errors(check_schema, rule), [])
        self.assertIn("CI", rule.get("suites", []))
        self.assertEqual(rule["kind"], "schema")
        self.assertEqual(rule["target"], {"path": ".engine/interfaces/*.json"})

    def test_live_check_passes_on_the_real_declaration(self):
        passed, found = validate.kind_schema(validate.load_json(RULE_PATH), {})
        self.assertTrue(passed)
        self.assertEqual([f for f in found if f["severity"] == "hard"], [])

    def test_catalog_resolves_interface_to_in_repo_schema(self):
        """The flip None -> interface.v1.json is load-bearing: without it kind_schema would silently
        skip interface declarations (governing_schema None -> nothing to check)."""
        catalog = validate.load_json(validate.CATALOG_PATH)
        self.assertEqual(catalog["surfaces"]["interface"]["governing_schema"], "interface.v1.json")


class TestResolutionLeg(unittest.TestCase):
    """validate.interface_resolution_findings — single-active + conformance + expected-pending fallback."""

    def _ops(self):
        return [op["name"] for op in KR["operations"]]

    def test_core_reality_no_findings(self):
        # the shipped fallback is present, no non-default implementation -> the steady state, silent
        f = validate.interface_resolution_findings([KR], {}, {"engine-knowledge-graph"}, "hard", "m")
        self.assertEqual(f, [])

    def test_two_non_default_implementations_is_hard(self):
        impls = {"knowledge-retrieval": [
            {"handle": "engine-a", "operations": self._ops()},
            {"handle": "engine-b", "operations": self._ops()}]}
        f = validate.interface_resolution_findings(
            [KR], impls, {"engine-knowledge-graph", "engine-a", "engine-b"}, "hard", "m")
        self.assertTrue(any(x["severity"] == "hard" for x in f))
        self.assertTrue(any("only one can be active" in x["message"] for x in f))

    def test_non_conforming_implementation_is_hard(self):
        impls = {"knowledge-retrieval": [{"handle": "engine-a", "operations": ["get-entity"]}]}
        f = validate.interface_resolution_findings(
            [KR], impls, {"engine-knowledge-graph", "engine-a"}, "hard", "m")
        self.assertTrue(any(x["severity"] == "hard" and "can't reliably stand in" in x["message"] for x in f))

    def test_absent_named_fallback_is_a_note_not_hard(self):
        # a synthetic protocol-only interface whose named fallback is not present -> expected-pending note
        # (the LIVE search.json exercise of this path is test_live_search_is_expected_pending_note below)
        synthetic = {"id": "synthetic-pending", "operations": [{"name": "noop"}],
                     "fallback": {"kind": "mcp", "handle": "engine-not-yet-present"}}
        f = validate.interface_resolution_findings([synthetic], {}, set(), "hard", "m")
        self.assertEqual([x["severity"] for x in f], ["note"])
        self.assertTrue(any("not currently wired" in x["message"] for x in f))

    def test_live_search_is_expected_pending_note(self):
        """The REAL committed search.json — with its named fallback absent from the present handles, the
        built-in that answers memory recall is not wired, so resolution is a single expected-setup NOTE,
        never a hard finding. The operator-facing message names the capability in plain language ('Memory
        recall'), never the interface's internal id or the backstage server handle."""
        f = validate.interface_resolution_findings([SEARCH], {}, set(), "hard", "m")
        self.assertEqual([x["severity"] for x in f], ["note"])
        self.assertIn("not currently wired", f[0]["message"])
        self.assertIn("Memory recall", f[0]["message"])
        self.assertNotIn("'search'", f[0]["message"])
        self.assertNotIn("engine-memory", f[0]["message"])


if __name__ == "__main__":
    unittest.main()
