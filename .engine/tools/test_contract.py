#!/usr/bin/env python3
"""Self-tests for the contract surface: the contract.v1 frontmatter grammar, the committed
contract template, the live shape-kind validation rule, and the catalog flip that wires both in.

Run: uv run --directory .engine --frozen -- python -m unittest discover -s tools -p 'test_*.py' -b

These lock: contract.v1 is a well-formed schema with teeth (a malformed eADR id, a status outside the
decision lifecycle, a bad date, a missing required field, or an unknown extra field is rejected, and the
optional supersedes link conforms when present and when absent); the committed template's shape-spec, the
contract-shape rule's params, and template.v1's worked example are all byte-identical (no drift between the
authoring scaffold, the machine-read rule, and the locked example); the template body carries exactly the
required sections plus Supersedes, in order; the shape rule is well-formed, joins CI, dispatches the shape
kind over .engine/contracts/**/*eADR-*.md (canon AND the deployment instance stream), and is green on the
empty stream; the rule has teeth (a missing
Anti-choice, an out-of-order section, and a stray section each fire a hard finding, while an over-length
body is only a soft nudge); and the catalog now routes the contract surface to its in-repo schema and
template. Contract frontmatter is now LIVE-validated against contract.v1 by the contract-frontmatter schema
rule (the validation foundation's YAML reader parses it) — green over the empty contract stream today, with
teeth on a malformed record — and the same conformance is also proven here over fixtures (the frontmatter
reader deferred has now landed in the validation foundation).
"""
from __future__ import annotations
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate          # noqa: E402

CONTRACT_SCHEMA = validate.load_json(os.path.join(validate.SCHEMAS_DIR, "contract.v1.json"))
TEMPLATE_SCHEMA = validate.load_json(os.path.join(validate.SCHEMAS_DIR, "template.v1.json"))
TEMPLATE_PATH = os.path.join(validate.ENGINE_DIR, "templates", "contract.md")
RULE_PATH = os.path.join(validate.CHECK_DIR, "contract-shape.json")
RULE = validate.load_json(RULE_PATH)
# Shape-spec now lives ONLY in the template frontmatter (single source: catalog -> template -> shape -> instance).
SHAPE_SPEC = validate.frontmatter(TEMPLATE_PATH)
FM_RULE = validate.load_json(os.path.join(validate.CHECK_DIR, "contract-frontmatter.json"))
CONTRACTS_DIR = os.path.join(validate.ENGINE_DIR, "contracts")
STATUS_ENUM = CONTRACT_SCHEMA["properties"]["status"]["enum"]

# A representative, conforming contract frontmatter instance.
VALID_FM = {"id": "eADR-0001", "title": "The validator is a thin core over a kind registry",
            "status": "accepted", "date": "2026-06-02"}

# A well-formed contract BODY (the shape kind reads the body, never the frontmatter).
VALID_BODY = (
    "## Decision\nUse a thin validator core.\n"
    "## Significance\nIt constrains how every later check is added.\n"
    "## Rationale\nKeeps the core small and data-driven.\n"
    "## Anti-choice\nA fat validator with hard-coded checks; rejected — it couples the kinds.\n"
    "## Status\naccepted\n")


def _errors(schema, instance):
    return list(validate.Draft202012Validator(schema).iter_errors(instance))


