#!/usr/bin/env python3
"""Self-tests for the module system: manifest grammar (module.v1 / engine.v1),
the ownership-coherence leg, and the module-coherence consumer.

Run: uv run --directory .engine --frozen -- python tools/selftest.py

These lock the load-bearing teeth: the manifest schemas bite on each malformed shape, the
ownership leg flags orphans and double-claims, the two committed schema rules resolve their
repo-root-relative schema override and pass the real manifests, and the consumer reports the
real repository as coherent. The deliverable-gate cold review attests each test's assertion
matches its name; CI runs them as a step in engine-ci.
"""
from __future__ import annotations
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate          # noqa: E402
import module_coherence  # noqa: E402
import wiring            # noqa: E402

MODULE_SCHEMA = validate.load_json(os.path.join(validate.SCHEMAS_DIR, "module.v1.json"))
ENGINE_SCHEMA = validate.load_json(os.path.join(validate.SCHEMAS_DIR, "engine.v1.json"))

VALID_MODULE = {"id": "x", "version": "1.0.0", "status": "optional",
                "provides": {"tool": [".engine/tools/*.py"]}}
VALID_ENGINE = {"engine_release": "0.0.0-dev", "packages": {"core": "0.0.0-dev"},
                "identity": "solo"}


def _errors(schema, instance):
    return list(validate.Draft202012Validator(schema).iter_errors(instance))


def _write(d, name, obj):
    p = os.path.join(d, name)
    with open(p, "w", encoding="utf-8") as fh:
        json.dump(obj, fh) if isinstance(obj, (dict, list)) else fh.write(obj)
    return p


def _run_kind(kind_fn, rule, files):
    """Run a kind callable with validate.target_files stubbed to `files`."""
    orig = validate.target_files
    validate.target_files = lambda r: list(files)
    try:
        return kind_fn(rule, {})
    finally:
        validate.target_files = orig


class TestModuleSchema(unittest.TestCase):
    def test_schema_is_well_formed(self):
        validate.Draft202012Validator.check_schema(MODULE_SCHEMA)

    def test_valid_manifest_passes(self):
        self.assertEqual(_errors(MODULE_SCHEMA, VALID_MODULE), [])

    def test_real_core_manifest_conforms(self):
        real = validate.load_json(os.path.join(validate.ROOT, ".engine", "modules", "core",
                                               "manifest.json"))
        self.assertEqual(_errors(MODULE_SCHEMA, real), [])

    def test_missing_required_field_is_flagged(self):
        for drop in ("id", "version", "status", "provides"):
            bad = {k: v for k, v in VALID_MODULE.items() if k != drop}
            self.assertTrue(_errors(MODULE_SCHEMA, bad), f"missing {drop} should fail")

    def test_status_outside_the_closed_set_is_flagged(self):
        self.assertTrue(_errors(MODULE_SCHEMA, {**VALID_MODULE, "status": "bogus"}))

    def test_version_must_be_semver(self):
        # #402 U07a: a non-semver version fails the hard schema gate rather than silently parsing to (0,) in
        # migration selection (validate._ver_tuple), which would mis-select/skip migrations. Strict
        # MAJOR.MINOR.PATCH (all shipped manifests are 0.1.0); the -dev/-rc pre-release suffix is allowed.
        for good in ("0.1.0", "1.4.0", "1.4.0-dev", "2.0.0-rc1", "10.20.30"):
            self.assertEqual(_errors(MODULE_SCHEMA, {**VALID_MODULE, "version": good}), [],
                             f"{good} is valid semver and must pass")
        for bad in ("abc", "latest", "v1", "1.4", "1", "", "1.2.3.4", "0"):
            self.assertTrue(_errors(MODULE_SCHEMA, {**VALID_MODULE, "version": bad}),
                            f"{bad} is not MAJOR.MINOR.PATCH semver and must be rejected")

    def test_migration_and_retired_capability_keys_must_be_canonical_semver(self):
        # #693: a version KEY in migrations/retired_capabilities is schema-constrained (propertyNames) to strict
        # MAJOR.MINOR.PATCH — no two-part key (the #689 range-boundary hazard), no pre-release suffix or fourth
        # part (a >3-part hazard), and no leading zeros (which int()-normalise to a colliding version, #694).
        # This is the KEY constraint, distinct from the entry-VALUE constraint (additionalProperties).
        mig = {"description": "x", "run": "migrations/a.py", "kind": "config"}
        ret = {"description": "gone"}
        for good in ("0.1.0", "1.4.0", "10.20.30"):
            self.assertEqual(_errors(MODULE_SCHEMA, {**VALID_MODULE, "migrations": {good: mig}}), [],
                             f"{good} is a canonical version key and must pass (migrations)")
            self.assertEqual(_errors(MODULE_SCHEMA, {**VALID_MODULE, "retired_capabilities": {good: ret}}), [],
                             f"{good} is a canonical version key and must pass (retired_capabilities)")
        for bad in ("0.4", "0.4.0-1", "0.4.0.0", "0.04.0", "00.4.0", "1", "", "latest"):
            self.assertTrue(_errors(MODULE_SCHEMA, {**VALID_MODULE, "migrations": {bad: mig}}),
                            f"{bad} is not a canonical version key and must be rejected (migrations)")
            self.assertTrue(_errors(MODULE_SCHEMA, {**VALID_MODULE, "retired_capabilities": {bad: ret}}),
                            f"{bad} is not a canonical version key and must be rejected (retired_capabilities)")

    def test_field_outside_the_grammar_is_flagged(self):
        self.assertTrue(_errors(MODULE_SCHEMA, {**VALID_MODULE, "extra": 1}))

    def test_wires_must_be_fully_formed_per_seam(self):
        # The closed-seam gate is retained: an unknown type is still rejected (no escape hatch).
        self.assertTrue(_errors(MODULE_SCHEMA, {**VALID_MODULE, "wires": [{"type": "custom"}]}),
                        "an unknown seam type must be rejected (no wiring escape hatch)")
        # A fully-formed wire of each seam passes.
        full = {
            "hook": {"type": "hook", "event": "PreToolUse", "matcher": "Bash",
                     "hook": {"type": "command", "command": "${CLAUDE_PROJECT_DIR}/.engine/x.py"}},
            "mcp": {"type": "mcp", "name": "engine-x", "definition": {"command": "uv", "args": []}},
            "ontology-entry": {"type": "ontology-entry", "name": "widget", "record": {"a": 1}},
            "permission": {"type": "permission", "value": "Read(./src/**)"},
            "gitignore": {"type": "gitignore", "key": "core", "lines": [".engine/.venv/"]},
        }
        for seam, wire in full.items():
            self.assertEqual(_errors(MODULE_SCHEMA, {**VALID_MODULE, "wires": [wire]}), [],
                             f"a fully-formed {seam} wire must pass")
        # A BARE wire (type only, missing the seam's required fields) is now REJECTED — the gap this
        # slice closes (a {type:mcp} with no definition used to pass the hard CI module-manifest gate).
        for seam in ("hook", "mcp", "ontology-entry", "permission", "gitignore"):
            self.assertTrue(_errors(MODULE_SCHEMA, {**VALID_MODULE, "wires": [{"type": seam}]}),
                            f"a bare {seam} wire (missing required fields) must be rejected")
        # An extra key beyond the seam's vocabulary is rejected (additionalProperties:false).
        self.assertTrue(_errors(MODULE_SCHEMA, {**VALID_MODULE,
                        "wires": [{"type": "mcp", "name": "engine-x", "definition": {}, "extra": 1}]}),
                        "an unexpected key on a wire must be rejected")

    def test_depends_with_range_is_allowed(self):
        self.assertEqual(_errors(MODULE_SCHEMA, {**VALID_MODULE, "depends": {"core": ">=1.0.0"}}), [])


class TestVersionKeyDuplicateFindings(unittest.TestCase):
    """#694: two keys in a migrations/retired_capabilities block that normalise to the SAME version are refused.
    The leg reads RAW json so it also catches a LITERAL duplicate key (which json.load silently collapses)."""

    def _find(self, raw, mid="m"):
        with tempfile.TemporaryDirectory() as d:
            p = _write(d, "manifest.json", raw)
            return validate.version_key_duplicate_findings([(p, mid)], "hard", "fix it")

    def test_distinct_keys_that_normalise_equal_are_flagged(self):
        fs = self._find('{"id":"m","migrations":{"0.4":{"description":"a","run":"r","kind":"config"},'
                        '"0.4.0":{"description":"b","run":"r2","kind":"config"}}}')
        self.assertEqual(len(fs), 1)
        self.assertEqual(fs[0]["severity"], "hard")
        self.assertIn("mean the same version", fs[0]["message"])

    def test_a_literal_duplicate_key_json_collapses_is_still_caught(self):
        # json.load keeps only the last "0.4.0"; the leg re-reads raw to see the collapsed duplicate.
        fs = self._find('{"id":"m","retired_capabilities":{"0.4.0":{"description":"a"},'
                        '"0.4.0":{"description":"b"}}}')
        self.assertEqual(len(fs), 1)
        self.assertIn("0.4.0", fs[0]["message"])

    def test_leading_zero_collision_is_flagged(self):
        # 0.04.0 and 0.4.0 both pass the schema pattern yet int()-normalise to (0,4,0).
        self.assertEqual(len(self._find(
            '{"id":"m","migrations":{"0.04.0":{"description":"a","run":"r","kind":"config"},'
            '"0.4.0":{"description":"b","run":"r2","kind":"config"}}}')), 1)

    def test_distinct_versions_and_absent_or_empty_blocks_are_clean(self):
        self.assertEqual(self._find(
            '{"id":"m","migrations":{"0.3.0":{"description":"a","run":"r","kind":"config"},'
            '"0.4.0":{"description":"b","run":"r2","kind":"config"}}}'), [])
        self.assertEqual(self._find('{"id":"m"}'), [])
        self.assertEqual(self._find('{"id":"m","migrations":{}}'), [])

    def test_an_unreadable_manifest_is_surfaced_not_silently_skipped(self):
        # Fail-closed: if the raw re-read fails (a race after discovery already parsed the file), the leg
        # SURFACES it rather than reporting clean — matching the codebase's halt-on-malformed posture.
        fs = validate.version_key_duplicate_findings([("/no/such/manifest.json", "ghost")], "hard", "fix it")
        self.assertEqual(len(fs), 1)
        self.assertEqual(fs[0]["severity"], "hard")
        self.assertIn("Could not re-read", fs[0]["message"])

    def test_check_coherence_surfaces_a_collision(self):
        # Wiring proof: a colliding manifest in the discovered set makes check_coherence report a HARD finding.
        with tempfile.TemporaryDirectory() as d:
            p = _write(d, "manifest.json",
                       '{"id":"dupmod","migrations":{"0.4":{"description":"a","run":"r","kind":"config"},'
                       '"0.4.0":{"description":"b","run":"r2","kind":"config"}}}')
            fake = (p, {"id": "dupmod", "migrations": {
                "0.4": {"description": "a", "run": "r", "kind": "config"},
                "0.4.0": {"description": "b", "run": "r2", "kind": "config"}}})
            real = module_coherence.discover_manifests()
            saved = module_coherence.discover_manifests
            module_coherence.discover_manifests = lambda: real + [fake]
            try:
                findings = module_coherence.check_coherence()
            finally:
                module_coherence.discover_manifests = saved
        hard = [f for f in findings if f["severity"] == "hard"]
        self.assertTrue(any("mean the same version" in f["message"] for f in hard),
                        "check_coherence must surface a version-key collision as a hard finding")


