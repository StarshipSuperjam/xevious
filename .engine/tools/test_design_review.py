#!/usr/bin/env python3
"""Self-tests for the design-review module — the plan-review lens roster.

Run: uv run --directory .engine --frozen -- python -m unittest discover -s tools -p 'test_*.py' -b

These lock the module's load-bearing facts, since nothing else does:
  - plan-review-finding.v1 is a well-formed schema with TEETH — it accepts a well-formed finding and
    rejects a severity outside {blocking, serious, nit}, a missing required field, an empty message, or a
    malformed location. This is the ONLY well-formedness lock on the schema: no live rule targets
    .engine/schemas/*.json (test_attention.py says the same of attention-result.v1), so this assertion
    must not be trimmed away.
  - the four committed personas declare role plan-review, the four distinct lenses, the judgment demand
    tier, read-only permissions, and the plan-review-finding.v1 output contract, and each conforms to
    agent.v1.
  - the real .claude/agents/ roster is coherent (validate.agent_coherence_findings is silent over it) and
    carries all four plan-review lenses — the falsifiable proof the suite installs and derives by presence
    (a bad role/tier, or a lens on a non-review role, would make the coherence leg fire).
  - the module is recorded in the install record three ways — its manifest, the engine.json packages list,
    and a verb-less provisioning-catalog entry — each validating against its schema.
"""
from __future__ import annotations
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate  # noqa: E402

AGENTS_DIR = os.path.join(validate.ROOT, ".claude", "agents")
FINDING_SCHEMA = validate.load_json(os.path.join(validate.SCHEMAS_DIR, "plan-review-finding.v1.json"))
MODULE_SCHEMA = validate.load_json(os.path.join(validate.SCHEMAS_DIR, "module.v1.json"))
CATALOG_SCHEMA = validate.load_json(os.path.join(validate.SCHEMAS_DIR, "provisioning-catalog.v1.json"))
AGENT_SCHEMA = validate.load_json(os.path.join(validate.SCHEMAS_DIR, "agent.v1.json"))

MODULE_DIR = os.path.join(validate.ENGINE_DIR, "modules", "design-review")
# The design-review pack is OPTIONAL. Loading its manifest at import time errors the WHOLE suite out in a
# deployment that declined it — a supported choice — rather than skipping the cases that need it.
if not os.path.exists(os.path.join(MODULE_DIR, "manifest.json")):
    raise unittest.SkipTest("the design-review pack is not installed in this repository")
MANIFEST = validate.load_json(os.path.join(MODULE_DIR, "manifest.json"))
ENGINE_JSON = validate.load_json(os.path.join(validate.ENGINE_DIR, "engine.json"))
CATALOG = validate.load_json(os.path.join(validate.ENGINE_DIR, "provisioning", "module-catalog.json"))

LENSES = {"product-intent", "architecture", "feasibility", "risk-governance"}
PERSONA_FILES = {lens: f"engine-design-review-{lens}.md" for lens in LENSES}


def _errors(schema, instance):
    return list(validate.Draft202012Validator(schema).iter_errors(instance))


class TestPlanReviewFindingSchema(unittest.TestCase):
    """The output contract is a well-formed schema that narrows severity — and this is its only lock."""

    def test_schema_is_well_formed(self):
        # No live rule and no schema-iterator test validates .engine/schemas/*.json; this is the sole
        # well-formedness lock on plan-review-finding.v1 — do not remove it.
        validate.Draft202012Validator.check_schema(FINDING_SCHEMA)

    def test_accepts_each_severity(self):
        for sev in ("blocking", "serious", "nit"):
            inst = {"severity": sev, "message": "The scope is too wide to check.",
                    "location": {"file": "plan.md", "line": 3}}
            self.assertEqual(_errors(FINDING_SCHEMA, inst), [], f"{sev} should be accepted")

    def test_accepts_null_location(self):
        inst = {"severity": "nit", "message": "A note about the plan as a whole.", "location": None}
        self.assertEqual(_errors(FINDING_SCHEMA, inst), [])

    def test_rejects_severity_outside_the_enum(self):
        # The narrowing to {blocking, serious, nit} is the whole point: finding.v1's free-string severity
        # (e.g. the check tier "hard") must NOT pass this profile.
        inst = {"severity": "hard", "message": "x", "location": None}
        self.assertTrue(_errors(FINDING_SCHEMA, inst), "a severity outside {blocking,serious,nit} must fail")

    def test_rejects_missing_required_field(self):
        for drop in ("severity", "message", "location"):
            inst = {"severity": "nit", "message": "x", "location": None}
            del inst[drop]
            self.assertTrue(_errors(FINDING_SCHEMA, inst), f"missing {drop} must fail")

    def test_rejects_empty_message(self):
        self.assertTrue(_errors(FINDING_SCHEMA, {"severity": "nit", "message": "", "location": None}))

    def test_rejects_location_without_file(self):
        inst = {"severity": "nit", "message": "x", "location": {"line": 1}}
        self.assertTrue(_errors(FINDING_SCHEMA, inst), "a location object without a file must fail")


