#!/usr/bin/env python3
"""Self-tests for the agent surface: the agent.v1 persona-frontmatter grammar, the committed
persona template, the live shape + frontmatter validation rules, the catalog flip that wires both in, and
the pure agent-set coherence leg (validate.agent_coherence_findings).

Run: uv run --directory .engine --frozen -- python -m unittest discover -s tools -p 'test_*.py' -b

These lock: agent.v1 is a well-formed schema with teeth (a missing required field, an unknown extra field,
or a permissions value outside {read-only, scoped-write} is rejected, and conforming reviewer/worker/audit
instances pass); and — the design's load-bearing inverse — agent.v1 ACCEPTS arbitrary role/model-tier/lens
strings, because closed-set membership is the coherence leg's job, NOT the schema's.
The committed template carries exactly the four sections in order, its shape-spec frontmatter is a well-formed
template.v1, and it is byte-identical to the agent-shape rule's params (no drift between scaffold and rule).
The shape rule is well-formed, joins CI, dispatches the shape kind over .claude/agents/*.md, is green on the
empty stream, and has teeth (a missing/out-of-order/stray section fires hard; over-length is a soft nudge).
The agent-frontmatter schema rule is well-formed, joins CI, is catalog-routed (no params.schema), green on the
empty persona stream, and has teeth on a malformed record. The catalog now routes the agent surface to its
in-repo schema and template. The coherence leg fires on a role outside the closed set, a model-tier outside
{judgment, mechanical}, and a lens on a non-review role — and is silent on a clean roster and on a review lens
(the dangling/unconsumed-lens check is a SEPARATE leg, dangling_lens_findings, driven by the lens-consumption
check that reads build-orchestration's consumed set). Both legs are fixture-tested here; the review/audit
personas now ship, so the live agent-coherence rule exercises the persona leg every CI.
"""
from __future__ import annotations
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate          # noqa: E402

AGENT_SCHEMA = validate.load_json(os.path.join(validate.SCHEMAS_DIR, "agent.v1.json"))
TEMPLATE_SCHEMA = validate.load_json(os.path.join(validate.SCHEMAS_DIR, "template.v1.json"))
TEMPLATE_PATH = os.path.join(validate.ENGINE_DIR, "templates", "agent.md")
SHAPE_RULE = validate.load_json(os.path.join(validate.CHECK_DIR, "agent-shape.json"))
# Shape-spec now lives ONLY in the template frontmatter (single source: catalog -> template -> shape -> instance).
SHAPE_SPEC = validate.frontmatter(TEMPLATE_PATH)
FM_RULE = validate.load_json(os.path.join(validate.CHECK_DIR, "agent-frontmatter.json"))
AGENTS_DIR = os.path.join(validate.ROOT, ".claude", "agents")
PERMISSIONS_ENUM = AGENT_SCHEMA["properties"]["permissions"]["enum"]

# Representative, conforming persona frontmatter — one per role shape.
# A read-only persona must BLOCK the authoritative-write tools (the permissions↔tools coherence
# rule), so the conforming reviewer/audit fixtures carry that lock; the worker is scoped-write
# and is not subject to the rule, so it carries none.
VALID_REVIEWER = {"name": "architecture-plan-reviewer",
                  "description": "Reviews the proposed plan for cross-system seams and scope.",
                  "role": "plan-review", "lens": "architecture", "model-tier": "judgment",
                  "permissions": "read-only", "output-contract": "review-finding.v1",
                  "disallowedTools": ["Edit", "Write", "NotebookEdit", "Bash"]}
VALID_WORKER = {"name": "scoped-worker",
                "description": "Implements one scoped commit and hands the result back.",
                "role": "worker", "model-tier": "mechanical",
                "permissions": "scoped-write", "output-contract": "worker-result.v1"}