class TestEngineSchema(unittest.TestCase):
    def test_schema_is_well_formed(self):
        validate.Draft202012Validator.check_schema(ENGINE_SCHEMA)

    def test_valid_manifest_passes(self):
        self.assertEqual(_errors(ENGINE_SCHEMA, VALID_ENGINE), [])

    def test_real_engine_manifest_conforms(self):
        real = validate.load_json(os.path.join(validate.ROOT, ".engine", "engine.json"))
        self.assertEqual(_errors(ENGINE_SCHEMA, real), [])

    def test_missing_required_field_is_flagged(self):
        for drop in ("engine_release", "packages", "identity"):
            bad = {k: v for k, v in VALID_ENGINE.items() if k != drop}
            self.assertTrue(_errors(ENGINE_SCHEMA, bad), f"missing {drop} should fail")

    def test_identity_outside_the_closed_set_is_flagged(self):
        self.assertTrue(_errors(ENGINE_SCHEMA, {**VALID_ENGINE, "identity": "boss"}))

    def test_field_outside_the_grammar_is_flagged(self):
        self.assertTrue(_errors(ENGINE_SCHEMA, {**VALID_ENGINE, "extra": 1}))

    def test_removed_capabilities_stamped_entry_validates(self):
        # #688: a fully-stamped removal record is valid.
        rc = {"legacy": {"description": "you could ask X; now ask Y", "removed_in": "0.4.0"}}
        self.assertEqual(_errors(ENGINE_SCHEMA, {**VALID_ENGINE, "removed_capabilities": rc}), [])

    def test_removed_capabilities_description_only_entry_validates(self):
        # removed_in is OPTIONAL: an entry authored at removal time (before the cut stamps removed_in) must
        # validate, or CI would redden the tree during the merge-to-cut window.
        rc = {"legacy": {"description": "you could ask X; now ask Y"}}
        self.assertEqual(_errors(ENGINE_SCHEMA, {**VALID_ENGINE, "removed_capabilities": rc}), [])

    def test_removed_capabilities_requires_a_nonempty_description(self):
        self.assertTrue(_errors(ENGINE_SCHEMA, {**VALID_ENGINE, "removed_capabilities": {"x": {}}}))
        self.assertTrue(_errors(ENGINE_SCHEMA, {**VALID_ENGINE,
                                                "removed_capabilities": {"x": {"description": ""}}}))

    def test_removed_capabilities_rejects_an_extra_field(self):
        rc = {"x": {"description": "d", "surprise": 1}}
        self.assertTrue(_errors(ENGINE_SCHEMA, {**VALID_ENGINE, "removed_capabilities": rc}))

    def test_removed_capabilities_block_is_optional(self):
        self.assertEqual(_errors(ENGINE_SCHEMA, VALID_ENGINE), [])   # absent stays valid


class TestSchemaRulesIntegration(unittest.TestCase):
    """The two committed schema rules resolve their repo-root-relative params.schema override
    and bite. Proves the override path (base = ROOT, not the schemas dir) — the
    plan-gate's blocking fix — actually validates the manifests it targets."""

    def _rule(self, name):
        return validate.load_json(os.path.join(validate.CHECK_DIR, name))

    def test_rules_are_well_formed_and_join_ci(self):
        check_schema = validate.load_json(os.path.join(validate.SCHEMAS_DIR, "check.v1.json"))
        for name in ("module-manifest.json", "engine-manifest.json"):
            rule = self._rule(name)
            self.assertEqual(list(validate.Draft202012Validator(check_schema).iter_errors(rule)), [])
            self.assertIn("CI", rule.get("suites", []))

    def test_real_module_manifest_passes_via_the_rule(self):
        rule = self._rule("module-manifest.json")
        real = os.path.join(validate.ROOT, ".engine", "modules", "core", "manifest.json")
        passed, found = _run_kind(validate.kind_schema, rule, [real])
        self.assertTrue(passed)
        self.assertEqual(found, [])

    def test_real_engine_manifest_passes_via_the_rule(self):
        rule = self._rule("engine-manifest.json")
        real = os.path.join(validate.ROOT, ".engine", "engine.json")
        passed, found = _run_kind(validate.kind_schema, rule, [real])
        self.assertTrue(passed)
        self.assertEqual(found, [])

    def test_malformed_manifest_is_flagged_at_tier_via_the_rule(self):
        rule = self._rule("module-manifest.json")
        with tempfile.TemporaryDirectory() as d:
            bad = _write(d, "manifest.json", {"id": "x"})  # missing version/status/provides
            passed, found = _run_kind(validate.kind_schema, rule, [bad])
        self.assertFalse(passed)
        self.assertTrue(any(f["severity"] == "hard" for f in found))

    def test_malformed_wire_is_flagged_via_the_rule(self):
        # The end-to-end CI path for defect (a): a manifest whose wire is missing its seam's
        # required fields ({type:mcp} with no definition) fails the hard module-manifest gate.
        rule = self._rule("module-manifest.json")
        with tempfile.TemporaryDirectory() as d:
            bad = _write(d, "manifest.json", {**VALID_MODULE, "wires": [{"type": "mcp"}]})
            passed, found = _run_kind(validate.kind_schema, rule, [bad])
        self.assertFalse(passed)
        self.assertTrue(any(f["severity"] == "hard" for f in found))


class TestOwnershipFindings(unittest.TestCase):
    """The pure ownership leg: orphan (no owner, not exempt), double-claim (>1 owner),
    exempt suppresses an orphan, and a single owner is clean."""

    def test_clean_when_each_file_has_one_owner_or_is_exempt(self):
        inv = [".engine/check/a.json", ".engine/engine.json"]
        claims = {".engine/check/a.json": ["core"]}
        self.assertEqual(
            validate.ownership_findings(inv, claims, {".engine/engine.json"}, "hard", "m"), [])

    def test_orphan_is_flagged(self):
        found = validate.ownership_findings([".engine/extra/thing.json"], {}, set(), "hard", "m")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["severity"], "hard")
        self.assertIn("orphan", found[0]["message"])
        self.assertEqual(found[0]["location"]["file"], ".engine/extra/thing.json")

    def test_exempt_file_is_not_an_orphan(self):
        self.assertEqual(
            validate.ownership_findings([".engine/uv.lock"], {}, {".engine/uv.lock"}, "hard", "m"), [])

    def test_double_claim_is_flagged(self):
        found = validate.ownership_findings(
            [".engine/x.json"], {".engine/x.json": ["core", "other"]}, set(), "hard", "m")
        self.assertEqual(len(found), 1)
        self.assertIn("more than one module", found[0]["message"])
        self.assertIn("core", found[0]["message"])
        self.assertIn("other", found[0]["message"])


class TestWiringFindings(unittest.TestCase):
    """The pure forward wiring leg: a not-applied directive flags HARD (uniform across all five
    seams — mcp too, because the carve-out is approval-blindness, not a soft tier), an applied one
    does not. The applied flag is supplied by the caller, so the leg is pure (no filesystem here)."""

    def test_applied_directives_produce_no_finding(self):
        declared = [("core", "mcp", ".mcp.json", True), ("core", "gitignore", ".gitignore", True)]
        self.assertEqual(validate.wiring_findings(declared, "hard", "m"), [])

    def test_not_applied_is_a_hard_finding_uniformly(self):
        for seam in ("hook", "mcp", "ontology-entry", "permission", "gitignore"):
            found = validate.wiring_findings([("core", seam, ".target", False)], "hard", "fix it")
            self.assertEqual(len(found), 1, f"a not-applied {seam} wire must flag")
            self.assertEqual(found[0]["severity"], "hard",
                             f"{seam} flags HARD (mcp too — approval-blind, not a soft tier)")
            self.assertIn(seam, found[0]["message"])
            self.assertIn("not applied", found[0]["message"])


