#!/usr/bin/env python3
"""Self-tests for the skill surface: the skill.v1 SKILL.md-frontmatter grammar, the committed
skill template, the live shape + frontmatter validation rules, the catalog flip that wires both in, and the
pure skill-set coherence leg (validate.skill_coherence_findings).

Run: uv run --directory .engine --frozen -- python -m unittest discover -s tools -p 'test_*.py' -b

These lock: skill.v1 is a well-formed schema with teeth (a missing `description`, a wrong-typed flag, or a
non-string field is rejected; representative model-auto / operator-typed / model-only instances pass) and — the
design's load-bearing inverse — skill.v1 ACCEPTS any well-formed `invocation` STRING and any unknown extra key,
because invocation membership is the coherence leg's job (NOT the schema's) and the schema is OPEN
(additionalProperties: true) so operators' own un-prefixed product skills and the platform's evolving passthrough
keys are not rejected. The committed template carries
the required Steps section plus an optional Notes section, its shape-spec frontmatter is a well-formed
template.v1, and it is byte-identical to the skill-shape rule's params (no drift between scaffold and rule). The
shape rule is well-formed, joins CI, dispatches the shape kind over .claude/skills/*/SKILL.md, is green on the
empty stream, and has teeth (a missing Steps / a stray section fires hard; over-length is a soft nudge; an
optional Notes is allowed). The skill-frontmatter schema rule is well-formed, joins CI, is catalog-routed (no
params.schema), green on the empty stream, and has teeth on a malformed record. The catalog now routes the skill
surface to its in-repo schema and template. The coherence leg fires on an invocation outside the closed set and
on every invocation↔platform-flag disagreement — the self-election leak-guard (operator-typed without
disable-model-invocation: true → "the model could still self-invoke") — and is silent on a clean skill set. The
leg is built + fixture-tested with no live rule (the interface_resolution_findings / agent_coherence_findings
precedent); SG ships zero skill instances, so the live shape/frontmatter rules pass vacuously today.
"""
from __future__ import annotations
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate          # noqa: E402

SKILL_SCHEMA = validate.load_json(os.path.join(validate.SCHEMAS_DIR, "skill.v1.json"))
TEMPLATE_SCHEMA = validate.load_json(os.path.join(validate.SCHEMAS_DIR, "template.v1.json"))
TEMPLATE_PATH = os.path.join(validate.ENGINE_DIR, "templates", "skill.md")
SHAPE_RULE = validate.load_json(os.path.join(validate.CHECK_DIR, "skill-shape.json"))
# Shape-spec now lives ONLY in the template frontmatter (single source: catalog -> template -> shape -> instance).
SHAPE_SPEC = validate.frontmatter(TEMPLATE_PATH)
FM_RULE = validate.load_json(os.path.join(validate.CHECK_DIR, "skill-frontmatter.json"))
SKILLS_DIR = os.path.join(validate.ROOT, ".claude", "skills")
INVOCATIONS = {"model-auto", "operator-typed", "model-only"}

# Representative, conforming skill frontmatter — one per invocation shape.
VALID_AUTO = {"name": "engine-summarize",
              "description": "Summarizes the uncommitted changes and flags anything risky."}
VALID_OPERATOR_TYPED = {"name": "engine-build",
                        "description": "Enter Build mode for a substantive change.",
                        "invocation": "operator-typed", "disable-model-invocation": True}
VALID_MODEL_ONLY = {"name": "engine-legacy-context",
                    "description": "Explains how the legacy subsystem works, for the model's reference.",
                    "invocation": "model-only", "user-invocable": False}