class TestDesignReviewPersonas(unittest.TestCase):
    """The four personas declare the right routing fields and each conforms to agent.v1."""

    def test_one_persona_per_lens_with_correct_routing(self):
        for lens, fname in PERSONA_FILES.items():
            path = os.path.join(AGENTS_DIR, fname)
            self.assertTrue(os.path.exists(path), f"missing persona file {fname}")
            fm = validate.frontmatter(path)
            self.assertEqual(fm.get("name"), f"engine-design-review-{lens}", fname)
            self.assertEqual(fm.get("role"), "plan-review", fname)
            self.assertEqual(fm.get("lens"), lens, fname)
            self.assertEqual(fm.get("model-tier"), "judgment", fname)
            self.assertEqual(fm.get("permissions"), "read-only", fname)
            self.assertEqual(fm.get("output-contract"), "plan-review-finding.v1", fname)
            self.assertEqual(_errors(AGENT_SCHEMA, fm), [], f"{fname} frontmatter must conform to agent.v1")


class TestDesignReviewRosterCoherence(unittest.TestCase):
    """The real roster is coherent and carries all four plan-review lenses — derive-by-presence proof."""

    def _roster(self):
        return [validate.frontmatter(os.path.join(AGENTS_DIR, f))
                for f in sorted(os.listdir(AGENTS_DIR)) if f.endswith(".md")]

    def test_real_roster_is_coherent(self):
        # Runs the same coherence leg the build-orchestration roster derivation will, over the REAL
        # committed personas (audit + the four new). A bad role/model-tier, or a lens on a non-review
        # role, would make this fire — so a green here is a real falsification, not a tautology.
        self.assertEqual(validate.agent_coherence_findings(self._roster(), "hard", "m"), [],
                         "the committed persona roster must produce no coherence finding")

    def test_all_four_plan_review_lenses_present(self):
        lenses = {a.get("lens") for a in self._roster() if a.get("role") == "plan-review"}
        self.assertTrue(LENSES.issubset(lenses),
                        f"the plan-review roster must carry all four lenses; saw {sorted(lenses)}")


class TestDesignReviewInstallRecord(unittest.TestCase):
    """The module is recorded three ways (manifest, engine.json, catalog), each valid against its schema."""

    def test_manifest_is_valid_and_claims_the_four_personas(self):
        self.assertEqual(_errors(MODULE_SCHEMA, MANIFEST), [])
        self.assertEqual(MANIFEST["id"], "design-review")
        self.assertEqual(MANIFEST["status"], "optional")
        self.assertEqual(MANIFEST.get("depends"), {"core": ""})
        self.assertEqual(MANIFEST.get("wires"), [])
        claimed = set(MANIFEST["provides"]["agent"])
        for fname in PERSONA_FILES.values():
            self.assertIn(f".claude/agents/{fname}", claimed)

    def test_engine_json_registers_the_module(self):
        self.assertIn("design-review", ENGINE_JSON["packages"])

    def test_catalog_entry_is_valid_and_verb_less(self):
        self.assertEqual(_errors(CATALOG_SCHEMA, CATALOG), [], "the whole catalog must validate")
        entries = [e for e in CATALOG if e["id"] == "design-review"]
        self.assertEqual(len(entries), 1, "design-review must be offered once at setup")
        entry = entries[0]
        self.assertNotIn("verb", entry, "design-review adds no command — its catalog entry must be verb-less")
        self.assertEqual(entry["category"], "Verification & Validation")
        self.assertEqual(entry.get("status"), "optional")


if __name__ == "__main__":
    unittest.main()