def _run_kind(kind_fn, rule, files):
    """Run a kind callable with validate.target_files stubbed to `files`, so a fixture under a temp dir
    can be targeted directly (the test_seed.py pattern)."""
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
    def test_contract_schema_is_well_formed(self):
        validate.Draft202012Validator.check_schema(CONTRACT_SCHEMA)

    def test_representative_instance_conforms(self):
        self.assertEqual(_errors(CONTRACT_SCHEMA, VALID_FM), [])

    def test_supersedes_optional_present_and_absent_both_conform(self):
        self.assertEqual(_errors(CONTRACT_SCHEMA, VALID_FM), [])      # absent
        with_link = {**VALID_FM, "id": "eADR-0002", "status": "accepted", "supersedes": "eADR-0001"}
        self.assertEqual(_errors(CONTRACT_SCHEMA, with_link), [])     # present

    def test_missing_required_field_is_rejected(self):
        for drop in ("id", "title", "status", "date"):
            bad = {k: v for k, v in VALID_FM.items() if k != drop}
            self.assertNotEqual(_errors(CONTRACT_SCHEMA, bad), [], f"dropping {drop} should fail")

    def test_malformed_eadr_id_is_rejected(self):
        for bad_id in ("eADR-99", "eADR-00001", "ADR-0001", "eADR-0001-slug", "eadr-0001",
                       # #467 out-of-charset namespaced forms: uppercase slug, slash, empty/leading/double hyphen
                       "Acme-eADR-0001", "a/b-eADR-0001", "-eADR-0001", "acme--eADR-0001", "acme-eADR-001"):
            bad = {**VALID_FM, "id": bad_id}
            self.assertNotEqual(_errors(CONTRACT_SCHEMA, bad), [], f"{bad_id} should fail the id pattern")

    def test_namespaced_deployment_id_conforms(self):
        # #467: a deployment's per-instance record carries a <project-slug>-eADR-#### id; BOTH the bare canon
        # form and the namespaced form must conform (the folder decides the population, the id is the wall).
        for good_id in ("eADR-0001", "acme-eADR-0007", "x1-eADR-0000", "a-b-c-eADR-9999"):
            ok = {**VALID_FM, "id": good_id}
            self.assertEqual(_errors(CONTRACT_SCHEMA, ok), [], f"{good_id} should conform to the id pattern")
        # `supersedes` accepts the namespaced form too (intra-stream supersession within instance/).
        ns = {**VALID_FM, "id": "acme-eADR-0007", "status": "accepted", "supersedes": "acme-eADR-0003"}
        self.assertEqual(_errors(CONTRACT_SCHEMA, ns), [], "a namespaced supersedes link should conform")

    def test_status_outside_the_decision_lifecycle_is_rejected(self):
        # there is deliberately NO 'rejected' state: a rejected alternative is an anti-choice.
        for bad_status in ("rejected", "draft", "active", "Accepted"):
            bad = {**VALID_FM, "status": bad_status}
            self.assertNotEqual(_errors(CONTRACT_SCHEMA, bad), [], f"{bad_status} is not a lifecycle state")
        self.assertEqual(set(STATUS_ENUM), {"proposed", "accepted", "superseded"})

    def test_non_iso_date_is_rejected_by_pattern(self):
        for bad_date in ("June 2", "2026-6-2", "06-02-2026", "2026/06/02", "2026-06-02T00:00:00Z"):
            bad = {**VALID_FM, "date": bad_date}
            self.assertNotEqual(_errors(CONTRACT_SCHEMA, bad), [], f"{bad_date} should fail the date pattern")

    def test_bad_supersedes_pattern_is_rejected(self):
        bad = {**VALID_FM, "supersedes": "eADR-1"}
        self.assertNotEqual(_errors(CONTRACT_SCHEMA, bad), [])

    def test_unknown_extra_field_is_rejected(self):
        bad = {**VALID_FM, "author": "someone"}
        self.assertNotEqual(_errors(CONTRACT_SCHEMA, bad), [], "the schema is closed (additionalProperties false)")


class TestTemplate(unittest.TestCase):
    def test_catalog_template_pointer_resolves_to_an_existing_file(self):
        """The authored catalog `template` pointer must name a real file (resolved the way the validator
        resolves the sibling governing_schema: relative to the schemas dir)."""
        catalog = validate.load_json(validate.CATALOG_PATH)
        pointer = catalog["surfaces"]["contract"]["template"]
        self.assertEqual(pointer, "../templates/contract.md")
        resolved = os.path.normpath(os.path.join(validate.SCHEMAS_DIR, pointer))
        self.assertTrue(os.path.isfile(resolved), f"{pointer} must resolve to a committed file")
        self.assertEqual(resolved, os.path.normpath(TEMPLATE_PATH))

    def test_template_body_has_exactly_the_required_sections_plus_supersedes_in_order(self):
        with open(TEMPLATE_PATH, encoding="utf-8") as fh:
            body = fh.read()
        self.assertEqual(validate.section_order(body),
                         SHAPE_SPEC["required_sections"] + SHAPE_SPEC.get("allowed_sections", []))

    def test_template_shape_spec_matches_template_v1_worked_example_no_drift(self):
        """The contract template's shape-spec frontmatter (now the single source kind_shape reads) and
        template.v1's committed worked example must stay byte-identical — so the locked example and the
        authoring-and-checked scaffold cannot silently diverge."""
        self.assertEqual(SHAPE_SPEC, TEMPLATE_SCHEMA["examples"][0])

    def test_template_shape_spec_is_a_well_formed_template_v1(self):
        # the template frontmatter IS the machine-read shape-spec; it must conform to template.v1.
        self.assertEqual(_errors(TEMPLATE_SCHEMA, SHAPE_SPEC), [])