# A well-formed skill BODY (the shape kind reads the body, never the frontmatter).
VALID_BODY = ("## Steps\n1. Confirm the branch is not main.\n"
              "2. Follow the operation at .engine/operations/session-build.md.\n")


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
    def test_skill_schema_is_well_formed(self):
        validate.Draft202012Validator.check_schema(SKILL_SCHEMA)

    def test_each_invocation_instance_conforms(self):
        for inst in (VALID_AUTO, VALID_OPERATOR_TYPED, VALID_MODEL_ONLY):
            self.assertEqual(_errors(SKILL_SCHEMA, inst), [], f"{inst['name']} should conform")

    def test_minimal_model_auto_instance_conforms(self):
        """The common case: description only — no name, no invocation, no flags — is model-auto."""
        inst = {"description": "Plain skill the model and the operator may both invoke."}
        self.assertEqual(_errors(SKILL_SCHEMA, inst), [])
        self.assertNotIn("invocation", inst)
        self.assertNotIn("name", inst)

    def test_missing_description_is_rejected(self):
        bad = {"name": "engine-x", "invocation": "operator-typed", "disable-model-invocation": True}
        self.assertNotEqual(_errors(SKILL_SCHEMA, bad), [], "description is the one required field")

    def test_non_string_description_is_rejected(self):
        self.assertNotEqual(_errors(SKILL_SCHEMA, {"description": 5}), [])

    def test_wrong_typed_flag_is_rejected(self):
        """A platform flag carries the wrong type — the string 'true' instead of the boolean — is caught."""
        self.assertNotEqual(_errors(SKILL_SCHEMA, {"description": "x", "disable-model-invocation": "true"}), [])
        self.assertNotEqual(_errors(SKILL_SCHEMA, {"description": "x", "user-invocable": "false"}), [])
        self.assertEqual(_errors(SKILL_SCHEMA, {"description": "x", "disable-model-invocation": True}), [])

    def test_arbitrary_invocation_string_is_accepted_by_the_schema(self):
        """The design's load-bearing inverse: invocation MEMBERSHIP is the coherence leg's job, NOT the
        schema's. So skill.v1 accepts any well-formed string for invocation — an 'invocation: banana' passes
        the schema and is rejected only by skill_coherence_findings (see TestSkillCoherenceLeg)."""
        self.assertEqual(_errors(SKILL_SCHEMA, {"description": "x", "invocation": "banana"}), [])
        self.assertEqual(_errors(SKILL_SCHEMA, {"description": "x", "invocation": "model-auto"}), [])

    def test_status_lifecycle_optional_and_enforced(self):
        # #402 U07b: the optional artifact-lifecycle status slot. Omitted means active; the enum is enforced
        # even though the schema is open, because status is a DEFINED property.
        self.assertEqual(_errors(SKILL_SCHEMA, VALID_AUTO), [])                       # omitted -> active
        for st in ("active", "deprecated", "retired"):
            self.assertEqual(_errors(SKILL_SCHEMA, {**VALID_AUTO, "status": st}), [], f"{st} must conform")
        for st in ("draft", "proposed", "bogus"):
            self.assertNotEqual(_errors(SKILL_SCHEMA, {**VALID_AUTO, "status": st}), [], f"{st} must be rejected")

    def test_unknown_extra_keys_pass_the_open_schema(self):
        """additionalProperties: true — operators' un-prefixed product skills and the platform's evolving
        passthrough keys must not be rejected by the engine grammar."""
        rich = {**VALID_OPERATOR_TYPED, "allowed-tools": "Read Grep",
                "when_to_use": "When the operator asks to start a build.", "argument-hint": "[scope]"}
        self.assertEqual(_errors(SKILL_SCHEMA, rich), [])


class TestTemplate(unittest.TestCase):
    def test_catalog_template_pointer_resolves_to_an_existing_file(self):
        catalog = validate.load_json(validate.CATALOG_PATH)
        pointer = catalog["surfaces"]["skill"]["template"]
        self.assertEqual(pointer, "../templates/skill.md")
        resolved = os.path.normpath(os.path.join(validate.SCHEMAS_DIR, pointer))
        self.assertTrue(os.path.isfile(resolved), f"{pointer} must resolve to a committed file")
        self.assertEqual(resolved, os.path.normpath(TEMPLATE_PATH))

    def test_template_body_has_exactly_the_required_then_allowed_sections_in_order(self):
        with open(TEMPLATE_PATH, encoding="utf-8") as fh:
            body = fh.read()
        self.assertEqual(validate.section_order(body),
                         SHAPE_SPEC["required_sections"] + SHAPE_SPEC.get("allowed_sections", []))

    # (Retired: template-vs-rule-params no-drift. The shape-spec's single source is the template frontmatter,
    # read by kind_shape via the catalog; no rule copy remains to drift from. The standing
    # engine/check/template-shape-spec check governs the template spec's shape.)

    def test_template_shape_spec_is_a_well_formed_template_v1(self):
        self.assertEqual(_errors(TEMPLATE_SCHEMA, validate.frontmatter(TEMPLATE_PATH)), [])


