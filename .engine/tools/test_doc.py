#!/usr/bin/env python3
"""Self-tests for the doc surface: the doc.v1 operator-doc-frontmatter grammar, the committed doc
template, the live shape + frontmatter validation rules, and the catalog flip that wires both in.

Run: uv run --directory .engine --frozen -- python -m unittest discover -s tools -p 'test_*.py' -b

These lock: doc.v1 is a well-formed schema with teeth (a missing title, an unknown extra field such as an id or
a date, or a status outside {active, deprecated, retired} is rejected; representative instances conform) AND
the maintainer-decided contract that STATUS IS OPTIONAL — a doc with no status line conforms (absence means
active), and 'draft'/'proposed' are rejected because draftness is a branch state, not a frontmatter value
(ontology), mirroring the operation/policy surfaces. The committed template carries exactly the two required
sections (What this covers, What you need to know) then the four optional sections, its shape-spec frontmatter
is a well-formed template.v1, and it is byte-identical to the doc-shape rule's params (no drift between scaffold
and rule). The shape rule is well-formed, joins CI, dispatches the shape kind over .engine/docs/*.md, is green
over the live doc stream (the committed orientation doc conforms), and has teeth (a missing/out-of-order/stray
section fires hard; an optional allowed section passes; over-length is a soft nudge). The doc-frontmatter schema
rule is well-formed, joins CI, is catalog-routed (no params.schema), green over the live doc stream, and has
teeth on a malformed record. The catalog now routes the doc surface to its in-repo schema and template. There is
deliberately NO coherence leg: a doc has no closed-vocabulary field whose membership a leg must police (the
status enum is enforced by the schema directly), so this slice adds no validate.py code. The doc surface ships ONE doc
instance (the operator orientation doc), so the live shape/frontmatter rules run over it and must be green.
"""
from __future__ import annotations
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate          # noqa: E402

DOC_SCHEMA = validate.load_json(os.path.join(validate.SCHEMAS_DIR, "doc.v1.json"))
TEMPLATE_SCHEMA = validate.load_json(os.path.join(validate.SCHEMAS_DIR, "template.v1.json"))
TEMPLATE_PATH = os.path.join(validate.ENGINE_DIR, "templates", "doc.md")
SHAPE_RULE = validate.load_json(os.path.join(validate.CHECK_DIR, "doc-shape.json"))
# The shape-spec now lives ONLY in the template frontmatter (single source: catalog -> template -> shape ->
# instance); the rule no longer inlines it. kind_shape resolves it via the catalog, which a temp-dir fixture
# cannot resolve to a surface, so _run_kind stubs _template_shape_spec to this committed template's spec.
SHAPE_SPEC = validate.frontmatter(TEMPLATE_PATH)
FM_RULE = validate.load_json(os.path.join(validate.CHECK_DIR, "doc-frontmatter.json"))
DOCS_DIR = os.path.join(validate.ENGINE_DIR, "docs")
STATUS_ENUM = DOC_SCHEMA["properties"]["status"]["enum"]

# Representative, conforming doc frontmatter.
VALID_MINIMAL = {"title": "Getting started with your Engine"}            # no status -> means active
VALID_ACTIVE = {"title": "How to tune the engine's limits", "status": "active"}
VALID_DEPRECATED = {"title": "Old setup guide", "status": "deprecated"}
VALID_RETIRED = {"title": "Retired walkthrough", "status": "retired"}

# A well-formed doc BODY (the shape kind reads the body, never the frontmatter): the two required sections.
VALID_BODY = (
    "## What this covers\nWho this guide is for and what they will learn.\n"
    "## What you need to know\nThe project runs on an Engine that keeps it grounded across sessions.\n")
KNOW_BLOCK = "## What you need to know\nThe project runs on an Engine that keeps it grounded across sessions.\n"


def _errors(schema, instance):
    return list(validate.Draft202012Validator(schema).iter_errors(instance))


