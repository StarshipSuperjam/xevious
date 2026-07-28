#!/usr/bin/env python3
"""Self-tests for the operation surface: the operation.v1 runbook-frontmatter grammar, the
committed operation template, the live shape + frontmatter validation rules, and the catalog flip that wires
both in.

Run: uv run --directory .engine --frozen -- python -m unittest discover -s tools -p 'test_*.py' -b

These lock: operation.v1 is a well-formed schema with teeth (a missing title, an unknown extra field such as
an id, or a status outside {active, deprecated, retired} is rejected; representative instances conform) AND
the maintainer-decided contract that STATUS IS OPTIONAL — an operation with no status line conforms (absence
means active), and 'draft'/'proposed' are rejected because draftness is a branch state, not a frontmatter
value (ontology). The committed template carries exactly Purpose/Steps/Done when then an optional trailing
Notes, its shape-spec frontmatter is a well-formed template.v1, and it is byte-identical to the operation-shape
rule's params (no drift between scaffold and rule). The shape rule is well-formed, joins CI, dispatches the
shape kind over .engine/operations/*.md, is green on the empty stream, and has teeth (a missing/out-of-order/
stray section fires hard; the optional Notes section passes; over-length is a soft nudge). The
operation-frontmatter schema rule is well-formed, joins CI, is catalog-routed (no params.schema), green on the
empty operation stream, and has teeth on a malformed record. The catalog now routes the operation surface to
its in-repo schema and template. There is deliberately NO coherence leg: an operation has no closed-vocabulary
field whose membership a leg must police (the status enum is enforced by the schema directly, the policy /
agent-permissions precedent), so the operation surface adds no validate.py code. It ships ZERO operation instances,
so the live shape/frontmatter rules pass vacuously today.
"""
from __future__ import annotations
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate          # noqa: E402

OPERATION_SCHEMA = validate.load_json(os.path.join(validate.SCHEMAS_DIR, "operation.v1.json"))
TEMPLATE_SCHEMA = validate.load_json(os.path.join(validate.SCHEMAS_DIR, "template.v1.json"))
TEMPLATE_PATH = os.path.join(validate.ENGINE_DIR, "templates", "operation.md")
SHAPE_RULE = validate.load_json(os.path.join(validate.CHECK_DIR, "operation-shape.json"))
# Shape-spec now lives ONLY in the template frontmatter (single source: catalog -> template -> shape -> instance).
# The rule keeps only length_budget_overrides (a rule-only, instance-specific recorded budget raise).
SHAPE_SPEC = validate.frontmatter(TEMPLATE_PATH)
FM_RULE = validate.load_json(os.path.join(validate.CHECK_DIR, "operation-frontmatter.json"))
OPS_DIR = os.path.join(validate.ENGINE_DIR, "operations")
STATUS_ENUM = OPERATION_SCHEMA["properties"]["status"]["enum"]

# Representative, conforming operation frontmatter.
VALID_MINIMAL = {"title": "Consolidate abandoned session deltas"}            # no status -> means active
VALID_ACTIVE = {"title": "Bootstrap the control plane", "status": "active"}
VALID_DEPRECATED = {"title": "Legacy import runbook", "status": "deprecated"}
VALID_RETIRED = {"title": "Retired migration runbook", "status": "retired"}

# A well-formed operation BODY (the shape kind reads the body, never the frontmatter).
VALID_BODY = (
    "## Purpose\nConsolidate the deltas left by sessions that ended without a close.\n"
    "## Steps\n1. Read the state cursor.\n2. Fold each abandoned delta into memory.\n3. Clear the cursor.\n"
    "## Done when\nThe cursor lists no abandoned deltas and memory reflects each folded change.\n")
DONE_WHEN_BLOCK = "## Done when\nThe cursor lists no abandoned deltas and memory reflects each folded change.\n"


def _errors(schema, instance):
    return list(validate.Draft202012Validator(schema).iter_errors(instance))


def _run_kind(kind_fn, rule, files):
    """Run a kind callable with validate.target_files stubbed to `files`, so a fixture can be targeted
    directly (the test_agent.py / test_contract.py pattern)."""
    orig_tf, orig_ss = validate.target_files, validate._template_shape_spec
    validate.target_files = lambda r: list(files)
    validate._template_shape_spec = lambda rel: SHAPE_SPEC
    try:
        return kind_fn(rule, {})
    finally:
        validate.target_files = orig_tf
        validate._template_shape_spec = orig_ss


def _write(d, name, text):
    p = os.path.join(d, name)
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(text)
    return p


