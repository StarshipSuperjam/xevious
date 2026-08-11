#!/usr/bin/env python3
"""Self-tests for the seed's checker-of-checkers (validator + the two guards).

Run: uv run --directory .engine --frozen -- python tools/selftest.py

These lock in the load-bearing teeth so a later edit to the trust root cannot
silently regress them. The deliverable-gate cold review attests that each test's
assertion matches its name; CI runs them as a step in `engine-ci`.
"""
from __future__ import annotations
import contextlib
import io
import json
import os
import re
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate          # noqa: E402
import weakening_guard   # noqa: E402
import protection_guard  # noqa: E402
import bootstrap         # noqa: E402  (floor_ruleset — the team-tier floor builder the verifier is checked against)
import module_coherence  # noqa: E402  (installed-means-present manifests, for roster-aware optional-script guards)


def _installed_module_ids() -> set:
    """The ids of the modules present on disk (installed-means-present). A deployment that DECLINED an
    optional module removes its subtree, so its id drops out here — the roster-aware signal for #646."""
    return {man.get("id") for _rel, man in module_coherence.discover_manifests()}


def _run_quiet(suite, ctx):
    """validate.run() prints its operator report to stdout; these tests assert on its exit CODE, not the
    text, so capture the report to keep the unittest run quiet — the leaked 'FAIL ... boom' / 'kaboom'
    fixture lines otherwise read like a real failure."""
    with contextlib.redirect_stdout(io.StringIO()):
        return validate.run(suite, ctx)


def _check_quiet(rule_id, ctx):
    """validate.run_check() (the --check single-rule path) prints its report too; capture it so the
    unittest run stays quiet (these tests assert on the exit code, not the text)."""
    with contextlib.redirect_stdout(io.StringIO()):
        return validate.run_check(rule_id, ctx)


SECTIONS = ["Purpose", "Scope", "Out of scope", "Risk", "Validation", "Review",
            "Demonstration", "Files of interest", "AI involvement"]
COMPLETENESS_RULE = {"id": "engine/check/pr-body-completeness",
                     "target": {"context": "pull-request-body"},
                     "kind": "presence", "tier": "hard",
                     "suites": ["CI"], "params": {"sections": SECTIONS},
                     "message": "Fill the section."}
# The same shape WITH the Impact-line enforcement param — mirrors the shipped rule so the
# new leg is exercised in isolation from the shipped-JSON drift tests below.
IMPACT_RULE = {"id": "engine/check/pr-body-completeness",
               "target": {"context": "pull-request-body"},
               "kind": "presence", "tier": "hard", "suites": ["CI"],
               "params": {"sections": SECTIONS, "filled_subsection_label": "Impact"},
               "message": "Fill the section and its Impact line."}
# The same shape WITH the required-phrases enforcement param — a two-anchor fixture, so the new
# leg is exercised in isolation from the shipped-JSON drift tests below.
PHRASES_RULE = {"id": "engine/check/pr-body-completeness",
                "target": {"context": "pull-request-body"},
                "kind": "presence", "tier": "hard", "suites": ["CI"],
                "params": {"sections": SECTIONS,
                           "required_phrases": ["binding gate anchor", "unverified anchor"]},
                "message": "Fill the sections and carry the preamble."}


class TestCompletenessTeeth(unittest.TestCase):
    def test_placeholder_and_blank_count_as_empty(self):
        self.assertTrue(validate.is_empty_section("<why this exists>"))
        self.assertTrue(validate.is_empty_section("   \n  \n"))
        self.assertTrue(validate.is_empty_section("<a>\n\n<b>"))

    def test_real_content_is_not_empty(self):
        self.assertFalse(validate.is_empty_section("Real text."))
        self.assertFalse(validate.is_empty_section("<placeholder>\nbut also real text"))

    def test_section_blocks_parses_h2_only(self):
        blocks = validate.section_blocks("## Purpose\nx\n### Sub\ny\n## Scope\nz")
        self.assertIn("Purpose", blocks)
        self.assertIn("Scope", blocks)
        self.assertNotIn("Sub", blocks)  # a ### subsection is not a contract section

    def test_missing_body_fails_open(self):
        passed, found = validate.kind_presence(COMPLETENESS_RULE, {"pr_body": None})
        self.assertTrue(passed)
        self.assertTrue(all(f["severity"] != "hard" for f in found))

    def test_empty_body_flags_all_sections_hard(self):
        passed, found = validate.kind_presence(COMPLETENESS_RULE, {"pr_body": ""})
        self.assertFalse(passed)
        self.assertEqual(len(found), len(SECTIONS))
        self.assertTrue(all(f["severity"] == "hard" for f in found))

    def test_placeholder_only_body_fails(self):
        body = "\n".join(f"## {s}\n<prompt>" for s in SECTIONS)
        passed, found = validate.kind_presence(COMPLETENESS_RULE, {"pr_body": body})
        self.assertFalse(passed)
        self.assertEqual(len(found), len(SECTIONS))

    def test_filled_body_passes(self):
        body = "\n".join(f"## {s}\nreal content for {s}" for s in SECTIONS)
        passed, found = validate.kind_presence(COMPLETENESS_RULE, {"pr_body": body})
        self.assertTrue(passed)
        self.assertEqual(found, [])


class TestCiAuthorExempt(unittest.TestCase):
    """The engine honors a rule's `ci_author_exempt` in the merge gate as a DISCLOSED
    not-applicable pass — never a silent green; the closed kinds stay author-agnostic; the
    by-id guard path is never exempt; exact-match only. (issue #116.)"""
    DISCLOSURE = "NOT APPLICABLE"

    def setUp(self):
        self._rules, self._reg = validate.load_rules, dict(validate.REGISTRY)

    def tearDown(self):
        validate.load_rules = self._rules
        validate.REGISTRY.clear()
        validate.REGISTRY.update(self._reg)

    def _install(self, *, suites=("CI",), exempt=("dependabot[bot]",)):
        # A synthetic rule whose kind ALWAYS hard-fails — so a PASS proves the engine skipped
        # it (exempt) and a FAIL proves the kind ran (enforced). The marker text "the kind ran"
        # appears iff the kind was dispatched.
        validate.load_rules = lambda: [{"id": "engine/check/synthetic-exempt",
                                        "kind": "always-fail", "tier": "hard",
                                        "suites": list(suites), "params": {},
                                        "ci_author_exempt": list(exempt)}]
        validate.REGISTRY["always-fail"] = lambda rule, ctx: (
            False, [validate.finding("hard", "the kind ran")])

    def _run(self, suite, ctx):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            rc = validate.run(suite, ctx)
        return rc, out.getvalue()

    def test_exempt_author_passes_in_ci_with_disclosure(self):
        self._install()
        rc, text = self._run("CI", {"pr_body": "", "pr_author": "dependabot[bot]"})
        self.assertEqual(rc, 0)                  # exempt → no hard finding → completed pass
        self.assertIn(self.DISCLOSURE, text)     # disclosed, never a silent green (build-owe #3)
        self.assertNotIn("the kind ran", text)   # the kind was skipped before dispatch

    def test_nonexempt_author_still_enforced(self):
        self._install()
        rc, text = self._run("CI", {"pr_body": "", "pr_author": "a-human"})
        self.assertEqual(rc, 1)
        self.assertIn("the kind ran", text)
        self.assertNotIn(self.DISCLOSURE, text)

    def test_no_author_enforced(self):
        # Local run / --pr-body-file / malformed event → None author → never matches → enforces.
        self._install()
        rc, _ = self._run("CI", {"pr_body": "", "pr_author": None})
        self.assertEqual(rc, 1)

    def test_wrong_case_author_enforced(self):
        self._install()
        rc, text = self._run("CI", {"pr_body": "", "pr_author": "Dependabot[Bot]"})
        self.assertEqual(rc, 1)                  # exact match only; no silent case-fold widening
        self.assertIn("the kind ran", text)

    def test_exempt_only_in_blocking_gate_suite(self):
        # The same rule + author in a non-blocking-gate suite is NOT exempted: the kind runs
        # (advisory there, so the exit code can't distinguish — assert on the text). This locks
        # the gate to the suite's blocking-gate CONTEXT, not the literal name "CI" (build-owe #2).
        self._install(suites=("pre-commit",))
        rc, text = self._run("pre-commit", {"pr_body": "", "pr_author": "dependabot[bot]"})
        self.assertNotIn(self.DISCLOSURE, text)
        self.assertIn("the kind ran", text)

    def test_by_id_guard_path_never_exempt(self):
        # run_check() (the by-id path engine-guard uses) carries no suite, so it never honors
        # ci_author_exempt — the weakening guard judges Dependabot too.
        self._install()
        with contextlib.redirect_stdout(io.StringIO()) as out:
            rc = validate.run_check("engine/check/synthetic-exempt",
                                    {"pr_body": "", "pr_author": "dependabot[bot]"})
        self.assertEqual(rc, 1)
        self.assertNotIn(self.DISCLOSURE, out.getvalue())

    def test_kind_presence_is_author_agnostic(self):
        # The closed kind never reads the author: an exempt author still fails an empty body.
        passed, found = validate.kind_presence(
            COMPLETENESS_RULE, {"pr_body": "", "pr_author": "dependabot[bot]"})
        self.assertFalse(passed)
        self.assertEqual(len(found), len(SECTIONS))

    def test_exempt_in_any_blocking_gate_suite_not_just_ci(self):
        # The positive of test_exempt_only_in_blocking_gate_suite: the gate keys on the
        # blocking-gate CONTEXT, not the literal name "CI", so a differently-named blocking-gate
        # suite ALSO exempts. Locks the plan-gate decision to gate on `gates`, future-proofing a
        # second blocking-gate suite. (Today CI is the only one — so this needs a synthetic suite.)
        self._install(suites=("release-gate",))
        saved = validate.load_suites
        validate.load_suites = lambda: {"release-gate": {"trigger": "x", "context": "blocking-gate"}}
        try:
            rc, text = self._run("release-gate", {"pr_body": "", "pr_author": "dependabot[bot]"})
        finally:
            validate.load_suites = saved
        self.assertEqual(rc, 0)
        self.assertIn(self.DISCLOSURE, text)
        self.assertNotIn("the kind ran", text)


class TestCiLabelExempt(unittest.TestCase):
    """The engine honors a rule's `ci_label_exempt` in the merge gate as a DISCLOSED not-applicable
    pass — keyed on a LABEL the pull request carries (e.g. engine-erasure), not its author — so a
    single-purpose pull-request class whose own plain body is its account is waived without a silent
    green; the closed kinds stay label-agnostic; the by-id guard path is never exempt; exact-match
    only. The label-keyed sibling of TestCiAuthorExempt (the memory-erasure proposal is operator-
    authored, so the author exemption cannot reach it — this is what clears its engine-ci)."""
    DISCLOSURE = "NOT APPLICABLE"

    def setUp(self):
        self._rules, self._reg = validate.load_rules, dict(validate.REGISTRY)

    def tearDown(self):
        validate.load_rules = self._rules
        validate.REGISTRY.clear()
        validate.REGISTRY.update(self._reg)

    def _install(self, *, suites=("CI",), exempt=("engine-erasure",)):
        # The same always-fail synthetic kind TestCiAuthorExempt uses: a PASS proves the engine
        # skipped it (exempt), a FAIL proves the kind ran ("the kind ran" appears iff dispatched).
        validate.load_rules = lambda: [{"id": "engine/check/synthetic-label-exempt",
                                        "kind": "always-fail", "tier": "hard",
                                        "suites": list(suites), "params": {},
                                        "ci_label_exempt": list(exempt)}]
        validate.REGISTRY["always-fail"] = lambda rule, ctx: (
            False, [validate.finding("hard", "the kind ran")])

    def _run(self, suite, ctx):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            rc = validate.run(suite, ctx)
        return rc, out.getvalue()

    def test_exempt_label_passes_in_ci_with_disclosure(self):
        self._install()
        rc, text = self._run("CI", {"pr_body": "", "pr_labels": ["engine-erasure"]})
        self.assertEqual(rc, 0)                  # waived → no hard finding → completed pass
        self.assertIn(self.DISCLOSURE, text)     # disclosed, never a silent green
        self.assertNotIn("the kind ran", text)   # the kind was skipped before dispatch

    def test_one_matching_label_among_many_exempts(self):
        self._install()
        rc, text = self._run("CI", {"pr_body": "", "pr_labels": ["chore", "engine-erasure", "z"]})
        self.assertEqual(rc, 0)
        self.assertIn(self.DISCLOSURE, text)
        self.assertNotIn("the kind ran", text)

    def test_nonexempt_label_still_enforced(self):
        self._install()
        rc, text = self._run("CI", {"pr_body": "", "pr_labels": ["some-other-label"]})
        self.assertEqual(rc, 1)
        self.assertIn("the kind ran", text)
        self.assertNotIn(self.DISCLOSURE, text)

    def test_no_labels_enforced(self):
        # Local run / --pr-body-file / malformed event → [] labels → never matches → enforces.
        self._install()
        rc, _ = self._run("CI", {"pr_body": "", "pr_labels": []})
        self.assertEqual(rc, 1)

    def test_wrong_case_label_enforced(self):
        self._install()
        rc, text = self._run("CI", {"pr_body": "", "pr_labels": ["Engine-Erasure"]})
        self.assertEqual(rc, 1)                  # exact match only; no silent case-fold widening
        self.assertIn("the kind ran", text)

    def test_by_id_guard_path_never_exempt(self):
        # run_check() (the by-id path engine-guard uses) carries no suite, so it never honors
        # ci_label_exempt — the weakening guard judges an engine-erasure-labelled PR too.
        self._install()
        with contextlib.redirect_stdout(io.StringIO()) as out:
            rc = validate.run_check("engine/check/synthetic-label-exempt",
                                    {"pr_body": "", "pr_labels": ["engine-erasure"]})
        self.assertEqual(rc, 1)
        self.assertNotIn(self.DISCLOSURE, out.getvalue())


class TestCheckSchemaCiAuthorExempt(unittest.TestCase):
    """The optional `ci_author_exempt` field is additive: the committed rule carries it,
    the schema still requires exactly the seven, and no committed rule is invalidated."""
    def _schema(self):
        return validate.load_json(os.path.join(validate.SCHEMAS_DIR, "check.v1.json"))

    def test_schema_still_requires_exactly_the_seven(self):
        self.assertEqual(self._schema()["required"],
                         ["id", "target", "kind", "params", "tier", "suites", "message"])

    def test_committed_pr_body_rule_declares_and_validates(self):
        # Two exempt bot AUTHORS: dependabot[bot] (its dependency PRs) and github-actions[bot] (the scheduled
        # self-review digest pull request, opened via the workflow token) — both carry their own plain-language
        # body, never the nine-section template. Plus one exempt LABEL, engine-erasure: the single-purpose
        # memory-erasure proposal is opened by a local hook under the operator's own identity (NOT a bot), so the
        # author exemption cannot reach it — its deliberate plain consent body is cleared by the label instead.
        # A drop of any of these silently re-breaks those PRs' engine-ci, so pin the exact lists.
        # The github-actions[bot] entry's spoof-safety re-confirmation is recorded in the PR closing #423.
        rule = validate.load_json(os.path.join(validate.CHECK_DIR, "pr-body-completeness.json"))
        self.assertEqual(rule.get("ci_author_exempt"), ["dependabot[bot]", "github-actions[bot]"])
        self.assertEqual(rule.get("ci_label_exempt"), ["engine-erasure"])
        errs = list(validate.Draft202012Validator(self._schema()).iter_errors(rule))
        self.assertEqual(errs, [])

    def test_every_committed_check_rule_validates(self):
        v = validate.Draft202012Validator(self._schema())
        for name in sorted(os.listdir(validate.CHECK_DIR)):
            if not name.endswith(".json"):
                continue
            rule = validate.load_json(os.path.join(validate.CHECK_DIR, name))
            errs = list(v.iter_errors(rule))
            self.assertEqual(errs, [], f"{name} does not conform to check.v1.json: {errs}")

    def test_status_lifecycle_optional_and_enforced(self):
        # #402 U07b: the optional artifact-lifecycle status slot on a check rule (mirrors doc/operation).
        # Omitted means active; active/deprecated/retired conform; a value outside the set is rejected; the
        # required set is still exactly the seven (asserted above).
        v = validate.Draft202012Validator(self._schema())
        base = {"id": "engine/check/x", "target": {"path": "a"}, "kind": "presence",
                "params": {}, "tier": "soft", "suites": [], "message": "m"}
        self.assertEqual(list(v.iter_errors(base)), [])                               # omitted -> active
        for st in ("active", "deprecated", "retired"):
            self.assertEqual(list(v.iter_errors({**base, "status": st})), [], f"{st} must conform")
        for st in ("draft", "bogus"):
            self.assertNotEqual(list(v.iter_errors({**base, "status": st})), [], f"{st} must be rejected")


class TestGetPrAuthor(unittest.TestCase):
    """get_pr_author() reads the trusted event context (.pull_request.user.login) and degrades
    to None — therefore to ENFORCING — on any doubt; it NEVER consults github.actor (the spoof
    vector a re-run would attribute to the re-runner). The one security-load-bearing parser, so
    it is tested directly against a real GITHUB_EVENT_PATH file, not via an injected ctx."""
    def setUp(self):
        self._env = {k: os.environ.get(k) for k in ("GITHUB_EVENT_PATH", "GITHUB_ACTOR")}
        self._paths = []

    def tearDown(self):
        for k, v in self._env.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
        for p in self._paths:
            if os.path.exists(p):
                os.unlink(p)

    def _event(self, raw):
        fd, path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w") as fh:
            fh.write(raw if isinstance(raw, str) else json.dumps(raw))
        self._paths.append(path)
        os.environ["GITHUB_EVENT_PATH"] = path

    def test_valid_event_returns_login(self):
        self._event({"pull_request": {"user": {"login": "octocat"}}})
        self.assertEqual(validate.get_pr_author(), "octocat")

    def test_degrades_to_none_on_every_malformed_shape(self):
        for raw in ({"pull_request": {"user": None}},        # null user
                    {"pull_request": {"user": {}}},          # user present, no login
                    {"pull_request": {"user": "a-string"}},  # type-confused user (was a crash)
                    {"pull_request": []},                    # type-confused pull_request
                    {"pull_request": None},                  # null pull_request
                    {},                                      # no pull_request
                    "this is not json"):                     # unparseable event
            with self.subTest(raw=raw):
                self._event(raw)
                self.assertIsNone(validate.get_pr_author())

    def test_missing_file_or_unset_env_returns_none(self):
        os.environ["GITHUB_EVENT_PATH"] = "/no/such/event/file.json"
        self.assertIsNone(validate.get_pr_author())
        os.environ.pop("GITHUB_EVENT_PATH", None)
        self.assertIsNone(validate.get_pr_author())

    def test_never_consults_github_actor(self):
        # Even with a planted actor, an event with no author resolves to None — never the actor.
        os.environ["GITHUB_ACTOR"] = "dependabot[bot]"
        self._event({"pull_request": {"user": {}}})
        self.assertIsNone(validate.get_pr_author())