class TestOrphanWireFindings(unittest.TestCase):
    """The pure REVERSE wiring leg (orphan-wire): an applied engine entry no manifest
    declares flags HARD; a declared one does not; a drifted same-identity entry is NOT double-flagged
    (the forward leg owns drift). permission and ontology-entry are excluded by construction —
    declared_wire_identity returns None and the enumerator never emits them."""

    def test_undeclared_applied_entry_is_flagged_per_shared_seam(self):
        for seam, key in (("hook", ("PreToolUse", "", "command", ".engine/x.py")),
                          ("mcp", "engine-x"), ("gitignore", "x-cache")):
            found = validate.orphan_wire_findings([(seam, key, ".target")], set(), "hard", "fix it")
            self.assertEqual(len(found), 1, f"an undeclared applied {seam} must flag")
            self.assertEqual(found[0]["severity"], "hard")
            self.assertIn(seam, found[0]["message"])
            self.assertIn("no installed module declares", found[0]["message"])

    def test_declared_applied_entry_is_not_flagged(self):
        applied = [("mcp", "engine-x", ".mcp.json")]
        self.assertEqual(validate.orphan_wire_findings(applied, {("mcp", "engine-x")}, "hard", "m"), [])

    def test_drifted_name_identity_entry_is_not_an_orphan(self):
        # mcp/gitignore identity is the name/key (not content), so a content-drifted entry keeps its
        # identity, still matches a declared directive, and is reported ONCE by the forward leg — not
        # double-flagged here.
        applied = [("mcp", "engine-knowledge-graph", ".mcp.json")]
        self.assertEqual(
            validate.orphan_wire_findings(applied, {("mcp", "engine-knowledge-graph")}, "hard", "m"), [])

    def test_hook_with_a_changed_command_is_an_orphan_by_the_identity_model(self):
        # A hook's identity is the full (event, matcher, type, command) tuple, so an applied engine hook whose command differs from every declared one IS an orphan
        # here — while the declared hook is separately reported not-applied by the forward leg. Two
        # accurate findings about two real facts, by design (documented on orphan_wire_findings).
        applied = [("hook", ("Stop", "", "command", ".engine/EDITED.py"), ".claude/settings.json")]
        declared = {("hook", ("Stop", "", "command", ".engine/orig.py"))}
        found = validate.orphan_wire_findings(applied, declared, "hard", "m")
        self.assertEqual(len(found), 1)
        self.assertIn("hook", found[0]["message"])

    def test_declared_wire_identity_is_single_homed_and_excludes_permission_and_ontology(self):
        self.assertIsNone(wiring.declared_wire_identity({"type": "permission", "value": "Bash(x)"}))
        self.assertIsNone(wiring.declared_wire_identity({"type": "ontology-entry", "name": "x"}))
        self.assertEqual(wiring.declared_wire_identity({"type": "mcp", "name": "engine-x"}),
                         ("mcp", "engine-x"))
        self.assertEqual(wiring.declared_wire_identity(
            {"type": "hook", "event": "Stop", "matcher": "",
             "hook": {"type": "command", "command": ".engine/c"}}),
            ("hook", ("Stop", "", "command", ".engine/c")))