class TestSchema(unittest.TestCase):
    def test_operation_schema_is_well_formed(self):
        validate.Draft202012Validator.check_schema(OPERATION_SCHEMA)

    def test_representative_instances_conform(self):
        for inst in (VALID_MINIMAL, VALID_ACTIVE, VALID_DEPRECATED, VALID_RETIRED):
            self.assertEqual(_errors(OPERATION_SCHEMA, inst), [], f"{inst['title']} should conform")

    def test_status_is_optional_absent_conforms(self):
        # The maintainer-decided contract: an operation needs no status line; absence means active.
        self.assertEqual(_errors(OPERATION_SCHEMA, VALID_MINIMAL), [])
        self.assertNotIn("status", VALID_MINIMAL)

    def test_missing_title_is_rejected(self):
        self.assertNotEqual(_errors(OPERATION_SCHEMA, {"status": "active"}), [], "title is required")

    def test_unknown_extra_field_is_rejected(self):
        # operations carry NO id field (slug filename is the identity, the policy precedent) and the schema
        # is closed, so an id key — or any extra key — is rejected.
        for extra in ("id", "date", "author", "trigger"):
            bad = {**VALID_ACTIVE, extra: "x"}
            self.assertNotEqual(_errors(OPERATION_SCHEMA, bad), [],
                                f"{extra} is not allowed (additionalProperties false)")

    def test_status_outside_the_enum_is_rejected(self):
        # 'draft'/'proposed' are explicitly NOT statuses: draftness is a branch state, not a frontmatter value.
        for bad_status in ("draft", "proposed", "archived", "Active"):
            bad = {**VALID_MINIMAL, "status": bad_status}
            self.assertNotEqual(_errors(OPERATION_SCHEMA, bad), [], f"{bad_status} is not a status value")
        self.assertEqual(set(STATUS_ENUM), {"active", "deprecated", "retired"})

    def test_title_must_be_a_nonempty_string(self):
        self.assertNotEqual(_errors(OPERATION_SCHEMA, {"title": ""}), [])
        self.assertNotEqual(_errors(OPERATION_SCHEMA, {"title": 123}), [])


class TestTemplate(unittest.TestCase):
    def test_catalog_template_pointer_resolves_to_an_existing_file(self):
        catalog = validate.load_json(validate.CATALOG_PATH)
        pointer = catalog["surfaces"]["operation"]["template"]
        self.assertEqual(pointer, "../templates/operation.md")
        resolved = os.path.normpath(os.path.join(validate.SCHEMAS_DIR, pointer))
        self.assertTrue(os.path.isfile(resolved), f"{pointer} must resolve to a committed file")
        self.assertEqual(resolved, os.path.normpath(TEMPLATE_PATH))

    def test_template_body_has_exactly_the_required_sections_in_order(self):
        with open(TEMPLATE_PATH, encoding="utf-8") as fh:
            body = fh.read()
        self.assertEqual(validate.section_order(body),
                         SHAPE_SPEC["required_sections"] + SHAPE_SPEC.get("allowed_sections", []))

    def test_length_budget_overrides_is_a_rule_only_field_absent_from_the_template(self):
        # The instance-specific recorded budget raise lives ONLY on the (guarded) rule; template.v1 forbids it
        # (additionalProperties:false), so it can never leak into the unguarded template scaffold. (The former
        # template-vs-rule-params no-drift test is retired: the shape-spec's single source is now the template.)
        self.assertNotIn("length_budget_overrides", validate.frontmatter(TEMPLATE_PATH))
        self.assertIn("length_budget_overrides", SHAPE_RULE["params"])

    def test_template_shape_spec_is_a_well_formed_template_v1(self):
        self.assertEqual(_errors(TEMPLATE_SCHEMA, validate.frontmatter(TEMPLATE_PATH)), [])