class TestGetPrLabels(unittest.TestCase):
    """get_pr_labels() reads the trusted event context (.pull_request.labels[].name) and degrades to
    [] — therefore to ENFORCING the rule — on any doubt: a non-list labels field, a label without a
    string name, an unreadable/partial event. The second security-load-bearing event parser (its
    falsely-exempt failure mode would waive a hard check), so it is tested directly against a real
    GITHUB_EVENT_PATH file, not via an injected ctx — the same posture as get_pr_author."""
    def setUp(self):
        self._env = {"GITHUB_EVENT_PATH": os.environ.get("GITHUB_EVENT_PATH")}
        self._paths = []

    def tearDown(self):
        for k, v in self._env.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
        for p in self._paths:
            if os.path.exists(p):
                os.unlink(p)

    def _event(self, raw):
        fd, path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w") as fh:
            fh.write(raw if isinstance(raw, str) else json.dumps(raw))
        self._paths.append(path)
        os.environ["GITHUB_EVENT_PATH"] = path

    def test_valid_event_returns_label_names_in_order(self):
        self._event({"pull_request": {"labels": [{"name": "engine-erasure"}, {"name": "chore"}]}})
        self.assertEqual(validate.get_pr_labels(), ["engine-erasure", "chore"])

    def test_no_labels_is_empty_not_an_error(self):
        self._event({"pull_request": {"labels": []}})
        self.assertEqual(validate.get_pr_labels(), [])

    def test_degrades_to_empty_on_every_malformed_shape(self):
        for raw in ({"pull_request": {"labels": None}},               # null labels
                    {"pull_request": {"labels": "engine-erasure"}},   # type-confused labels (str, not list)
                    {"pull_request": {"labels": [{"name": None}]}},   # name present but not a string
                    {"pull_request": []},                             # type-confused pull_request
                    {"pull_request": None},                           # null pull_request
                    {},                                               # no pull_request
                    "this is not json"):                              # unparseable event
            with self.subTest(raw=raw):
                self._event(raw)
                self.assertEqual(validate.get_pr_labels(), [])

    def test_mixed_items_keep_only_well_formed_string_names(self):
        # A real labels array of valid label objects mixed with junk → only the string names survive,
        # never a crash and never a falsely-included non-string.
        self._event({"pull_request": {"labels": [
            {"name": "engine-erasure"}, {"no-name": "x"}, "a-string", 7, None, {"name": 9}]}})
        self.assertEqual(validate.get_pr_labels(), ["engine-erasure"])

    def test_missing_file_or_unset_env_returns_empty(self):
        os.environ["GITHUB_EVENT_PATH"] = "/no/such/event/file.json"
        self.assertEqual(validate.get_pr_labels(), [])
        os.environ.pop("GITHUB_EVENT_PATH", None)
        self.assertEqual(validate.get_pr_labels(), [])


class TestDispatcherGate(unittest.TestCase):
    """Lock in the fix: the CI exit code gates on a hard-severity finding, never on
    a callable's verdict flag, so report() and the exit code can never disagree."""
    def setUp(self):
        self._rules, self._reg = validate.load_rules, dict(validate.REGISTRY)

    def tearDown(self):
        validate.load_rules = self._rules
        validate.REGISTRY.clear()
        validate.REGISTRY.update(self._reg)

    def _install(self, kind_fn, tier="hard"):
        validate.load_rules = lambda: [{"id": "synthetic", "kind": "synthetic",
                                        "tier": tier, "suites": ["CI"], "params": {}}]
        validate.REGISTRY["synthetic"] = kind_fn

    def test_hard_finding_fails_even_when_verdict_true(self):
        self._install(lambda rule, ctx: (True, [validate.finding("hard", "boom")]))
        self.assertEqual(_run_quiet("CI", {"pr_body": None}), 1)

    def test_soft_finding_passes_even_when_verdict_false(self):
        self._install(lambda rule, ctx: (False, [validate.finding("soft", "note")]))
        self.assertEqual(_run_quiet("CI", {"pr_body": None}), 0)

    def test_unregistered_hard_kind_fails_closed(self):
        validate.load_rules = lambda: [{"id": "d", "kind": "nope", "tier": "hard",
                                        "suites": ["CI"], "params": {}}]
        self.assertEqual(_run_quiet("CI", {"pr_body": None}), 1)

    def test_erroring_kind_fails_closed(self):
        def boom(rule, ctx):
            raise RuntimeError("kaboom")
        self._install(boom)
        self.assertEqual(_run_quiet("CI", {"pr_body": None}), 1)


class TestWeakeningClassifier(unittest.TestCase):
    def test_is_guardrail_covers_guards_and_lockfiles(self):
        for p in (".github/workflows/engine-ci.yml", ".engine/check/x.json",
                  ".engine/tools/validate.py", ".github/CODEOWNERS",
                  ".engine/pyproject.toml", ".engine/uv.lock", ".engine/suites.json",
                  ".github/dependabot.yml"):
            self.assertTrue(weakening_guard.is_guardrail(p), p)
        for p in ("README.md", "src/app.py", ".gitignore"):
            self.assertFalse(weakening_guard.is_guardrail(p), p)

    def test_dependabot_is_a_guarded_security_floor_provision(self):
        # dependabot.yml travels to every generated repo as the git-native Dependencies floor. It gates no
        # merge, but silently dropping/weakening it downgrades a safety pillar — the "disclose, never downgrade
        # silently" law — so a removal/modify routes through the ack, exactly like its twin secret-scan.yml (which
        # is covered incidentally by the .github/workflows/ prefix). A pure addition does not fire.
        self.assertTrue(weakening_guard.is_guardrail(".github/dependabot.yml"))
        for status in ("removed", "modified"):
            flagged = weakening_guard.flagged_changes(
                [{"filename": ".github/dependabot.yml", "status": status}])
            self.assertEqual(len(flagged), 1, status)
        self.assertEqual(weakening_guard.flagged_changes(
            [{"filename": ".github/dependabot.yml", "status": "added"}]), [])

    def test_suite_declarations_are_a_guarded_killswitch(self):
        # .engine/suites.json decides which suite blocks the merge; a schema-valid
        # edit (CI -> local-nudge) would silently un-gate CI, so modifying it must
        # be flagged for the guardrail-ack. A pure addition does not.
        self.assertTrue(weakening_guard.is_guardrail(".engine/suites.json"))
        flagged = weakening_guard.flagged_changes(
            [{"filename": ".engine/suites.json", "status": "modified"}])
        self.assertEqual(len(flagged), 1)
        self.assertEqual(weakening_guard.flagged_changes(
            [{"filename": ".engine/suites.json", "status": "added"}]), [])

    def test_copied_status_is_caught(self):
        self.assertIn("copied", weakening_guard.WEAKENING_STATUS)
        flagged = weakening_guard.flagged_changes(
            [{"filename": ".github/workflows/x.yml", "status": "copied"}])
        self.assertEqual(len(flagged), 1)

    def test_removed_renamed_and_modified_lock_are_flagged(self):
        files = [
            {"filename": ".engine/tools/validate.py", "status": "removed"},
            {"filename": ".github/workflows/new.yml", "status": "renamed",
             "previous_filename": ".github/workflows/engine-ci.yml"},
            {"filename": ".engine/uv.lock", "status": "modified"},
        ]
        self.assertEqual(len(weakening_guard.flagged_changes(files)), 3)

    def test_addition_and_nonguardrail_not_flagged(self):
        files = [
            {"filename": ".github/workflows/new.yml", "status": "added"},
            {"filename": "README.md", "status": "modified"},
        ]
        self.assertEqual(weakening_guard.flagged_changes(files), [])

    def test_validator_and_guard_are_permanent_floor_members(self):
        # The self-protection property that must NOT silently lapse: the validator and this
        # guard are guarded regardless of the derived set (validate.py is the sole home of the 5 built-in HARD
        # check kinds, which carry no params.script and so are unreachable by the derived clause).
        for p in (".engine/tools/validate.py", ".engine/tools/weakening_guard.py"):
            self.assertTrue(weakening_guard.is_guardrail(p, derived_scripts=frozenset()), p)

    def test_settings_json_and_ruleset_proxy_are_floored(self):
        # The live hole closed here (settings.json wires the enforcement hooks) + the ruleset-applying proxy.
        for p in (".claude/settings.json", ".engine/tools/bootstrap.py"):
            self.assertTrue(weakening_guard.is_guardrail(p, derived_scripts=frozenset()), p)

    def test_enforcement_hook_gates_are_floored(self):
        # The hand-listed enforcement-hook gates (build-owe 3 audit) — not check-scripts, guarded by the floor.
        # Includes BOTH block-budget members: modes.py (PreToolUse write-gate) and close.py (Stop disposition gate).
        for p in (".engine/tools/modes.py", ".engine/tools/close.py", ".engine/tools/hook-runner.sh",
                  ".engine/tools/hooks.py", ".engine/tools/issue_gate.py", ".engine/tools/github_client.py",
                  ".engine/tools/wiring.py", ".engine/tools/security_floor.py"):
            self.assertTrue(weakening_guard.is_guardrail(p, derived_scripts=frozenset()), p)

    def test_mechanic_build_gate_is_floored(self):
        # The engine-mechanic cross-repo-write gate (eADR-0026): its fail-closed, host-anchored belt authorizes
        # running a SEPARATE checkout's own tools and opening a PR against it — a live runtime gate with NO
        # on-disk floored correlate, so it MUST route through the guardrail-ack. Pinned so a future edit that drops
        # it from _FLOOR_ENFORCEMENT_HOOKS is caught here. Its checkout_health readers stay UNGUARDED (fail-soft
        # reporters, not the authorizing gate).
        self.assertTrue(weakening_guard.is_guardrail(".engine/tools/mechanic_build.py", derived_scripts=frozenset()))
        self.assertFalse(weakening_guard.is_guardrail(".engine/tools/checkout_health.py", derived_scripts=frozenset()))

    def test_non_gate_tooling_is_not_guarded(self):
        # The over-firing the narrowing fixes: benign tools (boot, memory, telemetry, status, the self-review renderer,
        # attention) are NOT guarded when the derived set does not name them — the whole point of the narrowing.
        derived = frozenset({".engine/tools/protection_guard.py"})
        for p in (".engine/tools/boot.py", ".engine/tools/engine_status.py",
                  ".engine/tools/memory/compact.py", ".engine/tools/telemetry.py",
                  ".engine/tools/self_map.py", ".engine/tools/scent.py",
                  ".engine/tools/audit_digest.py", "README.md", "src/app.py"):
            self.assertFalse(weakening_guard.is_guardrail(p, derived_scripts=derived), p)

    def test_check_script_is_guarded_by_presence_only(self):
        # A check rule's enforcement script is guarded BY PRESENCE in the derived set — and NOT when absent.
        p = ".engine/tools/product_design/lock_integrity.py"
        self.assertTrue(weakening_guard.is_guardrail(p, derived_scripts=frozenset({p})))
        self.assertFalse(weakening_guard.is_guardrail(p, derived_scripts=frozenset()))

    def test_module_kind_callable_is_guarded_by_path_property(self):
        # A module-provided check-kind callable (.engine/tools/<module>/kind_<name>.py) runs a
        # validation kind's enforcement in CI but carries NO params.script, so the check-script derivation cannot
        # reach it. It is guarded by the one-level filename↔kind PATH PROPERTY — covered even with an EMPTY derived
        # set (it is not a check-script) and even as a brand-new file.
        self.assertTrue(weakening_guard.is_guardrail(".engine/tools/mymod/kind_foo.py", derived_scripts=frozenset()))
        # A plain tool beside a kind is NOT guarded; a top-level kind_*.py is not a discovered module kind.
        self.assertFalse(weakening_guard.is_guardrail(".engine/tools/mymod/helper.py", derived_scripts=frozenset()))
        self.assertFalse(weakening_guard.is_guardrail(".engine/tools/kind_top.py", derived_scripts=frozenset()))

    def test_blanket_tools_prefix_dropped_but_fail_safe_restores_it(self):
        # The blanket .engine/tools/ prefix is GONE from the static roster (a non-gate tool is guarded only if the
        # derived set names it), but the None fail-safe sentinel restores whole-dir coverage.
        self.assertFalse(weakening_guard.is_guardrail(".engine/tools/boot.py", derived_scripts=frozenset()))
        self.assertTrue(weakening_guard.is_guardrail(".engine/tools/boot.py", derived_scripts=None))

    def test_instance_declared_path_is_guarded_by_the_union(self):
        # #532: a DEPLOYMENT's own product path (exact or under a declared prefix) is guarded via the instance
        # pair; a path it did not declare is not. The engine floor is checked FIRST and independently, so the
        # instance argument can only ADD — an empty instance pair never subtracts an engine-floor path.
        inst = ({"scanners/contain.py"}, ("scanners/",))
        self.assertTrue(weakening_guard.is_guardrail("scanners/contain.py",
                        derived_scripts=frozenset(), instance_guards=inst))
        self.assertTrue(weakening_guard.is_guardrail("scanners/deep/x.py",
                        derived_scripts=frozenset(), instance_guards=inst))
        self.assertFalse(weakening_guard.is_guardrail("src/app.py",
                         derived_scripts=frozenset(), instance_guards=inst))
        # engine floor still guards regardless of an EMPTY instance pair (the union never subtracts)
        for p in (".engine/tools/validate.py", ".github/workflows/engine-ci.yml", ".engine/check/x.json"):
            self.assertTrue(weakening_guard.is_guardrail(p, derived_scripts=frozenset(), instance_guards=(set(), ())), p)

    def test_instance_read_is_empty_and_silent_and_filters_degenerate(self):
        # An ABSENT declaration -> the empty pair, silently. Driven through an explicit path that cannot
        # exist rather than the no-argument default: the default reads the HOST repository's own
        # declaration, so asserting it is empty asserts a fact about whichever repo the suite runs in —
        # true here, false in a deployment that uses the feature, which would red its required self-tests
        # over adopting a shipped capability. The property under test is the reader's, not the repo's.
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(weakening_guard._read_instance_guards(
                os.path.join(d, "no-such-declaration.json")), (set(), ()))
        # Defensive parse behind the CI shape gate: non-string / empty / degenerate members are dropped so the
        # catastrophic empty-prefix (`startswith("")` guards everything) can never take effect even off-gate.
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump({"guarded_paths": ["a.py", "", 3], "guarded_prefixes": ["scanners/", "", ".", "/"]}, fh)
            p = fh.name
        try:
            self.assertEqual(weakening_guard._read_instance_guards(p), ({"a.py"}, ("scanners/",)))
        finally:
            os.unlink(p)

    def test_instance_declaration_shrink_flags_removal_and_deletion_not_addition(self):
        # The directional detector for the declaration file (#532): removing a declared path is a WEAKENING
        # (flag -> ack); a pure addition is a strengthening (never flags). Mirrors home_repoint's shape.
        rel = weakening_guard.INSTANCE_DECL_REL
        removed = ('@@\n   "guarded_paths": [\n-    "scanners/old.py",\n     "scanners/keep.py"\n   ]')
        self.assertEqual(weakening_guard.instance_declaration_shrink(
            [{"filename": rel, "status": "modified", "patch": removed}]), ("shrink", ["scanners/old.py"]))
        added = ('@@\n   "guarded_paths": [\n     "scanners/keep.py",\n+    "scanners/new.py"\n   ]')
        self.assertIsNone(weakening_guard.instance_declaration_shrink(
            [{"filename": rel, "status": "modified", "patch": added}]))
        self.assertEqual(weakening_guard.instance_declaration_shrink(
            [{"filename": rel, "status": "removed"}]), ("removed", []))
        # a non-declaration file is ignored; an unreadable declaration patch fails closed
        self.assertIsNone(weakening_guard.instance_declaration_shrink(
            [{"filename": "src/app.py", "status": "modified", "patch": removed}]))
        self.assertEqual(weakening_guard.instance_declaration_shrink(
            [{"filename": rel, "status": "modified", "patch": ""}]), ("unreadable-patch", []))
        # a RENAME off the canonical path is a full removal (the reader loads a fixed path) — the silent-drop
        # bypass the deliverable gate caught: it must NOT slip past unflagged.
        self.assertEqual(weakening_guard.instance_declaration_shrink(
            [{"filename": "docs/old-guards.json", "status": "renamed", "previous_filename": rel}]), ("removed", []))
        # an escape/embedded separator that could disguise a removal -> fail closed
        escaped = '@@\n   "guarded_paths": [\n-    "scanners/a\\b.py"\n   ]'
        self.assertEqual(weakening_guard.instance_declaration_shrink(
            [{"filename": rel, "status": "modified", "patch": escaped}]), ("escaped", []))

    def test_flagged_changes_unions_the_base_declaration_end_to_end(self):
        # The load-bearing WIRING (#532): flagged_changes with the DEFAULT instance seam actually reads the base
        # declaration from disk and flags an edit to a declared product path. A refactor that broke the base-read
        # threading would make THIS fail rather than passing green with the injected-pair tests above.
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump({"guarded_paths": ["scanners/contain.py"], "guarded_prefixes": ["scanners/"]}, fh)
            decl_path = fh.name
        original = weakening_guard._BASE_INSTANCE_GUARDS
        try:
            weakening_guard._BASE_INSTANCE_GUARDS = decl_path
            flagged = weakening_guard.flagged_changes(
                [{"filename": "scanners/contain.py", "status": "modified"},
                 {"filename": "scanners/deep/helper.py", "status": "modified"},
                 {"filename": "src/app.py", "status": "modified"}],
                derived_scripts=frozenset())
            names = {shown for _status, shown in flagged}
            self.assertIn("scanners/contain.py", names)      # the exact declared path
            self.assertIn("scanners/deep/helper.py", names)  # under the declared prefix
            self.assertNotIn("src/app.py", names)            # undeclared -> not flagged
        finally:
            weakening_guard._BASE_INSTANCE_GUARDS = original
            os.unlink(decl_path)


def _write_check_json(path, obj):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh)


