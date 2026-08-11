#!/usr/bin/env python3
"""Self-tests for the policy surface: the policy.v1 frontmatter grammar, the committed policy
template, the live policy-shape rule, the committed policy instances (the four v1-core policies plus the
attention policy the cognitive floor contributes), the contract-threshold filled-presence rule (the
forward-obligation), and the catalog flip that wires schema + template in.

Run: uv run --directory .engine --frozen -- python tools/selftest.py

These lock: policy.v1 is a well-formed schema with teeth (a status outside the decision lifecycle, a bad
date, a missing required field, an unknown extra field, a malformed established_by link, or a values block
that is empty, badly-keyed, or carries a non-number is rejected, while the optional established_by and the
optional values block of plain tuning numbers conform when present and when absent); the committed policy template's body
carries exactly the four required sections in order, its frontmatter shape-spec matches the policy-shape
rule's params byte-for-byte (no drift between the authoring scaffold and the machine-read rule), and that
shape-spec is a well-formed template.v1; the policy-shape rule is well-formed, joins CI, dispatches the shape
kind over .engine/policies/*.md, and the real committed policies pass it, while a missing/ out-of-order/
stray section fires a hard finding and an over-length body is only a soft nudge; the contract-threshold rule
is a well-formed presence rule that joins CI, is green on the empty contract stream, and fires a hard finding
when Significance or Anti-choice is blank or only the template placeholder (presence is checkable), passing
when both are filled; and the catalog now routes the policy surface to its in-repo schema and template.
Policy frontmatter is now LIVE-validated against policy.v1 by the policy-frontmatter schema rule — the
validation foundation's YAML reader parses it, so a malformed value (a tuning number written as text, say)
blocks the merge — and the same conformance is also proven here over fixtures (the frontmatter reader
deferred has now landed in the validation foundation; the live rule + reader are exercised here over the real
committed policies, including their real unquoted YAML dates).
"""
from __future__ import annotations
import glob
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate          # noqa: E402
import module_coherence  # noqa: E402  (installed-means-present manifests, for the roster-aware expected set)

POLICY_SCHEMA = validate.load_json(os.path.join(validate.SCHEMAS_DIR, "policy.v1.json"))
TEMPLATE_SCHEMA = validate.load_json(os.path.join(validate.SCHEMAS_DIR, "template.v1.json"))
TEMPLATE_PATH = os.path.join(validate.ENGINE_DIR, "templates", "policy.md")
POLICIES_DIR = os.path.join(validate.ENGINE_DIR, "policies")
SHAPE_RULE = validate.load_json(os.path.join(validate.CHECK_DIR, "policy-shape.json"))
# Shape-spec now lives ONLY in the template frontmatter (single source: catalog -> template -> shape -> instance).
SHAPE_SPEC = validate.frontmatter(TEMPLATE_PATH)
CT_RULE = validate.load_json(os.path.join(validate.CHECK_DIR, "contract-threshold.json"))
FM_RULE = validate.load_json(os.path.join(validate.CHECK_DIR, "policy-frontmatter.json"))
STATUS_ENUM = POLICY_SCHEMA["properties"]["status"]["enum"]
# The pre-existing kind:schema rules — none target a prose surface, so the prose class-routing must leave
# them on the load_json path (the byte-identical-behavior regression lock).
EXISTING_SCHEMA_RULES = ("engine-manifest", "interface-declaration", "module-manifest", "state-cursor")