class TestCheckRule(unittest.TestCase):
    def test_rule_is_well_formed_and_joins_ci(self):
        check_schema = validate.load_json(os.path.join(validate.SCHEMAS_DIR, "check.v1.json"))
        self.assertEqual(_errors(check_schema, RULE), [])
        self.assertIn("CI", RULE.get("suites", []))
        self.assertEqual(RULE["kind"], "shape")
        self.assertEqual(RULE["target"], {"path": ".engine/contracts/**/*eADR-*.md"})
        self.assertEqual(RULE["tier"], "hard")

    def test_live_rule_is_green_on_the_empty_stream(self):
        # the real rule over the real (empty) .engine/contracts/ — zero matches, trivially green.
        passed, found = validate.kind_shape(RULE, {})
        self.assertTrue(passed)
        self.assertEqual([f for f in found if f["severity"] == "hard"], [])

    def test_well_formed_body_passes(self):
        with tempfile.TemporaryDirectory() as d:
            p = _write(d, "eADR-0001-demo.md", VALID_BODY)
            passed, found = _run_kind(validate.kind_shape, RULE, [p])
        self.assertTrue(passed)
        self.assertEqual([f for f in found if f["severity"] == "hard"], [])

    def test_missing_anti_choice_is_a_hard_finding(self):
        body = VALID_BODY.replace(
            "## Anti-choice\nA fat validator with hard-coded checks; rejected — it couples the kinds.\n", "")
        with tempfile.TemporaryDirectory() as d:
            p = _write(d, "eADR-0001-demo.md", body)
            passed, found = _run_kind(validate.kind_shape, RULE, [p])
        self.assertFalse(passed)
        self.assertTrue(any(f["severity"] == "hard" and "Anti-choice" in f["message"] for f in found))

    def test_out_of_order_sections_are_a_hard_finding(self):
        # swap Status above Anti-choice
        body = ("## Decision\nd\n## Significance\ns\n## Rationale\nr\n"
                "## Status\naccepted\n## Anti-choice\nthe alternative, rejected\n")
        with tempfile.TemporaryDirectory() as d:
            p = _write(d, "eADR-0001-demo.md", body)
            passed, found = _run_kind(validate.kind_shape, RULE, [p])
        self.assertFalse(passed)
        self.assertTrue(any(f["severity"] == "hard" and "out of order" in f["message"] for f in found))

    def test_stray_section_is_a_hard_finding(self):
        body = VALID_BODY + "## Footnotes\nnot allowed here\n"
        with tempfile.TemporaryDirectory() as d:
            p = _write(d, "eADR-0001-demo.md", body)
            passed, found = _run_kind(validate.kind_shape, RULE, [p])
        self.assertFalse(passed)
        self.assertTrue(any(f["severity"] == "hard" and "does not allow" in f["message"] for f in found))

    def test_over_length_is_a_soft_nudge_not_a_block(self):
        body = VALID_BODY + "\n".join(f"filler line {i}" for i in range(200)) + "\n"
        with tempfile.TemporaryDirectory() as d:
            p = _write(d, "eADR-0001-demo.md", body)
            passed, found = _run_kind(validate.kind_shape, RULE, [p])
        self.assertTrue(passed)  # soft only -> still passes
        self.assertTrue(any(f["severity"] == "soft" and "budget" in f["message"] for f in found))
        self.assertEqual([f for f in found if f["severity"] == "hard"], [])


class TestContractFrontmatterRule(unittest.TestCase):
    """The live contract-frontmatter schema rule — the folded-in sibling of policy-frontmatter, validating
    each decision record's parsed YAML frontmatter against contract.v1 at the merge."""

    def test_rule_is_well_formed_and_joins_ci(self):
        check_schema = validate.load_json(os.path.join(validate.SCHEMAS_DIR, "check.v1.json"))
        self.assertEqual(_errors(check_schema, FM_RULE), [])
        self.assertIn("CI", FM_RULE.get("suites", []))
        self.assertEqual(FM_RULE["kind"], "schema")
        self.assertEqual(FM_RULE["target"], {"path": ".engine/contracts/**/*eADR-*.md"})
        self.assertEqual(FM_RULE["tier"], "hard")
        self.assertEqual(FM_RULE.get("params"), {})   # catalog-routed: no params.schema override

    def test_live_rule_is_green_on_the_empty_contract_stream(self):
        passed, found = validate.kind_schema(FM_RULE, {})
        self.assertTrue(passed)
        self.assertEqual([f for f in found if f["severity"] == "hard"], [])

    def test_malformed_record_fails_the_live_rule(self):
        # the rule has teeth: a contract whose frontmatter drops the required id is schema-caught via the
        # real rule + reader. The fixture lives under .engine/contracts/ (so the prose router engages) and
        # is scoped to this one test by addCleanup.
        path = os.path.join(CONTRACTS_DIR, "_test_frontmatter_fixture.md")
        body = ("---\ntitle: A decision missing its id\nstatus: accepted\ndate: 2026-06-03\n---\n"
                "## Decision\nd\n## Significance\ns\n## Rationale\nr\n## Anti-choice\na\n## Status\naccepted\n")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(body)
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        passed, found = _run_kind(validate.kind_schema, FM_RULE, [path])
        self.assertFalse(passed)
        self.assertTrue(any(f["severity"] == "hard" and "id" in f["message"] for f in found))