VALID_AUDIT = {"name": "self-audit",
               "description": "Runs the read-only self-audit persona under the audit-prep cron.",
               "role": "audit", "model-tier": "judgment",
               "permissions": "read-only", "output-contract": "audit-finding.v1",
               "disallowedTools": ["Edit", "Write", "NotebookEdit"]}

# A well-formed persona BODY (the shape kind reads the body, never the frontmatter).
VALID_BODY = (
    "## Mandate\nYou review the change for architectural soundness.\n"
    "## How you work\nYou read the diff cold, then the design docs it touches, then stop.\n"
    "## What you produce\nYou report findings, each with a severity, a sentence, and a location.\n"
    "## Boundaries\nYou never edit the work; you only report.\n")


def _errors(schema, instance):
    return list(validate.Draft202012Validator(schema).iter_errors(instance))


def _run_kind(kind_fn, rule, files):
    """Run a kind callable with validate.target_files stubbed to `files`, so a fixture can be targeted
    directly (the test_contract.py / test_seed.py pattern)."""
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
    def test_agent_schema_is_well_formed(self):
        validate.Draft202012Validator.check_schema(AGENT_SCHEMA)

    def test_each_role_instance_conforms(self):
        for inst in (VALID_REVIEWER, VALID_WORKER, VALID_AUDIT):
            self.assertEqual(_errors(AGENT_SCHEMA, inst), [], f"{inst['name']} should conform")

    def test_missing_required_field_is_rejected(self):
        for drop in ("name", "description", "role", "model-tier", "permissions", "output-contract"):
            bad = {k: v for k, v in VALID_REVIEWER.items() if k != drop}
            self.assertNotEqual(_errors(AGENT_SCHEMA, bad), [], f"dropping {drop} should fail")

    def test_unknown_extra_field_is_rejected(self):
        bad = {**VALID_REVIEWER, "trigger": "plan-gate"}
        self.assertNotEqual(_errors(AGENT_SCHEMA, bad), [],
                            "the schema is closed (additionalProperties false)")

    def test_permissions_outside_the_enum_is_rejected(self):
        for bad_perm in ("write", "read", "full", "Read-only"):
            bad = {**VALID_REVIEWER, "permissions": bad_perm}
            self.assertNotEqual(_errors(AGENT_SCHEMA, bad), [], f"{bad_perm} is not a permissions value")
        self.assertEqual(set(PERMISSIONS_ENUM), {"read-only", "scoped-write"})

    def test_lens_optional_present_and_absent_both_conform(self):
        self.assertEqual(_errors(AGENT_SCHEMA, VALID_WORKER), [])      # absent (a worker)
        self.assertEqual(_errors(AGENT_SCHEMA, VALID_REVIEWER), [])    # present (a reviewer)

    def test_status_lifecycle_optional_and_enforced(self):
        # #402 U07b: the optional artifact-lifecycle status slot (mirrors doc/operation). Omitted means active;
        # active/deprecated/retired conform; a value outside the set is rejected; status is NOT required.
        self.assertEqual(_errors(AGENT_SCHEMA, VALID_WORKER), [])                      # omitted -> active
        for st in ("active", "deprecated", "retired"):
            self.assertEqual(_errors(AGENT_SCHEMA, {**VALID_WORKER, "status": st}), [], f"{st} must conform")
        for st in ("draft", "proposed", "bogus"):
            self.assertNotEqual(_errors(AGENT_SCHEMA, {**VALID_WORKER, "status": st}), [], f"{st} must be rejected")

    def test_platform_passthrough_keys_conform(self):
        rich = {**VALID_WORKER, "tools": ["Read", "Edit"], "permissionMode": "acceptEdits",
                "model": "sonnet", "effort": "low"}
        self.assertEqual(_errors(AGENT_SCHEMA, rich), [])
        self.assertEqual(_errors(AGENT_SCHEMA, {**rich, "tools": "inherit"}), [])  # tools array OR string

    def test_disallowedtools_passthrough_conforms_array_or_string(self):
        """The denylist key is platform passthrough like tools: array OR string forms conform,
        and its WELL-FORMEDNESS is the schema's job — whether a read-only persona's denylist actually
        blocks the write tools is the coherence leg's (see TestAgentCoherenceLeg)."""
        self.assertEqual(_errors(AGENT_SCHEMA, {**VALID_WORKER, "disallowedTools": ["Edit", "Write"]}), [])
        self.assertEqual(_errors(AGENT_SCHEMA, {**VALID_WORKER, "disallowedTools": "Edit"}), [])

    def test_arbitrary_role_tier_lens_strings_are_accepted_by_the_schema(self):
        """The design's load-bearing inverse: closed-set MEMBERSHIP is the coherence leg's job, NOT the
        schema's. So agent.v1 accepts any well-formed string for role/model-tier/lens — a 'role: banana'
        passes the schema and is rejected only by agent_coherence_findings (see TestAgentCoherenceLeg)."""
        self.assertEqual(_errors(AGENT_SCHEMA, {**VALID_REVIEWER, "role": "banana"}), [])
        self.assertEqual(_errors(AGENT_SCHEMA, {**VALID_REVIEWER, "model-tier": "heroic"}), [])
        self.assertEqual(_errors(AGENT_SCHEMA, {**VALID_WORKER, "lens": "smuggled"}), [])