class TestWeakeningTiers(unittest.TestCase):
    """The eADR-0040 tier machinery: classify() and the four directional detectors. The hard criterion is
    a property (a weakening with no mechanical correlate and no readable diff, plus the guard's own
    machinery); everything else guarded discloses. Fail-safe direction: unclassifiable input resolves HARD."""

    _IG = (set(), ())  # no instance declaration

    # Hard-floor members owned by an OPTIONAL module: each is guarded by its check rule's PRESENCE, so a
    # deployment that DECLINED the owning module (the deployment gate's add-on-declined projection) legitimately
    # ships neither the tool file nor its `.engine/check/*.json` rule — is_guardrail() then correctly returns
    # False, a declined-shape truth rather than dead weight. Keyed on the EXACT _HARD_EXACT path (a module may own
    # several scripts). Any member NOT listed here is asserted unconditionally, so a typo'd or genuinely
    # unguarded floor entry is still caught; adding a new optional-owned member without listing it here fails
    # safe (it reds the declined projection until handled), never silently skips.
    _OPTIONAL_FLOOR_OWNERS = {
        ".engine/tools/product_design/lock_integrity.py": "product-design",
        ".engine/tools/dependency_discipline/review.py": "dependency-discipline",
    }

    def test_classify_tiers(self):
        c = weakening_guard.classify
        # disclosure tier: the measured noise — the validator, the hook substrate, readable configs
        for path in (".engine/tools/validate.py", ".engine/tools/modes.py", ".engine/tools/close.py",
                     ".engine/tools/hooks.py", ".engine/pyproject.toml", ".claude/settings.json",
                     ".github/workflows/engine-ci.yml", ".engine/check/pr-body-completeness.json",
                     ".engine/schemas/engine.v1.json", ".github/dependabot.yml"):
            self.assertEqual(c(path, "modified", instance_guards=self._IG), "soft", path)
        # killswitch tier: the hard floor, on any weakening status
        for path in weakening_guard._HARD_EXACT:
            self.assertEqual(c(path, "modified", instance_guards=self._IG), "hard", path)
        # removal/rename of ANY guarded file is hard — even where modification discloses
        self.assertEqual(c(".engine/tools/validate.py", "removed", instance_guards=self._IG), "hard")
        self.assertEqual(c(".github/workflows/engine-ci.yml", "renamed",
                           prev=".github/workflows/engine-ci.yml", instance_guards=self._IG), "hard")
        # a previous_filename on the hard floor is hard (a rename judged against where it came FROM)
        self.assertEqual(c("elsewhere.py", "modified", prev=".engine/tools/repo_identity.py",
                           instance_guards=self._IG), "hard")
        # an instance-declared path keeps the friction its operator opted into (#532)
        self.assertEqual(c("scanner/gate.py", "modified",
                           instance_guards=({"scanner/gate.py"}, ())), "hard")
        self.assertEqual(c("scanner/deep/x.py", "modified",
                           instance_guards=(set(), ("scanner/",))), "hard")

    def test_hard_floor_members_are_all_guarded(self):
        # every hard-floor member must be IN the guarded set, or classify() would never see it — a floor
        # entry outside is_guardrail() is dead weight that reads as protection. An optional-module-owned member
        # is skipped ONLY where its owning module is declined (see _OPTIONAL_FLOOR_OWNERS); everywhere it is
        # present it is still asserted, so a genuine dead-weight entry is still caught.
        derived = weakening_guard._derive_check_scripts()
        installed = _installed_module_ids()
        for path in weakening_guard._HARD_EXACT:
            owner = self._OPTIONAL_FLOOR_OWNERS.get(path)
            if owner and owner not in installed:
                continue
            self.assertTrue(weakening_guard.is_guardrail(path, derived, self._IG), path)

    def test_check_rule_demotion_detector(self):
        d = weakening_guard.check_rule_demotion
        rule = ".engine/check/foo.json"
        # a tier flip is structural -> escalates
        self.assertEqual(d([{"filename": rule, "status": "modified",
                             "patch": '@@ -5 +5 @@\n-  "tier": "hard",\n+  "tier": "soft",'}]),
                         [(rule, "structural")])
        # deleting the suites key entirely (the silent roster drop) -> escalates
        self.assertEqual(d([{"filename": rule, "status": "modified",
                             "patch": '@@ -5 +5 @@\n-  "suites": ["CI"],'}]),
                         [(rule, "structural")])
        # a params/script repoint -> escalates (the one-PR gate-gut)
        self.assertEqual(d([{"filename": rule, "status": "modified",
                             "patch": '@@ -5 +5 @@\n-  "params": { "script": "a.py" },\n+  "params": { "script": "b.py" },'}]),
                         [(rule, "structural")])
        # a message-only reword — the one provably-benign shape — passes
        self.assertIsNone(d([{"filename": rule, "status": "modified",
                              "patch": '@@ -5 +5 @@\n-  "message": "old words",\n+  "message": "new words, \\"quoted\\" fine",'}]))
        # a second key smuggled onto the message line -> escalates
        self.assertEqual(d([{"filename": rule, "status": "modified",
                             "patch": '@@ -5 +5 @@\n+  "message": "x", "tier": "soft",'}]),
                         [(rule, "unclear")])
        # unreadable patch -> fail closed
        self.assertEqual(d([{"filename": rule, "status": "modified"}]), [(rule, "unreadable-patch")])
        # an added rule is a pure addition — never judged here
        self.assertIsNone(d([{"filename": rule, "status": "added",
                              "patch": '@@ -0 +1 @@\n+  "tier": "hard",'}]))

    def test_workflow_gate_edit_detector(self):
        d = weakening_guard.workflow_gate_edit
        wf = ".github/workflows/engine-guard.yml"
        # the trigger flip that would run the PR's own copy of the guard -> escalates
        self.assertEqual(d([{"filename": wf, "status": "modified",
                             "patch": "@@ -18 +18 @@\n-  pull_request_target:\n+  pull_request:"}]),
                         [(wf, "gate-line")])
        # a same-action version pin (the routine dependabot bump) stays soft
        self.assertIsNone(d([{"filename": wf, "status": "modified",
                              "patch": "@@ -1 +1 @@\n-      - uses: astral-sh/setup-uv@aaa # v8\n+      - uses: astral-sh/setup-uv@bbb # v9"}]))
        # an action SWAP -> escalates
        self.assertEqual(d([{"filename": wf, "status": "modified",
                             "patch": "@@ -1 +1 @@\n-      - uses: astral-sh/setup-uv@aaa\n+      - uses: evil/setup-uv@aaa"}]),
                         [(wf, "action-swap")])
        # comment churn stays soft
        self.assertIsNone(d([{"filename": wf, "status": "modified",
                              "patch": "@@ -1 +1 @@\n-# old comment\n+# new comment"}]))
        # unreadable patch -> fail closed
        self.assertEqual(d([{"filename": wf, "status": "modified"}]), [(wf, "unreadable-patch")])

    def test_settings_and_bootstrap_detectors(self):
        s = weakening_guard.settings_gate_unwire
        # removing a gate-hook wire -> escalates; adding one stays soft
        self.assertEqual(s([{"filename": ".claude/settings.json", "status": "modified",
                             "patch": '@@ -1 +1 @@\n-    "command": "hook-runner.sh modes.py",'}]),
                         [(".claude/settings.json", "gate-unwire")])
        self.assertIsNone(s([{"filename": ".claude/settings.json", "status": "modified",
                              "patch": '@@ -1 +1 @@\n+    "command": "hook-runner.sh boot.py",'}]))
        b = weakening_guard.bootstrap_ruleset_edit
        # a ruleset-vocabulary line -> escalates; label churn stays soft
        self.assertEqual(b([{"filename": ".engine/tools/bootstrap.py", "status": "modified",
                             "patch": "@@ -1 +1 @@\n-    required_status_checks=[...]"}]),
                         [(".engine/tools/bootstrap.py", "ruleset-line")])
        self.assertIsNone(b([{"filename": ".engine/tools/bootstrap.py", "status": "modified",
                              "patch": '@@ -1 +1 @@\n+ACK_LABEL_DESCRIPTION = "words"'}]))

    def test_engine_guard_workflow_preserves_the_validator_exit_code(self):
        # DRIFT DETECTOR: the engine-guard workflow step appends output to the run summary, and three
        # regressions would each silently break required check #2 or its reporting: (a) a bare invocation
        # under GitHub's implicit `bash -e` aborts before the summary/annotation on a hard finding, so the
        # invocation must be the CONDITION of an `if` (errexit-exempt); (b) an rc captured after an
        # intervening command (cat, echo) records the wrong command's status, so rc must be set ONLY
        # inside that if/else; (c) the step must END by exiting the captured rc. Also pins the two
        # annotation sentinels the workflow greps for — each carries a character the guard's whitelist
        # sanitizer strips, so PR-controlled content cannot forge them.
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        wf_path = os.path.join(root, ".github", "workflows", "engine-guard.yml")
        with open(wf_path, encoding="utf-8") as fh:
            wf = fh.read()
        self.assertRegex(
            wf, r'if uv run [^\n]*validate\.py --check engine/check/guardrail-weakening[^\n]*; then',
            "the validator invocation must be the condition of an `if` (errexit-exempt), or a hard "
            "finding aborts the step before its own report is surfaced")
        run_block = wf[wf.index("if uv run"):]
        step_lines = [ln.strip() for ln in run_block.splitlines() if ln.strip()]
        self.assertRegex(step_lines[1], r"^rc=0$", "the then-arm must record rc=0 and nothing else")
        self.assertRegex(step_lines[3], r"^rc=\$\?$",
                         "the else-arm must capture rc directly from the invocation — an intervening "
                         "command would record the wrong exit status")
        self.assertEqual(step_lines[-1], 'exit "${rc}"',
                         "the step must END by exiting the captured validator rc")
        self.assertIn('grep -q "GUARDRAIL DISCLOSURE —"', wf)
        self.assertIn('grep -q "ACKNOWLEDGED (guardrail-ack"', wf)
        self.assertIn("GITHUB_STEP_SUMMARY:-/dev/null", wf,
                      "the summary append must survive an unset GITHUB_STEP_SUMMARY under set -u")


class TestWeakeningDerivedSet(unittest.TestCase):
    """The derived-by-presence clause + its ALL-OR-NOTHING fail-safe. The derivation reads the base
    check dir on disk; these tests inject a temp dir the way TestWeakeningReHome monkeypatches _read_base_home."""

    def test_derives_params_script_from_check_jsons(self):
        with tempfile.TemporaryDirectory() as d:
            _write_check_json(os.path.join(d, "a.json"),
                              {"kind": "custom/script", "params": {"script": ".engine/tools/a.py"}})
            _write_check_json(os.path.join(d, "b.json"), {"kind": "presence"})  # no params.script -> skipped
            _write_check_json(os.path.join(d, "c.json"),
                              {"kind": "custom/script", "params": {"script": ".engine/tools/sub/c.py"}})
            self.assertEqual(weakening_guard._derive_check_scripts(d),
                             {".engine/tools/a.py", ".engine/tools/sub/c.py"})

    def test_one_malformed_json_collapses_whole_derivation_to_none(self):
        # ALL-OR-NOTHING: a single corrupt rule returns None (the fail-safe sentinel), NEVER a partial set that
        # would silently drop the broken rule's own script from the guarded set (the fail-open the weakening guard rejects).
        with tempfile.TemporaryDirectory() as d:
            _write_check_json(os.path.join(d, "good.json"), {"params": {"script": ".engine/tools/good.py"}})
            with open(os.path.join(d, "bad.json"), "w", encoding="utf-8") as fh:
                fh.write("{ not valid json")
            self.assertIsNone(weakening_guard._derive_check_scripts(d))

    def test_missing_check_dir_is_the_fail_safe_sentinel(self):
        self.assertIsNone(weakening_guard._derive_check_scripts(os.path.join(tempfile.gettempdir(), "no-such-check")))

    def test_fail_safe_none_guards_the_whole_tools_dir(self):
        # When derivation fails, is_guardrail falls back to the blanket .engine/tools/ — never drops a gate.
        for p in (".engine/tools/anything.py", ".engine/tools/sub/deep/x.py", ".engine/tools/hook-runner.sh"):
            self.assertTrue(weakening_guard.is_guardrail(p, derived_scripts=None), p)
        self.assertFalse(weakening_guard.is_guardrail("README.md", derived_scripts=None))

    def test_live_check_dir_covers_real_scattered_enforcement_scripts(self):
        # Against the REAL base .engine/check dir: representative scattered enforcement scripts (a top-level guard
        # and two subpackage check-scripts) are guarded by presence, proving the derivation reaches the subpackages.
        # protection_guard.py is core (always present); the two subpackage scripts ship with OPTIONAL modules, so
        # each is asserted only when its module is installed — a deployment that DECLINED product-design /
        # dependency-discipline legitimately lacks its script and must not red here (#646).
        derived = weakening_guard._derive_check_scripts()
        self.assertIsNotNone(derived)
        installed = _installed_module_ids()
        expected = [".engine/tools/protection_guard.py"]
        if "product-design" in installed:
            expected.append(".engine/tools/product_design/lock_integrity.py")
        if "dependency-discipline" in installed:
            expected.append(".engine/tools/dependency_discipline/pinning.py")
        for p in expected:
            self.assertIn(p, derived, p)


class TestEnforcementHookGuardCoverage(unittest.TestCase):
    """Drift detector (issue #250): every hook wired on a block-eligible event (PreToolUse / Stop) in
    .claude/settings.json whose handler CAN emit a merge-relevant block MUST be guarded (in the weakening_guard
    floor). A NEW block-capable hook wired without being floored fails this test, converting the silent fail-open
    (an un-guarded new gate) into a loud CI failure at hook-add time.

    'Can block' is read from the hook's OWN CODE, not a hand-maintained non-gate allowlist (which is exactly what
    let close.py slip in the first draft — it was allowlisted as 'never blocks' when it HARD-BLOCKS the turn).
    hooks.py defines only two block channels: hooks.block (exit 2) and hooks.decide (the structured deny). A hook
    whose source references neither genuinely cannot deny (a pure regenerator / housekeeping) and need not be
    floored; one that references either must be. SessionStart / PostToolUse / PreCompact / UserPromptSubmit hooks
    are not block-eligible events and are out of scope."""

    ENFORCEMENT_EVENTS = ("PreToolUse", "Stop")
    BLOCK_PRIMITIVES = ("hooks.block", "hooks.decide")  # the ONLY two deny channels (hooks.py) — grep-derivable
    # Handler paths are extracted from the hook command strings; this regex encodes the "every engine tool is a
    # .py or .sh" assumption. A future non-py/sh handler (a .js, a binary) would not match — so wiring one is a
    # DELIBERATE decision that must also extend this regex, not a silent coverage gap.
    _SCRIPT_RE = re.compile(r"\.engine/tools/[\w./-]+\.(?:py|sh)")

    def _root(self):
        return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(weakening_guard.__file__))))

    def _settings(self):
        with open(os.path.join(self._root(), ".claude", "settings.json"), encoding="utf-8") as fh:
            return json.load(fh)

    def _can_block(self, script_rel: str) -> bool:
        """True iff the hook handler's committed source references a block channel (so it can deny). Fail-safe:
        an unreadable handler is treated as block-capable (it must then be floored)."""
        try:
            with open(os.path.join(self._root(), script_rel), encoding="utf-8") as fh:
                src = fh.read()
        except OSError:
            return True
        return any(prim in src for prim in self.BLOCK_PRIMITIVES)

    def test_every_block_capable_enforcement_hook_is_floored(self):
        settings = self._settings()
        derived = weakening_guard._derive_check_scripts()
        unguarded = []
        for event in self.ENFORCEMENT_EVENTS:
            for matcher in settings.get("hooks", {}).get(event, []):
                for hook in matcher.get("hooks", []):
                    for script in self._SCRIPT_RE.findall(hook.get("command", "")):
                        if self._can_block(script) and not weakening_guard.is_guardrail(script, derived):
                            unguarded.append((event, script))
        self.assertEqual(unguarded, [],
                         "block-capable enforcement hook(s) wired in settings.json but NOT guarded: "
                         f"{unguarded} — a hook that can emit hooks.block/hooks.decide can turn a gate off; add it "
                         "to _FLOOR_ENFORCEMENT_HOOKS in weakening_guard.py")

    def test_both_block_budget_gates_are_covered(self):
        # Ground the drift detector against the two real block-budget members, so it can't pass vacuously: modes.py
        # (PreToolUse write-gate, denies via hooks.decide) and close.py (Stop disposition gate, denies via
        # hooks.block) are BOTH wired on their events, BOTH block-capable, and BOTH floored.
        settings = self._settings()
        wired = {(ev, s) for ev in self.ENFORCEMENT_EVENTS
                 for m in settings.get("hooks", {}).get(ev, [])
                 for h in m.get("hooks", []) for s in self._SCRIPT_RE.findall(h.get("command", ""))}
        self.assertIn(("PreToolUse", ".engine/tools/modes.py"), wired)
        self.assertIn(("Stop", ".engine/tools/close.py"), wired)
        for gate in (".engine/tools/modes.py", ".engine/tools/close.py"):
            self.assertTrue(self._can_block(gate), gate)
            self.assertTrue(weakening_guard.is_guardrail(gate, frozenset()), gate)


class TestSchemaGateGuardCoverage(unittest.TestCase):
    """Drift detector (#467): every schema a hard, CI-suite `kind: schema` check resolves to is the TEETH of a
    merge gate — loosening the schema loosens that gate, and the `.engine/check/` rule itself may be untouched
    (the schema is resolved through the catalog's `governing_schema` or a `params.schema` override) — so it MUST
    be floored in weakening_guard (_FLOOR_GATE_SCHEMAS). The gate set is recomputed here from the LIVE check
    rules via the validator's OWN resolver (_surface_record_for + the _governing_schema join), so adding a new
    hard CI schema-kind check whose schema is not floored fails LOUD here rather than merging un-guarded (the
    silent fail-open #467 closes). Bidirectional: the computed set must EQUAL the floor, so a stale floor entry
    is caught too. This is the precise-floor alternative to a blanket `.engine/schemas/` prefix, which would
    re-introduce the over-firing (the output-contract schemas back only fixture tests, gate no merge)."""

    def _gate_schema_paths(self) -> set:
        out = set()
        for fn in sorted(os.listdir(validate.CHECK_DIR)):
            if not fn.endswith(".json"):
                continue
            rule = validate.load_json(os.path.join(validate.CHECK_DIR, fn))
            if rule.get("kind") != "schema" or rule.get("tier") != "hard" or "CI" not in rule.get("suites", []):
                continue
            params = rule.get("params") or {}
            if params.get("schema"):                       # explicit override — mirrors _governing_schema
                ref, base = params["schema"], validate.ROOT
            else:                                          # catalog routing via the surface's governing_schema
                # Assumes one check glob resolves to ONE surface (true for all current hard-CI schema checks): a
                # future check whose target were rooted ABOVE a surface dir and spanned two surfaces with
                # different governing_schemas would resolve only the first here — extend this if that ever lands.
                probe = rule.get("target", {}).get("path", "").split("*")[0]
                if not probe.endswith("/"):
                    probe = os.path.dirname(probe) + "/"
                rec = validate._surface_record_for(probe + "__probe__")
                ref, base = (rec.get("governing_schema") if rec else None), validate.SCHEMAS_DIR
            if not ref or ref.startswith("http") or ref == validate.META_SCHEMA_URI:
                continue                                   # a dialect / well-formedness self-check names no file
            out.add(os.path.relpath(os.path.normpath(os.path.join(base, ref)), validate.ROOT))
        return out

    def test_every_hard_ci_schema_gate_is_floored(self):
        gates = self._gate_schema_paths()
        self.assertTrue(gates, "expected at least one hard CI schema-kind gate")
        for p in sorted(gates):
            self.assertTrue(weakening_guard.is_guardrail(p, derived_scripts=frozenset()),
                            f"{p} is the teeth of a hard CI schema gate but is NOT guarded — add it to "
                            "_FLOOR_GATE_SCHEMAS in weakening_guard.py")
        self.assertEqual(gates, set(weakening_guard._FLOOR_GATE_SCHEMAS),
                         "the floored gate-schema set has drifted from the live check rules")

    def test_contract_schema_is_a_guarded_gate(self):
        # Ground the detector against the real #467 subject so it can't pass vacuously: contract.v1.json is the
        # teeth of contract-frontmatter (hard, CI), so its loosening is held — modify flagged, add is not.
        self.assertTrue(weakening_guard.is_guardrail(".engine/schemas/contract.v1.json", derived_scripts=frozenset()))
        self.assertTrue(weakening_guard.flagged_changes(
            [{"filename": ".engine/schemas/contract.v1.json", "status": "modified"}], derived_scripts=frozenset()))
        self.assertEqual(weakening_guard.flagged_changes(
            [{"filename": ".engine/schemas/contract.v1.json", "status": "added"}], derived_scripts=frozenset()), [])

    def test_output_contract_schema_is_not_guarded(self):
        # The precise-floor choice: a schema that backs only a fixture unit test (an agent/tool OUTPUT contract),
        # gating no merge, is correctly NOT guarded — a blanket .engine/schemas/ prefix would wrongly hold it.
        self.assertNotIn(".engine/schemas/plan-review-finding.v1.json", weakening_guard._FLOOR_GATE_SCHEMAS)
        self.assertFalse(weakening_guard.is_guardrail(".engine/schemas/plan-review-finding.v1.json",
                                                      derived_scripts=frozenset()))