class TestModuleCoherenceConsumer(unittest.TestCase):
    """The consumer over the REAL repository: discovery reads the present set, and both
    coherence legs report the committed tree as coherent (the clean baseline the demo
    starts from)."""

    def test_discover_manifests_finds_core(self):
        ids = [m.get("id") for _path, m in module_coherence.discover_manifests()]
        self.assertIn("core", ids)

    def test_load_engine_manifest_reads_solo_tier(self):
        em = module_coherence.load_engine_manifest()
        self.assertIsNotNone(em)
        self.assertEqual(em.get("identity"), "solo")
        self.assertIn("core", em.get("packages", {}))

    def test_provides_claims_map_real_files_to_core(self):
        claims = module_coherence.provides_claims(module_coherence.discover_manifests())
        self.assertEqual(claims.get(".engine/suites.json"), ["core"])  # the foundation group
        self.assertEqual(claims.get(".engine/tools/validate.py"), ["core"])

    def test_engine_parts_skill_is_claimed_by_core(self):
        # #402 U06a: engine-parts (shipped in #400) was committed under .claude/skills/ but claimed by NO
        # module's provides, so it rode along invisibly — the ownership walk is .engine/-only, and the
        # claim-driven graph inventory only entitizes CLAIMED files. Core now claims it, so the graph tracks it.
        # The ownership walk is deliberately NOT widened to .claude/ (co-occupied with operators' own
        # un-prefixed product skills — knowledge_gen.surface_instance_inventory / skill.v1); the residual
        # "an unclaimed engine skill under .claude/ can still ride invisibly" is a logged gap, not closed here.
        import knowledge_gen
        parts = ".claude/skills/engine-parts/SKILL.md"
        claims = module_coherence.provides_claims(module_coherence.discover_manifests())
        self.assertEqual(claims.get(parts), ["core"], "core must claim the engine-parts skill")
        catalog = validate.load_json(validate.CATALOG_PATH)
        self.assertIn(parts, knowledge_gen.surface_instance_inventory(catalog, claims),
                      "engine-parts must now be a tracked surface instance in the claim-driven inventory")

    def test_core_provides_no_gitkeep_placeholders_and_no_agent_group(self):
        # #411: core once carried .claude/skills/.gitkeep and a .claude/agents/.gitkeep-only `agent`
        # group as empty-dir placeholders. Both directories are now populated in every deployed repo (core
        # provides the engine-* skills; required audit-library provides .claude/agents/engine-audit.md), so the
        # placeholders are obsolete and a literal provides-kind read must be coherent: no .gitkeep in any
        # group, and core declares no `agent` group (it provides no persona).
        core = next(m for _p, m in module_coherence.discover_manifests() if m.get("id") == "core")
        provides = core.get("provides") or {}
        flat = [f for group in provides.values() for f in group]
        self.assertFalse(any(f.endswith("/.gitkeep") or f.endswith(".gitkeep") for f in flat),
                         "no .gitkeep placeholder remains in core's provides")
        self.assertNotIn("agent", provides, "core provides no persona, so it declares no agent group")
        self.assertIn(".claude/skills/engine-parts/SKILL.md", provides.get("skill", []))

    def test_check_corpus_split_core_two_guards_validators_core_owns_the_rest(self):
        # The locked engine/corpus boundary:
        # core ships the validation engine and owns ZERO rules EXCEPT the two frozen-named guards;
        # the self-validation corpus is validators-core's (65 rules: the read-only-persona write-lock
        # guard (the read-only-persona write-lock guard — every read-only review/audit persona must block
        # the file-writing tools, the live consumer of agent_coherence_findings) atop the negative-fixture
        # meta-check (engine-template #286 — the checker-of-checkers) atop
        # the untracked-surface detector
        # (engine-template #281 — names a surface file git is not tracking, e.g. sync-conflict cruft) atop
        # the optional-module catalog schema gate
        # (engine-template #254 — keeps the first-run catalog matching provisioning-catalog.v1.json now that a
        # command-less optional module is offerable) atop the memory-backup pointer public-safety
        # guard (engine-template #224 — keeps a configured vault pointer from shipping in the public
        # template) atop the in-tool demo failure-path floor
        # (engine-template #171) and the 31 prior plus the two
        # audit-digest gates (the audit-library module's seal/fingerprint gate and its staleness signal,
        # validated HERE — the detection-relay shape, audit-library owns the digest machinery while
        # validators-core owns the rules that verify it, exactly as the first-run reference-closure gate
        # enforces provisioning's invariant from here), and before them the audit
        # checklist schema gate (the audit-library module's concern-list, validated HERE because
        # engine-self-validation consolidates in validators-core, not the surface's owner), and before that the first-run
        # reference-closure gate (issue #150), and before it the
        # knowledge-vocabulary parity guard (issue #131) — the 20 after the
        # operation grammar and the skill grammar plus the doc-frontmatter and
        # doc-shape grammar rules and the uv-group-drift gate and the
        # skill-coherence self-election guard and the policy-override-stale rule
        # — plus the conduct-frontmatter, conduct-shape, and conduct-weakening-guard rules
        # the grammar plus the soft guard for the new codes-of-conduct surface).
        # The files stay under .engine/check/ — ownership is a `provides` claim, not a location. This
        # test pins that exact split so a future wildcard re-introduction (which would double-claim the
        # corpus) cannot pass silently.
        # An OPTIONAL module may additionally own a *domain* check — one that inspects the operator's
        # PRODUCT, not the engine itself — categorically distinct from core's two engine guards and from
        # validators-core's engine-self-validation corpus. dependency-discipline is the first: it owns the
        # dependency-pinning rule and the dependency-review gate, the module being "the
        # content" over core's check engine. The partition below therefore admits a third owner; the real
        # boundary this test pins is unchanged — each check is owned by exactly ONE module, core stays frozen
        # at its two guards, and no wildcard may re-claim the corpus.
        manifests = module_coherence.discover_manifests()
        ids = {m.get("id") for _path, m in manifests}
        self.assertIn("validators-core", ids, "validators-core must be a present module")

        claims = module_coherence.provides_claims(manifests)
        check_owner = {rel: owners for rel, owners in claims.items()
                       if rel.startswith(".engine/check/") and rel.endswith(".json")}
        # every check file is owned by exactly one module (no orphan, no double-claim)
        for rel, owners in check_owner.items():
            self.assertEqual(len(owners), 1, f"{rel} must have exactly one owner, got {owners}")

        def optional_owner(module_id, expected, why):
            """The checks an OPTIONAL module owns: the exact list when that module is installed, and
            nothing when it is not. Conditional on presence rather than assuming it — an optional module is
            offered at first-run setup and can be removed afterwards, so pinning its list unconditionally
            would assert a fact about which add-ons the HOST repository happens to carry. That reds a
            deployment's required self-tests for merely declining an add-on, with no fix but patching an
            engine-owned test the next upgrade reverts. The partition below accounts for every check
            either way, so the real boundary this test pins is unweakened."""
            owned = sorted(r for r, o in check_owner.items() if o == [module_id])
            self.assertEqual(owned, expected if module_id in ids else [], why)
            return owned

        core_checks = sorted(r for r, o in check_owner.items() if o == ["core"])
        vc_checks = sorted(r for r, o in check_owner.items() if o == ["validators-core"])
        self.assertEqual(core_checks, [
            ".engine/check/guardrail-weakening.json",
            ".engine/check/protection.json",
        ], "core owns exactly the two weakening guards")
        self.assertEqual(vc_checks, [
            ".engine/check/agent-coherence.json",
            ".engine/check/agent-frontmatter.json",
            ".engine/check/agent-shape.json",
            ".engine/check/audit-concern-list.json",
            ".engine/check/audit-digest-fingerprint.json",
            ".engine/check/audit-digest-staleness.json",
            ".engine/check/block-coherence.json",
            ".engine/check/catalog-completeness.json",
            ".engine/check/catalog-coverage.json",
            ".engine/check/census-completeness.json",
            ".engine/check/codex-agent-coherence.json",
            ".engine/check/codex-agent-schema.json",
            ".engine/check/codex-hooks-schema.json",
            ".engine/check/codex-provider-parity.json",
            ".engine/check/codex-skill-coherence.json",
            ".engine/check/codex-skill-frontmatter.json",
            ".engine/check/codex-skill-shape.json",
            ".engine/check/conduct-frontmatter.json",
            ".engine/check/conduct-shape.json",
            ".engine/check/conduct-weakening-guard.json",
            ".engine/check/contract-frontmatter.json",
            ".engine/check/contract-shape.json",
            ".engine/check/contract-threshold.json",
            ".engine/check/doc-frontmatter.json",
            ".engine/check/doc-shape.json",
            ".engine/check/engine-manifest.json",
            ".engine/check/engine-todo-form.json",
            ".engine/check/execution-state.json",
            ".engine/check/first-run-assets.json",
            ".engine/check/first-run-reference-closure.json",
            ".engine/check/hard-check-bite.json",
            ".engine/check/in-tool-demo-failure-path.json",
            ".engine/check/interface-coherence.json",
            ".engine/check/interface-declaration.json",
            ".engine/check/knowledge-coverage.json",
            ".engine/check/knowledge-vocabulary.json",
            ".engine/check/lens-consumption.json",
            ".engine/check/link-integrity.json",
            ".engine/check/manifest-write-funnel.json",
            ".engine/check/memory-pointer-public-safety.json",
            ".engine/check/model-bindings-schema.json",
            ".engine/check/module-manifest.json",
            ".engine/check/ontology-authority-reservation.json",
            ".engine/check/operation-frontmatter.json",
            ".engine/check/operation-shape.json",
            ".engine/check/operator-guarded-paths.json",
            ".engine/check/operator-local-references.json",
            ".engine/check/policy-frontmatter.json",
            ".engine/check/policy-override-stale.json",
            ".engine/check/policy-shape.json",
            ".engine/check/pr-behaviors-declared.json",
            ".engine/check/pr-body-completeness.json",
            ".engine/check/provider-exceptions-schema.json",
            ".engine/check/provider-vocabulary-confinement.json",
            ".engine/check/provisioning-catalog.json",
            ".engine/check/release-integrity.json",
            ".engine/check/self-map-drift.json",
            ".engine/check/shipped-issue-references.json",
            ".engine/check/skill-coherence.json",
            ".engine/check/skill-frontmatter.json",
            ".engine/check/skill-shape.json",
            ".engine/check/state-cursor.json",
            ".engine/check/template-shape-spec.json",
            ".engine/check/untracked-surface.json",
            ".engine/check/uv-group-drift.json",
        ], "validators-core owns exactly the 65 corpus rules")
        # the optional-module-owned DOMAIN checks: dependency-discipline inspects the product's dependencies,
        # not the engine — outside both core's guards and validators-core's self-validation corpus.
        dd_checks = optional_owner("dependency-discipline", [
            ".engine/check/dependency-pinning.json",
            ".engine/check/dependency-review.json",
        ], "dependency-discipline owns exactly its pinning and review checks")
        # external-contribution (an optional module) owns the upstream-clean nudge — a check that inspects an
        # OUTGOING cross-fork contribution diff for engine-owned paths. Like dependency-discipline's domain
        # checks it is an optional-module-owned check, neither core's guard nor validators-core's
        # self-validation corpus; the partition must admit it. The real boundary is unchanged — exactly one
        # owner per check, core frozen at its two guards, no wildcard re-claiming the corpus.
        ec_checks = optional_owner("external-contribution", [
            ".engine/check/upstream-clean.json",
        ], "external-contribution owns exactly the upstream-clean nudge")
        # migration-discipline (an optional module) owns the rollback-presence nudge — a check that inspects
        # the PRODUCT's own database migrations for a separate rollback script. Like dependency-discipline's
        # and external-contribution's domain checks it is an optional-module-owned check, neither core's
        # guard nor validators-core's self-validation corpus; the partition must admit it. The real boundary
        # is unchanged — exactly one owner per check, core frozen at its two guards, no wildcard re-claiming
        # the corpus.
        md_checks = optional_owner("migration-discipline", [
            ".engine/check/migration-rollback.json",
        ], "migration-discipline owns exactly the rollback-presence nudge")
        # product-design (an optional module) owns the spec-form check — a check that inspects the PRODUCT's
        # own committed docs/spec/ tree for well-formed structure (required sections, a well-formed
        # acceptance-criteria table, every doc reachable from the index). Like dependency-discipline's,
        # external-contribution's, and migration-discipline's domain checks it is an optional-module-owned
        # check, neither core's guard nor validators-core's self-validation corpus; the partition must
        # admit it. The real boundary is unchanged — exactly one owner per check, core frozen at its two
        # guards, no wildcard re-claiming the corpus. (This exact list extends as the
        # acceptance-criteria-coverage check lands.)
        pd_checks = optional_owner("product-design", [
            ".engine/check/product-adr-form.json",
            ".engine/check/product-design-form.json",
            ".engine/check/product-lock-integrity.json",
            ".engine/check/product-spec-coverage.json",
            ".engine/check/product-spec-form.json",
            ".engine/check/product-spec-matrix.json",
        ], "product-design owns the ADR-form, design-form, spec-form, coverage, lock-integrity, and "
           "obligation-matrix drift checks")
        # the split partitions ALL committed check files — nothing left unclaimed
        all_checks = sorted(r for r in module_coherence.engine_file_inventory()
                            if r.startswith(".engine/check/") and r.endswith(".json"))
        self.assertEqual(
            sorted(core_checks + vc_checks + dd_checks + ec_checks + md_checks + pd_checks), all_checks,
            "every .engine/check/*.json is claimed by exactly one of "
            "core / validators-core / dependency-discipline / external-contribution / "
            "migration-discipline / product-design")
        # validators-core depends on core (presence assertion, any version)
        vc = next(m for _p, m in manifests if m.get("id") == "validators-core")
        self.assertEqual(vc.get("depends"), {"core": ""})

    def test_audit_library_owns_persona_and_concern_list(self):
        # audit-library (required, L3) ships the static self-audit artifacts: the audit persona, the
        # seeded concern-list (audits-owned data), and the operator setup page for arming the scheduled
        # self-review. It ALSO owns the run-time self-review digest (.engine/audits/audit-digest.md) — the
        # plain-language output the scheduled run writes and commits. The digest does not exist at
        # construction time (provides_claims only claims files that exist, so the literal entry claims
        # nothing here), but once a real run commits one it is a "system-owned" artifact (audits design),
        # so claiming it keeps a committed digest from reading as an ownership orphan — the gap the first
        # real run (digest PR #194) exposed. The literal path (not a `*.md` glob) avoids double-claiming the
        # setup page. It owns NO check or schema this slice — the concern-list check is validators-core's and
        # the schema rides core's schema glob — and it depends on validators-core only (see the dedicated
        # depends test below for why the explicit core edge was dropped).
        manifests = module_coherence.discover_manifests()
        al = next((m for _p, m in manifests if m.get("id") == "audit-library"), None)
        self.assertIsNotNone(al, "audit-library must be a present module")
        self.assertEqual(al.get("status"), "required")
        self.assertEqual(al.get("wires"), [])
        self.assertEqual(al.get("depends"), {"validators-core": ""})

    def test_audit_library_depends_on_validators_core_only(self):
        # #402: the explicit `core` edge is redundant and was dropped. The catalog table,
        # the dependency graph, and audit-library's own spec all name validators-core
        # ONLY; core reaches audit-library transitively (core -> validators-core -> audit-library),
        # since validators-core declares depends {core}. Dropping the edge is behaviour-neutral: the install
        # order is unchanged because core stays present and still orders before validators-core.
        manifests = module_coherence.discover_manifests()
        al = next(m for _p, m in manifests if m.get("id") == "audit-library")
        self.assertEqual(set(al.get("depends", {})), {"validators-core"},
                         "audit-library's only direct dependency is validators-core (the redundant core edge is gone)")
        # core still present and still transitively reached, so the topological order is unchanged.
        order = [m["id"] for m in validate.topological_order([m for _p, m in manifests])]
        self.assertLess(order.index("core"), order.index("validators-core"))
        self.assertLess(order.index("validators-core"), order.index("audit-library"))
        self.assertEqual(al.get("provides"), {
            "agent": [".claude/agents/engine-audit.md"],
            "audits": [".engine/audits/concern-list.json", ".engine/audits/self-review-setup.md",
                       ".engine/audits/audit-digest.md"],
            "codex-agent": [".codex/agents/engine-audit.toml"],
        }, "audit-library owns the persona (both runtime forms), the seeded concern-list, the setup page, "
           "and the run-time digest")

    def test_committed_digest_is_owned_not_an_orphan(self):
        # Regression (digest PR #194): the scheduled run commits .engine/audits/audit-digest.md, but that file
        # does not exist at construction time, so the construction-tree coherence test never meets it — it was
        # only on a real digest PR that the ownership walk saw it and, unclaimed, reported it as an orphan and
        # failed engine-ci. audit-library now claims it. This guards the claim the two ways
        # test_real_repository_is_coherent cannot (it never sees a digest):
        #   (a) the claimed path is the EXACT path the digest tool writes (audit_digest.AUDIT_DIGEST_PATH), so
        #       a path drift on either side is caught — not just a hard-coded string; and
        #   (b) the pure ownership leg treats a PRESENT, claimed digest as owned, and would still orphan the
        #       same digest if the claim were dropped (the mutation that proves this test bites).
        import audit_digest
        digest_rel = os.path.relpath(audit_digest.AUDIT_DIGEST_PATH, validate.ROOT).replace(os.sep, "/")
        al = next((m for _p, m in module_coherence.discover_manifests() if m.get("id") == "audit-library"), None)
        self.assertIn(digest_rel, al["provides"]["audits"],
                      "audit-library must claim the exact path the digest tool writes, or a committed digest orphans")
        # (b) the resolution proof, pure: a present digest claimed by audit-library is NOT an orphan; the same
        # digest UNCLAIMED is — so a future drop of the claim is caught downstream.
        owned = validate.ownership_findings([digest_rel], {digest_rel: ["audit-library"]},
                                            exempt=set(), tier="hard", message="")
        self.assertEqual(owned, [], "a claimed committed digest must not be an orphan")
        orphaned = validate.ownership_findings([digest_rel], {}, exempt=set(), tier="hard", message="")
        self.assertEqual(len(orphaned), 1, "an UNCLAIMED committed digest must orphan — proves this guard bites")
        self.assertIn("orphan", orphaned[0]["message"])

    def test_memory_substrate_owns_tools_and_the_erasure_proposal(self):
        # memory-substrate-sqlite-fts5 owns its tools (the memory/*.py glob) AND the committed
        # single-purpose erasure proposal the emitter writes + the observer reads — a content-free machine artifact
        # (a uuid-hex target + a plain-language cost), owned the way audit-library owns its concern-list, under an
        # "erasures" provides group with .engine/erasures/ carved into catalog-coverage's infra_dirs (it is engine
        # infrastructure, not an authored surface). Pinning the exact provides locks the new group against drift.
        manifests = module_coherence.discover_manifests()
        ms = next((m for _p, m in manifests if m.get("id") == "memory-substrate-sqlite-fts5"), None)
        self.assertIsNotNone(ms, "memory-substrate-sqlite-fts5 must be a present module")
        self.assertEqual(ms.get("provides"), {
            "tool": [".engine/tools/memory/*.py"],
            "erasures": [".engine/erasures/proposal.json"],
            "backup": [".engine/memory-backup/pointer.json"],
        }, "memory owns its tools, the committed erasure proposal, and the committed backup-vault pointer")

    def test_product_design_owns_its_front_door(self):
        # product-design (optional) grows from one read-only check to the operator-facing
        # front door: the engine-design skill (the typed command), the product-intake operation (the ceremony
        # runbook), the operator orientation doc, and the spec-authoring scaffold the operation writes a
        # docs/spec/ tree from. The scaffold is committed product-authoring template files (the maintainer's decision), homed beside the manifest under .engine/modules/ — NOT a catalogued surface
        # location, so it is owned (via this `scaffold` group) without being entitized into the knowledge
        # graph. Pinning the exact provides locks the expanded footprint against drift. Later work added the
        # lock-integrity check, then the acceptance-criteria-coverage check + the build-order scaffold
        # template (still under the same `scaffold` *.md glob, so only the check list grew); a further step extends the
        # operation/build-orchestration copy, not this provides list.
        manifests = module_coherence.discover_manifests()
        pd = next((m for _p, m in manifests if m.get("id") == "product-design"), None)
        if pd is None:
            # product-design is OPTIONAL — offered at first-run setup and removable afterwards. Requiring it
            # to be present would assert a fact about which add-ons the HOST repository carries, reddening a
            # deployment's required self-tests for declining one. Absent is a legitimate installed shape;
            # there is simply no footprint to pin, and the ownership partition already proves nothing is
            # left unclaimed in that shape.
            self.skipTest("product-design is not installed in this repository")
        self.assertEqual(pd.get("status"), "optional")
        # product-design's first wire (Slice: obligation-matrix): a PreToolUse regen hook that refreshes the
        # committed obligation matrix at the git-commit boundary, mirroring core's graph/self-map regen hooks.
        self.assertEqual(pd.get("wires"), [{
            "type": "hook", "event": "PreToolUse", "matcher": "",
            "hook": {"type": "command",
                     "command": "sh \"${CLAUDE_PROJECT_DIR}/.engine/tools/hook-runner.sh\" "
                                "\"${CLAUDE_PROJECT_DIR}/.engine/.venv/bin/python\" "
                                "\"${CLAUDE_PROJECT_DIR}/.engine/tools/product_design/obligation_matrix.py\" hook"},
        }, {
            "type": "codex-hook", "event": "PreToolUse",
            "hook": {"type": "command",
                     "command": 'cd "$(git rev-parse --show-toplevel 2>/dev/null || echo .)" && '
                                'sh ".engine/tools/codex-hook-runner.sh" '
                                '".engine/tools/product_design/obligation_matrix.py" hook'},
        }], "product-design wires its obligation-matrix commit-boundary regen hook on both runtimes")
        self.assertEqual(pd.get("depends"), {"core": ""})
        self.assertEqual(pd.get("provides"), {
            "check": [".engine/check/product-adr-form.json", ".engine/check/product-design-form.json",
                      ".engine/check/product-lock-integrity.json",
                      ".engine/check/product-spec-coverage.json", ".engine/check/product-spec-form.json",
                      ".engine/check/product-spec-matrix.json"],
            "tool": [".engine/tools/product_design/*.py"],
            "foundation": [".engine/product-spec-matrix.json"],
            "operation": [".engine/operations/product-intake.md"],
            "policy": [".engine/policies/spec-structure-integrity.md"],
            "skill": [".claude/skills/engine-design/SKILL.md"],
            "codex-skill": [".agents/skills/engine-design/SKILL.md",
                            ".agents/skills/engine-design/agents/openai.yaml"],
            "doc": [".engine/docs/product-design.md"],
            "scaffold": [".engine/modules/product-design/scaffold/*.md"],
        }, "product-design owns its front door: the ADR-form + design-form + spec-form + coverage + "
           "lock-integrity + obligation-matrix checks and their tools, the committed obligation matrix (its "
           "foundation file), the intake operation, the spec-structure-integrity policy, the engine-design "
           "skill (both runtime forms), the orientation doc, and the scaffold")

    def test_doc_ownership_is_partitioned_core_and_product_design(self):
        # The orientation doc lives under .engine/docs/, which core used to claim by a whole-surface glob
        # (.engine/docs/*.md). product-design now owns one doc there, so core's glob was narrowed to an
        # explicit list — a Python glob cannot carve out a sibling's file, and a doubly-claimed file is a HARD
        # ownership finding caught by test_real_repository_is_coherent. This pins the resulting split, so a
        # future re-widening of core's doc glob (which would re-double-claim product-design.md, or silently
        # re-grab a new product doc) is caught precisely here — not only as a coherence red with a vaguer
        # message. Defense-in-depth over the already-hard coherence leg.
        claims = module_coherence.provides_claims(module_coherence.discover_manifests())
        doc_owner = {rel: owners for rel, owners in claims.items()
                     if rel.startswith(".engine/docs/") and rel.endswith(".md")}
        for rel, owners in doc_owner.items():
            self.assertEqual(len(owners), 1, f"{rel} must have exactly one owner, got {owners}")
        self.assertEqual(sorted(r for r, o in doc_owner.items() if o == ["core"]),
                         [".engine/docs/getting-started.md"], "core owns exactly the getting-started doc")
        # product-design is OPTIONAL, so its footprint is asserted only when it is actually installed —
        # the same reason the check-ownership leg above is conditional. Requiring it to be present would red
        # a deployment's required self-tests for declining an add-on it was offered at setup.
        installed = {m.get("id") for _p, m in module_coherence.discover_manifests()}
        self.assertEqual(sorted(r for r, o in doc_owner.items() if o == ["product-design"]),
                         [".engine/docs/product-design.md"] if "product-design" in installed else [],
                         "product-design owns exactly its orientation doc when it is installed")

    def test_seed_concern_list_conforms_to_its_schema(self):
        # the committed seed concern-list is well-formed against concern-list.v1 — the same schema + dialect
        # the audit-concern-list check validates it with at the merge. Pins the seed (every entry carries its
        # required justification) and that the schema is itself usable.
        from jsonschema import Draft202012Validator
        schema = validate.load_json(os.path.join(validate.ROOT, ".engine/schemas/concern-list.v1.json"))
        data = validate.load_json(os.path.join(validate.ROOT, ".engine/audits/concern-list.json"))
        errors = [e.message for e in Draft202012Validator(schema).iter_errors(data)]
        self.assertEqual(errors, [], f"seed concern-list must conform: {errors}")
        self.assertTrue(data.get("concerns"), "the seed ships with concerns")
        for c in data["concerns"]:
            self.assertTrue(c.get("justification"), "every seed concern carries its justification")

    def test_real_repository_is_coherent(self):
        # The whole point of the slice: the committed tree has no unowned engine file and no
        # broken dependency. This is the green baseline the operator's fail-then-pass demo
        # perturbs (add a stray file -> orphan; add a ghost dependency -> unsatisfied).
        findings = module_coherence.check_coherence()
        hard = [f for f in findings if f["severity"] == "hard"]
        self.assertEqual(hard, [], f"unexpected coherence findings: {[f['message'] for f in hard]}")

    def test_no_default_on_module_depends_on_a_capability_not_guaranteed_present(self):
        # #891: the committed tree must satisfy the release-cut author guard — no default-on module may depend on
        # an optional / experimental / retired capability a deployment might lack. Runs the SAME guard the cut
        # uses, over the REAL present set, on every PR (unittest discovery in engine-ci) — so a future bad manifest
        # is caught at its introducing PR (and in first-cut mode), WITHOUT a check_coherence leg that would re-fire
        # tree-wide at operator upgrade and wedge #759's graceful default-on -> offer demotion.
        import release_cut
        violations = release_cut._default_on_dependency_violations(release_cut._present_modules())
        self.assertEqual(violations, [], f"default-on dependency violations in the committed tree: {violations}")

    def test_inventory_prunes_gitignored_memory_runtime_but_keeps_the_memory_code_package(self):
        # #180: the ownership inventory must skip the gitignored RUNTIME store `.engine/memory/` (the live
        # NDJSON ledger / index / capture-state / lock, created when the memory hooks run, never committed)
        # the same way `.venv`/`.pytest_cache` are skipped — else a working copy on which the engine has run
        # reports each runtime file as an unowned orphan and the full local suite fails. This is the
        # DETERMINISTIC guard: test_real_repository_is_coherent is silently green when `.engine/memory/`
        # happens to be empty (e.g. a fresh CI clone), so it does not by itself lock the carve-out.
        #
        # The load-bearing half is the SECOND assertion: the prune is anchored on the exact path
        # `.engine/memory`, NOT the bare name `memory`, so the committed memory CODE package
        # `.engine/tools/memory/` (owned via the module's `provides` glob) stays ownership-checked. A
        # bare-name `"memory"` prune would silently un-own that whole package — this test goes red if a
        # future change weakens the path-anchor to a name, or removes PRUNE_PATHS entirely.
        saved_root, saved_engine = validate.ROOT, validate.ENGINE_DIR
        try:
            with tempfile.TemporaryDirectory() as d:
                engine = os.path.join(d, ".engine")
                runtime = os.path.join(engine, "memory")             # gitignored runtime root -> must prune
                code = os.path.join(engine, "tools", "memory")       # committed code package -> must keep
                control = os.path.join(engine, "check")              # ordinary engine dir -> must keep
                for sub in (runtime, code, control):
                    os.makedirs(sub)
                open(os.path.join(runtime, "ledger.ndjson"), "w").close()
                open(os.path.join(code, "forget.py"), "w").close()
                open(os.path.join(control, "some-rule.json"), "w").close()
                validate.ROOT, validate.ENGINE_DIR = d, engine
                inv = module_coherence.engine_file_inventory()
            self.assertNotIn(".engine/memory/ledger.ndjson", inv,
                             "the gitignored .engine/memory/ runtime must be pruned (the #180 bug)")
            self.assertIn(".engine/tools/memory/forget.py", inv,
                          "the committed .engine/tools/memory/ code package must stay owned "
                          "(the path-anchor gotcha — a bare-name 'memory' prune would drop it)")
            self.assertIn(".engine/check/some-rule.json", inv,
                          "ordinary committed engine files are still inventoried (only runtime is excluded)")
        finally:
            validate.ROOT, validate.ENGINE_DIR = saved_root, saved_engine

    def test_inventory_prunes_the_fixtures_namespace_but_keeps_a_same_named_dir_elsewhere(self):
        # #286: the committed negative-fixture namespace `.engine/_fixtures/` holds deliberately-broken
        # test data that no module `provides`, so the ownership inventory must skip it — else every committed
        # fixture reports as an unowned orphan. This is the deterministic guard for the FIXTURE_PATHS prune,
        # the sibling of the PRUNE_PATHS test above.
        #
        # The load-bearing half is the SECOND assertion: the prune is anchored on the exact path
        # `.engine/_fixtures`, NOT the bare name `_fixtures`, so a same-named directory ANYWHERE ELSE in the
        # tree (e.g. `.engine/tools/_fixtures/`) stays ownership-checked. A bare-name `"_fixtures"` prune would
        # silently un-own any such directory — this test goes red if a future change weakens the path-anchor to
        # a name, or removes FIXTURE_PATHS entirely.
        saved_root, saved_engine = validate.ROOT, validate.ENGINE_DIR
        try:
            with tempfile.TemporaryDirectory() as d:
                engine = os.path.join(d, ".engine")
                fixtures = os.path.join(engine, "_fixtures", "kind-schema")   # the namespace -> must prune
                lookalike = os.path.join(engine, "tools", "_fixtures")        # same name, other path -> keep
                control = os.path.join(engine, "check")                       # ordinary engine dir -> keep
                for sub in (fixtures, lookalike, control):
                    os.makedirs(sub)
                open(os.path.join(fixtures, "bad.json"), "w").close()
                open(os.path.join(lookalike, "real.py"), "w").close()
                open(os.path.join(control, "some-rule.json"), "w").close()
                validate.ROOT, validate.ENGINE_DIR = d, engine
                inv = module_coherence.engine_file_inventory()
            self.assertNotIn(".engine/_fixtures/kind-schema/bad.json", inv,
                             "the committed-test-data .engine/_fixtures/ namespace must be pruned")
            self.assertIn(".engine/tools/_fixtures/real.py", inv,
                          "a same-named directory elsewhere must stay owned (the path-anchor gotcha — a "
                          "bare-name '_fixtures' prune would drop it)")
            self.assertIn(".engine/check/some-rule.json", inv,
                          "ordinary committed engine files are still inventoried (only the namespace is excluded)")
        finally:
            validate.ROOT, validate.ENGINE_DIR = saved_root, saved_engine

    def test_inventory_prunes_the_deployment_eADR_stream_but_keeps_the_engine_canon(self):
        # #410: the deployment's per-instance eADR stream `.engine/contracts/instance/`
        # holds COMMITTED decision records a deployment authors — in no module's `provides`, preserved across an
        # engine overlay. The ownership inventory must skip that subtree, or every real deployment eADR reports as
        # an unowned orphan in a deployed repo (the exact false-orphan class #410 fixes for `.engine/boot/`).
        # test_real_repository_is_coherent only proves the SEED README does not orphan; this is the DETERMINISTIC
        # guard that a real, non-README deployment eADR under instance/ is pruned, while the engine's own eADR
        # CANON directly in `.engine/contracts/` stays inventoried (it rides core's non-recursive glob).
        #
        # The load-bearing half is the SECOND assertion: the prune is anchored on the exact path
        # `.engine/contracts/instance`, NOT the bare name `instance`, so a same-named directory ANYWHERE ELSE
        # (e.g. `.engine/tools/instance/`) stays ownership-checked. A bare-name `"instance"` prune would silently
        # un-own it — this test goes red if a future change weakens the path-anchor or drops DEPLOYMENT_CONTRACTS.
        saved_root, saved_engine = validate.ROOT, validate.ENGINE_DIR
        try:
            with tempfile.TemporaryDirectory() as d:
                engine = os.path.join(d, ".engine")
                stream = os.path.join(engine, "contracts", "instance")   # deployment stream -> must prune
                canon = os.path.join(engine, "contracts")                # engine canon home -> keep
                lookalike = os.path.join(engine, "tools", "instance")    # same name, other path -> keep
                for sub in (stream, canon, lookalike):
                    os.makedirs(sub, exist_ok=True)
                open(os.path.join(stream, "eADR-9001-picked-postgres.md"), "w").close()
                open(os.path.join(canon, "eADR-0001-versioned-template.md"), "w").close()
                open(os.path.join(lookalike, "real.py"), "w").close()
                validate.ROOT, validate.ENGINE_DIR = d, engine
                inv = module_coherence.engine_file_inventory()
            self.assertNotIn(".engine/contracts/instance/eADR-9001-picked-postgres.md", inv,
                             "the deployment's committed per-instance eADR stream must be pruned")
            self.assertIn(".engine/contracts/eADR-0001-versioned-template.md", inv,
                          "the engine's own eADR canon directly in .engine/contracts/ must stay owned")
            self.assertIn(".engine/tools/instance/real.py", inv,
                          "a same-named directory elsewhere must stay owned (the path-anchor gotcha — a "
                          "bare-name 'instance' prune would drop it)")
        finally:
            validate.ROOT, validate.ENGINE_DIR = saved_root, saved_engine

    def test_a_deployment_eADR_home_draws_no_engine_codeowners_but_the_canon_does(self):
        # #410: the point of the deployment home is that a deployment's OWN decision records route to the
        # deployment, not engine review — so nothing under .engine/contracts/instance/ may acquire an engine
        # CODEOWNERS line (it is off core's non-recursive .engine/contracts/*.md glob), while the engine's own
        # eADR canon directly in .engine/contracts/ IS engine-owned. Real-tree correlate of the operator demo:
        # the seed README lives under instance/ and must be absent from the owned set.
        owned = module_coherence.codeowners_path_set()
        instance_owned = [p for p in owned if p.startswith(".engine/contracts/instance/")]
        self.assertEqual(instance_owned, [], "nothing under the deployment eADR home draws an engine CODEOWNERS line")
        canon_owned = [p for p in owned if p.startswith(".engine/contracts/")
                       and "/instance/" not in p and p.endswith(".md")]
        self.assertTrue(canon_owned, "the engine's own eADR canon in .engine/contracts/ must stay engine-owned")

    def test_real_repository_is_wiring_coherent_and_approval_blind(self):
        # The committed tree's declared wires are ALL applied -> the forward wiring leg is silent.
        # The mcp leg is APPROVAL-BLIND: it reports the engine-knowledge-graph server applied from the
        # committed .mcp.json DEFINITION, never from the operator's runtime client-approval (which the
        # repo does not record) -> the MCP-pending-setup carve-out, shown positively. (.claude/settings.json
        # was added for the SessionStart hook wiring; that is the engine's own hook config,
        # not an operator MCP approval, so it is irrelevant to approval-blindness — the point is that the
        # mcp wire is green on the committed definition alone.)
        status = module_coherence.wiring_status(module_coherence.discover_manifests())
        mcp = [s for s in status if s[1] == "mcp"]
        self.assertTrue(mcp, "core declares an mcp wire to exercise")
        self.assertTrue(all(applied for _id, _seam, _t, applied in mcp),
                        "the mcp wire is applied from the committed .mcp.json definition (approval-blind)")
        self.assertTrue(all(applied for _id, _seam, _t, applied in status),
                        f"every committed wire must be applied: {status}")
        self.assertEqual(validate.wiring_findings(status, "hard", "m"), [])

    def test_unapplied_declared_wires_are_hard_findings(self):
        # Redirect the wiring library's shared targets to empty temp files -> the wires core declares
        # are no longer applied -> check_coherence reports them as HARD drift (the half-applied/stale
        # catcher). Dependency + ownership stay green (they don't read the wiring targets).
        saved = (wiring.GITIGNORE_PATH, wiring.MCP_PATH, wiring.CATALOG_PATH, wiring.SETTINGS_PATH)
        with tempfile.TemporaryDirectory() as d:
            wiring.GITIGNORE_PATH = os.path.join(d, ".gitignore")
            wiring.MCP_PATH = os.path.join(d, ".mcp.json")
            wiring.CATALOG_PATH = os.path.join(d, "surface-catalog.json")
            wiring.SETTINGS_PATH = os.path.join(d, "settings.json")
            try:
                findings = module_coherence.check_coherence()
            finally:
                (wiring.GITIGNORE_PATH, wiring.MCP_PATH, wiring.CATALOG_PATH,
                 wiring.SETTINGS_PATH) = saved
        msgs = " ".join(f["message"] for f in findings if f["severity"] == "hard")
        self.assertIn("mcp wire that is not applied", msgs)
        self.assertIn("gitignore wire that is not applied", msgs)

    def test_drifted_mcp_definition_is_a_hard_wiring_finding(self):
        # Binds leg (c) to fix (b): a committed .mcp.json whose engine-knowledge-graph definition has
        # DRIFTED from the manifest -> the full-content is_applied reads it not-applied -> a HARD
        # wiring finding. (Name-only is_applied would have stayed green here.) Only MCP_PATH is
        # redirected, so the gitignore wire stays applied and silent.
        saved = wiring.MCP_PATH
        with tempfile.TemporaryDirectory() as d:
            wiring.MCP_PATH = os.path.join(d, ".mcp.json")
            with open(wiring.MCP_PATH, "w", encoding="utf-8") as fh:
                json.dump({"mcpServers": {"engine-knowledge-graph":
                                          {"command": "DRIFTED", "args": []}}}, fh)
            try:
                findings = module_coherence.check_coherence()
            finally:
                wiring.MCP_PATH = saved
        hard = [f for f in findings if f["severity"] == "hard"]
        self.assertTrue(any("mcp wire that is not applied" in f["message"] for f in hard),
                        "a drifted mcp definition must flag a hard wiring finding")

    def test_real_repository_has_no_orphan_wires(self):
        # The REVERSE leg adds zero findings over the committed tree: every applied
        # engine-identified hook / mcp / gitignore entry is declared by a present manifest, and the
        # foundation .venv ignore is a plain line (not a fence) so it is never enumerated.
        manifests = module_coherence.discover_manifests()
        orphans = validate.orphan_wire_findings(
            wiring.applied_engine_wires(), module_coherence.declared_wire_identities(manifests),
            "hard", "m")
        self.assertEqual(orphans, [], f"unexpected orphan wires: {[f['message'] for f in orphans]}")

    def test_permission_and_venv_plain_line_are_never_enumerated(self):
        applied = wiring.applied_engine_wires()
        self.assertNotIn("permission", {seam for seam, _k, _t in applied})  # not engine-identifiable
        fences = {key for seam, key, _t in applied if seam == "gitignore"}
        self.assertNotIn(".engine/.venv/", fences)         # the plain line is not a fence id

    def test_injected_undeclared_engine_hook_is_an_orphan_wire(self):
        # Redirect only SETTINGS_PATH to a faithful COPY + an engine hook NO manifest declares -> the
        # reverse leg flags it; the other shared files stay real (and fully declared), adding nothing.
        saved = wiring.SETTINGS_PATH
        with tempfile.TemporaryDirectory() as d:
            copy = os.path.join(d, "settings.json")
            shutil.copyfile(saved, copy)
            wiring.SETTINGS_PATH = copy
            try:
                wiring.apply({"type": "hook", "event": "PostToolUse", "matcher": "",
                              "hook": {"type": "command",
                                       "command": ".engine/.venv/bin/python .engine/tools/boot.py --x"}})
                findings = module_coherence.check_coherence()
            finally:
                wiring.SETTINGS_PATH = saved
        hard = [f for f in findings if f["severity"] == "hard"]
        self.assertTrue(any("hook" in f["message"] and "no installed module declares" in f["message"]
                            for f in hard),
                        "an applied engine hook no manifest declares must be an orphan-wire finding")

    def test_injected_orphan_gitignore_fence_is_flagged(self):
        saved = wiring.GITIGNORE_PATH
        with tempfile.TemporaryDirectory() as d:
            copy = os.path.join(d, ".gitignore")
            shutil.copyfile(saved, copy)
            wiring.GITIGNORE_PATH = copy
            try:
                wiring.apply({"type": "gitignore", "key": "ghost-cache",
                              "lines": [".engine/ghost/.cache/"]})
                findings = module_coherence.check_coherence()
            finally:
                wiring.GITIGNORE_PATH = saved
        hard = [f for f in findings if f["severity"] == "hard"]
        self.assertTrue(any("gitignore" in f["message"] and "no installed module declares" in f["message"]
                            for f in hard),
                        "an undeclared engine-managed fence must be an orphan-wire finding")

    def test_main_exit_zero_on_clean_tree(self):
        import contextlib
        import io
        with contextlib.redirect_stdout(io.StringIO()):  # main() prints its operator report; keep tests quiet
            self.assertEqual(module_coherence.main([]), 0)

    def test_malformed_manifest_is_a_plain_config_error_not_a_traceback(self):
        # A non-JSON manifest must surface as a plain CONFIG ERROR (exit 2), never a raw
        # Python traceback to the non-engineer — the validator's halt-on-malformed posture.
        import contextlib
        import io
        orig = module_coherence.discover_manifests
        module_coherence.discover_manifests = lambda: (_ for _ in ()).throw(
            ValueError("manifest.json is not valid JSON"))
        err = io.StringIO()
        try:
            with contextlib.redirect_stderr(err):
                rc = module_coherence.main([])
        finally:
            module_coherence.discover_manifests = orig
        self.assertEqual(rc, 2)
        self.assertIn("CONFIG ERROR", err.getvalue())