class TestTemplate(unittest.TestCase):
    def test_catalog_template_pointer_resolves_to_an_existing_file(self):
        catalog = validate.load_json(validate.CATALOG_PATH)
        pointer = catalog["surfaces"]["agent"]["template"]
        self.assertEqual(pointer, "../templates/agent.md")
        resolved = os.path.normpath(os.path.join(validate.SCHEMAS_DIR, pointer))
        self.assertTrue(os.path.isfile(resolved), f"{pointer} must resolve to a committed file")
        self.assertEqual(resolved, os.path.normpath(TEMPLATE_PATH))

    def test_template_body_has_exactly_the_required_sections_in_order(self):
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
        self.assertEqual(SHAPE_RULE["target"], {"path": ".claude/agents/*.md"})
        self.assertEqual(SHAPE_RULE["tier"], "hard")

    def test_live_shape_rule_is_green_on_the_committed_personas(self):
        # the real shape rule over the real .claude/agents/ — the committed personas are well-shaped, so
        # green; persona validity is load-bearing (this is no longer an empty stream).
        passed, found = validate.kind_shape(SHAPE_RULE, {})
        self.assertTrue(passed)
        self.assertEqual([f for f in found if f["severity"] == "hard"], [])

    def test_live_audit_persona_is_read_only_and_bash_locked(self):
        # The audit persona reports and never runs a command, so its read-only guarantee must block
        # Bash too — the same lock the design-review lenses carry — not only the file-writing tools. The
        # coherence leg enforces only the write-tool floor (Bash is above it), so this pins the audit
        # persona's own frontmatter lock: a future edit can't silently reopen Bash on the self-audit.
        fm = validate.frontmatter(os.path.join(AGENTS_DIR, "engine-audit.md"))
        self.assertEqual(fm.get("role"), "audit")
        denied = fm.get("disallowedTools", [])
        for tool in ("Edit", "Write", "NotebookEdit", "Bash"):
            self.assertIn(tool, denied,
                          f"the audit persona must block {tool} (read-only, no command execution)")

    def test_well_formed_body_passes(self):
        with tempfile.TemporaryDirectory() as d:
            p = _write(d, "demo.md", VALID_BODY)
            passed, found = _run_kind(validate.kind_shape, SHAPE_RULE, [p])
        self.assertTrue(passed)
        self.assertEqual([f for f in found if f["severity"] == "hard"], [])

    def test_missing_section_is_a_hard_finding(self):
        body = VALID_BODY.replace("## Boundaries\nYou never edit the work; you only report.\n", "")
        with tempfile.TemporaryDirectory() as d:
            p = _write(d, "demo.md", body)
            passed, found = _run_kind(validate.kind_shape, SHAPE_RULE, [p])
        self.assertFalse(passed)
        self.assertTrue(any(f["severity"] == "hard" and "Boundaries" in f["message"] for f in found))

    def test_out_of_order_sections_are_a_hard_finding(self):
        body = ("## Mandate\nm\n## How you work\nh\n"
                "## Boundaries\nb\n## What you produce\nw\n")  # Boundaries above What you produce
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
        body = VALID_BODY + "\n".join(f"filler line {i}" for i in range(120)) + "\n"
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
        self.assertEqual(FM_RULE["target"], {"path": ".claude/agents/*.md"})
        self.assertEqual(FM_RULE["tier"], "hard")
        self.assertEqual(FM_RULE.get("params"), {})   # catalog-routed: no params.schema override

    def test_live_frontmatter_rule_is_green_on_the_committed_personas(self):
        # the real frontmatter rule over the real .claude/agents/ — the committed audit persona conforms to
        # agent.v1, so green; persona validity is load-bearing (this is no longer an empty stream).
        passed, found = validate.kind_schema(FM_RULE, {})
        self.assertTrue(passed)
        self.assertEqual([f for f in found if f["severity"] == "hard"], [])

    def test_well_formed_persona_passes_the_live_rule(self):
        # a conforming persona under .claude/agents/ (so the catalog 'agent' surface routes agent.v1).
        fm = "\n".join(f"{k}: {v}" for k, v in VALID_WORKER.items())
        path = os.path.join(AGENTS_DIR, "_test_agent_fixture.md")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(f"---\n{fm}\n---\n{VALID_BODY}")
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        passed, found = _run_kind(validate.kind_schema, FM_RULE, [path])
        self.assertTrue(passed)
        self.assertEqual([f for f in found if f["severity"] == "hard"], [])

    def test_malformed_record_fails_the_live_rule(self):
        # teeth: a persona whose frontmatter drops the required output-contract is schema-caught via the
        # real rule + reader + catalog routing. The fixture lives under .claude/agents/ and is scoped to
        # this test by addCleanup.
        body = ("---\nname: broken\ndescription: missing its output-contract\nrole: worker\n"
                "model-tier: mechanical\npermissions: scoped-write\n---\n" + VALID_BODY)
        path = os.path.join(AGENTS_DIR, "_test_agent_fixture.md")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(body)
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        passed, found = _run_kind(validate.kind_schema, FM_RULE, [path])
        self.assertFalse(passed)
        self.assertTrue(any(f["severity"] == "hard" and "output-contract" in f["message"] for f in found))