class TestCatalog(unittest.TestCase):
    def test_catalog_routes_contract_to_in_repo_schema_and_template(self):
        """The flip None -> contract.v1.json / template path is load-bearing: it wires the surface's
        governing schema and names its authoring template."""
        catalog = validate.load_json(validate.CATALOG_PATH)
        rec = catalog["surfaces"]["contract"]
        self.assertEqual(rec["governing_schema"], "contract.v1.json")
        self.assertEqual(rec["template"], "../templates/contract.md")
        self.assertEqual(rec["class"], "prose")
        self.assertEqual(rec["lifecycle"], "decision")


class TestInstanceEADRCoverage(unittest.TestCase):
    """The three contract checks (shape/frontmatter/threshold) cover a deployment's OWN eADRs under `instance/`,
    held to the same well-formed-contract bar as the canon — the knowledge supersedes derivation reads their
    frontmatter, so a malformed one would silently break a deployment's own decision history. The target glob
    widened in two steps: #422 took it from `.engine/contracts/*.md` to `.engine/contracts/**/eADR-*.md`, and
    #467 to `.engine/contracts/**/*eADR-*.md` so it also matches a project-namespaced `<project-slug>-eADR-####`
    record (the deployment naming scheme, eADR-0017) — while still excluding `instance/README.md` (no
    `eADR` in its name). The surface catalog resolves an instance eADR to the `contract` surface by
    directory-prefix, so all three kinds validate it with only the glob change (no inlined params, no
    validate.py change). The checks are exercised end-to-end by patching `validate.ROOT` to a temp tree (the
    catalog/template/schema stay bound to the real repo, so surface resolution is genuine)."""

    _RULES = {name: validate.load_json(os.path.join(validate.CHECK_DIR, f"contract-{name}.json"))
              for name in ("shape", "frontmatter", "threshold")}
    _KINDS = {"shape": "kind_shape", "frontmatter": "kind_schema", "threshold": "kind_presence"}

    _GOOD_FM = "---\nid: {eid}\ntitle: {eid} decision\nstatus: accepted\ndate: 2026-07-12\n---\n\n"
    _GOOD_BODY = (
        "## Decision\nTurn on the projects-sync module.\n"
        "## Significance\nIt constrains how this deployment tracks its own work.\n"
        "## Rationale\nThe team wanted a board; the cost is another integration to keep green.\n"
        "## Anti-choice\nA spreadsheet; rejected — it would drift from the issues.\n"
        "## Status\naccepted\n")

    def _tree(self, files):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        inst = os.path.join(tmp.name, ".engine", "contracts", "instance")
        os.makedirs(inst)
        for name, text in files.items():
            _write(inst, name, text)
        return tmp.name

    def _run(self, name, root):
        with mock.patch.object(validate, "ROOT", root):
            return getattr(validate, self._KINDS[name])(self._RULES[name], {})

    def test_all_three_rules_target_the_widened_glob(self):
        for name, rule in self._RULES.items():
            self.assertEqual(rule["target"]["path"], ".engine/contracts/**/*eADR-*.md",
                             f"contract-{name} must cover instance eADRs")

    def test_an_instance_eadr_resolves_to_the_contract_surface(self):
        rec = validate._surface_record_for(".engine/contracts/instance/eADR-9001-x.md")
        self.assertIsNotNone(rec, "an instance eADR must resolve to the contract surface (prefix match)")
        self.assertEqual(rec.get("governing_schema"), "contract.v1.json")
        self.assertEqual(rec.get("class"), "prose")

    def test_widened_glob_matches_instance_eadrs_and_excludes_the_readme(self):
        root = self._tree({"eADR-9001-good.md": self._GOOD_FM.format(eid="eADR-9001") + self._GOOD_BODY,
                           "README.md": "# not an eADR\n"})
        with mock.patch.object(validate, "ROOT", root):
            matched = [os.path.relpath(p, root) for p in validate.target_files(self._RULES["shape"])]
        self.assertIn(".engine/contracts/instance/eADR-9001-good.md", matched)
        self.assertNotIn(".engine/contracts/instance/README.md", matched)

    def test_a_well_formed_instance_eadr_passes_all_three_checks(self):
        root = self._tree({"eADR-9001-good.md": self._GOOD_FM.format(eid="eADR-9001") + self._GOOD_BODY})
        for name in self._RULES:
            passed, found = self._run(name, root)
            self.assertTrue(passed, f"contract-{name} should PASS a well-formed instance eADR: {found}")

    def test_a_structurally_broken_instance_eadr_is_flagged_by_shape_and_threshold(self):
        # missing Significance + Anti-choice: not structurally a contract.
        broken = ("---\nid: eADR-9002\ntitle: broken\nstatus: accepted\ndate: 2026-07-12\n---\n\n"
                  "## Decision\nA choice with no weighed alternative.\n## Status\naccepted\n")
        root = self._tree({"eADR-9002-broken.md": broken})
        for name in ("shape", "threshold"):
            passed, _found = self._run(name, root)
            self.assertFalse(passed, f"contract-{name} should FLAG a structurally broken instance eADR")

    def test_a_bad_frontmatter_instance_eadr_is_flagged_by_frontmatter(self):
        # a status outside the decision lifecycle — the frontmatter schema check must catch it.
        bad = (self._GOOD_FM.replace("status: accepted", "status: nonsense").format(eid="eADR-9003")
               + self._GOOD_BODY)
        root = self._tree({"eADR-9003-badfm.md": bad})
        passed, _found = self._run("frontmatter", root)
        self.assertFalse(passed, "contract-frontmatter should FLAG an instance eADR with a bad status")

    # ---- #467: the widened gate must BITE on a PROJECT-NAMESPACED deployment record, not merely stop rejecting.

    def test_the_widened_glob_matches_a_namespaced_eadr(self):
        root = self._tree({"acme-eADR-9001-good.md": self._GOOD_FM.format(eid="acme-eADR-9001") + self._GOOD_BODY,
                           "README.md": "# not an eADR\n"})
        with mock.patch.object(validate, "ROOT", root):
            matched = [os.path.relpath(p, root) for p in validate.target_files(self._RULES["shape"])]
        self.assertIn(".engine/contracts/instance/acme-eADR-9001-good.md", matched)
        self.assertNotIn(".engine/contracts/instance/README.md", matched)

    def test_a_well_formed_namespaced_instance_eadr_passes_all_three_checks(self):
        root = self._tree({"acme-eADR-9001-good.md": self._GOOD_FM.format(eid="acme-eADR-9001") + self._GOOD_BODY})
        for name in self._RULES:
            passed, found = self._run(name, root)
            self.assertTrue(passed, f"contract-{name} should PASS a well-formed namespaced eADR: {found}")

    def test_a_broken_namespaced_instance_eadr_is_flagged_by_each_check(self):
        # The load-bearing bite: a MALFORMED project-namespaced record is caught by all three checks — a bad
        # id (schema), missing Significance/Anti-choice (shape), a blank required section (threshold).
        bad_id = ("---\nid: Acme-eADR-9001\ntitle: broken\nstatus: accepted\ndate: 2026-07-12\n---\n\n"  # uppercase slug
                  + self._GOOD_BODY)
        self.assertFalse(self._run("frontmatter", self._tree({"acme-eADR-9001-badid.md": bad_id}))[0],
                         "contract-frontmatter should FLAG a namespaced eADR whose id is off-pattern")
        broken_shape = ("---\nid: acme-eADR-9002\ntitle: broken\nstatus: accepted\ndate: 2026-07-12\n---\n\n"
                        "## Decision\nA choice with no weighed alternative.\n## Status\naccepted\n")
        root2 = self._tree({"acme-eADR-9002-broken.md": broken_shape})
        for name in ("shape", "threshold"):
            self.assertFalse(self._run(name, root2)[0],
                             f"contract-{name} should FLAG a structurally broken namespaced eADR")


if __name__ == "__main__":
    unittest.main()