class TestSeededRootFileOwnership(unittest.TestCase):
    """The seeded root SECURITY.md is operator-owned product territory, and its template seed is operator config.
    (Re-homed from the retired test_security_seed.py — these assert permanent module_coherence constants that
    govern the upgrade overlay, so they must survive first-run retirement; issue #150.)"""
    def test_seeded_root_security_md_is_not_engine_owned(self):
        # product territory, in no `provides`, NO carve-out: it must NOT be a FOUNDATION_INFRA member
        # (that set is overlay-REPLACED on upgrade, which would clobber the operator's edited disclosure).
        self.assertNotIn("SECURITY.md", module_coherence.FOUNDATION_INFRA)

    def test_security_seed_source_is_carved_out_in_operator_config(self):
        self.assertIn(".engine/provisioning/security-seed.md", module_coherence.OPERATOR_CONFIG)


class TestTopologicalOrder(unittest.TestCase):
    """The pure validate.topological_order — dependency-first, deterministic, input-order-independent,
    cycle-safe. The self-map renders in this order; the module manager installs
    in it."""

    @staticmethod
    def _m(mid, *deps):
        return {"id": mid, "depends": {d: "" for d in deps}}  # empty-string range, as on disk

    def _ids(self, manifests):
        return [m["id"] for m in validate.topological_order(manifests)]

    def test_linear_chain_orders_dependencies_first(self):
        # a depends-on b depends-on c  ->  c, b, a
        ms = [self._m("a", "b"), self._m("b", "c"), self._m("c")]
        self.assertEqual(self._ids(ms), ["c", "b", "a"])

    def test_topological_differs_from_alphabetical(self):
        # alpha depends-on zebra -> [zebra, alpha], the REVERSE of alphabetical [alpha, zebra]
        self.assertEqual(self._ids([self._m("alpha", "zebra"), self._m("zebra")]),
                         ["zebra", "alpha"])

    def test_independent_roots_break_ties_alphabetically(self):
        self.assertEqual(self._ids([self._m("c"), self._m("a"), self._m("b")]), ["a", "b", "c"])

    def test_diamond_orders_base_first_dependents_last(self):
        # d depends-on b,c ; b,c depend-on a  ->  a, b, c, d
        ms = [self._m("d", "b", "c"), self._m("b", "a"), self._m("c", "a"), self._m("a")]
        self.assertEqual(self._ids(ms), ["a", "b", "c", "d"])

    def test_input_order_independent(self):
        import itertools
        ms = [self._m("d", "b", "c"), self._m("b", "a"), self._m("c", "a"), self._m("a")]
        outs = {tuple(self._ids(list(p))) for p in itertools.permutations(ms)}
        self.assertEqual(len(outs), 1, "topological order must not depend on input sequence")

    def test_cycle_is_deterministic_and_lossless(self):
        # a<->b cycle (separately flagged hard by coherence_findings): no crash, all ids present,
        # deterministic (alphabetical fallback), independent of input order.
        ms = [self._m("b", "a"), self._m("a", "b")]
        out = self._ids(ms)
        self.assertEqual(sorted(out), ["a", "b"])
        self.assertEqual(out, self._ids(list(reversed(ms))))

    def test_absent_dependency_is_ignored(self):
        # a depends-on a module not in the set -> treated as a root, no crash (mirrors _dependency_cycle)
        self.assertEqual(self._ids([self._m("x", "not-present")]), ["x"])