class TestCatalog(unittest.TestCase):
    def test_catalog_routes_agent_to_in_repo_schema_and_template(self):
        """The flip None -> agent.v1.json / template path is load-bearing: without the schema pointer
        kind_schema would silently skip persona frontmatter (governing_schema None -> nothing to check)."""
        catalog = validate.load_json(validate.CATALOG_PATH)
        rec = catalog["surfaces"]["agent"]
        self.assertEqual(rec["governing_schema"], "agent.v1.json")
        self.assertEqual(rec["template"], "../templates/agent.md")
        self.assertEqual(rec["class"], "prose")
        self.assertEqual(rec["lifecycle"], "artifact")


class TestAgentCoherenceLeg(unittest.TestCase):
    """validate.agent_coherence_findings — closed-role + closed-model-tier + lens-on-non-review-role. Built
    + fixture-tested, no live rule (the interface_resolution_findings precedent); live roster consumption is
    build-orchestration's. The mechanical correlate of the slice's operator demo."""

    def test_clean_roster_no_findings(self):
        f = validate.agent_coherence_findings([VALID_REVIEWER, VALID_WORKER, VALID_AUDIT], "hard", "m")
        self.assertEqual(f, [])

    def test_role_outside_the_closed_set_is_a_finding(self):
        f = validate.agent_coherence_findings([{**VALID_REVIEWER, "role": "reviewer"}], "hard", "m")
        self.assertTrue(any(x["severity"] == "hard" and "recognized role" in x["message"] for x in f))

    def test_model_tier_outside_the_closed_set_is_a_finding(self):
        f = validate.agent_coherence_findings([{**VALID_WORKER, "model-tier": "heroic"}], "hard", "m")
        self.assertTrue(any(x["severity"] == "hard" and "demand level" in x["message"] for x in f))

    def test_lens_on_a_worker_is_a_symmetric_finding(self):
        f = validate.agent_coherence_findings([{**VALID_WORKER, "lens": "architecture"}], "hard", "m")
        self.assertTrue(any(x["severity"] == "hard" and "carries no lens" in x["message"] for x in f))

    def test_lens_on_an_audit_is_a_symmetric_finding(self):
        f = validate.agent_coherence_findings([{**VALID_AUDIT, "lens": "risk-governance"}], "hard", "m")
        self.assertTrue(any(x["severity"] == "hard" and "carries no lens" in x["message"] for x in f))

    def test_lens_on_a_review_role_is_clean(self):
        for role in ("plan-review", "pre-submission-review"):
            inst = {**VALID_REVIEWER, "role": role, "lens": "feasibility"}
            self.assertEqual(validate.agent_coherence_findings([inst], "hard", "m"), [],
                             f"a lens on {role} is valid")

    def test_persona_leg_does_not_emit_a_dangling_lens_finding(self):
        """The PERSONA-coherence leg owns only persona-internal rules — it does NOT do the
        dangling/unconsumed-lens check (an installed review lens nothing consumes), which is the
        separate dangling_lens_findings leg (below). A review persona carrying an arbitrary lens is
        therefore SILENT here (lens is an open vocabulary); the dangling check is what judges it."""
        f = validate.agent_coherence_findings([{**VALID_REVIEWER, "lens": "nobody-consumes-this"}], "hard", "m")
        self.assertEqual(f, [])

    # --- the permissions↔write-tools rule: a read-only persona must BLOCK Edit/Write/NotebookEdit ---

    def test_readonly_with_denylist_blocking_writes_is_clean(self):
        inst = {**VALID_REVIEWER, "disallowedTools": ["Edit", "Write", "NotebookEdit"]}
        self.assertEqual(validate.agent_coherence_findings([inst], "hard", "m"), [])

    def test_readonly_with_write_excluding_allowlist_is_clean(self):
        """A tools ALLOWLIST that omits the write tools blocks them just as a denylist does — the
        execution lenses keep Bash and broad read/MCP reach via the denylist form, but the allowlist
        form must also pass so the rule is mechanism-agnostic."""
        inst = {k: v for k, v in VALID_REVIEWER.items() if k != "disallowedTools"}
        inst["tools"] = ["Read", "Grep", "Glob", "WebFetch", "mcp__engine-knowledge-graph__find"]
        self.assertEqual(validate.agent_coherence_findings([inst], "hard", "m"), [])

    def test_readonly_with_no_tool_lock_is_a_finding_inherit_all(self):
        """The inherit-all trap: a read-only persona declaring NEITHER tools nor disallowedTools
        inherits every tool, including the write tools — the exact gap this rule closes."""
        inst = {k: v for k, v in VALID_REVIEWER.items() if k != "disallowedTools"}
        f = validate.agent_coherence_findings([inst], "hard", "m")
        self.assertTrue(any(x["severity"] == "hard" and "neither a tools allowlist nor a disallowedTools"
                            in x["message"] for x in f))

    def test_readonly_denylist_missing_a_write_tool_is_a_finding(self):
        inst = {**VALID_REVIEWER, "disallowedTools": ["Edit", "Write"]}   # NotebookEdit not blocked
        f = validate.agent_coherence_findings([inst], "hard", "m")
        self.assertTrue(any(x["severity"] == "hard" and "NotebookEdit" in x["message"] for x in f))

    def test_readonly_allowlist_including_a_write_tool_is_a_finding(self):
        inst = {k: v for k, v in VALID_REVIEWER.items() if k != "disallowedTools"}
        inst["tools"] = ["Read", "Grep", "Edit"]   # an allowlist that grants a write tool
        f = validate.agent_coherence_findings([inst], "hard", "m")
        self.assertTrue(any(x["severity"] == "hard" and "Edit" in x["message"] for x in f))

    def test_scoped_write_worker_without_a_lock_is_not_subject_to_the_rule(self):
        """The rule fires only on permissions: read-only — a scoped-write worker carrying no
        disallowedTools is NOT flagged (its write posture is legitimate)."""
        self.assertEqual(validate.agent_coherence_findings([VALID_WORKER], "hard", "m"), [])

    def test_bash_is_not_policed_the_honest_write_tool_floor(self):
        """Honest limit: a read-only persona that blocks the write tools but KEEPS Bash is clean —
        the execution roles (qa, audit) need Bash, and Bash-via-shell confinement is the orchestration
        worktree's + merge gate's job, not this static leg's. So Bash present is NOT a finding."""
        inst = {**VALID_REVIEWER, "disallowedTools": ["Edit", "Write", "NotebookEdit"]}  # Bash NOT denied
        self.assertEqual(validate.agent_coherence_findings([inst], "hard", "m"), [])