class TestProtectionFloor(unittest.TestCase):
    CHECKS = ["engine-ci", "engine-guard"]

    def _full(self):
        return [
            {"type": "pull_request", "parameters": {
                "required_review_thread_resolution": True, "required_approving_review_count": 0}},
            {"type": "required_status_checks", "parameters": {
                "required_status_checks": [{"context": "engine-ci"}, {"context": "engine-guard"}]}},
            {"type": "non_fast_forward", "parameters": {}},
            {"type": "deletion", "parameters": {}},
        ]

    def test_full_floor_has_nothing_missing(self):
        self.assertEqual(protection_guard.missing_floor(self._full(), self.CHECKS), [])

    def test_empty_rules_flags_every_floor_piece(self):
        missing = protection_guard.missing_floor([], self.CHECKS)
        self.assertTrue(any("pull request" in m for m in missing))
        self.assertTrue(any("status checks" in m for m in missing))
        self.assertTrue(any("force-push" in m for m in missing))
        self.assertTrue(any("deletion" in m for m in missing))

    def test_unbound_required_check_is_flagged(self):
        rules = self._full()
        rules[1]["parameters"]["required_status_checks"] = [{"context": "engine-ci"}]
        missing = protection_guard.missing_floor(rules, self.CHECKS)
        self.assertTrue(any("engine-guard" in m for m in missing))

    def test_conversation_resolution_required(self):
        rules = self._full()
        rules[0]["parameters"]["required_review_thread_resolution"] = False
        missing = protection_guard.missing_floor(rules, self.CHECKS)
        self.assertTrue(any("conversations" in m for m in missing))

    # ---- team tier: the stronger floor, its fidelity, and the deadlock-proof resolution ----
    def test_team_floor_from_the_builder_self_satisfies_its_verifier(self):
        # floor_ruleset(TEAM) must be EXACTLY what missing_floor(TEAM) accepts — the applier↔verifier
        # cross-consistency the solo floor already has, so a team repo's applied floor and the standing CI check
        # never disagree.
        rules = bootstrap.floor_ruleset(tier=protection_guard.TEAM)["rules"]
        self.assertEqual(protection_guard.missing_floor(rules, self.CHECKS, tier=protection_guard.TEAM), [])

    def test_team_floor_requires_approval_codeowner_and_last_push(self):
        pr = next(r["parameters"] for r in bootstrap.floor_ruleset(tier=protection_guard.TEAM)["rules"]
                  if r["type"] == "pull_request")
        self.assertEqual(pr["required_approving_review_count"], 1)
        self.assertTrue(pr["require_code_owner_review"])
        self.assertTrue(pr["require_last_push_approval"])

    def test_team_verifier_flags_a_solo_shaped_ruleset(self):
        # The solo floor (0 approvals, no code-owner) does NOT satisfy the team floor — the standing CI check on a
        # team repo flags exactly the team-specific rules a drift back to solo would drop.
        missing = protection_guard.missing_floor(self._full(), self.CHECKS, tier=protection_guard.TEAM)
        self.assertTrue(any("review approval" in m for m in missing))
        self.assertTrue(any("code-owner" in m for m in missing))
        self.assertTrue(any("after approval" in m for m in missing))

    def test_solo_floor_is_unchanged_by_the_tier_param(self):
        self.assertEqual(protection_guard.missing_floor(self._full(), self.CHECKS), [])
        self.assertEqual(protection_guard.missing_floor(self._full(), self.CHECKS, tier=protection_guard.SOLO), [])

    def test_resolve_tier_matrix_including_the_deadlock_guard(self):
        cases = [
            ({"identity": "solo"}, "solo"),
            ({"identity": "team", "engine_identity": {"login": "bot"}}, "team"),
            ({"identity": "team"}, "solo"),                          # team WITHOUT identity -> deadlock guard -> solo
            ({"identity": "team", "engine_identity": {}}, "solo"),   # empty identity object -> solo
            ({}, "solo"),                                            # no identity field -> solo
        ]
        with tempfile.TemporaryDirectory() as d:
            for manifest, expected in cases:
                with open(os.path.join(d, "engine.json"), "w", encoding="utf-8") as fh:
                    json.dump(manifest, fh)
                self.assertEqual(protection_guard.resolve_tier(d), expected, manifest)

    def test_resolve_tier_never_raises_on_a_non_object_or_missing_manifest(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "engine.json")
            for bad in ("[1,2,3]", '"astring"', "42", "true", "not json {"):
                with open(p, "w", encoding="utf-8") as fh:
                    fh.write(bad)
                self.assertEqual(protection_guard.resolve_tier(d), "solo", bad)
            os.remove(p)
            self.assertEqual(protection_guard.resolve_tier(d), "solo")   # missing file


class TestDecoratedScaffold(unittest.TestCase):
    """The visible-scaffold template: decorated placeholder slots still read as
    unfilled, real content reads as filled, and an inline <token> in real text is
    not mistaken for a placeholder (the over-strip guard)."""

    def test_decorated_placeholder_lines_are_empty(self):
        for line in ("**<summary>**", "- <detail>", "*<Impact: why>*",
                     "<bare>", "__<x>__", "  - <y>  "):
            self.assertTrue(validate.is_empty_section(line), line)

    def test_real_content_lines_are_not_empty(self):
        for line in ("**Real bold summary**", "- a real detail", "*Impact: real text*",
                     "Uses the <head> ref here.", "- text with <token> inside"):
            self.assertFalse(validate.is_empty_section(line), line)

    def test_decorated_section_with_one_real_line_is_not_empty(self):
        self.assertFalse(validate.is_empty_section("**<summary>**\n- a real bullet\n*<Impact: x>*"))

    def test_committed_template_body_fails_completeness(self):
        # Use IMPACT_RULE (mirrors the shipped rule's filled_subsection_label): the committed
        # template ships `*Impact: <...>*` lines that read as unfilled only when the enforced
        # label is known, so the param-less COMPLETENESS_RULE would not see them as empty.
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        with open(os.path.join(root, ".github", "pull_request_template.md"), encoding="utf-8") as fh:
            tmpl = fh.read()
        passed, found = validate.kind_presence(IMPACT_RULE, {"pr_body": tmpl})
        self.assertFalse(passed)
        self.assertEqual(len(found), len(SECTIONS))  # every section unfilled

    def test_filled_scaffold_passes(self):
        body = "\n".join(
            f"## {s}\n**Real summary for {s}**\n- a real bullet\n*Impact: real impact*"
            for s in SECTIONS)
        passed, found = validate.kind_presence(COMPLETENESS_RULE, {"pr_body": body})
        self.assertTrue(passed)
        self.assertEqual(found, [])


class TestPRContractNoDrift(unittest.TestCase):
    """The control-plane 9-section PR-body contract must not silently drift.

    The locked contract names nine required sections, in order — Purpose, Scope,
    Out of scope, Risk, Validation, Review, Demonstration, Files of interest, AI
    involvement — transcribed once above as SECTIONS (a human transcription of the control-plane
    spec; there is no in-repo machine source to derive it from, so the transcription
    itself has no mechanical correlate and is read by a human against the spec). The
    two legs below pin BOTH committed artifacts to that canonical anchor: the PR
    template a contributor fills, and the pr-body-completeness check (owned by
    validators-core) that gates the merge. A future edit that drops, renames,
    reorders, or adds a section to either one then fails CI instead of slipping
    through.

    These close two real gaps the existing completeness tests leave: those assert
    behaviour against the in-file COMPLETENESS_RULE *fixture* and only count the
    committed template's unfilled sections (test_committed_template_body_fails_
    completeness) — neither pins the heading IDENTITY of the committed template,
    nor reads the committed check at all. A `## Review` -> `## Reviewed` rename in
    the template passes the count-only test but fails leg (b) here.

    Honest ceiling: a *consistent* trim across the template, the check, AND this
    SECTIONS literal would pass both legs (the trimmer edits the anchor too). That
    residue is walled elsewhere, not here — the check and this test file both sit
    under guarded `.engine/` prefixes, so editing either is a guardrail-weakening
    change requiring `guardrail-ack`, and the operator's merge is the binding gate.
    """

    def _repo_root(self):
        return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    def test_committed_template_headings_match_canonical(self):
        # Leg (b): the committed PR template's level-2 (##) headings equal the
        # canonical nine, in order, with no extras or omissions. Catches a
        # dropped/renamed/reordered section the count-only completeness test misses.
        path = os.path.join(self._repo_root(), ".github", "pull_request_template.md")
        with open(path, encoding="utf-8") as fh:
            headings = validate.section_order(fh.read())
        self.assertEqual(headings, SECTIONS)

    def test_committed_completeness_check_sections_match_canonical(self):
        # Leg (a): the committed pr-body-completeness check (validators-core) enforces
        # exactly the canonical nine, in order. Pins the real gating enforcement to
        # the contract; the other completeness tests only exercise the in-file fixture.
        path = os.path.join(self._repo_root(), ".engine", "check",
                            "pr-body-completeness.json")
        with open(path, encoding="utf-8") as fh:
            rule = json.load(fh)
        self.assertEqual(rule["params"]["sections"], SECTIONS)

    def test_committed_completeness_check_enforces_impact_line(self):
        # Leg (c): the committed check carries the filled-Impact-line param. Pins the
        # SHIPPED behaviour (not just the in-file fixtures) so a future edit that drops
        # the param — silently un-gating the Impact line the operator reads first —
        # fails CI instead of slipping through.
        path = os.path.join(self._repo_root(), ".engine", "check",
                            "pr-body-completeness.json")
        with open(path, encoding="utf-8") as fh:
            rule = json.load(fh)
        self.assertEqual(rule["params"].get("filled_subsection_label"), "Impact")

    def test_committed_preamble_anchors_are_present_in_the_template(self):
        # Leg (d): the check's required_phrases (the consent-preamble anchors) must each
        # appear VERBATIM in the committed PR template. This binds template<->check so a
        # future reword of the preamble in the template that leaves the check hunting the
        # old phrase — which would redden every correctly-authored PR — fails CI here
        # instead. The subset direction (each check phrase is in the template) permits a
        # COORDINATED reword of both; an un-coordinated one is what this catches.
        check_path = os.path.join(self._repo_root(), ".engine", "check",
                                  "pr-body-completeness.json")
        with open(check_path, encoding="utf-8") as fh:
            phrases = json.load(fh)["params"].get("required_phrases")
        self.assertTrue(phrases, "the shipped check must declare the preamble anchors")
        tmpl_path = os.path.join(self._repo_root(), ".github", "pull_request_template.md")
        with open(tmpl_path, encoding="utf-8") as fh:
            template = fh.read()
        for phrase in phrases:
            self.assertIn(phrase, template,
                          f"preamble anchor {phrase!r} required by the check is absent from the template")


class TestDemonstrationSectionRequired(unittest.TestCase):
    """#881: the behavioral Demonstration section is its own required slot, separate from the
    spec-derived acceptance steps in Review. A behaviour-changing change cannot discharge it with
    the 'no settled description' no-op line — that line clears only the spec-derived lane. The
    floor is presence (the reviewer judges whether the demonstration is real); these pin that the
    slot is REQUIRED and that its absence is caught by the SHIPPED hard check, with or without any
    spec-derived content in Review (the SDD-removed shape)."""

    _PREAMBLE = (
        "> *A green mechanical check below shows this change conforms to the engine's rules. "
        "Your merge is the binding gate.*\n>\n"
        "> *A check that could not run leaves its area unverified.*")

    def _shipped(self):
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        with open(os.path.join(root, ".engine", "check", "pr-body-completeness.json"),
                  encoding="utf-8") as fh:
            return json.load(fh)

    def _section(self, name, demo_filled, review_line):
        if name == "Demonstration" and not demo_filled:
            # a fully unfilled Demonstration: placeholder summary, bullet, and Impact line
            return [f"## {name}", "", "**<the operator-runnable step or walkthrough>**", "",
                    "- <give the operator something they can run>", "",
                    "*Impact: <what the operator can watch work>*", ""]
        summary = f"**Real summary for {name}.**"
        if name == "Review":
            bullet = review_line
        elif name == "Demonstration":
            bullet = "- run `foo` and watch the widget flip when the behaviour is correct"
        else:
            bullet = f"- a real bullet for {name}"
        return [f"## {name}", "", summary, "", bullet, "", f"*Impact: real impact for {name}.*", ""]

    def _body(self, demo_filled=True, review_line="- things you can confirm yourself: run X"):
        parts = [self._PREAMBLE, ""]
        for s in SECTIONS:
            parts += self._section(s, demo_filled, review_line)
        return "\n".join(parts)

    def test_filled_demonstration_passes_the_shipped_gate(self):
        passed, findings = validate.kind_presence(self._shipped(), {"pr_body": self._body(demo_filled=True)})
        self.assertTrue(passed, f"a filled Demonstration should pass the shipped gate: {findings}")

    def test_empty_demonstration_is_caught_by_the_shipped_gate(self):
        passed, findings = validate.kind_presence(self._shipped(), {"pr_body": self._body(demo_filled=False)})
        self.assertFalse(passed, "an empty Demonstration must fail the hard completeness gate")
        self.assertTrue(any("Demonstration" in str(f) for f in findings),
                        f"the finding must name the Demonstration section: {findings}")

    def test_no_spec_line_in_review_does_not_excuse_an_empty_demonstration(self):
        # the SDD-removed shape: Review carries only the honest 'no settled description' no-op line,
        # yet an empty Demonstration is STILL caught — missing spec state never fills the behavioral slot.
        body = self._body(demo_filled=False,
                          review_line="- Nothing here is something you can run yourself — there's no "
                                      "settled description for this project yet.")
        passed, findings = validate.kind_presence(self._shipped(), {"pr_body": body})
        self.assertFalse(passed, "a no-spec Review line must not excuse an empty Demonstration")
        self.assertTrue(any("Demonstration" in str(f) for f in findings), findings)

    def test_committed_check_requires_the_demonstration_section(self):
        self.assertIn("Demonstration", self._shipped()["params"]["sections"])


class TestEmptinessLabelScope(unittest.TestCase):
    """The emptiness leg's Impact-awareness is scoped to the enforced label and off by
    default. With NO label its behaviour is byte-identical to before this change, so the
    other presence consumer (contract-threshold) and any real `Word: <token>` content are
    untouched. With the `Impact` label, the Impact line is excluded from the content count
    (judged by its own fill leg instead), so a section still needs real summary/bullet
    content AND a filled Impact line."""

    def test_no_label_leaves_real_labelled_content_alone(self):
        # regression: a Markdown autolink or ref after a label is REAL content, never a slot
        self.assertFalse(validate.is_empty_section("See: <https://example.com/wiki>"))
        self.assertFalse(validate.is_empty_section("Ref: <ticket-123>"))
        # contract-threshold uses no label — a Significance slot stays exactly as before
        self.assertFalse(validate.is_empty_section("*Significance: <fill me>*"))
        # a bare token is still empty, with or without a label (unchanged)
        self.assertTrue(validate.is_empty_section("<why this exists>"))

    def test_label_excludes_the_impact_line_from_content(self):
        # the same *Impact: <slot>* line: content when unscoped, excluded when the label is set
        self.assertFalse(validate.is_empty_section("*Impact: <what this enables>*"))
        self.assertTrue(validate.is_empty_section("*Impact: <what this enables>*", "Impact"))
        # an unrelated labelled line is NOT the Impact line, so it still counts as content
        self.assertFalse(validate.is_empty_section("See: <https://example.com>", "Impact"))

    def test_filled_impact_alone_does_not_satisfy_a_section(self):
        # summary/bullet enforcement is preserved: a filled Impact line is excluded, so a
        # section whose only non-placeholder line is the Impact line is still empty
        section = "**<summary>**\n- <detail>\n*Impact: a real consequence*"
        self.assertFalse(validate.is_empty_section(section))          # unscoped: Impact counts
        self.assertTrue(validate.is_empty_section(section, "Impact"))  # scoped: it does not


class TestSubsectionLineStatus(unittest.TestCase):
    def test_filled_forms(self):
        self.assertEqual(validate._subsection_line_status("*Impact: real prose*", "Impact"), "filled")
        self.assertEqual(validate._subsection_line_status("Impact: enables <slug> flow", "Impact"), "filled")
        # a filled line keeps an inline token: "Impact: <a> and <b>" is filled, not a slot
        self.assertEqual(validate._subsection_line_status("Impact: <a> and <b>", "Impact"), "filled")

    def test_bold_label_is_not_the_italic_form(self):
        # the shipped Impact line is italic (*Impact: ...*); a bold label is off-convention
        # and is NOT accepted — it reads as missing, nudging authors back to the italic form
        self.assertEqual(validate._subsection_line_status("**Impact:** real prose", "Impact"), "missing")

    def test_unfilled_new_sentinel(self):
        self.assertEqual(validate._subsection_line_status("*Impact: <guidance>*", "Impact"), "unfilled")

    def test_unfilled_old_sentinel(self):
        self.assertEqual(validate._subsection_line_status("*<Impact: guidance>*", "Impact"), "unfilled")

    def test_unfilled_empty_label(self):
        self.assertEqual(validate._subsection_line_status("*Impact:*", "Impact"), "unfilled")

    def test_missing(self):
        self.assertEqual(validate._subsection_line_status("just prose\n- a bullet", "Impact"), "missing")


class TestImpactFillEnforcement(unittest.TestCase):
    """The filled-Impact-line leg, gated behind params.filled_subsection_label."""

    def _body(self, impact):  # all filled sections, each with `impact` as its Impact line
        return "\n".join(f"## {s}\n**Real summary**\n- a real bullet\n{impact}" for s in SECTIONS)

    def test_filled_impact_passes(self):
        passed, found = validate.kind_presence(IMPACT_RULE, {"pr_body": self._body("*Impact: real consequence*")})
        self.assertTrue(passed)
        self.assertEqual(found, [])

    def test_unfilled_impact_flags_all_sections(self):
        passed, found = validate.kind_presence(IMPACT_RULE, {"pr_body": self._body("*Impact: <slot>*")})
        self.assertFalse(passed)
        self.assertEqual(len(found), len(SECTIONS))
        self.assertTrue(all("no filled Impact line" in f["message"] and f["severity"] == "hard" for f in found))

    def test_deleted_impact_line_flags(self):
        # the delete-the-line bypass: a filled section with NO Impact line at all must fail
        body = "\n".join(f"## {s}\n**Real summary**\n- a real bullet" for s in SECTIONS)
        passed, found = validate.kind_presence(IMPACT_RULE, {"pr_body": body})
        self.assertFalse(passed)
        self.assertEqual(len(found), len(SECTIONS))
        self.assertTrue(all("no filled Impact line" in f["message"] for f in found))

    def test_missing_section_not_double_counted(self):
        # a wholly-missing body yields ONE finding per section (presence leg), never two
        passed, found = validate.kind_presence(IMPACT_RULE, {"pr_body": ""})
        self.assertEqual(len(found), len(SECTIONS))

    def test_present_but_empty_section_not_double_counted(self):
        # a section PRESENT but only placeholders (incl. its Impact slot) is reported once,
        # by the presence leg; the fill leg skips it (it is empty), so no 2x per section
        body = "\n".join(f"## {s}\n**<summary>**\n- <detail>\n*Impact: <slot>*" for s in SECTIONS)
        passed, found = validate.kind_presence(IMPACT_RULE, {"pr_body": body})
        self.assertFalse(passed)
        self.assertEqual(len(found), len(SECTIONS))
        self.assertTrue(all("empty or only contains the template placeholder" in f["message"] for f in found))

    def test_param_absent_skips_the_leg(self):
        # COMPLETENESS_RULE sets no label; a filled-but-Impact-missing body still passes,
        # proving the leg is strictly gated (this is what keeps contract-threshold safe)
        body = "\n".join(f"## {s}\n**Real summary**\n- a real bullet" for s in SECTIONS)
        passed, found = validate.kind_presence(COMPLETENESS_RULE, {"pr_body": body})
        self.assertTrue(passed)
        self.assertEqual(found, [])

    def test_shipped_rule_flags_unfilled_impact(self):
        # exercise the SHIPPED rule, not just the in-file fixture
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                            ".engine", "check", "pr-body-completeness.json")
        with open(path, encoding="utf-8") as fh:
            shipped = json.load(fh)
        passed, found = validate.kind_presence(shipped, {"pr_body": self._body("*Impact: <slot>*")})
        self.assertFalse(passed)
        # this _body carries no preamble, so the shipped rule also fires its required_phrases leg; assert on
        # the Impact-leg findings specifically (the behaviour this test pins), not the total count.
        impact_findings = [f for f in found if "no filled Impact line" in f["message"]]
        self.assertEqual(len(impact_findings), len(SECTIONS))