# The four v1-core policies that ship from layer one, non-removable: three
# trust-model policies plus triage-threshold. The attention cognitive floor contributes a FIFTH
# policy on the same surface — the tuning values its ranking tool reads — which is NOT one of the foundational
# four but the attention system's own governed policy. An OPTIONAL
# module may also ship its own policy on this surface: the dependency-discipline module contributes a posture
# policy stating its pinning/review-gate/cadence expectations, the
# migration-discipline module a posture policy for the product's own schema migrations, the external-contribution module a posture policy for how the
# engine narrates contributing to an upstream the operator does not own, and the product-design module the
# spec-structure-integrity policy that keeps a description's structure from being dismantled — each present in this construction repo and removed in a generated
# repo that opts the module out. The committed set is their union; growing it here is how a missing/renamed/
# unexpected policy fails this suite.
def _policies_from_manifests(manifests) -> set:
    """The policy slugs the given module manifests declare they provide (`provides.policy`). Deriving the
    expected roster from the INSTALLED manifests — `module_coherence.discover_manifests()` is
    installed-means-present — rather than a hardcoded union keeps the exact-set drift check (a policy present
    on disk but undeclared, or declared but missing, still fails) while staying correct in a deployment that
    DECLINED an optional module: the committed policy set is exactly what the modules on disk declare. Core
    (always installed) declares the foundational four plus attention and model-routing; each optional module
    declares its own posture policy (dependency-discipline, migration-discipline, external-contribution, and
    product-design's spec-structure-integrity) — so declining one legitimately drops its policy here (#646)."""
    # Only the PROSE (.md) policies — the shape rule this test exercises targets `.engine/policies/*.md`, so a
    # `.json` data-policy a manifest also declares (model-bindings, provider-exceptions) is out of scope here.
    slugs = set()
    for _rel, man in manifests:
        for p in (man.get("provides") or {}).get("policy") or []:
            if p.endswith(".md"):
                slugs.add(os.path.splitext(os.path.basename(p))[0])
    return slugs


EXPECTED_POLICIES = _policies_from_manifests(module_coherence.discover_manifests())

# A representative, conforming policy frontmatter instance (a foundational policy omits established_by).
VALID_FM = {"title": "Contract threshold", "status": "accepted", "date": "2026-06-03"}

# A well-formed policy BODY (the shape kind reads the body, never the frontmatter).
VALID_BODY = (
    "## Rule\nRecord a contract only for a significant, hard-to-reverse decision.\n"
    "## Scope\nApplies to every decision made inside the engine.\n"
    "## Rationale\nKeeps permanent decision records rare and worth reading.\n"
    "## Enforcement-tier\nPosture, plus a hard-fail on filled presence and a soft-warn rate signal.\n")

# A well-formed CONTRACT body for the contract-threshold presence rule (it checks Significance + Anti-choice).
CONTRACT_BODY_FILLED = (
    "## Decision\nUse a thin validator core.\n"
    "## Significance\nIt constrains how every later check is added.\n"
    "## Rationale\nKeeps the core small and data-driven.\n"
    "## Anti-choice\nA fat validator with hard-coded checks; rejected — it couples the kinds.\n"
    "## Status\naccepted\n")


def _errors(schema, instance):
    return list(validate.Draft202012Validator(schema).iter_errors(instance))


def _run_kind(kind_fn, rule, files):
    """Run a kind callable with validate.target_files stubbed to `files`, so a fixture under a temp dir
    can be targeted directly (the test_seed.py / test_contract.py pattern)."""
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


def _template_frontmatter_shape_spec(path):
    """Parse the TEMPLATE's frontmatter shape-spec with a line-wise json.loads. This is deliberately NOT
    the validation foundation's YAML `frontmatter` reader (which now exists, and which policy INSTANCE
    frontmatter is live-validated through): a template's shape-spec is a different frontmatter dialect — its
    keys' values are all JSON-compatible (quoted strings, arrays, an int), so each 'key: <value>' line in
    the block between the first two '---' fences parses with json.loads, with no YAML coupling to the
    instance grammar. Unifying the two is out of scope here; they govern different things."""
    text = validate.read(path)
    block = text.split("---", 2)[1]
    spec = {}
    for line in block.splitlines():
        line = line.strip()
        if not line or ":" not in line:
            continue
        key, _, val = line.partition(":")
        spec[key.strip()] = json.loads(val.strip())
    return spec