def _run_kind(kind_fn, rule, files):
    """Run a kind callable with validate.target_files stubbed to `files` (so a fixture is targeted directly) and
    _template_shape_spec stubbed to the surface's committed template spec (so a temp-dir fixture the catalog
    cannot resolve to a surface still shape-checks against the real template). The spec stub is inert for
    non-shape kinds."""
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
    def test_doc_schema_is_well_formed(self):
        validate.Draft202012Validator.check_schema(DOC_SCHEMA)

    def test_representative_instances_conform(self):
        for inst in (VALID_MINIMAL, VALID_ACTIVE, VALID_DEPRECATED, VALID_RETIRED):
            self.assertEqual(_errors(DOC_SCHEMA, inst), [], f"{inst['title']} should conform")

    def test_status_is_optional_absent_conforms(self):
        # The maintainer-decided contract: a doc needs no status line; absence means active.
        self.assertEqual(_errors(DOC_SCHEMA, VALID_MINIMAL), [])
        self.assertNotIn("status", VALID_MINIMAL)

    def test_missing_title_is_rejected(self):
        self.assertNotEqual(_errors(DOC_SCHEMA, {"status": "active"}), [], "title is required")

    def test_unknown_extra_field_is_rejected(self):
        # docs carry NO id field (slug filename is the identity, the operation precedent) and NO date; the
        # schema is closed, so an id/date key — or any extra key — is rejected.
        for extra in ("id", "date", "author", "established_by"):
            bad = {**VALID_ACTIVE, extra: "x"}
            self.assertNotEqual(_errors(DOC_SCHEMA, bad), [],
                                f"{extra} is not allowed (additionalProperties false)")

    def test_status_outside_the_enum_is_rejected(self):
        # 'draft'/'proposed' are explicitly NOT statuses: draftness is a branch state, not a frontmatter value.
        for bad_status in ("draft", "proposed", "archived", "Active"):
            bad = {**VALID_MINIMAL, "status": bad_status}
            self.assertNotEqual(_errors(DOC_SCHEMA, bad), [], f"{bad_status} is not a status value")
        self.assertEqual(set(STATUS_ENUM), {"active", "deprecated", "retired"})

    def test_title_must_be_a_nonempty_string(self):
        self.assertNotEqual(_errors(DOC_SCHEMA, {"title": ""}), [])
        self.assertNotEqual(_errors(DOC_SCHEMA, {"title": 123}), [])


class TestTemplate(unittest.TestCase):
    def test_catalog_template_pointer_resolves_to_an_existing_file(self):
        catalog = validate.load_json(validate.CATALOG_PATH)
        pointer = catalog["surfaces"]["doc"]["template"]
        self.assertEqual(pointer, "../templates/doc.md")
        resolved = os.path.normpath(os.path.join(validate.SCHEMAS_DIR, pointer))
        self.assertTrue(os.path.isfile(resolved), f"{pointer} must resolve to a committed file")
        self.assertEqual(resolved, os.path.normpath(TEMPLATE_PATH))

    def test_template_body_has_exactly_the_required_then_allowed_sections_in_order(self):
        with open(TEMPLATE_PATH, encoding="utf-8") as fh:
            body = fh.read()
        self.assertEqual(validate.section_order(body),
                         SHAPE_SPEC["required_sections"] + SHAPE_SPEC.get("allowed_sections", []))

    # (Retired: the per-surface template-vs-rule-params no-drift test. The shape-spec now has a SINGLE source —
    # the template frontmatter — read directly by kind_shape via the catalog, so there is no second copy on the
    # rule to drift from. The standing engine/check/template-shape-spec check governs the template spec's shape.)

    def test_template_shape_spec_is_a_well_formed_template_v1(self):
        self.assertEqual(_errors(TEMPLATE_SCHEMA, validate.frontmatter(TEMPLATE_PATH)), [])