class TestRequiredPhrasesLeg(unittest.TestCase):
    """The required-phrases leg, gated behind params.required_phrases. It guards a fixed
    anchor a heading scan cannot see — the consent preamble that drops when a body is
    reconstructed instead of filled verbatim. Absent param => the leg is skipped."""

    def _sections(self):  # all filled sections + Impact lines, no preamble
        return "\n".join(f"## {s}\n**Real summary**\n- a real bullet\n*Impact: real consequence*"
                         for s in SECTIONS)

    def _body(self, anchors):  # the anchors (a preamble stand-in) above the filled sections
        return ("\n".join(anchors) + "\n" + self._sections()) if anchors else self._sections()

    def test_all_missing_anchors_flag_in_one_finding(self):
        # a whole-preamble drop is ONE finding that lists every missing anchor, not one per anchor
        passed, found = validate.kind_presence(PHRASES_RULE, {"pr_body": self._body([])})
        self.assertFalse(passed)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["severity"], "hard")
        self.assertIn("consent preamble", found[0]["message"])
        self.assertIn("binding gate anchor", found[0]["message"])
        self.assertIn("unverified anchor", found[0]["message"])

    def test_all_anchors_present_passes(self):
        passed, found = validate.kind_presence(
            PHRASES_RULE, {"pr_body": self._body(["binding gate anchor", "unverified anchor"])})
        self.assertTrue(passed)
        self.assertEqual(found, [])

    def test_partial_lists_only_the_missing_anchor(self):
        passed, found = validate.kind_presence(
            PHRASES_RULE, {"pr_body": self._body(["binding gate anchor"])})  # second anchor absent
        self.assertFalse(passed)
        self.assertEqual(len(found), 1)
        self.assertIn("unverified anchor", found[0]["message"])
        self.assertNotIn('"binding gate anchor"', found[0]["message"])  # the present anchor is not listed

    def test_hard_wrapped_anchor_is_flagged_with_a_wrap_hint(self):
        # a present-but-wrapped anchor reads as absent (substring match is one physical line); the finding
        # points the author at the wrap, not only "restore the preamble" (usability recovery path).
        wrapped = self._body(["binding gate\nanchor", "unverified anchor"])  # first anchor split by a wrap
        passed, found = validate.kind_presence(PHRASES_RULE, {"pr_body": wrapped})
        self.assertFalse(passed)
        self.assertEqual(len(found), 1)
        self.assertIn("line wrap", found[0]["message"])
        self.assertIn("binding gate anchor", found[0]["message"])  # the split anchor is the one listed

    def test_param_absent_skips_the_leg(self):
        # COMPLETENESS_RULE declares no required_phrases; a preamble-less body still passes,
        # proving the leg is strictly gated (every other presence check is unaffected).
        passed, found = validate.kind_presence(COMPLETENESS_RULE, {"pr_body": self._body([])})
        self.assertTrue(passed)
        self.assertEqual(found, [])

    def test_file_target_branch_flags_missing_anchor(self):
        # parity with the file-target path: the leg also runs on a prose-file target
        with tempfile.TemporaryDirectory() as d:
            p = _write(d, "doc.md", "## Alpha\nreal content\n")
            rule = _rule(kind="presence", target={"path": "x"},
                         params={"sections": ["Alpha"], "required_phrases": ["must appear here"]})
            passed, found = _run_kind(validate.kind_presence, rule, [p])
        self.assertFalse(passed)
        self.assertTrue(any("must appear here" in f["message"] for f in found))

    def test_shipped_rule_flags_a_preamble_less_body_and_passes_with_it(self):
        # exercise the SHIPPED rule against the REAL template preamble — the falsification
        # that a body dropping the preamble is caught, and one carrying it is cleared.
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        with open(os.path.join(root, ".engine", "check", "pr-body-completeness.json"),
                  encoding="utf-8") as fh:
            shipped = json.load(fh)
        passed, found = validate.kind_presence(shipped, {"pr_body": self._body([])})
        self.assertFalse(passed)
        self.assertTrue(any("consent preamble" in f["message"] for f in found))
        # now with every declared anchor present, the preamble leg is satisfied
        anchors = shipped["params"]["required_phrases"]
        passed, found = validate.kind_presence(shipped, {"pr_body": self._body(anchors)})
        self.assertTrue(passed)
        self.assertEqual(found, [])


# ---- the generic closed kinds + suite-context gating --------------

META = validate.META_SCHEMA_URI


def _rule(**kw):
    base = {"id": "engine/check/x", "target": {}, "kind": "schema", "tier": "hard",
            "suites": ["CI"], "params": {}, "message": "Fix it."}
    base.update(kw)
    return base


def _run_kind(kind_fn, rule, files):
    """Run a kind callable with validate.target_files stubbed to `files`, so a
    fixture under a temp dir (or a real repo file) can be targeted directly."""
    orig = validate.target_files
    validate.target_files = lambda r: list(files)
    try:
        return kind_fn(rule, {})
    finally:
        validate.target_files = orig


def _write(d, name, obj):
    p = os.path.join(d, name)
    with open(p, "w", encoding="utf-8") as fh:
        json.dump(obj, fh) if isinstance(obj, (dict, list)) else fh.write(obj)
    return p


class TestPresenceFileTarget(unittest.TestCase):
    """presence works on a prose file target too, reusing the placeholder teeth."""
    def test_missing_and_placeholder_sections_on_a_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = _write(d, "doc.md", "## Alpha\nreal content\n## Beta\n<placeholder>\n")
            rule = _rule(kind="presence", target={"path": "x"},
                         params={"sections": ["Alpha", "Beta", "Gamma"]})
            passed, found = _run_kind(validate.kind_presence, rule, [p])
        self.assertFalse(passed)
        msgs = " ".join(f["message"] for f in found)
        self.assertIn("Beta", msgs)      # present but placeholder-only -> empty
        self.assertIn("Gamma", msgs)     # missing
        self.assertNotIn("Alpha", msgs)  # filled -> fine
        self.assertTrue(all(f["severity"] == "hard" for f in found))


class TestSchemaKind(unittest.TestCase):
    SCHEMA = {"type": "object", "required": ["a"], "properties": {"a": {"type": "string"}}}

    def test_valid_instance_passes(self):
        with tempfile.TemporaryDirectory() as d:
            sp = _write(d, "s.json", self.SCHEMA)
            ip = _write(d, "i.json", {"a": "hi"})
            passed, found = _run_kind(validate.kind_schema, _rule(params={"schema": sp}), [ip])
        self.assertTrue(passed)
        self.assertEqual(found, [])

    def test_invalid_instance_flags_at_tier(self):
        with tempfile.TemporaryDirectory() as d:
            sp = _write(d, "s.json", self.SCHEMA)
            ip = _write(d, "i.json", {"a": 1})  # wrong type
            passed, found = _run_kind(validate.kind_schema, _rule(params={"schema": sp}), [ip])
        self.assertFalse(passed)
        self.assertTrue(any(f["severity"] == "hard" for f in found))

    def test_malformed_governing_schema_is_loud(self):
        with tempfile.TemporaryDirectory() as d:
            sp = _write(d, "s.json", {"type": 123})  # not a well-formed schema
            ip = _write(d, "i.json", {"a": "hi"})
            passed, found = _run_kind(validate.kind_schema, _rule(params={"schema": sp}), [ip])
        self.assertFalse(passed)
        self.assertTrue(any(f["severity"] == "hard" for f in found))

    def test_offline_external_ref_is_caught_never_fetched(self):
        with tempfile.TemporaryDirectory() as d:
            sp = _write(d, "s.json", {"$ref": "https://example.com/nope.json"})
            ip = _write(d, "i.json", {"a": "hi"})
            passed, found = _run_kind(validate.kind_schema, _rule(params={"schema": sp}), [ip])
        self.assertFalse(passed)
        self.assertTrue(any("unresolvable" in f["message"] for f in found))

    def test_malformed_json_target_is_loud(self):
        with tempfile.TemporaryDirectory() as d:
            sp = _write(d, "s.json", self.SCHEMA)
            ip = _write(d, "i.json", "{ not json")
            passed, found = _run_kind(validate.kind_schema, _rule(params={"schema": sp}), [ip])
        self.assertFalse(passed)
        self.assertTrue(any("not valid JSON" in f["message"] for f in found))

    def test_wellformedness_failure_via_meta_schema(self):
        # params.schema == the dialect URI => validate the target file AS a schema.
        with tempfile.TemporaryDirectory() as d:
            bad = _write(d, "bad.schema.json", {"type": 123})
            passed, found = _run_kind(validate.kind_schema, _rule(params={"schema": META}), [bad])
        self.assertFalse(passed)
        self.assertTrue(any(f["severity"] == "hard" for f in found))

    def test_catalog_routing_wellformedness_on_a_real_schema_passes(self):
        # No params: routing resolves the schema surface -> the 2020-12 meta-schema ->
        # finding.v1.json is checked for well-formedness and passes. Proves catalog-first
        # resolution against a real merged file.
        real = os.path.join(validate.SCHEMAS_DIR, "finding.v1.json")
        rule = _rule(target={"path": ".engine/schemas/finding.v1.json"})
        passed, found = _run_kind(validate.kind_schema, rule, [real])
        self.assertTrue(passed)
        self.assertEqual(found, [])

    def test_catalog_routing_validates_a_real_check_rule_instance(self):
        # The central "routing reuses the catalog" path for an ordinary INSTANCE (not a
        # schema): a real check file routes via the catalog (.engine/check/ -> the check
        # surface -> governing_schema check.v1.json) and is validated against it. Proves
        # catalog-first resolution loads the sibling schema and checks the instance.
        real = os.path.join(validate.ENGINE_DIR, "check", "link-integrity.json")
        rule = _rule(target={"path": ".engine/check/link-integrity.json"})
        passed, found = _run_kind(validate.kind_schema, rule, [real])
        self.assertTrue(passed)
        self.assertEqual(found, [])

    def test_params_override_validates_catalog_against_its_meta_contract(self):
        # The catalog is governed by its own meta-contract, not the meta-schema URL its
        # schema-surface record carries; a params override names the right schema. (This
        # proves the override path validates the catalog against surface-catalog.schema.json;
        # the override exists for this self-governance edge that catalog routing can't express.)
        rule = _rule(params={"schema": ".engine/schemas/surface-catalog.schema.json"})
        passed, found = _run_kind(validate.kind_schema, rule, [validate.CATALOG_PATH])
        self.assertTrue(passed)
        self.assertEqual(found, [])


class TestShapeKind(unittest.TestCase):
    PARAMS = {"required_sections": ["Decision", "Rationale", "Status"],
              "allowed_sections": ["Supersedes"], "length_budget": 6}

    def _shape(self, body, tier="hard"):
        with tempfile.TemporaryDirectory() as d:
            p = _write(d, "x.md", body)
            rule = _rule(kind="shape", tier=tier, target={"path": "x"}, params=self.PARAMS)
            return _run_kind(validate.kind_shape, rule, [p])

    def test_well_formed_instance_passes(self):
        passed, found = self._shape("## Decision\nx\n## Rationale\ny\n## Status\nz\n")
        self.assertTrue(passed)
        self.assertEqual([f for f in found if f["severity"] == "hard"], [])

    def test_missing_required_is_rule_tier(self):
        passed, found = self._shape("## Decision\nx\n## Status\nz\n")  # Rationale missing
        self.assertFalse(passed)
        self.assertTrue(any("Rationale" in f["message"] and f["severity"] == "hard" for f in found))

    def test_required_out_of_order_is_flagged(self):
        passed, found = self._shape("## Rationale\ny\n## Decision\nx\n## Status\nz\n")
        self.assertTrue(any("out of order" in f["message"] for f in found))

    def test_section_outside_allowed_is_flagged(self):
        passed, found = self._shape("## Decision\nx\n## Rationale\ny\n## Status\nz\n## Bogus\nq\n")
        self.assertTrue(any("Bogus" in f["message"] for f in found))

    def test_over_budget_is_soft_only_never_hard(self):
        body = "## Decision\n" + "\n".join(["line"] * 12) + "\n## Rationale\ny\n## Status\nz\n"
        passed, found = self._shape(body)
        over = [f for f in found if "budget" in f["message"]]
        self.assertTrue(over)
        self.assertTrue(all(f["severity"] == "soft" for f in over))
        self.assertFalse(any(f["severity"] == "hard" for f in found))  # length never the hard tier

    # A long body (over the default budget) plus a recorded per-file override.
    _OVER = "## Decision\n" + "\n".join(["line"] * 12) + "\n## Rationale\ny\n## Status\nz\n"

    _OK_OVERRIDE = {"budget": 99, "why": "a recorded reason"}

    def test_per_file_override_raises_budget(self):
        # The overridden file is over the default 6 but under its own 99 ceiling -> no finding at all.
        with tempfile.TemporaryDirectory() as d:
            p = _write(d, "x.md", self._OVER)
            key = os.path.relpath(p, validate.ROOT)
            params = dict(self.PARAMS, length_budget_overrides={key: self._OK_OVERRIDE})
            rule = _rule(kind="shape", tier="hard", target={"path": "x"}, params=params)
            passed, found = _run_kind(validate.kind_shape, rule, [p])
            self.assertTrue(passed)
            self.assertEqual(found, [])  # well-formed body, under its 99 override -> nothing

    def test_override_applies_only_to_its_file(self):
        # An override for a.md must not lift b.md, which still nudges at the default 6.
        with tempfile.TemporaryDirectory() as d:
            a, b = _write(d, "a.md", self._OVER), _write(d, "b.md", self._OVER)
            params = dict(self.PARAMS,
                          length_budget_overrides={os.path.relpath(a, validate.ROOT): self._OK_OVERRIDE})
            rule = _rule(kind="shape", tier="hard", target={"path": "x"}, params=params)
            passed, found = _run_kind(validate.kind_shape, rule, [a, b])
            over = [f for f in found if f["severity"] == "soft" and "over its" in f["message"]]
            self.assertTrue(any("b.md" in f["message"] for f in over))      # default still bites b
            self.assertFalse(any("a.md" in f["message"] for f in over))     # override lifts a

    def test_stale_override_key_is_hard(self):
        # A key naming no targeted operation is the rule's hard tier — a dead grant can't accumulate.
        with tempfile.TemporaryDirectory() as d:
            p = _write(d, "x.md", "## Decision\nx\n## Rationale\ny\n## Status\nz\n")  # well-formed, in-budget
            params = dict(self.PARAMS, length_budget_overrides={"no/such/operation.md": self._OK_OVERRIDE})
            rule = _rule(kind="shape", tier="hard", target={"path": "x"}, params=params)
            passed, found = _run_kind(validate.kind_shape, rule, [p])
            self.assertFalse(passed)
            self.assertTrue(any("stale or mistyped key" in f["message"]
                                and f["severity"] == "hard" for f in found))

    def test_override_without_recorded_reason_is_hard(self):
        # An override must carry both an integer budget AND a recorded why (#273, made mechanical).
        # A budget-only entry, a non-int budget, and a bare value each fail at the hard tier.
        for bad in ({"budget": 99}, {"budget": "lots", "why": "r"}, 99):
            with self.subTest(bad=bad), tempfile.TemporaryDirectory() as d:
                p = _write(d, "x.md", "## Decision\nx\n## Rationale\ny\n## Status\nz\n")
                key = os.path.relpath(p, validate.ROOT)
                params = dict(self.PARAMS, length_budget_overrides={key: bad})
                rule = _rule(kind="shape", tier="hard", target={"path": "x"}, params=params)
                passed, found = _run_kind(validate.kind_shape, rule, [p])
                self.assertFalse(passed)
                self.assertTrue(any("is incomplete" in f["message"]
                                    and f["severity"] == "hard" for f in found))


class TestSuiteContextGating(unittest.TestCase):
    """The locked tier-vs-context law: a hard finding fails the run ONLY in a
    blocking-gate context (CI). A suites.json that mislabeled CI's context would
    silently un-gate the merge — this is the test that would catch that."""
    def setUp(self):
        self._rules, self._reg, self._suites = (
            validate.load_rules, dict(validate.REGISTRY), validate.SUITES_PATH)

    def tearDown(self):
        validate.load_rules = self._rules
        validate.SUITES_PATH = self._suites
        validate.REGISTRY.clear()
        validate.REGISTRY.update(self._reg)

    def _install_hard(self):
        validate.load_rules = lambda: [{"id": "synthetic", "kind": "synthetic", "tier": "hard",
                                        "suites": ["CI", "pre-close"], "params": {}}]
        validate.REGISTRY["synthetic"] = lambda rule, ctx: (False, [validate.finding("hard", "boom")])

    def test_ci_blocking_gate_fails_on_hard(self):
        self._install_hard()
        self.assertEqual(_run_quiet("CI", {}), 1)

    def test_local_nudge_suite_does_not_gate_the_same_hard_finding(self):
        self._install_hard()
        self.assertEqual(_run_quiet("pre-close", {}), 0)

    def test_undeclared_suite_is_a_config_error(self):
        self.assertEqual(_run_quiet("nope", {}), 2)

    def test_malformed_suites_file_fails_closed(self):
        with tempfile.TemporaryDirectory() as d:
            validate.SUITES_PATH = _write(d, "suites.json", "{ not json")
            self.assertEqual(_run_quiet("CI", {}), 2)

    def test_malformed_rule_file_fails_closed_in_plain_language(self):
        # A broken check rule file is a CONFIG ERROR (exit 2), not an uncaught traceback,
        # so the non-engineer operator sees plain language, never engineer shorthand.
        def boom():
            raise ValueError("check rule is not valid JSON")
        validate.load_rules = boom
        self.assertEqual(_run_quiet("CI", {}), 2)


# ---- coverage / coherence / custom-script + protection re-home ----

class TestCoverageKind(unittest.TestCase):
    def test_unrecognized_mode_fails_closed(self):
        passed, found = validate.kind_coverage(_rule(kind="coverage", params={"mode": "bogus"}), {})
        self.assertFalse(passed)
        self.assertTrue(any(f["severity"] == "hard" for f in found))

    def test_links_hard_in_repo_soft_outside(self):
        # mode=links keeps the link-integrity teeth: an in-repo missing target is hard,
        # an outside-repo target is a soft note. ROOT + markdown_files are stubbed so the
        # in/out-of-repo split is exercised against a controlled tree.
        with tempfile.TemporaryDirectory() as d:
            md = os.path.join(d, "doc.md")
            with open(md, "w", encoding="utf-8") as fh:
                fh.write("[broken](./missing.md)\n[outside](../nope.md)\n[ok](https://x)\n")
            orig_root, orig_mf = validate.ROOT, validate.markdown_files
            validate.ROOT = d
            validate.markdown_files = lambda exclude: [md]
            try:
                passed, found = validate.kind_coverage(
                    _rule(kind="coverage", params={"mode": "links"}), {})
            finally:
                validate.ROOT, validate.markdown_files = orig_root, orig_mf
        sev = {f["severity"] for f in found}
        self.assertIn("hard", sev)   # the in-repo missing target
        self.assertIn("soft", sev)   # the outside-repo target
        self.assertFalse(passed)

    def test_catalog_coverage_pure(self):
        surfaces = {"alpha": {"location": ".engine/alpha/"},
                    "beta": {"location": ".engine/beta/"}}
        present = {".engine/alpha/", ".engine/orphan/"}
        msgs = " ".join(f["message"] for f in
                        validate.catalog_coverage_findings(surfaces, present, "hard", "msg"))
        self.assertIn("beta", msgs)      # catalogued but absent
        self.assertIn("orphan", msgs)    # present but unclaimed
        self.assertNotIn("alpha", msgs)  # present + catalogued -> fine
        self.assertEqual(validate.catalog_coverage_findings(
            {"alpha": {"location": ".engine/alpha/"}}, {".engine/alpha/"}, "hard", "m"), [])
        self.assertEqual(validate.catalog_coverage_findings(  # infra allowlist suppresses an orphan
            {}, {".engine/boot/"}, "hard", "m", infra=[".engine/boot/"]), [])
        # #410: the exemption is load-bearing — WITHOUT .engine/boot/ in infra it IS flagged. Boot is boot's
        # topology-sanctioned artifact home (its ledger writes .cache/ there), so once boot has run the dir
        # materializes and, absent the carve-out, fires a false HARD orphan in the operator's tree (green in CI's
        # fresh checkout, red locally). This asserts the mechanism; the next test asserts the shipped rule uses it.
        self.assertTrue(validate.catalog_coverage_findings({}, {".engine/boot/"}, "hard", "m"),
                        "boot/ must orphan when NOT exempted — proving the allowlist entry is what suppresses it")

    def test_boot_is_exempted_in_the_real_catalog_coverage_rule(self):
        # #410, the data side: the shipped rule must actually list .engine/boot/, or a working tree where boot
        # has run reds on the false orphan even though CI's fresh checkout (no .engine/boot/ yet) stays green.
        rule = validate.load_json(os.path.join(validate.ROOT, ".engine", "check", "catalog-coverage.json"))
        self.assertIn(".engine/boot/", rule["params"]["infra_dirs"],
                      "the shipped catalog-coverage rule must exempt boot's topology-sanctioned artifact home")

    def test_infra_exemption_may_not_shadow_a_surface_home(self):
        # #402 U06b: the disjointness invariant — an infra exemption that names a catalogued surface's own home
        # is a HARD finding (the allowlist may not silently suppress a surface's own coverage). This is an
        # allowlist-integrity guard that PROTECTS the two coverage legs, not the third leg itself: the spec's
        # full "no uncatalogued surface-shaped instance in use" leg is a logged build-spec leaf (authoring
        # judgment at the cataloguing PR), because a general rule cannot tell an uncatalogued surface apart from
        # a legitimate non-surface bucket (provides groups non-surface files).
        surfaces = {"check": {"location": ".engine/check/"}}
        shadow = " ".join(f["message"] for f in validate.catalog_coverage_findings(
            surfaces, {".engine/check/"}, "hard", "m", infra=[".engine/check/"]))
        self.assertIn("shadow", shadow, "an infra exemption naming a catalogued surface's home must fire HARD")
        # A genuine non-surface infra dir does NOT trip the invariant (only shadowing a surface home does).
        clean = " ".join(f["message"] for f in validate.catalog_coverage_findings(
            surfaces, {".engine/check/", ".engine/boot/"}, "hard", "m", infra=[".engine/boot/"]))
        self.assertNotIn("shadow", clean)
        # The live shipped rule + catalog satisfy the invariant (no exemption names a surface home).
        rule = validate.load_json(os.path.join(validate.ROOT, ".engine", "check", "catalog-coverage.json"))
        catalog = validate.load_json(validate.CATALOG_PATH)
        surface_locs = {r.get("location") for r in catalog.get("surfaces", {}).values()}
        self.assertEqual(set(rule["params"]["infra_dirs"]) & surface_locs, set(),
                         "no infra exemption may name a catalogued surface's home directory")