class TestSchema(unittest.TestCase):
    def test_policy_schema_is_well_formed(self):
        validate.Draft202012Validator.check_schema(POLICY_SCHEMA)

    def test_representative_instance_conforms(self):
        self.assertEqual(_errors(POLICY_SCHEMA, VALID_FM), [])

    def test_established_by_optional_present_and_absent_both_conform(self):
        self.assertEqual(_errors(POLICY_SCHEMA, VALID_FM), [])                       # absent
        with_link = {**VALID_FM, "established_by": "eADR-0001"}
        self.assertEqual(_errors(POLICY_SCHEMA, with_link), [])                      # present

    def test_missing_required_field_is_rejected(self):
        for drop in ("title", "status", "date"):
            bad = {k: v for k, v in VALID_FM.items() if k != drop}
            self.assertNotEqual(_errors(POLICY_SCHEMA, bad), [], f"dropping {drop} should fail")

    def test_status_outside_the_decision_lifecycle_is_rejected(self):
        # the same 'decision' vocabulary contracts use; there is deliberately no 'rejected' state.
        for bad_status in ("rejected", "draft", "active", "Accepted"):
            bad = {**VALID_FM, "status": bad_status}
            self.assertNotEqual(_errors(POLICY_SCHEMA, bad), [], f"{bad_status} is not a lifecycle state")
        self.assertEqual(set(STATUS_ENUM), {"proposed", "accepted", "superseded"})

    def test_non_iso_date_is_rejected_by_pattern(self):
        # the pattern is the real enforcer; format:"date" is not asserted by the bundled validator.
        for bad_date in ("June 3", "2026-6-3", "06-03-2026", "2026/06/03", "2026-06-03T00:00:00Z"):
            bad = {**VALID_FM, "date": bad_date}
            self.assertNotEqual(_errors(POLICY_SCHEMA, bad), [], f"{bad_date} should fail the date pattern")

    def test_bad_established_by_pattern_is_rejected(self):
        bad = {**VALID_FM, "established_by": "eADR-1"}
        self.assertNotEqual(_errors(POLICY_SCHEMA, bad), [])

    def test_unknown_extra_field_is_rejected(self):
        bad = {**VALID_FM, "enforcement_tier": "posture"}   # enforcement tier is the body section, not frontmatter
        self.assertNotEqual(_errors(POLICY_SCHEMA, bad), [], "the schema is closed (additionalProperties false)")

    def test_values_block_of_numbers_conforms(self):
        ok = {**VALID_FM, "values": {"persistence": 3, "auto_resolve": 2, "triage_pressure": 10}}
        self.assertEqual(_errors(POLICY_SCHEMA, ok), [])
        # a fractional value conforms too — number, not integer, so attention's ranking weights reuse this carrier
        self.assertEqual(_errors(POLICY_SCHEMA, {**VALID_FM, "values": {"weight": 0.5}}), [])

    def test_values_is_optional(self):
        # the value-less policies (finding-disposition, escalation) omit the block entirely
        self.assertEqual(_errors(POLICY_SCHEMA, VALID_FM), [])

    def test_non_numeric_value_is_rejected(self):
        # the headline: a tuning number written as text is schema-caught (the machine cannot read it)
        self.assertNotEqual(_errors(POLICY_SCHEMA, {**VALID_FM, "values": {"persistence": "three"}}), [])
        # a YAML boolean is not a number either
        self.assertNotEqual(_errors(POLICY_SCHEMA, {**VALID_FM, "values": {"persistence": True}}), [])

    def test_empty_values_block_is_rejected(self):
        # if a policy declares values, it must carry at least one (minProperties)
        self.assertNotEqual(_errors(POLICY_SCHEMA, {**VALID_FM, "values": {}}), [])

    def test_badly_keyed_value_is_rejected(self):
        # keys are stable lower-snake machine identifiers the reading machinery looks up (propertyNames pattern)
        for bad_key in ("Persistence", "1st", "triage pressure"):
            self.assertNotEqual(_errors(POLICY_SCHEMA, {**VALID_FM, "values": {bad_key: 3}}), [],
                                f"{bad_key!r} is not a valid machine key")

    def test_schema_carries_no_id_field(self):
        # policies are slug-named (no numbered id scheme), unlike contracts' eADR-####.
        self.assertNotIn("id", POLICY_SCHEMA["properties"])