class TestUntrackedSurfaceGuard(unittest.TestCase):
    """#281: the surface walk reads git, not just the filesystem. The OWNERSHIP inventory is tracked-only
    (an untracked file raises no spurious local orphan / double-claim), and untracked_surface_findings
    names every surface file git neither tracks nor ignores — sync-conflict cruft a file-sync tool dropped,
    or a not-yet-committed new file. Fail-soft: git unavailable -> the inventory is the full walk and the
    detector returns one explicit 'skipped' note (never a silent all-clear)."""

    @staticmethod
    def _git(root, *a):
        subprocess.run(["git", "-C", root, *a], capture_output=True, text=True, check=False)

    def _init_committed(self, root):
        """A throwaway git repo with one committed engine tool + a surface catalog naming .claude/skills/.
        Returns the engine dir. The caller adds untracked files, then points validate.ROOT/ENGINE_DIR/
        CATALOG_PATH at the fixture."""
        engine = os.path.join(root, ".engine")
        os.makedirs(os.path.join(engine, "tools"))
        os.makedirs(os.path.join(engine, "schemas"))
        _write(os.path.join(engine, "schemas"), "surface-catalog.json",
               {"surfaces": {"skill": {"location": ".claude/skills/"}}})
        _write(os.path.join(engine, "tools"), "real_tool.py", "# committed\n")
        self._git(root, "init")
        self._git(root, "add", "-A")
        self._git(root, "-c", "user.email=e@x", "-c", "user.name=e", "commit", "-m", "base")
        return engine

    def _run(self, build, check):
        """Init a committed fixture, let build(root, engine) add files, then run check() with
        validate.ROOT/ENGINE_DIR/CATALOG_PATH pointed at it; restore the globals after."""
        saved = (validate.ROOT, validate.ENGINE_DIR, validate.CATALOG_PATH)
        with tempfile.TemporaryDirectory() as root:
            engine = self._init_committed(root)
            validate.ROOT, validate.ENGINE_DIR = root, engine
            validate.CATALOG_PATH = os.path.join(engine, "schemas", "surface-catalog.json")
            try:
                build(root, engine)
                return check()
            finally:
                validate.ROOT, validate.ENGINE_DIR, validate.CATALOG_PATH = saved

    def test_tracked_and_untracked_helpers_split_the_tree(self):
        def build(root, engine):
            _write(os.path.join(engine, "tools"), "stray.py", "# untracked\n")
        tracked, untracked = self._run(
            build, lambda: (module_coherence._tracked_paths(), module_coherence._untracked_surface_paths()))
        self.assertIn(".engine/tools/real_tool.py", tracked)
        self.assertNotIn(".engine/tools/stray.py", tracked)
        self.assertIn(".engine/tools/stray.py", untracked)
        self.assertNotIn(".engine/tools/real_tool.py", untracked)

    def test_helpers_return_none_when_git_unavailable(self):
        # A non-repo tmp dir -> `git ls-files` exits non-zero -> the fail-soft helpers return None.
        saved = (validate.ROOT, validate.ENGINE_DIR)
        with tempfile.TemporaryDirectory() as root:
            validate.ROOT, validate.ENGINE_DIR = root, os.path.join(root, ".engine")
            try:
                self.assertIsNone(module_coherence._tracked_paths())
                self.assertIsNone(module_coherence._untracked_surface_paths())
            finally:
                validate.ROOT, validate.ENGINE_DIR = saved

    def test_ownership_inventory_is_tracked_only_so_untracked_raises_no_orphan(self):
        # The load-bearing #281 fix: an untracked file is excluded from the ownership inventory (no spurious
        # local orphan), while a TRACKED unclaimed file still orphans (teeth survive for real problems).
        def build(root, engine):
            _write(os.path.join(engine, "tools"), "ghost.py", "# untracked, unclaimed\n")
        inv = self._run(build, module_coherence.engine_file_inventory)
        self.assertIn(".engine/tools/real_tool.py", inv)
        self.assertNotIn(".engine/tools/ghost.py", inv)
        # over an empty claims map, the tracked real_tool.py orphans; the untracked ghost.py never enters.
        findings = validate.ownership_findings(inv, {}, set(), "hard", "own")
        msgs = " ".join(f["message"] for f in findings)
        self.assertIn(".engine/tools/real_tool.py", msgs)
        self.assertNotIn("ghost.py", msgs)

    def test_inventory_is_full_walk_when_git_unavailable(self):
        # Fail-soft: with the tracked-set helper forced to None, the inventory falls back to the raw walk
        # (the prior behavior) so a degraded git never strands the ownership leg.
        saved_helper = module_coherence._tracked_paths
        def build(root, engine):
            _write(os.path.join(engine, "tools"), "stray.py", "# untracked\n")
            module_coherence._tracked_paths = lambda: None
        try:
            inv = self._run(build, module_coherence.engine_file_inventory)
        finally:
            module_coherence._tracked_paths = saved_helper
        self.assertIn(".engine/tools/stray.py", inv)        # full walk includes the untracked file

    def test_detector_names_untracked_surface_file_as_soft(self):
        def build(root, engine):
            _write(os.path.join(engine, "tools"), "real_tool 2.py", "# sync-conflict duplicate\n")
        findings = self._run(build, lambda: module_coherence.untracked_surface_findings("soft"))
        self.assertTrue(findings)
        self.assertTrue(all(f["severity"] == "soft" for f in findings))
        self.assertTrue(any(".engine/tools/real_tool 2.py" in f["message"] for f in findings))
        self.assertFalse(any("real_tool.py'" in f["message"] for f in findings))  # tracked -> not named

    def test_detector_is_silent_on_a_clean_tree(self):
        findings = self._run(lambda root, engine: None,
                             lambda: module_coherence.untracked_surface_findings("soft"))
        self.assertEqual(findings, [])

    def test_detector_excludes_gitignored_files(self):
        # A gitignored file is intentional, not cruft -> --exclude-standard keeps it out of the detector,
        # while a plain untracked file is still named. (Guards against a 'walk minus tracked' false-positive.)
        def build(root, engine):
            _write(root, ".gitignore", "ignored.py\n")
            _write(os.path.join(engine, "tools"), "ignored.py", "# gitignored\n")
            _write(os.path.join(engine, "tools"), "stray.py", "# plain untracked\n")
        findings = self._run(build, lambda: module_coherence.untracked_surface_findings("soft"))
        msgs = " ".join(f["message"] for f in findings)
        self.assertIn(".engine/tools/stray.py", msgs)
        self.assertNotIn("ignored.py", msgs)

    def test_detector_reaches_a_cruft_dir_under_a_claude_surface_root(self):
        # The issue's own example: a duplicated skill DIRECTORY under .claude/skills/. The detector walks the
        # catalogued .claude/ roots, so it catches this even though no literal `provides` path matches it.
        def build(root, engine):
            d = os.path.join(root, ".claude", "skills", "engine-help 2")
            os.makedirs(d)
            _write(d, "SKILL.md", "# sync-conflict skill dir\n")
        findings = self._run(build, lambda: module_coherence.untracked_surface_findings("soft"))
        self.assertTrue(any(".claude/skills/engine-help 2/SKILL.md" in f["message"] for f in findings))

    def test_detector_returns_a_skip_note_when_git_unavailable(self):
        # Git-absent must NOT be a silent all-clear: one soft finding saying the check was skipped.
        saved = (validate.ROOT, validate.ENGINE_DIR, validate.CATALOG_PATH)
        with tempfile.TemporaryDirectory() as root:
            engine = os.path.join(root, ".engine")
            os.makedirs(os.path.join(engine, "schemas"))
            _write(os.path.join(engine, "schemas"), "surface-catalog.json", {"surfaces": {}})
            validate.ROOT, validate.ENGINE_DIR = root, engine
            validate.CATALOG_PATH = os.path.join(engine, "schemas", "surface-catalog.json")
            try:
                findings = module_coherence.untracked_surface_findings("soft")
            finally:
                validate.ROOT, validate.ENGINE_DIR, validate.CATALOG_PATH = saved
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["severity"], "soft")
        self.assertIn("git was unavailable", findings[0]["message"])

    def test_relay_swallows_errors_to_empty_array_and_exits_zero(self):
        # The custom/script kind turns any non-zero exit into a HARD fail-closed finding regardless of tier,
        # so the relay must exit 0 + emit valid JSON even when the detector raises (a transient git hiccup
        # must never become a hard block).
        import io
        import contextlib
        import untracked_surface_check
        saved = module_coherence.untracked_surface_findings
        module_coherence.untracked_surface_findings = lambda tier: (_ for _ in ()).throw(RuntimeError("boom"))
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                rc = untracked_surface_check.main()
        finally:
            module_coherence.untracked_surface_findings = saved
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(buf.getvalue()), [])

    def test_check_rule_conforms_and_is_soft_in_both_suites(self):
        rule = validate.load_json(os.path.join(validate.CHECK_DIR, "untracked-surface.json"))
        schema = validate.load_json(os.path.join(validate.SCHEMAS_DIR, "check.v1.json"))
        self.assertEqual(_errors(schema, rule), [])
        self.assertEqual(rule["tier"], "soft")
        self.assertEqual(set(rule["suites"]), {"CI", "audit-prep"})
        self.assertEqual(rule["params"]["script"], ".engine/tools/untracked_surface_check.py")


if __name__ == "__main__":
    unittest.main()