class TestCoherenceKind(unittest.TestCase):
    def test_missing_dependency(self):
        m = [{"id": "a", "version": "1.0.0", "depends": {"b": ""}}]
        self.assertTrue(any("not installed" in x["message"]
                            for x in validate.coherence_findings(m, "hard", "msg")))

    def test_version_out_of_range(self):
        m = [{"id": "a", "version": "1.0.0", "depends": {"b": ">=2.0.0"}},
             {"id": "b", "version": "1.5.0", "depends": {}}]
        self.assertTrue(any("needs 'b'" in x["message"]
                            for x in validate.coherence_findings(m, "hard", "msg")))

    def test_in_range_is_clean(self):
        m = [{"id": "a", "version": "1.0.0", "depends": {"b": ">=1.0.0"}},
             {"id": "b", "version": "1.5.0", "depends": {}}]
        self.assertEqual(validate.coherence_findings(m, "hard", "msg"), [])

    def test_dependency_cycle(self):
        m = [{"id": "a", "version": "1.0.0", "depends": {"b": ""}},
             {"id": "b", "version": "1.0.0", "depends": {"a": ""}}]
        self.assertTrue(any("cycle" in x["message"]
                            for x in validate.coherence_findings(m, "hard", "msg")))

    def test_kind_with_no_manifests_is_clean(self):
        passed, found = validate.kind_coherence(_rule(kind="coherence"), {})
        self.assertTrue(passed)
        self.assertEqual(found, [])


class TestCustomScriptKind(unittest.TestCase):
    def _run(self, body, tier="hard", params_extra=None):
        # ROOT is repointed at the temp dir so the in-repo containment check accepts the
        # fixture script (a custom/script must be a committed, in-repo file).
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "s.py"), "w", encoding="utf-8") as fh:
                fh.write(body)
            params = {"script": "s.py"}
            if params_extra:
                params.update(params_extra)
            rule = _rule(kind="custom/script", tier=tier, params=params)
            orig = validate.ROOT
            validate.ROOT = d
            try:
                return validate.kind_custom_script(rule, {})
            finally:
                validate.ROOT = orig

    def test_findings_pass_through(self):
        passed, found = self._run(
            "import json; print(json.dumps([{'severity':'hard','message':'boom','location':None}]))")
        self.assertFalse(passed)
        self.assertTrue(any(f["severity"] == "hard" and "boom" in f["message"] for f in found))

    def test_empty_array_is_pass(self):
        passed, found = self._run("print('[]')")
        self.assertTrue(passed)
        self.assertEqual(found, [])

    def test_nonzero_exit_is_hard_regardless_of_tier(self):
        # A soft-tier rule whose script crashes still fails CLOSED (hard) — a broken guard
        # can never silently pass.
        passed, found = self._run("import sys; print('[]'); sys.exit(3)", tier="soft")
        self.assertFalse(passed)
        self.assertTrue(any(f["severity"] == "hard" for f in found))

    def test_unparseable_output_is_hard_fail_closed(self):
        passed, found = self._run("print('not json at all')")
        self.assertFalse(passed)
        self.assertTrue(any(f["severity"] == "hard" for f in found))

    def test_non_dict_finding_is_hard(self):
        passed, found = self._run("import json; print(json.dumps(['a', 'b']))")
        self.assertFalse(passed)
        self.assertTrue(any(f["severity"] == "hard" for f in found))

    def test_missing_script_param_is_hard(self):
        passed, found = validate.kind_custom_script(_rule(kind="custom/script", params={}), {})
        self.assertFalse(passed)
        self.assertTrue(any(f["severity"] == "hard" for f in found))

    def test_out_of_repo_script_is_refused(self):
        rule = _rule(kind="custom/script", params={"script": "../../../../etc/passwd"})
        passed, found = validate.kind_custom_script(rule, {})
        self.assertFalse(passed)
        self.assertTrue(any("outside the repository" in f["message"] for f in found))

    def test_nonexistent_in_repo_script_is_hard(self):
        rule = _rule(kind="custom/script",
                     params={"script": ".engine/tools/_nope_does_not_exist.py"})
        passed, found = validate.kind_custom_script(rule, {})
        self.assertFalse(passed)
        self.assertTrue(any("does not exist" in f["message"] for f in found))

    def test_token_reaches_only_opted_in_scripts(self):
        body = ("import os, json; print(json.dumps([{'severity': 'soft', 'location': None, "
                "'message': 'TOKEN_SEEN' if os.environ.get('GITHUB_TOKEN') else 'no-token'}]))")
        saved = dict(os.environ)
        os.environ["GITHUB_TOKEN"] = "secret"
        try:
            _, without = self._run(body)
            _, withtok = self._run(body, params_extra={"pass_token": True})
        finally:
            os.environ.clear()
            os.environ.update(saved)
        self.assertEqual(without[0]["message"], "no-token")    # not forwarded by default
        self.assertEqual(withtok[0]["message"], "TOKEN_SEEN")  # forwarded on opt-in


class TestRunUnitSeam(unittest.TestCase):
    """run_unit (#286): drive ONE real check-logic unit against a
    caller-substituted target so the negative-fixture meta-check can witness that each
    hard check actually bites. Assertions are by SET-MEMBERSHIP (a finding with the
    expected severity/text is present) — never order or count. The production
    run()/run_check()/--check paths never call run_unit and are covered unchanged elsewhere."""

    def test_drives_a_kind_callable_against_a_substituted_path(self):
        # schema kind: a transient rule + target.path point the REAL kind_schema at a
        # fixture file under (a substituted) ROOT. A bad file bites; a good one passes —
        # so the seam runs the real callable, not a reimplementation.
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "schema.json"), "w", encoding="utf-8") as fh:
                json.dump({"type": "object", "required": ["x"],
                           "additionalProperties": False, "properties": {"x": {"type": "integer"}}}, fh)
            with open(os.path.join(d, "bad.json"), "w", encoding="utf-8") as fh:
                json.dump({"y": 1}, fh)          # missing required "x"
            with open(os.path.join(d, "good.json"), "w", encoding="utf-8") as fh:
                json.dump({"x": 1}, fh)
            unit = _rule(kind="schema", params={"schema": "schema.json"})
            orig = validate.ROOT
            validate.ROOT = d
            try:
                bad_pass, bad_found = validate.run_unit(unit, {"path": "bad.json"}, {})
                ok_pass, ok_found = validate.run_unit(unit, {"path": "good.json"}, {})
            finally:
                validate.ROOT = orig
        self.assertFalse(bad_pass)
        self.assertTrue(any(f["severity"] == "hard" for f in bad_found))
        self.assertTrue(ok_pass)
        self.assertFalse(any(f["severity"] == "hard" for f in ok_found))

    def test_coverage_override_aims_the_real_callable_at_a_mini_tree(self):
        # The named coverage override: run_unit substitutes BOTH the catalog source and the
        # walk root via ctx, so the REAL kind_coverage (catalog mode) bites a seeded mini-tree
        # — the same entry point CI runs, not the pure helper.
        with tempfile.TemporaryDirectory() as tree, tempfile.TemporaryDirectory() as cd:
            os.makedirs(os.path.join(tree, ".engine", "orphan"))   # present, unclaimed -> orphan
            catalog = os.path.join(cd, "catalog.json")
            with open(catalog, "w", encoding="utf-8") as fh:
                json.dump({"surfaces": {"alpha": {"location": ".engine/alpha/"}}}, fh)  # claimed, absent
            unit = _rule(kind="coverage", params={"mode": "catalog"}, message="map drift")
            passed, found = validate.run_unit(
                unit, {"coverage_catalog": catalog, "coverage_root": tree}, {})
        msgs = " ".join(f["message"] for f in found)
        self.assertFalse(passed)
        self.assertIn("orphan", msgs)   # the present-but-unclaimed directory
        self.assertIn("alpha", msgs)    # the catalogued-but-absent surface

    def test_drives_the_coherence_callable_against_substituted_manifests(self):
        # The manifests class: a substituted manifest set drives the REAL kind_coherence —
        # a manifest depending on an uninstalled module bites; a satisfiable set passes.
        unit = _rule(kind="coherence", message="modules inconsistent")
        bad = [{"id": "a", "version": "1.0.0", "depends": {"b": ""}}]   # b not installed
        good = [{"id": "a", "version": "1.0.0", "depends": {"b": ">=1.0.0"}},
                {"id": "b", "version": "1.5.0", "depends": {}}]
        bad_pass, bad_found = validate.run_unit(unit, {"manifests": bad}, {})
        ok_pass, ok_found = validate.run_unit(unit, {"manifests": good}, {})
        self.assertFalse(bad_pass)
        self.assertTrue(any(f["severity"] == "hard" for f in bad_found))
        self.assertTrue(ok_pass)
        self.assertEqual(ok_found, [])

    def test_custom_script_substitutes_through_env_and_restores(self):
        # A custom/script reads its target from the environment; run_unit sets the env var
        # around the child and ALWAYS restores os.environ afterward (whether the key was
        # absent before, or held a prior value).
        body = ("import os, json; print(json.dumps([{'severity': 'hard', 'location': None, "
                "'message': 'SEEDED:' + os.environ.get('ENGINE_PR_BODY_FILE', '<unset>')}]))")
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "s.py"), "w", encoding="utf-8") as fh:
                fh.write(body)
            unit = _rule(kind="custom/script", params={"script": "s.py"})
            orig_root = validate.ROOT
            validate.ROOT = d
            saved = os.environ.get("ENGINE_PR_BODY_FILE")
            os.environ.pop("ENGINE_PR_BODY_FILE", None)   # absent before
            try:
                _, found = validate.run_unit(unit, {"env": {"ENGINE_PR_BODY_FILE": "/seeded/path"}}, {})
                seen_when_absent = "ENGINE_PR_BODY_FILE" in os.environ  # must be restored to absent
                os.environ["ENGINE_PR_BODY_FILE"] = "/prior"           # prior value before a second run
                validate.run_unit(unit, {"env": {"ENGINE_PR_BODY_FILE": "/seeded/path"}}, {})
                restored_prior = os.environ.get("ENGINE_PR_BODY_FILE")
            finally:
                validate.ROOT = orig_root
                os.environ.pop("ENGINE_PR_BODY_FILE", None) if saved is None \
                    else os.environ.__setitem__("ENGINE_PR_BODY_FILE", saved)
        self.assertTrue(any(f["message"] == "SEEDED:/seeded/path" for f in found))  # the child saw it
        self.assertFalse(seen_when_absent)        # restored to absent
        self.assertEqual(restored_prior, "/prior")  # restored to the prior value

    def test_unregistered_kind_fails_closed(self):
        passed, found = validate.run_unit(_rule(kind="no-such-kind"), {}, {})
        self.assertFalse(passed)
        self.assertTrue(any(f["severity"] == "hard" and "unregistered" in f["message"]
                            for f in found))

    def test_does_not_mutate_the_caller_rule_or_ctx(self):
        # run_unit overlays a substitution onto private copies; the caller's unit and ctx
        # are left exactly as passed (so a roster loop can reuse them).
        unit = _rule(kind="coherence", target={"path": "orig"})
        ctx = {"manifests": []}
        validate.run_unit(unit, {"path": "swapped", "manifests": [{"id": "x"}]}, ctx)
        self.assertEqual(unit["target"], {"path": "orig"})
        self.assertEqual(ctx, {"manifests": []})


class TestProtectionReHome(unittest.TestCase):
    """The re-homed protection guard emits finding.v1 JSON: a soft fail-open note with no
    token (local), and a hard finding when the floor is missing (token present, CI)."""
    def _main_json(self):
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = protection_guard.main()
        return rc, json.loads(buf.getvalue())

    def test_no_token_is_soft_and_exit_zero(self):
        saved = dict(os.environ)
        os.environ.pop("GITHUB_TOKEN", None)
        os.environ.pop("GITHUB_REPOSITORY", None)
        try:
            rc, out = self._main_json()
        finally:
            os.environ.clear()
            os.environ.update(saved)
        self.assertEqual(rc, 0)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["severity"], "soft")

    def test_missing_floor_emits_hard(self):
        saved, orig_api = dict(os.environ), protection_guard.get_json
        os.environ.update({"GITHUB_TOKEN": "x", "GITHUB_REPOSITORY": "o/r",
                           "ENGINE_RULE_TIER": "hard"})
        protection_guard.get_json = lambda path, token, **kw: []  # no rules in force -> floor missing
        try:
            rc, out = self._main_json()
        finally:
            os.environ.clear()
            os.environ.update(saved)
            protection_guard.get_json = orig_api
        self.assertEqual(rc, 0)
        self.assertEqual(out[0]["severity"], "hard")
        self.assertIn("not fully in force", out[0]["message"])


# ---- re-home the weakening guard as a custom/script rule ----