class TestTemplate(unittest.TestCase):
    def test_catalog_template_pointer_resolves_to_an_existing_file(self):
        catalog = validate.load_json(validate.CATALOG_PATH)
        pointer = catalog["surfaces"]["policy"]["template"]
        self.assertEqual(pointer, "../templates/policy.md")
        resolved = os.path.normpath(os.path.join(validate.SCHEMAS_DIR, pointer))
        self.assertTrue(os.path.isfile(resolved), f"{pointer} must resolve to a committed file")
        self.assertEqual(resolved, os.path.normpath(TEMPLATE_PATH))

    def test_template_body_has_exactly_the_required_sections_in_order(self):
        body = validate.read(TEMPLATE_PATH)
        self.assertEqual(validate.section_order(body),
                         SHAPE_SPEC["required_sections"] + SHAPE_SPEC.get("allowed_sections", []))

    # (Retired: template-vs-rule-params no-drift. The shape-spec's single source is the template frontmatter,
    # read by kind_shape via the catalog; no rule copy remains to drift from.)

    def test_template_shape_spec_is_a_well_formed_template_v1(self):
        self.assertEqual(_errors(TEMPLATE_SCHEMA, SHAPE_SPEC), [])


class TestPolicyShapeRule(unittest.TestCase):
    def test_rule_is_well_formed_and_joins_ci(self):
        check_schema = validate.load_json(os.path.join(validate.SCHEMAS_DIR, "check.v1.json"))
        self.assertEqual(_errors(check_schema, SHAPE_RULE), [])
        self.assertIn("CI", SHAPE_RULE.get("suites", []))
        self.assertEqual(SHAPE_RULE["kind"], "shape")
        self.assertEqual(SHAPE_RULE["target"], {"path": ".engine/policies/*.md"})
        self.assertEqual(SHAPE_RULE["tier"], "hard")

    def test_the_committed_policies_exist_and_pass_the_live_rule(self):
        slugs = {os.path.splitext(os.path.basename(p))[0] for p in glob.glob(os.path.join(POLICIES_DIR, "*.md"))}
        self.assertEqual(slugs, EXPECTED_POLICIES,
                         "the committed policies on disk must be exactly the set the installed modules declare "
                         "they provide (core's foundational four plus attention and model-routing, and each "
                         "installed optional module's own posture policy)")
        passed, found = validate.kind_shape(SHAPE_RULE, {})        # the REAL rule over the REAL policies
        self.assertTrue(passed)
        self.assertEqual([f for f in found if f["severity"] == "hard"], [])

    def test_roster_derivation_is_installed_aware(self):
        # Two-directional proof of the #646 derivation: the home repo has every module installed, so the
        # DECLINED branch is otherwise never exercised. Feed synthetic manifests directly.
        core = ("core", {"provides": {"policy": [".engine/policies/attention.md",
                                                 ".engine/policies/escalation.md"]}})
        dep = ("dependency-discipline", {"provides": {"policy": [".engine/policies/dependency-discipline.md"]}})
        full = _policies_from_manifests([core, dep])
        declined = _policies_from_manifests([core])                       # dependency-discipline declined
        self.assertIn("dependency-discipline", full)
        self.assertNotIn("dependency-discipline", declined)              # its policy drops out of the expectation
        self.assertEqual(declined, {"attention", "escalation"})          # core's prose policies still expected
        # A .json DATA policy a manifest also declares is out of scope for this .md shape rule.
        self.assertEqual(_policies_from_manifests(
            [("m", {"provides": {"policy": [".engine/policies/model-bindings.json"]}})]), set())

    def test_missing_required_section_is_a_hard_finding(self):
        body = VALID_BODY.replace("## Scope\nApplies to every decision made inside the engine.\n", "")
        with tempfile.TemporaryDirectory() as d:
            p = _write(d, "demo.md", body)
            passed, found = _run_kind(validate.kind_shape, SHAPE_RULE, [p])
        self.assertFalse(passed)
        self.assertTrue(any(f["severity"] == "hard" and "Scope" in f["message"] for f in found))

    def test_out_of_order_sections_are_a_hard_finding(self):
        body = ("## Rule\nr\n## Rationale\nwhy\n## Scope\nwhere\n## Enforcement-tier\nposture\n")
        with tempfile.TemporaryDirectory() as d:
            p = _write(d, "demo.md", body)
            passed, found = _run_kind(validate.kind_shape, SHAPE_RULE, [p])
        self.assertFalse(passed)
        self.assertTrue(any(f["severity"] == "hard" and "out of order" in f["message"] for f in found))

    def test_stray_section_is_a_hard_finding(self):
        body = VALID_BODY + "## Notes\nnot allowed here\n"
        with tempfile.TemporaryDirectory() as d:
            p = _write(d, "demo.md", body)
            passed, found = _run_kind(validate.kind_shape, SHAPE_RULE, [p])
        self.assertFalse(passed)
        self.assertTrue(any(f["severity"] == "hard" and "does not allow" in f["message"] for f in found))

    def test_over_length_is_a_soft_nudge_not_a_block(self):
        body = VALID_BODY + "\n".join(f"filler line {i}" for i in range(200)) + "\n"
        with tempfile.TemporaryDirectory() as d:
            p = _write(d, "demo.md", body)
            passed, found = _run_kind(validate.kind_shape, SHAPE_RULE, [p])
        self.assertTrue(passed)
        self.assertTrue(any(f["severity"] == "soft" and "budget" in f["message"] for f in found))
        self.assertEqual([f for f in found if f["severity"] == "hard"], [])