class TestShapeRule(unittest.TestCase):
    def test_rule_is_well_formed_and_joins_ci(self):
        check_schema = validate.load_json(os.path.join(validate.SCHEMAS_DIR, "check.v1.json"))
        self.assertEqual(_errors(check_schema, SHAPE_RULE), [])
        self.assertIn("CI", SHAPE_RULE.get("suites", []))
        self.assertEqual(SHAPE_RULE["kind"], "shape")
        self.assertEqual(SHAPE_RULE["target"], {"path": ".claude/skills/*/SKILL.md"})
        self.assertEqual(SHAPE_RULE["tier"], "hard")

    def test_live_rule_is_green_on_the_committed_skills(self):
        # the real shape rule over the real .claude/skills/ — the committed engine-* SKILL.md files are
        # well-shaped, so green.
        passed, found = validate.kind_shape(SHAPE_RULE, {})
        self.assertTrue(passed)
        self.assertEqual([f for f in found if f["severity"] == "hard"], [])

    def test_well_formed_body_passes(self):
        with tempfile.TemporaryDirectory() as d:
            p = _write(d, "SKILL.md", VALID_BODY)
            passed, found = _run_kind(validate.kind_shape, SHAPE_RULE, [p])
        self.assertTrue(passed)
        self.assertEqual([f for f in found if f["severity"] == "hard"], [])

    def test_optional_notes_section_passes(self):
        body = VALID_BODY + "## Notes\nReach for this only at the start of a session.\n"
        with tempfile.TemporaryDirectory() as d:
            p = _write(d, "SKILL.md", body)
            passed, found = _run_kind(validate.kind_shape, SHAPE_RULE, [p])
        self.assertTrue(passed)
        self.assertEqual([f for f in found if f["severity"] == "hard"], [])

    def test_missing_steps_is_a_hard_finding(self):
        body = "## Notes\nthis skill has notes but no steps\n"
        with tempfile.TemporaryDirectory() as d:
            p = _write(d, "SKILL.md", body)
            passed, found = _run_kind(validate.kind_shape, SHAPE_RULE, [p])
        self.assertFalse(passed)
        self.assertTrue(any(f["severity"] == "hard" and "missing the required section" in f["message"]
                            and "Steps" in f["message"] for f in found))

    def test_stray_section_is_a_hard_finding(self):
        body = VALID_BODY + "## Extra\nnot allowed here\n"
        with tempfile.TemporaryDirectory() as d:
            p = _write(d, "SKILL.md", body)
            passed, found = _run_kind(validate.kind_shape, SHAPE_RULE, [p])
        self.assertFalse(passed)
        self.assertTrue(any(f["severity"] == "hard" and "does not allow" in f["message"] for f in found))

    def test_over_length_is_a_soft_nudge_not_a_block(self):
        body = VALID_BODY + "\n".join(f"filler line {i}" for i in range(70)) + "\n"
        with tempfile.TemporaryDirectory() as d:
            p = _write(d, "SKILL.md", body)
            passed, found = _run_kind(validate.kind_shape, SHAPE_RULE, [p])
        self.assertTrue(passed)  # soft only -> still passes
        self.assertTrue(any(f["severity"] == "soft" and "budget" in f["message"] for f in found))
        self.assertEqual([f for f in found if f["severity"] == "hard"], [])


class TestFrontmatterRule(unittest.TestCase):
    FIXTURE_DIR = os.path.join(SKILLS_DIR, "_test_skill_fixture")
    FIXTURE = os.path.join(FIXTURE_DIR, "SKILL.md")

    def _write_fixture(self, frontmatter_block, body=VALID_BODY):
        os.makedirs(self.FIXTURE_DIR, exist_ok=True)
        self.addCleanup(lambda: shutil.rmtree(self.FIXTURE_DIR, ignore_errors=True))
        with open(self.FIXTURE, "w", encoding="utf-8") as fh:
            fh.write(f"---\n{frontmatter_block}---\n{body}")
        return self.FIXTURE

    def test_rule_is_well_formed_and_joins_ci(self):
        check_schema = validate.load_json(os.path.join(validate.SCHEMAS_DIR, "check.v1.json"))
        self.assertEqual(_errors(check_schema, FM_RULE), [])
        self.assertIn("CI", FM_RULE.get("suites", []))
        self.assertEqual(FM_RULE["kind"], "schema")
        self.assertEqual(FM_RULE["target"], {"path": ".claude/skills/*/SKILL.md"})
        self.assertEqual(FM_RULE["tier"], "hard")
        self.assertEqual(FM_RULE.get("params"), {})   # catalog-routed: no params.schema override

    def test_live_rule_is_green_on_the_empty_skill_stream(self):
        passed, found = validate.kind_schema(FM_RULE, {})
        self.assertTrue(passed)
        self.assertEqual([f for f in found if f["severity"] == "hard"], [])

    def test_well_formed_skill_passes_the_live_rule(self):
        # a conforming skill under .claude/skills/<dir>/SKILL.md (so the catalog 'skill' surface routes
        # skill.v1). The fixture is scoped to this test by addCleanup.
        fm = ("name: engine-build\n"
              "description: Enter Build mode for a substantive change.\n"
              "invocation: operator-typed\n"
              "disable-model-invocation: true\n")
        path = self._write_fixture(fm)
        passed, found = _run_kind(validate.kind_schema, FM_RULE, [path])
        self.assertTrue(passed)
        self.assertEqual([f for f in found if f["severity"] == "hard"], [])

    def test_malformed_record_fails_the_live_rule(self):
        # teeth: a skill whose frontmatter drops the required description is schema-caught via the real rule
        # + reader + catalog routing.
        fm = ("name: broken\ninvocation: operator-typed\ndisable-model-invocation: true\n")  # no description
        path = self._write_fixture(fm)
        passed, found = _run_kind(validate.kind_schema, FM_RULE, [path])
        self.assertFalse(passed)
        self.assertTrue(any(f["severity"] == "hard" and "description" in f["message"] for f in found))