class TestWeakeningReHome(unittest.TestCase):
    """The re-homed weakening guard emits finding.v1 JSON via the custom/script contract
    (two tiers, eADR-0040): [] when nothing weakens; one HARD finding (carrying the
    plain-language ack guidance) on an unacknowledged killswitch-tier change, DOWNGRADED
    to a soft ACKNOWLEDGED record when the ack label is present (never erased); one SOFT
    disclosure finding whenever disclosure-tier enforcement files are modified (the ack
    does not touch it); and a hard fail-closed finding when the pull-request context
    cannot be read OR the guard could not read every changed file (a partial view — a
    too-large PR past GitHub's file-listing cap). The latter is the non-falsifiability
    property: a weakening edit must not hide past file 100 of a big PR, so the guard
    paginates the diff to completion and cross-checks what it read against the pull
    request's authoritative changed_files."""

    _AUTO = object()  # sentinel: derive expected from len(files) unless overridden

    def _main_json(self, event, files, expected=_AUTO, base_home=None, base_tier=None,
                   base_product_build_target=None):
        """Drive main() with the network seams stubbed: the complete changed-file list, the authoritative
        changed_files count, the BASE manifest's recorded home (`base_home`, default None = no home
        recorded, so a home in the diff reads as a first recording), the BASE manifest's recorded identity
        tier (`base_tier`, default None = no tier recorded, so a team->solo detector has nothing to downgrade
        FROM), and the BASE manifest's recorded executable build target (`base_product_build_target`, default
        None = no target recorded — so a target APPEARING in the diff reads as a first-set ARMING, which the
        product_build_target detector flags, unlike a first-home recording). `expected` defaults to len(files)
        (a fully-seen PR); pass a larger int to simulate a truncated / over-cap view, or None for the count
        being unavailable."""
        import contextlib
        import io
        if expected is self._AUTO:
            expected = len(files)
        saved = dict(os.environ)
        orig_fetch = weakening_guard.fetch_all_changed_files
        orig_count = weakening_guard.changed_files_total
        orig_home = weakening_guard._read_base_home
        orig_tier = weakening_guard._read_base_tier
        orig_target = weakening_guard._read_base_product_build_target
        buf = io.StringIO()
        with tempfile.TemporaryDirectory() as d:
            ep = os.path.join(d, "event.json")
            with open(ep, "w", encoding="utf-8") as fh:
                json.dump(event, fh)
            os.environ.update({"GITHUB_REPOSITORY": "o/r", "GITHUB_TOKEN": "x",
                               "ENGINE_RULE_TIER": "hard", "GITHUB_EVENT_PATH": ep})
            weakening_guard.fetch_all_changed_files = lambda repo, number, token: files
            weakening_guard.changed_files_total = lambda repo, number, token: expected
            weakening_guard._read_base_home = lambda: base_home
            weakening_guard._read_base_tier = lambda: base_tier
            weakening_guard._read_base_product_build_target = lambda: base_product_build_target
            try:
                with contextlib.redirect_stdout(buf):
                    rc = weakening_guard.main()
            finally:
                os.environ.clear()
                os.environ.update(saved)
                weakening_guard.fetch_all_changed_files = orig_fetch
                weakening_guard.changed_files_total = orig_count
                weakening_guard._read_base_home = orig_home
                weakening_guard._read_base_tier = orig_tier
                weakening_guard._read_base_product_build_target = orig_target
        return rc, json.loads(buf.getvalue())

    def test_no_weakening_is_empty_and_exit_zero(self):
        rc, out = self._main_json(
            {"pull_request": {"number": 1, "labels": []}},
            [{"filename": "README.md", "status": "modified"}])
        self.assertEqual(rc, 0)
        self.assertEqual(out, [])

    def test_soft_tier_modification_is_one_soft_disclosure(self):
        # validate.py is DISCLOSURE tier (eADR-0040): the check passes, the notice still names the file
        # and says plainly it needs no action.
        rc, out = self._main_json(
            {"pull_request": {"number": 1, "labels": []}},
            [{"filename": ".engine/tools/validate.py", "status": "modified"}])
        self.assertEqual(rc, 0)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["severity"], "soft")
        self.assertIn("GUARDRAIL DISCLOSURE", out[0]["message"])
        self.assertIn("validate.py", out[0]["message"])
        self.assertIn("does not block", out[0]["message"])

    def test_unacked_hard_floor_edit_is_one_hard_with_ack_guidance(self):
        # a hard-floor member (the suite declarations — a global killswitch) still blocks pending the ack.
        rc, out = self._main_json(
            {"pull_request": {"number": 1, "labels": []}},
            [{"filename": ".engine/suites.json", "status": "modified"}])
        self.assertEqual(rc, 0)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["severity"], "hard")
        self.assertIn("guardrail-ack", out[0]["message"])  # the informed-consent surface

    def test_removal_of_any_guarded_file_is_hard(self):
        # removal/rename of ANY guarded file is killswitch tier, even where modification discloses.
        rc, out = self._main_json(
            {"pull_request": {"number": 1, "labels": []}},
            [{"filename": ".engine/tools/validate.py", "status": "removed"}])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["severity"], "hard")
        self.assertIn("guardrail-ack", out[0]["message"])

    def test_ack_label_downgrades_hard_to_disclosure_never_erases(self):
        # eADR-0040: the ack DOWNGRADES the killswitch finding to a soft ACKNOWLEDGED record.
        rc, out = self._main_json(
            {"pull_request": {"number": 1, "labels": [{"name": "guardrail-ack"}]}},
            [{"filename": ".engine/suites.json", "status": "modified"}])
        self.assertEqual(rc, 0)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["severity"], "soft")
        self.assertIn("ACKNOWLEDGED", out[0]["message"])
        self.assertIn("suites.json", out[0]["message"])

    def test_ack_label_leaves_the_disclosure_untouched(self):
        # the ack is about the killswitch tier; a disclosure still emits with the label present.
        rc, out = self._main_json(
            {"pull_request": {"number": 1, "labels": [{"name": "guardrail-ack"}]}},
            [{"filename": ".engine/tools/validate.py", "status": "modified"}])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["severity"], "soft")
        self.assertIn("GUARDRAIL DISCLOSURE", out[0]["message"])

    # ---- the engine's update-home repoint is a guardrail weakening (content-aware; #367) ----
    _REPOINT_PATCH = ('@@ -1,4 +1,4 @@\n'
                      '   "identity": "solo",\n'
                      '-  "home_repository": "acme/engine-home"\n'
                      '+  "home_repository": "evil/look-alike"\n'
                      ' }\n')

    def test_home_repoint_is_flagged_as_weakening(self):
        rc, out = self._main_json(
            {"pull_request": {"number": 1, "labels": []}},
            [{"filename": ".engine/engine.json", "status": "modified", "patch": self._REPOINT_PATCH}],
            base_home="acme/engine-home")
        self.assertEqual(rc, 0)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["severity"], "hard")
        self.assertIn("GUARDRAIL CHANGE DETECTED", out[0]["message"])
        self.assertIn("update home", out[0]["message"])
        self.assertIn("acme/engine-home", out[0]["message"])   # names old -> new so the operator sees the redirect
        self.assertIn("evil/look-alike", out[0]["message"])
        self.assertIn("guardrail-ack", out[0]["message"])

    def test_home_repoint_is_downgraded_by_the_ack_never_erased(self):
        rc, out = self._main_json(
            {"pull_request": {"number": 1, "labels": [{"name": "guardrail-ack"}]}},
            [{"filename": ".engine/engine.json", "status": "modified", "patch": self._REPOINT_PATCH}],
            base_home="acme/engine-home")
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["severity"], "soft")
        self.assertIn("ACKNOWLEDGED", out[0]["message"])
        self.assertIn("update home", out[0]["message"])  # the record survives the operator's act

    def test_home_first_recording_is_not_a_repoint(self):
        # No home in the base (base_home=None) -> the seed/back-fill add is a first recording, not a redirect,
        # and must not demand an ack.
        patch = ('@@ -1,3 +1,4 @@\n'
                 '   "identity": "solo"\n'
                 '+  ,"home_repository": "acme/engine-home"\n'
                 ' }\n')
        rc, out = self._main_json(
            {"pull_request": {"number": 1, "labels": []}},
            [{"filename": ".engine/engine.json", "status": "modified", "patch": patch}])   # base_home None
        self.assertEqual(out, [])

    def test_home_version_bump_is_not_a_repoint(self):
        # .engine/engine.json legitimately churns on every upgrade (version bumps) with NO home line in the
        # patch — so even with a home recorded it must NOT be flagged (why it is not whole-file guarded).
        patch = ('@@ -1,3 +1,3 @@\n'
                 '-  "engine_release": "1.0.0",\n'
                 '+  "engine_release": "1.1.0",\n'
                 '   "identity": "solo"\n')
        rc, out = self._main_json(
            {"pull_request": {"number": 1, "labels": []}},
            [{"filename": ".engine/engine.json", "status": "modified", "patch": patch}],
            base_home="acme/engine-home")
        self.assertEqual(out, [])

    def test_home_duplicate_key_injection_is_flagged(self):
        # #367 security review: a repoint hidden by ADDING a second home_repository key (JSON's last value
        # wins) shows only a '+' line — the old line-pair match missed it. The fail-closed detector flags any
        # touch of the home key when a home is already recorded.
        dup = ('@@ -1,3 +1,4 @@\n'
               '   "home_repository": "acme/engine-home",\n'          # the original stays as context
               '+  "home_repository": "evil/look-alike",\n'          # a second key, last-wins -> effective home
               '   "identity": "solo"\n')
        rc, out = self._main_json(
            {"pull_request": {"number": 1, "labels": []}},
            [{"filename": ".engine/engine.json", "status": "modified", "patch": dup}],
            base_home="acme/engine-home")
        self.assertEqual(len(out), 1)
        self.assertIn("GUARDRAIL CHANGE DETECTED", out[0]["message"])
        self.assertIn("update home", out[0]["message"])

    def test_home_formatting_only_touch_is_not_flagged(self):
        # #515: first-run appends control_plane AFTER home_repository, so the home line gains a trailing
        # comma — same value, pure formatting. Every adopter's first PR false-alarmed on this. The benign
        # carve-out: every touched home line parses cleanly to exactly the base value -> no flag.
        patch = ('@@ -1,3 +1,4 @@\n'
                 '-  "home_repository": "acme/engine-home"\n'
                 '+  "home_repository": "acme/engine-home",\n'
                 '+  "control_plane": {"ruleset_id": 901}\n'
                 ' }\n')
        rc, out = self._main_json(
            {"pull_request": {"number": 1, "labels": []}},
            [{"filename": ".engine/engine.json", "status": "modified", "patch": patch}],
            base_home="acme/engine-home")
        self.assertEqual(out, [], "a same-value formatting touch must not demand an ack")

    def test_home_same_value_duplicate_add_is_not_flagged(self):
        # A second key line carrying the SAME value is harmless whichever line JSON's last-value-wins
        # picks — the effective home is still the base. Documented as the carve-out's boundary: the value
        # must EQUAL the base to clear; any difference flags (the injection test above).
        patch = ('@@ -1,3 +1,4 @@\n'
                 '   "identity": "solo",\n'
                 '+  "home_repository": "acme/engine-home",\n'
                 ' }\n')
        rc, out = self._main_json(
            {"pull_request": {"number": 1, "labels": []}},
            [{"filename": ".engine/engine.json", "status": "modified", "patch": patch}],
            base_home="acme/engine-home")
        self.assertEqual(out, [])

    def test_home_value_split_across_lines_is_flagged(self):
        # #367 evasion: a value split across physical lines defeats a line regex — the carve-out requires
        # a clean one-line parse of EVERY touched home line, so this fails the parse and stays flagged.
        patch = ('@@ -1,3 +1,4 @@\n'
                 '-  "home_repository": "acme/engine-home"\n'
                 '+  "home_repository": "evil/\n'
                 '+look-alike"\n'
                 ' }\n')
        rc, out = self._main_json(
            {"pull_request": {"number": 1, "labels": []}},
            [{"filename": ".engine/engine.json", "status": "modified", "patch": patch}],
            base_home="acme/engine-home")
        self.assertEqual(len(out), 1)
        self.assertIn("GUARDRAIL CHANGE DETECTED", out[0]["message"])

    def test_home_unreadable_new_value_says_so_never_asserts_a_destination(self):
        # #515's message honesty: when the touched home line can't be parsed, the operator is told the
        # value couldn't be read — never that the home is changing "to a different repository".
        patch = ('@@ -1,3 +1,3 @@\n'
                 '-  "home_repository": "acme/engine-home"\n'
                 '+  "home_repository": "evil/\n'
                 ' }\n')
        rc, out = self._main_json(
            {"pull_request": {"number": 1, "labels": []}},
            [{"filename": ".engine/engine.json", "status": "modified", "patch": patch}],
            base_home="acme/engine-home")
        self.assertEqual(len(out), 1)
        self.assertIn("couldn't cleanly read", out[0]["message"])
        self.assertNotIn("a different repository", out[0]["message"])

    def test_home_deletion_is_flagged_and_names_the_removal(self):
        # THE #550 BLOCKING FIX: a pure removal of the home line parses cleanly to the base value, but the
        # home does not SURVIVE — with no home recorded, a later add is a first recording, unflagged, so a
        # deletion + a later add would compose into a silent two-PR repoint. The carve-out now requires an
        # ADDED benign home line, so a deletion falls through to the flag, and the message says it's a removal.
        patch = ('@@ -1,4 +1,3 @@\n'
                 '   "identity": "solo",\n'
                 '-  "home_repository": "acme/engine-home"\n'
                 ' }\n')
        rc, out = self._main_json(
            {"pull_request": {"number": 1, "labels": []}},
            [{"filename": ".engine/engine.json", "status": "modified", "patch": patch}],
            base_home="acme/engine-home")
        self.assertEqual(len(out), 1)
        self.assertIn("GUARDRAIL CHANGE DETECTED", out[0]["message"])
        self.assertIn("REMOVED", out[0]["message"])            # named as a removal, not a value change
        self.assertIn("guardrail-ack", out[0]["message"])

    def test_home_pure_classifier_flags_a_deletion(self):
        # The same at the classifier seam, so the reason is pinned directly.
        patch = ('@@ -1,4 +1,3 @@\n   "identity": "solo",\n'
                 '-  "home_repository": "acme/engine-home"\n }\n')
        files = [{"filename": ".engine/engine.json", "status": "modified", "patch": patch}]
        self.assertEqual(weakening_guard.home_repoint(files, "acme/engine-home"),
                         ("acme/engine-home", None, "deletion"))

    def test_home_trailing_fragment_on_the_line_is_flagged(self):
        # The conformance lens's finding: a base-valued home line with a SECOND fragment after it is not
        # "formatting churn" — the strict full-line benign match rejects it, so it flags.
        patch = ('@@ -1,3 +1,3 @@\n'
                 '-  "home_repository": "acme/engine-home",\n'
                 '+  "home_repository": "acme/engine-home", "update_channel": "evil",\n'
                 ' }\n')
        rc, out = self._main_json(
            {"pull_request": {"number": 1, "labels": []}},
            [{"filename": ".engine/engine.json", "status": "modified", "patch": patch}],
            base_home="acme/engine-home")
        self.assertEqual(len(out), 1)
        self.assertIn("GUARDRAIL CHANGE DETECTED", out[0]["message"])

    def test_home_escaped_key_injection_is_flagged(self):
        # The pre-existing evasion the review surfaced: an escaped key ("home_repositor\\u0079") slips past a
        # substring touch-test while JSON folds it back to the real key (last value wins). The engine
        # manifest never legitimately carries an escape, so any backslash in an added manifest line fails
        # closed — the escaped-key repoint now demands the ack instead of passing silently.
        patch = ('@@ -1,3 +1,4 @@\n'
                 '   "home_repository": "acme/engine-home",\n'
                 '+  "home_repositor\\u0079": "evil/look-alike",\n'
                 ' }\n')
        rc, out = self._main_json(
            {"pull_request": {"number": 1, "labels": []}},
            [{"filename": ".engine/engine.json", "status": "modified", "patch": patch}],
            base_home="acme/engine-home")
        self.assertEqual(len(out), 1)
        self.assertIn("GUARDRAIL CHANGE DETECTED", out[0]["message"])
        self.assertIn("guardrail-ack", out[0]["message"])

    def test_home_no_patch_message_does_not_assert_the_home_line_was_touched(self):
        # #515's message honesty, second half: when the WHOLE diff was too large to return, the guard only
        # knows the manifest changed unreadably — it must not claim the home line itself was changed.
        rc, out = self._main_json(
            {"pull_request": {"number": 1, "labels": []}},
            [{"filename": ".engine/engine.json", "status": "modified"}],   # no 'patch' field
            base_home="acme/engine-home")
        self.assertEqual(len(out), 1)
        self.assertIn("too large for this check to read in full", out[0]["message"])
        self.assertNotIn("home_repository` line", out[0]["message"])   # never asserts the line was touched

    def test_home_cr_hidden_repoint_is_flagged(self):
        # #550 review (the exploitable split): a carriage return is a line break to str.splitlines but NOT
        # to a GitHub `\n`-delimited diff or to JSON (where it is whitespace). One added `+` line carries a
        # benign base-valued home entry, a CR, then a SECOND home entry with an evil value — which the
        # applied manifest resolves as the effective home (last value wins). The guard must extract lines on
        # `\n` only and fail closed on the embedded separator, never clear it.
        patch = ('@@ -1,3 +1,3 @@\n'
                 '-  "home_repository": "acme/engine-home"\n'
                 '+  "home_repository": "acme/engine-home",\r  "home_repository": "evil/look-alike"\n'
                 ' }\n')
        rc, out = self._main_json(
            {"pull_request": {"number": 1, "labels": []}},
            [{"filename": ".engine/engine.json", "status": "modified", "patch": patch}],
            base_home="acme/engine-home")
        self.assertEqual(len(out), 1)
        self.assertIn("GUARDRAIL CHANGE DETECTED", out[0]["message"])
        self.assertIn("guardrail-ack", out[0]["message"])

    def test_home_same_line_duplicate_key_is_flagged(self):
        # The no-separator sibling: two home keys on ONE added line, last value wins. The strict full-line
        # benign match rejects it (trailing content), so it flags.
        patch = ('@@ -1,3 +1,3 @@\n'
                 '-  "home_repository": "acme/engine-home"\n'
                 '+  "home_repository": "acme/engine-home", "home_repository": "evil/look-alike"\n'
                 ' }\n')
        rc, out = self._main_json(
            {"pull_request": {"number": 1, "labels": []}},
            [{"filename": ".engine/engine.json", "status": "modified", "patch": patch}],
            base_home="acme/engine-home")
        self.assertEqual(len(out), 1)
        self.assertIn("GUARDRAIL CHANGE DETECTED", out[0]["message"])

    def test_home_trailing_crlf_reformat_does_not_false_alarm(self):
        # The false-alarm guard on the separator defense: a Windows-checkout diff ends each line with a
        # trailing CRLF. That trailing `\r` is NOT an embedded separator (splitlines yields one piece), so
        # the benign trailing-comma reformat must still CLEAR — the #515 fix survives CRLF line endings.
        patch = ('@@ -1,3 +1,4 @@\r\n'
                 '-  "home_repository": "acme/engine-home"\r\n'
                 '+  "home_repository": "acme/engine-home",\r\n'
                 '+  "control_plane": {"ruleset_id": 901}\r\n'
                 ' }\r\n')
        rc, out = self._main_json(
            {"pull_request": {"number": 1, "labels": []}},
            [{"filename": ".engine/engine.json", "status": "modified", "patch": patch}],
            base_home="acme/engine-home")
        self.assertEqual(out, [], "a trailing-CRLF reformat of the unchanged home must not demand an ack")

    def test_home_change_with_no_patch_fails_closed(self):
        # #367 security review: GitHub elides the patch on a large PR. A manifest change we cannot inspect,
        # with a home recorded, fails CLOSED (demands the ack) rather than passing silently.
        rc, out = self._main_json(
            {"pull_request": {"number": 1, "labels": []}},
            [{"filename": ".engine/engine.json", "status": "modified"}],   # no 'patch' field
            base_home="acme/engine-home")
        self.assertEqual(len(out), 1)
        self.assertIn("GUARDRAIL CHANGE DETECTED", out[0]["message"])
        self.assertIn("guardrail-ack", out[0]["message"])

    def test_home_repoint_pure_classifier_reads_only_the_manifest(self):
        files = [{"filename": ".engine/engine.json", "status": "modified", "patch": self._REPOINT_PATCH}]
        self.assertEqual(weakening_guard.home_repoint(files, "acme/engine-home"),
                         ("acme/engine-home", "evil/look-alike", "changed"))
        # no base home recorded -> a first recording, never a repoint
        self.assertIsNone(weakening_guard.home_repoint(files, None))
        # the same patch on a NON-manifest file is ignored — only the manifest carries the home coordinate
        self.assertIsNone(weakening_guard.home_repoint(
            [{"filename": "docs/x.md", "status": "modified", "patch": self._REPOINT_PATCH}], "acme/engine-home"))

    # ---- the team->solo identity downgrade is a guardrail weakening (mirrors home_repoint) ----
    _DOWNGRADE_PATCH = ('@@ -15,3 +15,3 @@\n'
                        '-  "identity": "team",\n'
                        '+  "identity": "solo",\n'
                        ' }\n')

    def _f(self, patch, status="modified"):
        return [{"filename": ".engine/engine.json", "status": status, "patch": patch}]

    def test_identity_downgrade_pure_classifier(self):
        d = weakening_guard.identity_downgrade
        # base is team + the diff lowers it to solo -> a weakening to flag
        self.assertTrue(d(self._f(self._DOWNGRADE_PATCH), "team"))
        # base is solo (or none) -> solo->team and a first team recording are STRENGTHENINGS, never gated
        self.assertFalse(d(self._f(self._DOWNGRADE_PATCH), "solo"))
        self.assertFalse(d(self._f(self._DOWNGRADE_PATCH), None))
        # base team, but the identity key is untouched by this diff -> not a downgrade
        self.assertFalse(d(self._f('@@ -1,1 +1,1 @@\n-  "engine_release": "1"\n+  "engine_release": "2"\n'), "team"))
        # base team, a manifest change with NO inspectable patch -> fail closed (flag)
        self.assertTrue(d([{"filename": ".engine/engine.json", "status": "modified", "patch": None}], "team"))
        # base team, the diff touches identity but PROVABLY keeps team -> not a downgrade
        self.assertFalse(d(self._f('@@ -15,1 +15,1 @@\n-  "identity": "team",\n+  "identity": "team" ,\n'), "team"))
        # a duplicate-key / value-split injection adding a non-team identity value -> flagged (fail closed)
        self.assertTrue(d(self._f('@@ -15,1 +16,1 @@\n+  "identity": "solo",\n'), "team"))
        # the same patch on a NON-manifest file is ignored
        self.assertFalse(d([{"filename": "docs/x.md", "status": "modified", "patch": self._DOWNGRADE_PATCH}], "team"))
        # #550 review: the identity guard shares the home guard's line-extraction, so it shares both
        # evasions. A CR-hidden second `"identity": "solo"` (str.splitlines would fragment the line and
        # drop the `+` marker) must fail closed:
        self.assertTrue(d(self._f('@@ -15,1 +15,1 @@\n-  "identity": "team"\n'
                                  '+  "identity": "team",\r  "identity": "solo"\n'), "team"))
        # ...and a same-line duplicate `identity` key (last value wins) must be caught by reading EVERY
        # value on the line (findall, not first-match search):
        self.assertTrue(d(self._f('@@ -15,1 +15,1 @@\n-  "identity": "team"\n'
                                  '+  "identity": "team", "identity": "solo"\n'), "team"))
        # a trailing CRLF on a legit keep-team reformat is NOT an embedded separator -> not flagged
        self.assertFalse(d(self._f('@@ -15,1 +15,1 @@\r\n-  "identity": "team"\r\n'
                                   '+  "identity": "team",\r\n'), "team"))

    def test_identity_downgrade_is_flagged_and_downgraded_by_the_ack(self):
        # end-to-end through main(): a team->solo edit on a team-base repo blocks until the ack, which downgrades it to a record.
        rc, out = self._main_json(
            {"pull_request": {"number": 1, "labels": []}},
            self._f(self._DOWNGRADE_PATCH), base_tier="team")
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["severity"], "hard")
        self.assertIn("team mode back to", out[0]["message"])
        self.assertIn("guardrail-ack", out[0]["message"])
        rc, out = self._main_json(
            {"pull_request": {"number": 1, "labels": [{"name": "guardrail-ack"}]}},
            self._f(self._DOWNGRADE_PATCH), base_tier="team")
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["severity"], "soft")
        self.assertIn("ACKNOWLEDGED", out[0]["message"])

    # ---- arming the executable build target is a guardrail weakening; INVERTED from home (first-set FIRES) ----
    _ARM_PATCH = ('@@ -1,3 +1,4 @@\n'
                  '   "identity": "solo",\n'
                  '+  "product_build_target": "StarshipSuperjam/engine-template",\n'
                  ' }\n')

    def test_product_build_target_pure_classifier(self):
        a = weakening_guard.product_build_target_arm
        # FIRST-SET (base absent -> a value appears) FIRES — the inverse of home_repoint; this is the arming.
        self.assertEqual(a(self._f(self._ARM_PATCH), None),
                         (None, "StarshipSuperjam/engine-template", "set"))
        # a REPOINT (base present, value changes) FIRES.
        repoint = self._f('@@ -1,3 +1,3 @@\n'
                          '-  "product_build_target": "acme/old",\n'
                          '+  "product_build_target": "evil/look-alike",\n')
        self.assertEqual(a(repoint, "acme/old"), ("acme/old", "evil/look-alike", "changed"))
        # DELETION (present -> absent, no added target line) is BENIGN — reverts to the safe self-building default.
        deletion = self._f('@@ -1,3 +1,2 @@\n'
                           '   "identity": "solo",\n'
                           '-  "product_build_target": "acme/old",\n')
        self.assertIsNone(a(deletion, "acme/old"))
        # a same-value formatting touch (unchanged value) is BENIGN.
        same = self._f('@@ -1,3 +1,3 @@\n'
                       '-  "product_build_target": "acme/old"\n'
                       '+  "product_build_target": "acme/old",\n')
        self.assertIsNone(a(same, "acme/old"))
        # a version-only bump (no target line touched) does NOT flag, even with a target recorded.
        self.assertIsNone(a(self._f('@@ -1,1 +1,1 @@\n-  "engine_release": "1"\n+  "engine_release": "2"\n'),
                            "acme/old"))
        # a manifest change with NO inspectable patch -> fail closed (flag), even with no base target.
        self.assertEqual(a([{"filename": ".engine/engine.json", "status": "modified", "patch": None}], None),
                         (None, None, "unreadable-patch"))
        # the same arming patch on a NON-manifest file is ignored.
        self.assertIsNone(a([{"filename": "docs/x.md", "status": "modified", "patch": self._ARM_PATCH}], None))
        # a duplicate-key injection (JSON last value wins) is read by findall on the added line -> flagged.
        dup = self._f('@@ -1,3 +1,4 @@\n'
                      '   "product_build_target": "acme/old",\n'
                      '+  "product_build_target": "evil/look-alike",\n')
        self.assertEqual(a(dup, "acme/old"), ("acme/old", "evil/look-alike", "changed"))
        # a value split across physical lines defeats the one-line parse -> "unclear" (fail closed).
        split = self._f('@@ -1,3 +1,4 @@\n'
                        '+  "product_build_target": "evil/\n'
                        '+look-alike"\n')
        self.assertEqual(a(split, None), (None, None, "unclear"))
        # a CR-hidden second value (str.splitlines would fragment and drop the `+`) fails closed on the anomaly.
        cr = self._f('@@ -1,3 +1,4 @@\n'
                     '+  "product_build_target": "acme/ok",\r  "product_build_target": "evil/x"\n')
        self.assertEqual(a(cr, None), (None, None, "escaped"))

    def test_product_build_target_first_set_is_flagged_and_downgraded_by_the_ack(self):
        # end-to-end through main(): arming the target on a repo with none recorded blocks until the ack, which downgrades it to a record.
        rc, out = self._main_json(
            {"pull_request": {"number": 1, "labels": []}},
            self._f(self._ARM_PATCH))  # base_product_build_target None -> first-set
        self.assertEqual(rc, 0)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["severity"], "hard")
        self.assertIn("GUARDRAIL CHANGE DETECTED", out[0]["message"])
        self.assertIn("build target", out[0]["message"])
        self.assertIn("StarshipSuperjam/engine-template", out[0]["message"])
        self.assertIn("guardrail-ack", out[0]["message"])
        rc, out = self._main_json(
            {"pull_request": {"number": 1, "labels": [{"name": "guardrail-ack"}]}},
            self._f(self._ARM_PATCH))
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["severity"], "soft")
        self.assertIn("ACKNOWLEDGED", out[0]["message"])

    def test_product_build_target_repoint_names_old_and_new(self):
        rc, out = self._main_json(
            {"pull_request": {"number": 1, "labels": []}},
            self._f('@@ -1,3 +1,3 @@\n'
                    '-  "product_build_target": "acme/old",\n'
                    '+  "product_build_target": "evil/look-alike",\n'),
            base_product_build_target="acme/old")
        self.assertEqual(len(out), 1)
        self.assertIn("acme/old", out[0]["message"])
        self.assertIn("evil/look-alike", out[0]["message"])

    def test_product_build_target_deletion_is_not_flagged(self):
        # de-arming (removing the target) reverts to the safe self-building default -> no ack demanded.
        rc, out = self._main_json(
            {"pull_request": {"number": 1, "labels": []}},
            self._f('@@ -1,3 +1,2 @@\n   "identity": "solo",\n-  "product_build_target": "acme/old",\n'),
            base_product_build_target="acme/old")
        self.assertEqual(out, [])

    def test_product_build_target_added_manifest_first_set_fires(self):
        # SECURITY (deliverable-gate finding): a first-set arriving on an `added` manifest (base has no manifest —
        # e.g. a delete-then-re-add composition, which WEAKENING_STATUS would let slip) is still the arming event
        # and MUST fire. This INVERTED detector evaluates `added`, unlike its siblings. A first-run manifest with
        # NO target still passes.
        a = weakening_guard.product_build_target_arm
        added_with = [{"filename": ".engine/engine.json", "status": "added",
                       "patch": '@@ -0,0 +1,3 @@\n+{\n+  "product_build_target": "evil/repo"\n+}\n'}]
        self.assertEqual(a(added_with, None), (None, "evil/repo", "set"))
        added_without = [{"filename": ".engine/engine.json", "status": "added",
                          "patch": '@@ -0,0 +1,2 @@\n+{\n+  "engine_release": "1.0.0"\n+}\n'}]
        self.assertIsNone(a(added_without, None))
        # end-to-end: the added first-set blocks until the ack
        rc, out = self._main_json({"pull_request": {"number": 1, "labels": []}}, added_with)
        self.assertEqual(len(out), 1)
        self.assertIn("evil/repo", out[0]["message"])

    def test_missing_pr_context_is_hard_fail_closed(self):
        import contextlib
        import io
        saved = dict(os.environ)
        for k in ("GITHUB_REPOSITORY", "GITHUB_TOKEN", "GITHUB_EVENT_PATH"):
            os.environ.pop(k, None)
        os.environ["ENGINE_RULE_TIER"] = "hard"
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                rc = weakening_guard.main()
        finally:
            os.environ.clear()
            os.environ.update(saved)
        out = json.loads(buf.getvalue())
        self.assertEqual(rc, 0)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["severity"], "hard")

    # ---- issue #25: paginate the changed-files fetch; fail closed on a partial view ----

    def test_next_link_parses_next_url_and_none_when_absent(self):
        """_next_link returns the rel="next" URL, and None when there is no next page."""
        header = ('<https://api.github.com/repositories/1/pulls/1/files?per_page=100&page=2>; '
                  'rel="next", <https://api.github.com/repositories/1/pulls/1/files?'
                  'per_page=100&page=9>; rel="last"')
        self.assertEqual(
            weakening_guard.next_link(header),
            "https://api.github.com/repositories/1/pulls/1/files?per_page=100&page=2")
        # only rel="last" (the last page) -> no next; and no header at all -> no next
        self.assertIsNone(weakening_guard.next_link(
            '<https://api.github.com/repositories/1/pulls/1/files?page=9>; rel="last"'))
        self.assertIsNone(weakening_guard.next_link(None))

    def test_fetch_all_changed_files_follows_pagination(self):
        """The loop follows Link: rel="next" across pages and returns EVERY changed file —
        the direct regression guard for the fail-open bug (a late-page file was invisible)."""
        page1 = [{"filename": f"docs/f{i}.md", "status": "modified"} for i in range(100)]
        page2 = [{"filename": ".engine/check/pr-body-completeness.json", "status": "modified"}]
        page2_url = ("https://api.github.com/repos/o/r/pulls/1/files?per_page=100&page=2")
        pages = {
            "/repos/o/r/pulls/1/files?per_page=100": (page1, f'<{page2_url}>; rel="next"'),
            page2_url: (page2, None),
        }
        orig = weakening_guard.get_page
        weakening_guard.get_page = lambda url, token, **kw: pages[url]
        try:
            got = weakening_guard.fetch_all_changed_files("o/r", 1, "x")
        finally:
            weakening_guard.get_page = orig
        self.assertEqual(len(got), 101)  # both pages, not just the first 100
        self.assertEqual(got[-1]["filename"], ".engine/check/pr-body-completeness.json")

    def test_an_off_host_pagination_link_fails_the_fetch_closed(self):
        """End-to-end: a crafted off-host Link header reaching the REAL get_page must raise (the
        off-host guard now homed in github_client.request), so fetch_all_changed_files fails closed
        rather than following the link off-host. Only the network boundary (github_client._urlopen) is
        faked, so the guard is driven through weakening_guard's highest-stakes caller, not in isolation."""
        import github_client

        class _Resp:
            def __init__(self, body, link):
                self._b = json.dumps(body).encode("utf-8")
                self.headers = {"Link": link}

            def read(self):
                return self._b

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        orig = github_client._urlopen
        github_client._urlopen = lambda req, timeout=None: _Resp(
            [{"filename": "docs/a.md", "status": "modified"}],
            '<https://evil.example/repos/o/r/pulls/1/files?page=2>; rel="next"')
        try:
            with self.assertRaises(ValueError):
                weakening_guard.fetch_all_changed_files("o/r", 1, "x")
        finally:
            github_client._urlopen = orig

    def test_directional_escalation_promotes_to_hard_through_main(self):
        # end-to-end: a check-rule tier flip (a disclosure-tier FILE, a killswitch-shaped EDIT) must land
        # in the hard finding with the escalation note — and downgrade to ACKNOWLEDGED under the ack.
        files = [{"filename": ".engine/check/foo.json", "status": "modified",
                  "patch": '@@ -5 +5 @@\n-  "tier": "hard",\n+  "tier": "soft",'}]
        rc, out = self._main_json({"pull_request": {"number": 1, "labels": []}}, files)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["severity"], "hard")
        self.assertIn("foo.json", out[0]["message"])
        self.assertIn("configures the gate itself", out[0]["message"])
        self.assertIn("guardrail-ack", out[0]["message"])
        rc, out = self._main_json({"pull_request": {"number": 1, "labels": [{"name": "guardrail-ack"}]}}, files)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["severity"], "soft")
        self.assertIn("ACKNOWLEDGED", out[0]["message"])

    def test_mixed_hard_and_soft_emit_two_findings(self):
        files = [{"filename": ".engine/suites.json", "status": "modified"},
                 {"filename": ".engine/tools/modes.py", "status": "modified"}]
        rc, out = self._main_json({"pull_request": {"number": 1, "labels": []}}, files)
        self.assertEqual(len(out), 2)
        self.assertEqual({f["severity"] for f in out}, {"hard", "soft"})
        hard = next(f for f in out if f["severity"] == "hard")
        soft = next(f for f in out if f["severity"] == "soft")
        self.assertIn("suites.json", hard["message"])
        self.assertIn("modes.py", soft["message"])

    def test_oversized_pr_with_ack_downgrades_to_record(self):
        # the fail-closed completeness path honors the ack as a DOWNGRADE (it used to promise this and
        # never check the label at all).
        files = [{"filename": f"docs/f{i}.md", "status": "modified"} for i in range(100)]
        rc, out = self._main_json({"pull_request": {"number": 1, "labels": [{"name": "guardrail-ack"}]}},
                                  files, expected=5000)
        self.assertEqual(rc, 0)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["severity"], "soft")
        self.assertIn("acknowledged", out[0]["message"].lower())

    def test_pr_controlled_values_are_sanitized_in_findings(self):
        # a crafted filename and a crafted repoint value must not carry markdown/workflow-command
        # metacharacters into any rendered finding (the eADR-0040 sanitization guarantee).
        # under the guarded .engine/check/ prefix, so the crafted name genuinely enters the rendered
        # listing (a non-guarded evil name never reaches _listing at all — re-audit).
        evil_file = {"filename": ".engine/check/foo.json`[x](https://e)::stop-commands::",
                     "status": "modified",
                     "patch": '@@ -5 +5 @@\n-  "tier": "hard",\n+  "tier": "soft",'}
        patch = ('@@ -1,4 +1,4 @@\n'
                 '   "identity": "solo",\n'
                 '-  "home_repository": "acme/engine-home"\n'
                 '+  "home_repository": "evil/[CLICK](https://phish)::notice::x"\n'
                 ' }\n')
        files = [evil_file,
                 {"filename": ".engine/engine.json", "status": "modified", "patch": patch}]
        rc, out = self._main_json({"pull_request": {"number": 1, "labels": []}}, files,
                                  base_home="acme/engine-home")
        joined = "\n".join(f["message"] for f in out)
        # the guard's own static prose legitimately uses backticks (`guardrail-ack`), so assert the
        # INJECTED sequences are gone — including the backtick-bearing filename fragment.
        for bad in ("[CLICK]", "::notice::", "::stop-commands::", "foo.json`", "(https"):
            self.assertNotIn(bad, joined)
        self.assertIn("foo.json", joined)  # the sanitized name itself IS rendered

    def test_settings_matcher_rewrite_escalates_to_hard(self):
        # neutering the write-gate by rewriting a PreToolUse block's matcher (never touching a hook line)
        # must escalate — the adversarial shape the divergence review proved against the token list.
        files = [{"filename": ".claude/settings.json", "status": "modified",
                  "patch": '@@ -10 +10 @@\n-        "matcher": "",\n+        "matcher": "never-match-anything",'}]
        rc, out = self._main_json({"pull_request": {"number": 1, "labels": []}}, files)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["severity"], "hard")

    def test_bootstrap_comment_churn_stays_soft(self):
        # the ordinary self.required_checks attribute (and prose around it) must NOT escalate — the
        # tokens are full ruleset-field literals, never bare required_/require_ prefixes (re-audit).
        files = [{"filename": ".engine/tools/bootstrap.py", "status": "modified",
                  "patch": "@@ -1 +1 @@\n-        # self.required_checks is the effective set\n"
                           "+        # self.required_checks is the effective SET"}]
        rc, out = self._main_json({"pull_request": {"number": 1, "labels": []}}, files)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["severity"], "soft")

    def test_bootstrap_real_ruleset_vocabulary_escalates(self):
        # the fields bootstrap.py actually writes (beyond the required_status_checks family) must escalate.
        for line in ('-            "require_code_owner_review": True,',
                     '-            {"type": "non_fast_forward"},',
                     '-            "dismiss_stale_reviews_on_push": True,'):
            files = [{"filename": ".engine/tools/bootstrap.py", "status": "modified",
                      "patch": "@@ -1 +1 @@\n" + line}]
            rc, out = self._main_json({"pull_request": {"number": 1, "labels": []}}, files)
            self.assertEqual(out[0]["severity"], "hard", line)

    def test_weakening_on_a_late_page_is_caught(self):
        """The classifier sees the WHOLE paginated list, so a guardrail edit that lands
        after file 100 is flagged — the behavior the single-page fetch missed."""
        files = [{"filename": f"docs/f{i}.md", "status": "modified"} for i in range(100)]
        files.append({"filename": ".engine/uv.lock", "status": "modified"})
        rc, out = self._main_json({"pull_request": {"number": 1, "labels": []}}, files)
        self.assertEqual(rc, 0)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["severity"], "hard")
        self.assertIn("guardrail-ack", out[0]["message"])
        self.assertIn("uv.lock", out[0]["message"])
        self.assertIn("GUARDRAIL CHANGE DETECTED", out[0]["message"])

    def test_oversized_pr_fails_closed_not_clean(self):
        """When the guard reads fewer files than the PR's authoritative changed_files (a
        too-large PR past the listing cap), it fails CLOSED — a hard finding demanding the
        ack, naming PR SIZE as the cause, never the 'weakening detected' message, never []."""
        files = [{"filename": f"docs/f{i}.md", "status": "modified"} for i in range(100)]
        rc, out = self._main_json({"pull_request": {"number": 1, "labels": []}},
                                  files, expected=5000)
        self.assertEqual(rc, 0)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["severity"], "hard")
        self.assertIn("guardrail-ack", out[0]["message"])
        self.assertIn("5000", out[0]["message"])
        self.assertNotIn("GUARDRAIL CHANGE DETECTED", out[0]["message"])

    def test_duplicate_listing_entry_cannot_mask_a_missing_file(self):
        """The completeness gate counts DISTINCT filenames, so a duplicate listing entry
        cannot inflate the tally to match changed_files while a real file goes unseen
        (the guard must not be falsifiable by the change it judges). This would pass
        clean under a raw len() comparator — it must fail closed."""
        files = [{"filename": f"docs/f{i}.md", "status": "modified"} for i in range(99)]
        files.append({"filename": "docs/f0.md", "status": "modified"})  # dup -> len 100, distinct 99
        rc, out = self._main_json({"pull_request": {"number": 1, "labels": []}},
                                  files, expected=100)  # the PR truly changed 100 files
        self.assertEqual(rc, 0)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["severity"], "hard")
        self.assertIn("guardrail-ack", out[0]["message"])
        self.assertNotIn("GUARDRAIL CHANGE DETECTED", out[0]["message"])

    def test_unavailable_count_fails_closed(self):
        """If the authoritative count is unavailable (not an int), the guard cannot confirm
        it read every file, so it fails closed rather than judging a partial view."""
        files = [{"filename": "README.md", "status": "modified"}]
        rc, out = self._main_json({"pull_request": {"number": 1, "labels": []}},
                                  files, expected=None)  # count unavailable
        self.assertEqual(rc, 0)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["severity"], "hard")
        self.assertIn("guardrail-ack", out[0]["message"])
        self.assertNotIn("GUARDRAIL CHANGE DETECTED", out[0]["message"])