class TestContractThresholdRule(unittest.TestCase):
    def test_rule_is_well_formed_and_joins_ci(self):
        check_schema = validate.load_json(os.path.join(validate.SCHEMAS_DIR, "check.v1.json"))
        self.assertEqual(_errors(check_schema, CT_RULE), [])
        self.assertIn("CI", CT_RULE.get("suites", []))
        self.assertEqual(CT_RULE["kind"], "presence")
        self.assertEqual(CT_RULE["target"], {"path": ".engine/contracts/**/*eADR-*.md"})
        self.assertEqual(CT_RULE["tier"], "hard")
        self.assertEqual(set(CT_RULE["params"]["sections"]), {"Significance", "Anti-choice"})

    def test_live_rule_is_green_on_the_empty_contract_stream(self):
        # the real rule over the real (empty) .engine/contracts/ — zero *.md matches, trivially green.
        passed, found = validate.kind_presence(CT_RULE, {})
        self.assertTrue(passed)
        self.assertEqual([f for f in found if f["severity"] == "hard"], [])

    def test_filled_significance_and_anti_choice_pass(self):
        with tempfile.TemporaryDirectory() as d:
            p = _write(d, "eADR-0001-demo.md", CONTRACT_BODY_FILLED)
            passed, found = _run_kind(validate.kind_presence, CT_RULE, [p])
        self.assertTrue(passed)
        self.assertEqual([f for f in found if f["severity"] == "hard"], [])

    def test_placeholder_only_anti_choice_is_a_hard_finding(self):
        body = CONTRACT_BODY_FILLED.replace(
            "## Anti-choice\nA fat validator with hard-coded checks; rejected — it couples the kinds.\n",
            "## Anti-choice\n<Name the strongest alternative that was weighed and rejected.>\n")
        with tempfile.TemporaryDirectory() as d:
            p = _write(d, "eADR-0001-demo.md", body)
            passed, found = _run_kind(validate.kind_presence, CT_RULE, [p])
        self.assertFalse(passed)
        self.assertTrue(any(f["severity"] == "hard" and "Anti-choice" in f["message"] for f in found))

    def test_blank_significance_is_a_hard_finding(self):
        body = CONTRACT_BODY_FILLED.replace(
            "## Significance\nIt constrains how every later check is added.\n", "## Significance\n\n")
        with tempfile.TemporaryDirectory() as d:
            p = _write(d, "eADR-0001-demo.md", body)
            passed, found = _run_kind(validate.kind_presence, CT_RULE, [p])
        self.assertFalse(passed)
        self.assertTrue(any(f["severity"] == "hard" and "Significance" in f["message"] for f in found))


class TestCatalog(unittest.TestCase):
    def test_catalog_routes_policy_to_in_repo_schema_and_template(self):
        catalog = validate.load_json(validate.CATALOG_PATH)
        rec = catalog["surfaces"]["policy"]
        self.assertEqual(rec["governing_schema"], "policy.v1.json")
        self.assertEqual(rec["template"], "../templates/policy.md")
        self.assertEqual(rec["class"], "prose")
        self.assertEqual(rec["lifecycle"], "decision")
        self.assertEqual(rec["authority"], "standing-rules")