class TestShapeRule(unittest.TestCase):
    def test_rule_is_well_formed_and_joins_ci(self):
        check_schema = validate.load_json(os.path.join(validate.SCHEMAS_DIR, "check.v1.json"))
        self.assertEqual(_errors(check_schema, SHAPE_RULE), [])
        self.assertIn("CI", SHAPE_RULE.get("suites", []))
        self.assertEqual(SHAPE_RULE["kind"], "shape")
        self.assertEqual(SHAPE_RULE["target"], {"path": ".engine/operations/*.md"})
        self.assertEqual(SHAPE_RULE["tier"], "hard")

    def test_live_rule_is_green_on_the_empty_stream(self):
        # the real rule over the real .engine/operations/ — only .gitkeep, zero *.md matches, trivially green.
        passed, found = validate.kind_shape(SHAPE_RULE, {})
        self.assertTrue(passed)
        self.assertEqual([f for f in found if f["severity"] == "hard"], [])

    def test_well_formed_body_passes(self):
        with tempfile.TemporaryDirectory() as d:
            p = _write(d, "demo.md", VALID_BODY)
            passed, found = _run_kind(validate.kind_shape, SHAPE_RULE, [p])
        self.assertTrue(passed)
        self.assertEqual([f for f in found if f["severity"] == "hard"], [])

    def test_optional_notes_section_passes(self):
        body = VALID_BODY + "## Notes\nWatch for a half-written cursor; a re-run is safe.\n"
        with tempfile.TemporaryDirectory() as d:
            p = _write(d, "demo.md", body)
            passed, found = _run_kind(validate.kind_shape, SHAPE_RULE, [p])
        self.assertTrue(passed)
        self.assertEqual([f for f in found if f["severity"] == "hard"], [])

    def test_missing_section_is_a_hard_finding(self):
        body = VALID_BODY.replace(DONE_WHEN_BLOCK, "")
        with tempfile.TemporaryDirectory() as d:
            p = _write(d, "demo.md", body)
            passed, found = _run_kind(validate.kind_shape, SHAPE_RULE, [p])
        self.assertFalse(passed)
        self.assertTrue(any(f["severity"] == "hard" and "Done when" in f["message"] for f in found))

    def test_out_of_order_sections_are_a_hard_finding(self):
        body = ("## Purpose\np\n## Done when\nd\n## Steps\ns\n")  # Done when above Steps
        with tempfile.TemporaryDirectory() as d:
            p = _write(d, "demo.md", body)
            passed, found = _run_kind(validate.kind_shape, SHAPE_RULE, [p])
        self.assertFalse(passed)
        self.assertTrue(any(f["severity"] == "hard" and "out of order" in f["message"] for f in found))

    def test_stray_section_is_a_hard_finding(self):
        body = VALID_BODY + "## Appendix\nnot allowed here\n"
        with tempfile.TemporaryDirectory() as d:
            p = _write(d, "demo.md", body)
            passed, found = _run_kind(validate.kind_shape, SHAPE_RULE, [p])
        self.assertFalse(passed)
        self.assertTrue(any(f["severity"] == "hard" and "does not allow" in f["message"] for f in found))

    def test_over_length_is_a_soft_nudge_not_a_block(self):
        body = VALID_BODY + "\n".join(f"filler line {i}" for i in range(140)) + "\n"
        with tempfile.TemporaryDirectory() as d:
            p = _write(d, "demo.md", body)
            passed, found = _run_kind(validate.kind_shape, SHAPE_RULE, [p])
        self.assertTrue(passed)  # soft only -> still passes
        self.assertTrue(any(f["severity"] == "soft" and "budget" in f["message"] for f in found))
        self.assertEqual([f for f in found if f["severity"] == "hard"], [])


class TestFrontmatterRule(unittest.TestCase):
    def test_rule_is_well_formed_and_joins_ci(self):
        check_schema = validate.load_json(os.path.join(validate.SCHEMAS_DIR, "check.v1.json"))
        self.assertEqual(_errors(check_schema, FM_RULE), [])
        self.assertIn("CI", FM_RULE.get("suites", []))
        self.assertEqual(FM_RULE["kind"], "schema")
        self.assertEqual(FM_RULE["target"], {"path": ".engine/operations/*.md"})
        self.assertEqual(FM_RULE["tier"], "hard")
        self.assertEqual(FM_RULE.get("params"), {})   # catalog-routed: no params.schema override

    def test_live_rule_is_green_on_the_empty_operation_stream(self):
        passed, found = validate.kind_schema(FM_RULE, {})
        self.assertTrue(passed)
        self.assertEqual([f for f in found if f["severity"] == "hard"], [])

    def test_well_formed_operation_passes_the_live_rule(self):
        # a conforming operation under .engine/operations/ (so the catalog 'operation' surface routes
        # operation.v1). The frontmatter carries title + an optional status.
        fm = "\n".join(f"{k}: {v}" for k, v in VALID_ACTIVE.items())
        path = os.path.join(OPS_DIR, "_test_operation_fixture.md")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(f"---\n{fm}\n---\n{VALID_BODY}")
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        passed, found = _run_kind(validate.kind_schema, FM_RULE, [path])
        self.assertTrue(passed)
        self.assertEqual([f for f in found if f["severity"] == "hard"], [])

    def test_malformed_record_fails_the_live_rule(self):
        # teeth: an operation whose frontmatter drops the required title is schema-caught via the real rule
        # + reader + catalog routing. The fixture lives under .engine/operations/ and is scoped to this test
        # by addCleanup.
        body = "---\nstatus: active\n---\n" + VALID_BODY
        path = os.path.join(OPS_DIR, "_test_operation_fixture.md")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(body)
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        passed, found = _run_kind(validate.kind_schema, FM_RULE, [path])
        self.assertFalse(passed)
        self.assertTrue(any(f["severity"] == "hard" and "title" in f["message"] for f in found))


class TestCatalog(unittest.TestCase):
    def test_catalog_routes_operation_to_in_repo_schema_and_template(self):
        """The flip None -> operation.v1.json / template path is load-bearing: without the schema pointer
        kind_schema would silently skip operation frontmatter (governing_schema None -> nothing to check)."""
        catalog = validate.load_json(validate.CATALOG_PATH)
        rec = catalog["surfaces"]["operation"]
        self.assertEqual(rec["governing_schema"], "operation.v1.json")
        self.assertEqual(rec["template"], "../templates/operation.md")
        self.assertEqual(rec["class"], "prose")
        self.assertEqual(rec["lifecycle"], "artifact")


if __name__ == "__main__":
    unittest.main()