class TestSkillCoherenceLeg(unittest.TestCase):
    """validate.skill_coherence_findings — closed invocation membership + the invocation↔platform-flag
    self-election leak-guard. Built + fixture-tested, no live rule (the interface_resolution_findings /
    agent_coherence_findings precedent); live consumption is the operator verbs'. The mechanical
    correlate of the slice's operator demo."""

    def test_clean_skill_set_no_findings(self):
        f = validate.skill_coherence_findings([VALID_AUTO, VALID_OPERATOR_TYPED, VALID_MODEL_ONLY], "hard", "m")
        self.assertEqual(f, [])

    def test_absent_invocation_no_flags_is_clean(self):
        f = validate.skill_coherence_findings([{"description": "plain model-auto skill"}], "hard", "m")
        self.assertEqual(f, [])

    def test_invocation_outside_the_closed_set_is_a_finding(self):
        f = validate.skill_coherence_findings([{"description": "x", "invocation": "operator-types"}], "hard", "m")
        self.assertTrue(any(x["severity"] == "hard" and "recognized invocation value" in x["message"] for x in f))

    def test_unknown_invocation_yields_only_one_finding(self):
        """An unknown value yields only the membership finding — the flag-mapping is skipped (agent-leg
        precedent), so a bad invocation carrying a flag does not double up."""
        f = validate.skill_coherence_findings(
            [{"description": "x", "invocation": "banana", "disable-model-invocation": True}], "hard", "m")
        self.assertEqual(len(f), 1)
        self.assertIn("recognized invocation value", f[0]["message"])

    def test_operator_typed_without_flag_is_the_self_election_finding(self):
        f = validate.skill_coherence_findings([{"description": "x", "invocation": "operator-typed"}], "hard", "m")
        self.assertTrue(any(x["severity"] == "hard" and "could still self-invoke" in x["message"] for x in f))

    def test_model_only_without_flag_is_a_finding(self):
        f = validate.skill_coherence_findings([{"description": "x", "invocation": "model-only"}], "hard", "m")
        self.assertTrue(any(x["severity"] == "hard" and "not hidden from" in x["message"] for x in f))

    def test_restricting_flag_without_declaration_is_a_finding(self):
        """A skill carrying disable-model-invocation: true but no invocation (defaulting to model-auto)
        behaves restricted while declaring nothing — the symmetric leak-guard finding."""
        f = validate.skill_coherence_findings([{"description": "x", "disable-model-invocation": True}], "hard", "m")
        self.assertTrue(any(x["severity"] == "hard" and "restricts who may invoke" in x["message"] for x in f))

    def test_operator_typed_with_conflicting_menu_flag_is_a_finding(self):
        inst = {"description": "x", "invocation": "operator-typed",
                "disable-model-invocation": True, "user-invocable": False}
        f = validate.skill_coherence_findings([inst], "hard", "m")
        self.assertTrue(any(x["severity"] == "hard" and "conflict" in x["message"] for x in f))


class TestCatalog(unittest.TestCase):
    def test_catalog_routes_skill_to_in_repo_schema_and_template(self):
        """The flip None -> skill.v1.json / template path is load-bearing: without the schema pointer
        kind_schema would silently skip skill frontmatter (governing_schema None -> nothing to check)."""
        catalog = validate.load_json(validate.CATALOG_PATH)
        rec = catalog["surfaces"]["skill"]
        self.assertEqual(rec["governing_schema"], "skill.v1.json")
        self.assertEqual(rec["template"], "../templates/skill.md")
        self.assertEqual(rec["class"], "prose")
        self.assertEqual(rec["lifecycle"], "artifact")


if __name__ == "__main__":
    unittest.main()
