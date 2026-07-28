#!/usr/bin/env python3
"""Self-tests for the knowledge-graph entity-type vocabulary guard (engine/check/knowledge-vocabulary,
issue #131).

Run: uv run --directory .engine --frozen -- python -m unittest discover -s tools -p 'test_*.py' -b

These lock the teeth that close the fingerprint gate's blind spot: the source of truth is the surface
catalog (surface names + 'module'); the guard discovers every vocabulary site (type enums and entity-id
pattern alternations) across BOTH files that hard-code the list; it is GREEN on the real repo; it goes RED
on a re-introduced stray type (the 'state' bug) in either a type enum or an entity-id pattern, and on a
catalogued surface a file omits; and the rule data is well-formed and owned by validators-core.
"""
from __future__ import annotations
import contextlib
import io
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate                      # noqa: E402
import knowledge_vocabulary_check as kvc   # noqa: E402

CATALOG = validate.load_json(validate.CATALOG_PATH)
EXPECTED = kvc.expected_vocabulary(CATALOG)
PATTERN = ("^(contract|policy|conduct|schema|check|tool|operation|skill|agent|codex-skill|codex-agent"
           "|interface|doc|module):[A-Za-z0-9._-]+$")


class TestExpectedVocabulary(unittest.TestCase):
    def test_expected_is_catalog_surfaces_plus_module(self):
        self.assertEqual(EXPECTED, set(CATALOG["surfaces"]) | {"module"})
        self.assertIn("module", EXPECTED)
        self.assertNotIn("state", EXPECTED)              # the retired type is not catalogued


class TestAlternationParsing(unittest.TestCase):
    def test_parses_an_entity_id_pattern(self):
        self.assertEqual(kvc._alternation_types(PATTERN), EXPECTED)

    def test_ignores_a_non_vocabulary_pattern(self):
        self.assertIsNone(kvc._alternation_types("^sha256:[0-9a-f]{64}$"))
        self.assertIsNone(kvc._alternation_types("^[A-Za-z0-9._-]+$"))


class TestSiteDiscovery(unittest.TestCase):
    def test_iter_finds_enum_and_pattern_sites(self):
        doc = {"properties": {"type": {"enum": sorted(EXPECTED)}},
               "$defs": {"entityId": {"pattern": PATTERN}},
               "noise": {"enum": ["a", "b"], "pattern": "^x$"}}
        kinds = sorted(k for k, _ in kvc.iter_vocabulary_sites(doc))
        self.assertEqual(kinds, ["entity-id pattern", "type enum"])   # the 'noise' enum/pattern are ignored

    def test_collect_sites_covers_both_real_files_seven_sites(self):
        sites = kvc.collect_sites()
        labels = {label for label, _k, _t in sites}
        self.assertEqual(len(labels), 2, "both vocabulary files are scanned")
        self.assertEqual(len(sites), 7)                  # schema: 1 enum + 1 pattern; interface: 1 enum + 4 patterns


class TestFindings(unittest.TestCase):
    def test_real_repo_is_green(self):
        self.assertEqual(kvc.vocabulary_findings(EXPECTED, kvc.collect_sites(), "hard"), [])

    def test_stray_type_in_an_enum_is_flagged(self):
        sites = [("the schema", "type enum", EXPECTED | {"state"})]
        findings = kvc.vocabulary_findings(EXPECTED, sites, "hard")
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["severity"], "hard")
        self.assertIn("'state'", findings[0]["message"])

    def test_stray_type_in_an_entity_id_pattern_is_flagged(self):
        stray_pattern = PATTERN.replace("|module)", "|state|module)")
        sites = [("a file", kind, types)
                 for kind, types in kvc.iter_vocabulary_sites({"pattern": stray_pattern})]
        findings = kvc.vocabulary_findings(EXPECTED, sites, "hard")
        self.assertEqual(len(findings), 1)
        self.assertIn("'state'", findings[0]["message"])

    def test_a_catalogued_surface_a_file_omits_is_flagged(self):
        sites = [("a stale file", "type enum", EXPECTED - {"doc"})]
        findings = kvc.vocabulary_findings(EXPECTED, sites, "hard")
        self.assertEqual(len(findings), 1)
        self.assertIn("'doc'", findings[0]["message"])


class TestCheckEntryPoint(unittest.TestCase):
    def test_main_emits_empty_array_and_exits_zero(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = kvc.main([])
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(buf.getvalue()), [])

    def test_demo_runs_and_shows_the_fail_then_pass(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = kvc.main(["demo"])
        out = buf.getvalue()
        self.assertEqual(rc, 0)
        self.assertIn("GREEN", out)
        self.assertIn("turns RED", out)
        self.assertIn("'state'", out)


class TestRuleData(unittest.TestCase):
    def test_rule_is_well_formed_and_points_at_the_script(self):
        rule = validate.load_json(os.path.join(validate.CHECK_DIR, "knowledge-vocabulary.json"))
        self.assertEqual(rule["id"], "engine/check/knowledge-vocabulary")
        self.assertEqual(rule["kind"], "custom/script")
        self.assertEqual(rule["params"]["script"], ".engine/tools/knowledge_vocabulary_check.py")
        self.assertEqual(rule["tier"], "hard")
        self.assertIn("CI", rule["suites"])

    def test_rule_is_owned_by_validators_core(self):
        manifest = validate.load_json(os.path.join(
            validate.ENGINE_DIR, "modules", "validators-core", "manifest.json"))
        self.assertIn(".engine/check/knowledge-vocabulary.json", manifest["provides"]["check"])


if __name__ == "__main__":
    unittest.main()