class TestPolicyFrontmatterRule(unittest.TestCase):
    """The live policy-frontmatter schema rule: it validates each policy's parsed YAML frontmatter against
    policy.v1 at the merge (the frontmatter reader deferred, now landed)."""

    def test_rule_is_well_formed_and_joins_ci(self):
        check_schema = validate.load_json(os.path.join(validate.SCHEMAS_DIR, "check.v1.json"))
        self.assertEqual(_errors(check_schema, FM_RULE), [])
        self.assertIn("CI", FM_RULE.get("suites", []))
        self.assertEqual(FM_RULE["kind"], "schema")
        self.assertEqual(FM_RULE["target"], {"path": ".engine/policies/*.md"})
        self.assertEqual(FM_RULE["tier"], "hard")
        self.assertEqual(FM_RULE.get("params"), {})   # catalog-routed: no params.schema override

    def test_live_rule_is_green_over_the_real_policies(self):
        # the REAL rule + REAL YAML reader over the REAL committed policies, including their real UNQUOTED
        # `date:` — which YAML coerces to a date object; the reader normalizes it back to a string so the
        # schema's date:{type:string} is satisfied. This locks the date-coercion fix against the real files.
        passed, found = validate.kind_schema(FM_RULE, {})
        self.assertTrue(passed)
        self.assertEqual([f for f in found if f["severity"] == "hard"], [])

    def test_real_policies_carry_machine_readable_values(self):
        # the values load straight from frontmatter with no prose parsing — the numbers telemetry/attention read
        triage = validate.frontmatter(os.path.join(POLICIES_DIR, "triage-threshold.md"))
        self.assertEqual(triage["values"], {"persistence": 3, "auto_resolve": 2, "triage_pressure": 10})
        contract = validate.frontmatter(os.path.join(POLICIES_DIR, "contract-threshold.md"))
        self.assertEqual(contract["values"], {"contract_rate_max": 3})
        attention = validate.frontmatter(os.path.join(POLICIES_DIR, "attention.md"))   # ranking dials
        self.assertTrue(attention.get("values"), "the attention policy carries the ranking tool's tuning values")
        self.assertTrue(all(isinstance(v, (int, float)) and not isinstance(v, bool)
                            for v in attention["values"].values()),
                        "every attention tuning value is a plain number the ranking tool can read")
        # the briefing-budget dials boot reads to fit the pack to the platform size limit (eADR-0033).
        briefing = validate.frontmatter(os.path.join(POLICIES_DIR, "briefing-budget.md"))
        self.assertEqual(set(briefing.get("values", {})),
                         {"excerpt_chars", "pin_index_title_chars", "posture_lines_max",
                          "posture_chars_max", "neighborhood_groups_max", "dashboard_chars_max",
                          "margin_floor_chars"},
                         "the briefing-budget policy carries exactly the dials the boot reader expects")
        self.assertTrue(all(isinstance(v, (int, float)) and not isinstance(v, bool)
                            for v in briefing["values"].values()),
                        "every briefing-budget value is a plain number the boot reader can read")
        for slug in ("finding-disposition", "escalation"):     # the value-less policies carry no block
            self.assertNotIn("values", validate.frontmatter(os.path.join(POLICIES_DIR, slug + ".md")))

    def _run_live_rule_over_fixture(self, body):
        """Write a fixture INTO .engine/policies/ (so the surface-class router reads it as a prose policy
        via the YAML reader), run the REAL rule pointed at just that file, and clean up. The fixture is
        scoped to one test and removed by addCleanup, so it never pollutes the real-policy assertions."""
        path = os.path.join(POLICIES_DIR, "_test_frontmatter_fixture.md")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(body)
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        return _run_kind(validate.kind_schema, FM_RULE, [path])

    def test_non_numeric_value_fails_the_live_rule(self):
        # the schema-caught regression lock: a tuning number written as text blocks via the real rule + reader
        body = ("---\ntitle: T\nstatus: accepted\ndate: 2026-06-03\n"
                "values:\n  persistence: \"three\"\n---\n"
                "## Rule\nr\n## Scope\ns\n## Rationale\nw\n## Enforcement-tier\np\n")
        passed, found = self._run_live_rule_over_fixture(body)
        self.assertFalse(passed)
        self.assertTrue(any(f["severity"] == "hard" and "values/persistence" in f["message"]
                            and "number" in f["message"] for f in found))

    def test_malformed_frontmatter_fails_closed_with_a_prose_message(self):
        # malformed YAML frontmatter -> a hard, plain-language 'settings block' finding (NOT 'not valid JSON')
        body = ("---\ntitle: T\nstatus: accepted\nvalues:\n  persistence: [unclosed\n---\n"
                "## Rule\nr\n## Scope\ns\n## Rationale\nw\n## Enforcement-tier\np\n")
        passed, found = self._run_live_rule_over_fixture(body)
        self.assertFalse(passed)
        msgs = " ".join(f["message"] for f in found if f["severity"] == "hard")
        self.assertIn("settings block", msgs)
        self.assertNotIn("not valid JSON", msgs)