class TestRunCheckById(unittest.TestCase):
    """validate.py --check <id> runs ONE rule by id, outside any suite: it gates on a
    hard finding (exit 1 / 0 clean / 2 on unknown id), fails closed on a dangling or
    erroring kind, and does NOT load suites.json (the isolation from the suite grammar)."""
    def setUp(self):
        self._rules, self._reg, self._suites = (
            validate.load_rules, dict(validate.REGISTRY), validate.SUITES_PATH)

    def tearDown(self):
        validate.load_rules = self._rules
        validate.SUITES_PATH = self._suites
        validate.REGISTRY.clear()
        validate.REGISTRY.update(self._reg)

    def _install(self, kind_fn, kind="synthetic", tier="hard", rid="engine/check/synthetic"):
        validate.load_rules = lambda: [{"id": rid, "kind": kind, "tier": tier,
                                        "suites": [], "params": {}}]
        validate.REGISTRY[kind] = kind_fn

    def test_hard_finding_exits_one(self):
        self._install(lambda rule, ctx: (False, [validate.finding("hard", "boom")]))
        self.assertEqual(_check_quiet("engine/check/synthetic", {}), 1)

    def test_clean_exits_zero(self):
        self._install(lambda rule, ctx: (True, []))
        self.assertEqual(_check_quiet("engine/check/synthetic", {}), 0)

    def test_soft_only_exits_zero(self):
        self._install(lambda rule, ctx: (False, [validate.finding("soft", "note")]))
        self.assertEqual(_check_quiet("engine/check/synthetic", {}), 0)

    def test_unknown_id_exits_two(self):
        self._install(lambda rule, ctx: (True, []))
        self.assertEqual(_check_quiet("engine/check/nope", {}), 2)

    def test_dangling_kind_fails_closed(self):
        validate.load_rules = lambda: [{"id": "engine/check/x", "kind": "ghost",
                                        "tier": "hard", "suites": [], "params": {}}]
        self.assertEqual(_check_quiet("engine/check/x", {}), 1)

    def test_erroring_kind_fails_closed(self):
        def boom(rule, ctx):
            raise RuntimeError("kaboom")
        self._install(boom)
        self.assertEqual(_check_quiet("engine/check/synthetic", {}), 1)

    def test_does_not_load_suites_json(self):
        # Point SUITES_PATH at garbage; a by-id run must still work — it never reads the
        # suite declarations, so a broken/loosened suites.json cannot strand the guard.
        with tempfile.TemporaryDirectory() as d:
            validate.SUITES_PATH = _write(d, "suites.json", "{ not json")
            self._install(lambda rule, ctx: (True, []))
            self.assertEqual(_check_quiet("engine/check/synthetic", {}), 0)


class TestGuardRuleIsolation(unittest.TestCase):
    """The re-homed weakening guard joins NO suite (suites: []), so the head-checkout CI
    suite can never run it — it is invoked only by id from engine-guard.yml, which runs
    from the trusted base. The rule is also well-formed under check.v1.json."""
    def _guard_rule(self):
        return validate.load_json(os.path.join(validate.CHECK_DIR, "guardrail-weakening.json"))

    def test_guard_rule_joins_no_suite(self):
        self.assertEqual(self._guard_rule().get("suites"), [])

    def test_ci_roster_excludes_the_guard(self):
        # Over the real committed rules, the CI suite roster never includes the guard.
        ci = [r["id"] for r in validate.load_rules() if "CI" in r.get("suites", [])]
        self.assertNotIn("engine/check/guardrail-weakening", ci)

    def test_guard_rule_validates_against_check_schema(self):
        schema = validate.load_json(os.path.join(validate.SCHEMAS_DIR, "check.v1.json"))
        errs = list(validate.Draft202012Validator(schema).iter_errors(self._guard_rule()))
        self.assertEqual(errs, [])


if __name__ == "__main__":
    unittest.main()