class TestShapeRule(unittest.TestCase):
    def test_rule_is_well_formed_and_joins_ci(self):
        check_schema = validate.load_json(os.path.join(validate.SCHEMAS_DIR, "check.v1.json"))
        self.assertEqual(_errors(check_schema, SHAPE_RULE), [])
        self.assertIn("CI", SHAPE_RULE.get("suites", []))
        self.assertEqual(SHAPE_RULE["kind"], "shape")
        self.assertEqual(SHAPE_RULE["target"], {"path": ".engine/docs/*.md"})
        self.assertEqual(SHAPE_RULE["tier"], "hard")

    def test_live_rule_is_green_over_the_committed_doc_stream(self):
        # the real rule over the real .engine/docs/ — the committed orientation doc must conform.
        passed, found = validate.kind_shape(SHAPE_RULE, {})
        self.assertTrue(passed)
        self.assertEqual([f for f in found if f["severity"] == "hard"], [])

    def test_well_formed_body_passes(self):
        with tempfile.TemporaryDirectory() as d:
            p = _write(d, "demo.md", VALID_BODY)
            passed, found = _run_kind(validate.kind_shape, SHAPE_RULE, [p])
        self.assertTrue(passed)
        self.assertEqual([f for f in found if f["severity"] == "hard"], [])

    def test_optional_allowed_section_passes(self):
        body = VALID_BODY + "## Where to go next\nType / to see the commands you can use.\n"
        with tempfile.TemporaryDirectory() as d:
            p = _write(d, "demo.md", body)
            passed, found = _run_kind(validate.kind_shape, SHAPE_RULE, [p])
        self.assertTrue(passed)
        self.assertEqual([f for f in found if f["severity"] == "hard"], [])

    def test_missing_required_section_is_a_hard_finding(self):
        body = VALID_BODY.replace(KNOW_BLOCK, "")
        with tempfile.TemporaryDirectory() as d:
            p = _write(d, "demo.md", body)
            passed, found = _run_kind(validate.kind_shape, SHAPE_RULE, [p])
        self.assertFalse(passed)
        self.assertTrue(any(f["severity"] == "hard" and "What you need to know" in f["message"] for f in found))

    def test_out_of_order_sections_are_a_hard_finding(self):
        body = ("## What you need to know\nk\n## What this covers\nc\n")  # required two, reversed
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
        body = VALID_BODY + "\n".join(f"filler line {i}" for i in range(220)) + "\n"
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
        self.assertEqual(FM_RULE["target"], {"path": ".engine/docs/*.md"})
        self.assertEqual(FM_RULE["tier"], "hard")
        self.assertEqual(FM_RULE.get("params"), {})   # catalog-routed: no params.schema override

    def test_live_rule_is_green_over_the_committed_doc_stream(self):
        passed, found = validate.kind_schema(FM_RULE, {})
        self.assertTrue(passed)
        self.assertEqual([f for f in found if f["severity"] == "hard"], [])

    def test_well_formed_doc_passes_the_live_rule(self):
        # a conforming doc under .engine/docs/ (so the catalog 'doc' surface routes doc.v1). The frontmatter
        # carries title + an optional status.
        fm = "\n".join(f"{k}: {v}" for k, v in VALID_ACTIVE.items())
        path = os.path.join(DOCS_DIR, "_test_doc_fixture.md")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(f"---\n{fm}\n---\n{VALID_BODY}")
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        passed, found = _run_kind(validate.kind_schema, FM_RULE, [path])
        self.assertTrue(passed)
        self.assertEqual([f for f in found if f["severity"] == "hard"], [])

    def test_malformed_record_fails_the_live_rule(self):
        # teeth: a doc whose frontmatter drops the required title is schema-caught via the real rule + reader +
        # catalog routing. The fixture lives under .engine/docs/ and is scoped to this test by addCleanup.
        body = "---\nstatus: active\n---\n" + VALID_BODY
        path = os.path.join(DOCS_DIR, "_test_doc_fixture.md")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(body)
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        passed, found = _run_kind(validate.kind_schema, FM_RULE, [path])
        self.assertFalse(passed)
        self.assertTrue(any(f["severity"] == "hard" and "title" in f["message"] for f in found))


class TestCatalog(unittest.TestCase):
    def test_catalog_routes_doc_to_in_repo_schema_and_template(self):
        """The flip None -> doc.v1.json / template path is load-bearing: without the schema pointer kind_schema
        would silently skip doc frontmatter (governing_schema None -> nothing to check)."""
        catalog = validate.load_json(validate.CATALOG_PATH)
        rec = catalog["surfaces"]["doc"]
        self.assertEqual(rec["governing_schema"], "doc.v1.json")
        self.assertEqual(rec["template"], "../templates/doc.md")
        self.assertEqual(rec["class"], "prose")
        self.assertEqual(rec["lifecycle"], "artifact")


if __name__ == "__main__":
    unittest.main()