class TestFrontmatterReader(unittest.TestCase):
    """The validation foundation's YAML frontmatter reader (validate.frontmatter)."""

    def test_parses_a_nested_yaml_block(self):
        with tempfile.TemporaryDirectory() as d:
            p = _write(d, "x.md", "---\ntitle: T\nvalues:\n  a: 1\n  b: 2\n---\n## Body\ntext\n")
            self.assertEqual(validate.frontmatter(p), {"title": "T", "values": {"a": 1, "b": 2}})

    def test_date_scalar_normalizes_to_a_string(self):
        # YAML coerces an unquoted ISO date to a datetime.date; the reader renders it back to an ISO string
        # so a schema {type: string} is satisfied (the date-coercion regression lock, at the reader level).
        with tempfile.TemporaryDirectory() as d:
            p = _write(d, "x.md", "---\ndate: 2026-06-03\n---\n## Body\ntext\n")
            fm = validate.frontmatter(p)
        self.assertIsInstance(fm["date"], str)
        self.assertEqual(fm["date"], "2026-06-03")

    def test_numbers_stay_native(self):
        with tempfile.TemporaryDirectory() as d:
            p = _write(d, "x.md", "---\nvalues:\n  n: 3\n  f: 0.5\n---\n## Body\nb\n")
            fm = validate.frontmatter(p)
        self.assertEqual(fm["values"], {"n": 3, "f": 0.5})
        self.assertIsInstance(fm["values"]["n"], int)

    def test_no_frontmatter_yields_empty_dict(self):
        with tempfile.TemporaryDirectory() as d:
            p = _write(d, "x.md", "## Body only\nno fence here\n")
            self.assertEqual(validate.frontmatter(p), {})

    def test_a_body_thematic_break_is_not_mistaken_for_frontmatter(self):
        with tempfile.TemporaryDirectory() as d:
            p = _write(d, "x.md", "---\ntitle: T\n---\n## A\none\n\n---\n\n## B\ntwo\n")
            self.assertEqual(validate.frontmatter(p), {"title": "T"})

    def test_malformed_yaml_raises(self):
        with tempfile.TemporaryDirectory() as d:
            p = _write(d, "x.md", "---\ntitle: [unclosed\n---\n## Body\nb\n")
            with self.assertRaises(Exception):
                validate.frontmatter(p)


class TestSchemaKindRoutingRegression(unittest.TestCase):
    """The prose class-routing added to kind_schema must leave every pre-existing kind:schema rule on the
    load_json path, byte-identical (the three override-schema rules carry no surface record — a naive
    rec.get('class') would crash on None — and interface-declaration is a structured surface)."""

    def test_existing_schema_rules_route_as_structured_not_prose(self):
        for rid in EXISTING_SCHEMA_RULES:
            rule = validate.load_json(os.path.join(validate.CHECK_DIR, rid + ".json"))
            self.assertEqual(rule["kind"], "schema")
            for path in validate.target_files(rule):
                rel = os.path.relpath(path, validate.ROOT)
                rec = validate._surface_record_for(rel)
                cls = rec.get("class") if rec else None
                self.assertNotEqual(cls, "prose", f"{rid} target {rel} would route as prose; must load_json")

    def test_existing_schema_rules_remain_green(self):
        for rid in EXISTING_SCHEMA_RULES:
            rule = validate.load_json(os.path.join(validate.CHECK_DIR, rid + ".json"))
            passed, found = validate.kind_schema(rule, {})
            hard = [f["message"] for f in found if f["severity"] == "hard"]
            self.assertTrue(passed, f"{rid} regressed after the routing change: {hard}")


if __name__ == "__main__":
    unittest.main()
