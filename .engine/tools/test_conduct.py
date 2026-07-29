#!/usr/bin/env python3
"""Unit tests for the conduct surface: the frontmatter schema is well-formed, the two
committed layer files conform, the shipped defaults carry the expected codes each with a matching section,
and the two custom/script checks (shape correspondence + the soft weakening guard) behave on planted
inputs. The operator-runnable demos in the two check tools are the behavioral correlate; these pin the
logic in the suite."""
from __future__ import annotations
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate                 # noqa: E402
import conduct_shape_check      # noqa: E402
import conduct_weakening_check  # noqa: E402

_CONDUCT = os.path.join(validate.ENGINE_DIR, "conduct")
_DEFAULTS = os.path.join(_CONDUCT, "defaults.md")
_OPERATOR = os.path.join(_CONDUCT, "operator.md")
_SCHEMA = os.path.join(validate.ENGINE_DIR, "schemas", "conduct.v1.json")


class TestConductSchema(unittest.TestCase):
    def test_schema_is_well_formed(self):
        import jsonschema
        with open(_SCHEMA, encoding="utf-8") as fh:
            jsonschema.Draft202012Validator.check_schema(json.load(fh))

    def test_committed_layer_files_conform(self):
        import jsonschema
        with open(_SCHEMA, encoding="utf-8") as fh:
            schema = json.load(fh)
        for path in (_DEFAULTS, _OPERATOR):
            jsonschema.validate(validate.frontmatter(path), schema)  # raises on nonconformance

    def test_operator_override_ships_empty(self):
        self.assertEqual(validate.frontmatter(_OPERATOR).get("codes"), [])


class TestShippedDefaults(unittest.TestCase):
    _EXPECTED = {
        "conduct-critical-partner", "conduct-plain-language", "conduct-explain-before-acting",
        "conduct-ground-claims", "conduct-verify-and-report", "conduct-preserve-intent",
        "conduct-smallest-safe-change", "conduct-full-capability", "conduct-stay-in-scope",
        "conduct-record-decisions", "conduct-care-with-risk",
    }

    def test_universal_codes_present(self):
        ids = {c["id"] for c in validate.frontmatter(_DEFAULTS)["codes"]}
        self.assertEqual(ids, self._EXPECTED)

    def test_every_default_has_a_matching_section(self):
        # the REAL shape check over the committed layer files finds nothing
        self.assertEqual(conduct_shape_check.findings("hard"), [])

    def test_no_default_reads_as_weakening(self):
        # the REAL weakening guard over the committed layer files finds nothing
        self.assertEqual(conduct_weakening_check.findings("soft"), [])


class TestConductShapeCheck(unittest.TestCase):
    @staticmethod
    def _write(tmp, name, body):
        p = os.path.join(tmp, name)
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(body)
        return p

    def test_missing_section_flagged(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = self._write(tmp, "x.md",
                            "---\ncodes:\n  - id: conduct-a\n    title: A\n    status: active\n---\n")
            fs = conduct_shape_check.findings("hard", paths=[p])
            self.assertTrue(any("no matching" in f["message"] for f in fs))

    def test_orphan_section_flagged(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = self._write(tmp, "x.md", "---\ncodes: []\n---\n\n## Stray\n\nrule\n")
            fs = conduct_shape_check.findings("hard", paths=[p])
            self.assertTrue(any("no matching entry" in f["message"] for f in fs))

    def test_disables_on_defaults_layer_flagged(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = self._write(tmp, "defaults.md", "---\ncodes: []\ndisables:\n  - conduct-x\n---\n")
            fs = conduct_shape_check.findings("hard", paths=[p])
            self.assertTrue(any("disables" in f["message"] for f in fs))

    def test_well_formed_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = self._write(tmp, "x.md",
                            "---\ncodes:\n  - id: conduct-a\n    title: A\n    status: active\n---\n\n## A\n\nrule\n")
            self.assertEqual(conduct_shape_check.findings("hard", paths=[p]), [])


class TestConductWeakeningGuard(unittest.TestCase):
    @staticmethod
    def _write(tmp, body):
        p = os.path.join(tmp, "operator.md")
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(body)
        return p

    def test_weakening_line_flagged_soft(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = self._write(tmp, "---\ncodes: []\n---\n\n## X\n\nAuto-approve the merge and skip the review gate.\n")
            fs = conduct_weakening_check.findings("soft", paths=[p])
            self.assertTrue(fs)
            self.assertTrue(all(f["severity"] == "soft" for f in fs))

    def test_pro_guardrail_line_not_flagged(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = self._write(tmp, "---\ncodes: []\n---\n\n## X\n\nYour real protection is the review gate every change passes through.\n")
            self.assertEqual(conduct_weakening_check.findings("soft", paths=[p]), [])


class TestConductLoadsInTheWakeupFloor(unittest.TestCase):
    """The engine loads its codes of conduct at the wake-up floor in EVERY repo: since #323 the committed root
    CLAUDE.md IS the adopter floor (the separate CLAUDE.deployed.md retired with the greenfield swap), and it
    @imports the two layer files, so a session — in this home repo or a generated one that inherits the floor —
    never wakes up without conduct. Guards the #299 fix: the root floor itself carries the imports, so the
    engine's own sessions can't wake up with conduct switched off."""
    _IMPORTS = ("@.engine/conduct/defaults.md", "@.engine/conduct/operator.md")

    def _floor_text(self, name):
        with open(os.path.join(validate.ROOT, name), encoding="utf-8") as fh:
            return fh.read()

    def test_root_floor_imports_conduct(self):
        text = self._floor_text("CLAUDE.md")
        for imp in self._IMPORTS:
            self.assertIn(imp, text,
                          f"the root CLAUDE.md floor must @import {imp} so the engine wakes up with its conduct")

    def test_imported_layer_files_resolve_to_active_codes(self):
        self.assertTrue(os.path.exists(_DEFAULTS) and os.path.exists(_OPERATOR))
        active = [c for c in validate.frontmatter(_DEFAULTS)["codes"] if c.get("status") == "active"]
        self.assertGreaterEqual(len(active), 1)


if __name__ == "__main__":
    unittest.main()