class TestDanglingLensLeg(unittest.TestCase):
    """The pure dangling/unconsumed-lens leg (validate.dangling_lens_findings): an INSTALLED review
    lens no build stage consumes is a finding; a fully-consumed roster is silent; a consumed lens with
    zero installed agents is NOT an error (the reverse direction is a disclosed no-op, not a coherence
    fault); only the two review roles carry a consumable lens. The consumer's fail-closed guard on an
    unreadable consumed set is tested in test_lens_consumption.py."""
    CONSUMED = {"product-intent", "architecture", "feasibility", "risk-governance",
                "spec-conformance", "usability", "technical-integrity", "security-governance"}

    def test_clean_when_every_installed_lens_is_consumed(self):
        agents = [{"name": "r", "role": "plan-review", "lens": "architecture"},
                  {"name": "q", "role": "pre-submission-review", "lens": "usability"}]
        self.assertEqual(validate.dangling_lens_findings(agents, self.CONSUMED, "hard", "m"), [])

    def test_installed_lens_no_stage_consumes_is_a_finding(self):
        agents = [{"name": "review-nobody-runs", "role": "plan-review", "lens": "orphaned-review"}]
        found = validate.dangling_lens_findings(agents, self.CONSUMED, "hard", "m")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["severity"], "hard")
        self.assertIn("no build stage consumes it", found[0]["message"])
        self.assertIn("orphaned-review", found[0]["message"])
        self.assertIn("review-nobody-runs", found[0]["message"])

    def test_consumed_lens_with_zero_agents_is_not_a_finding(self):
        """A lens listed in the consumed set but carried by no installed persona is a gate that ran
        no review — disclosed elsewhere as such, never a dangling-lens error here (installed − consumed)."""
        self.assertEqual(validate.dangling_lens_findings([], self.CONSUMED, "hard", "m"), [])

    def test_a_non_review_role_lens_is_not_judged_here(self):
        """worker/audit carry no consumable lens (the symmetric agent_coherence_findings guard owns that);
        this leg scopes strictly to the two review roles."""
        agents = [{"name": "w", "role": "worker", "lens": "orphaned-review"},
                  {"name": "a", "role": "audit", "lens": "orphaned-review"}]
        self.assertEqual(validate.dangling_lens_findings(agents, self.CONSUMED, "hard", "m"), [])


if __name__ == "__main__":
    unittest.main()
