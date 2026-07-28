"""Tests for the control-plane bootstrap.

Run: uv run --directory .engine --frozen -- python -m unittest discover -s tools -p 'test_*.py' -b

The GitHub network is the ONLY thing faked (an in-memory transport returning (status, json, headers), and a
fake label-ensure boundary); every test exercises the real capability-detection, floor-merge, create/repair,
verify, degrade, and copy-rendering logic. The protection floor the tool writes is checked against the
REAL protection_guard.missing_floor — the same evaluation the committed CI guard uses — so a drift between
what the bootstrap writes and what the guard requires fails here.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bootstrap  # noqa: E402
import protection_guard  # noqa: E402
import weakening_guard  # noqa: E402  (ACK_LABEL — the frozen name bootstrap reuses when provisioning the label)
import telemetry  # noqa: E402  (ENGINE_DOMAIN_LABEL — the engine label's canonical name/color/description)

REPO = "you/proj"


class FakeIssues:
    """A stand-in for telemetry.GitHubIssues — records ensure_label (the engine label) and ensure_named_label
    (the guardrail-ack and any other provisioned label), optionally fails."""

    def __init__(self, fail: bool = False):
        self.fail = fail
        self.ensured = 0                 # engine-domain label ensures
        self.named = []                  # (name, color, description) for each ensure_named_label call

    def ensure_label(self):
        if self.fail:
            raise RuntimeError("GitHub unreachable")
        self.ensured += 1

    def ensure_named_label(self, name, color, description):
        if self.fail:
            raise RuntimeError("GitHub unreachable")
        self.named.append((name, color, description))


class FakeGitHub:
    """In-memory GitHub for the bootstrap transport seam. `scopes` is the X-OAuth-Scopes header value
    (or None for a fine-grained token); `floor_met` drives the evaluated per-branch rules; `rulesets`
    is the admin ruleset list. Records every call so writes can be asserted."""

    def __init__(self, scopes="repo", floor_met=False, rulesets=None,
                 deny_writes=0, deny_body=None, verify_raises=False, rulesets_read_raises=False):
        self.scopes = scopes
        self.floor_met = floor_met
        self.rulesets = [dict(r) for r in (rulesets or [])]
        self.calls = []
        self._next_id = 900
        self.deny_writes = deny_writes        # number of initial writes to reject with 403
        self.deny_body = deny_body            # the 403 body (e.g. an org-policy message)
        self.verify_raises = verify_raises    # make the post-write verify re-read unreachable
        self.rulesets_read_raises = rulesets_read_raises
        self._eval_reads = 0

    def _evaluated_rules(self):
        # A floor-meeting branch returns exactly the rules the engine writes; an unprotected one returns [].
        return bootstrap.floor_ruleset(tier=bootstrap.SOLO)["rules"] if self.floor_met else []

    def transport(self, method, path, body=None):
        self.calls.append((method, path, body))
        headers = {} if self.scopes is None else {"X-OAuth-Scopes": self.scopes}
        if method == "GET" and path == f"/repos/{REPO}":
            return 200, {"full_name": REPO}, headers
        if method == "GET" and path == f"/repos/{REPO}/rules/branches/main":
            self._eval_reads += 1
            # The verify read is the SECOND evaluated read (the first is the idempotence check).
            if self.verify_raises and self._eval_reads >= 2:
                raise bootstrap.BootstrapError("evaluated rules unreachable")
            return 200, self._evaluated_rules(), headers
        if method == "GET" and path == f"/repos/{REPO}/rulesets":
            if self.rulesets_read_raises:
                raise bootstrap.BootstrapError("rulesets unreachable")
            return 200, self.rulesets, headers
        if method in ("POST", "PUT") and self.deny_writes > 0:
            self.deny_writes -= 1
            return 403, (self.deny_body or {"message": "Resource not accessible"}), headers
        if method == "POST" and path == f"/repos/{REPO}/rulesets":
            rid = self._next_id
            self._next_id += 1
            self.rulesets.append({"id": rid, "name": body["name"]})
            self.floor_met = True
            return 201, {"id": rid, "name": body["name"]}, headers
        if method == "PUT" and path.startswith(f"/repos/{REPO}/rulesets/"):
            self.floor_met = True
            return 200, {"id": int(path.rsplit("/", 1)[1])}, headers
        return 404, None, headers

    # convenience assertions
    def writes(self):
        return [c for c in self.calls if c[0] in ("POST", "PUT")]

    def names(self):
        return [r["name"] for r in self.rulesets]


def cp(fake, refresh_fn=None, issues=None):
    return bootstrap.ControlPlane(
        REPO, "tok", transport=fake.transport,
        refresh_fn=refresh_fn or (lambda scope: True), issues=issues or FakeIssues())


def quiet(_text):
    """An announce sink so tests don't print operator copy."""


class TestProvisionedLabelsMatchTheirOwners(unittest.TestCase):
    """bootstrap re-types two externally-owned label identities as local mirrors, because it cannot import their
    owning modules on the pre-venv first-run path (system Python, before the venv exists). These drift-guards hold
    each mirror equal to its canonical home — they run in the venv, where importing the owner is free — so a rename
    or recolour in the owning module that isn't reflected in bootstrap's mirror fails here, not silently in a
    generated repo."""

    def test_needs_reauthoring_label_matches_conformance_contract(self):
        import issue_conformance_ci  # noqa: E402 — venv-side; canonical home of the needs-reauthoring identity
        self.assertEqual(
            (bootstrap.NEEDS_REAUTHORING_LABEL, bootstrap.NEEDS_REAUTHORING_LABEL_COLOR,
             bootstrap.NEEDS_REAUTHORING_LABEL_DESCRIPTION),
            (issue_conformance_ci.NEEDS_REAUTHORING_LABEL, issue_conformance_ci.NEEDS_REAUTHORING_LABEL_COLOR,
             issue_conformance_ci.NEEDS_REAUTHORING_LABEL_DESCRIPTION))

    def test_erasure_label_matches_the_memory_contract(self):
        from memory import erasure_observer  # noqa: E402 — venv-side; canonical home of the engine-erasure identity
        self.assertEqual(
            (bootstrap.ERASURE_LABEL, bootstrap.ERASURE_LABEL_COLOR, bootstrap.ERASURE_LABEL_DESCRIPTION),
            (erasure_observer.ERASURE_LABEL, erasure_observer.ERASURE_LABEL_COLOR,
             erasure_observer.ERASURE_LABEL_DESCRIPTION))


class TestFloorPayload(unittest.TestCase):
    def test_floor_satisfies_the_real_guard(self):
        # The decisive fidelity test: what the bootstrap WRITES must satisfy the SAME evaluation the
        # committed protection_guard / CI guard uses. Any drift (a dropped rule, a wrong param) fails here.
        rules = bootstrap.floor_ruleset(tier=bootstrap.SOLO)["rules"]
        missing = protection_guard.missing_floor(rules, protection_guard.REQUIRED_CHECKS)
        self.assertEqual(missing, [], f"floor payload does not satisfy the guard: {missing}")

    def test_floor_binds_the_frozen_check_names_from_the_single_home(self):
        rules = bootstrap.floor_ruleset(tier=bootstrap.SOLO)["rules"]
        rsc = next(r for r in rules if r["type"] == "required_status_checks")
        bound = [c["context"] for c in rsc["parameters"]["required_status_checks"]]
        self.assertEqual(bound, protection_guard.REQUIRED_CHECKS)

    def test_floor_requires_conversation_resolution_and_zero_approvals(self):
        pr = next(r for r in bootstrap.floor_ruleset(tier=bootstrap.SOLO)["rules"] if r["type"] == "pull_request")
        self.assertTrue(pr["parameters"]["required_review_thread_resolution"])
        self.assertEqual(pr["parameters"]["required_approving_review_count"], 0)


class TestApplyCreatesAndIsIdempotent(unittest.TestCase):
    def test_unprotected_repo_creates_the_engine_ruleset(self):
        fake = FakeGitHub(floor_met=False, rulesets=[])
        issues = FakeIssues()
        result = cp(fake, issues=issues).apply(announce=quiet)
        self.assertEqual(result.status, "applied")
        self.assertTrue(result.is_protected())
        self.assertEqual([c[0] for c in fake.writes()], ["POST"])  # created, not repaired
        self.assertIn(bootstrap.ENGINE_RULESET_NAME, fake.names())
        # Provisioning ensures the COMPLETE required-label set — all four via ensure_named_label, so the engine
        # label no longer routes through the bare ensure_label (ensured stays 0). The four: the engine-domain
        # label, guardrail-ack, needs-reauthoring, and engine-erasure.
        self.assertEqual(issues.ensured, 0)
        provisioned = {name: (color, desc) for name, color, desc in issues.named}
        self.assertEqual(set(provisioned), {
            telemetry.ENGINE_DOMAIN_LABEL,
            weakening_guard.ACK_LABEL,
            bootstrap.NEEDS_REAUTHORING_LABEL,
            bootstrap.ERASURE_LABEL,
        })
        # ensure_labels emits EXACTLY the REQUIRED_LABELS table (the one auditable home), so a row dropped or
        # mis-sourced there fails here; every description fits GitHub's 100-char label cap.
        self.assertEqual(issues.named, list(bootstrap.REQUIRED_LABELS))
        for name, color, desc in issues.named:
            self.assertLessEqual(len(desc), 100, name)
        # guardrail-ack keeps its build-spec-leaf identity (name from the guard, color/desc from bootstrap).
        self.assertEqual(provisioned[weakening_guard.ACK_LABEL],
                         (bootstrap.ACK_LABEL_COLOR, bootstrap.ACK_LABEL_DESCRIPTION))

    def test_already_protected_is_a_no_op(self):
        fake = FakeGitHub(floor_met=True, rulesets=[])
        result = cp(fake).apply(announce=quiet)
        self.assertEqual(result.status, "already")
        self.assertEqual(fake.writes(), [])                        # never writes when the floor is met

    def test_existing_engine_ruleset_is_repaired_in_place(self):
        fake = FakeGitHub(floor_met=False,
                          rulesets=[{"id": 42, "name": bootstrap.ENGINE_RULESET_NAME}])
        result = cp(fake).apply(announce=quiet)
        self.assertEqual(result.status, "applied")
        self.assertEqual([c[0] for c in fake.writes()], ["PUT"])   # repaired its own, not a new one
        self.assertEqual(fake.calls[-2][1], f"/repos/{REPO}/rulesets/42")

    def test_verify_after_write_catches_a_silent_no_op(self):
        # A transport whose POST does NOT actually turn protection on -> the verify step degrades.
        fake = FakeGitHub(floor_met=False, rulesets=[])
        orig = fake.transport

        def transport(method, path, body=None):
            status, data, headers = orig(method, path, body)
            if method == "POST":
                fake.floor_met = False  # the write "succeeded" but protection never took
            return status, data, headers
        cpx = bootstrap.ControlPlane(REPO, "tok", transport=transport,
                                     refresh_fn=lambda s: True, issues=FakeIssues())
        result = cpx.apply(announce=quiet)
        self.assertEqual(result.status, "degraded")
        self.assertEqual(result.cause, "verify-failed")


class TestVerifyAndDegrade(unittest.TestCase):
    def test_unreadable_verify_does_not_claim_applied(self):
        # The write succeeds, but the verify re-read is unreachable -> NOT 'applied' (never assume the
        # write took); a distinct 'unverified' outcome that does not read as protected.
        fake = FakeGitHub(floor_met=False, rulesets=[], verify_raises=True)
        result = cp(fake).apply(announce=quiet)
        self.assertEqual(result.status, "unverified")
        self.assertFalse(result.is_protected())
        self.assertIn("couldn't", bootstrap.render(result).lower())

    def test_org_policy_403_routes_to_org_admin_banner(self):
        # A 403 whose body names an organization policy -> the org-policy cause, whose banner points the operator
        # at their org admin. It does NOT offer team mode as an escape: team's identity is deliberately non-admin
        # (so it "cannot weaken at all"), so it cannot hold the org-blocked branch-protection permission.
        fake = FakeGitHub(scopes="repo", floor_met=False, rulesets=[],
                          deny_writes=2,  # both the first write and the post-refresh retry are blocked
                          deny_body={"message": "Organization ruleset policy prevents this change."})
        result = cp(fake).apply(announce=quiet)
        self.assertEqual(result.status, "degraded")
        self.assertEqual(result.cause, "org-policy")
        rendered = bootstrap.render(result)
        self.assertIn("admin", rendered)
        self.assertNotIn("team mode", rendered)   # team is not an org-policy escape (non-admin identity)

    def test_plain_403_routes_to_not_admin_banner(self):
        fake = FakeGitHub(scopes="repo", floor_met=False, rulesets=[],
                          deny_writes=2, deny_body={"message": "Resource not accessible by integration"})
        result = cp(fake).apply(announce=quiet)
        self.assertEqual(result.cause, "not-admin")
        self.assertIn("administer", bootstrap.render(result))

    def test_fine_grained_403_then_refresh_retries_and_applies(self):
        # A fine-grained token (no scope header): the first write 403s, the refresh "grants" admin, the
        # retry succeeds -> applied, with exactly one engine ruleset created (no duplicate).
        fake = FakeGitHub(scopes=None, floor_met=False, rulesets=[], deny_writes=1)
        result = cp(fake, refresh_fn=lambda s: True).apply(announce=quiet)
        self.assertEqual(result.status, "applied")
        self.assertEqual(fake.names().count(bootstrap.ENGINE_RULESET_NAME), 1)  # no duplicate

    def test_rulesets_read_failure_still_proceeds_to_create(self):
        # Listing rulesets is unreachable -> treat as "no existing engine ruleset" and POST, never crash.
        fake = FakeGitHub(floor_met=False, rulesets=[], rulesets_read_raises=True)
        result = cp(fake).apply(announce=quiet)
        self.assertEqual(result.status, "applied")
        self.assertEqual([c[0] for c in fake.writes()], ["POST"])


class TestNeverWeakensProduct(unittest.TestCase):
    def test_product_ruleset_is_left_untouched(self):
        # A pre-existing product ruleset that doesn't meet the floor -> the engine adds its OWN, never
        # mutating the product's (augment-never-weaken; the engine does not augment the product's ruleset
        # in place). The product ruleset survives unchanged; no PUT touches it.
        fake = FakeGitHub(floor_met=False, rulesets=[{"id": 7, "name": "team protections"}])
        cp(fake).apply(announce=quiet)
        self.assertIn("team protections", fake.names())            # product still present
        self.assertIn(bootstrap.ENGINE_RULESET_NAME, fake.names())  # engine added its own
        put_targets = {c[1].rsplit("/", 1)[1] for c in fake.calls if c[0] == "PUT"}
        self.assertNotIn("7", put_targets)                         # the product ruleset id is never PUT


class TestCapabilityAndConsent(unittest.TestCase):
    def test_missing_scope_triggers_consent_then_proceeds(self):
        fake = FakeGitHub(scopes="read:org", floor_met=False, rulesets=[])
        announced = []

        def refresh(scope):
            fake.scopes = "read:org, repo"   # the operator approved -> scope now present
            return True
        result = cp(fake, refresh_fn=refresh).apply(announce=announced.append)
        self.assertEqual(result.status, "applied")
        # the pre-bootstrap explanation was shown first, and it NAMES + defuses the felt-sweeping label
        # rather than paraphrasing it away.
        self.assertTrue(any("full control of your repositories" in a for a in announced))
        self.assertTrue(any("you already" in a and "control" in a for a in announced))

    def test_refresh_that_does_not_persist_degrades_didnt_save(self):
        fake = FakeGitHub(scopes="read:org", floor_met=False, rulesets=[])
        result = cp(fake, refresh_fn=lambda s: False).apply(announce=quiet)  # scope never granted
        self.assertEqual(result.status, "degraded")
        self.assertEqual(result.cause, "didnt-save")
        self.assertEqual(fake.writes(), [])                        # never attempted the write

    def test_token_scopes_parses_header_case_insensitively(self):
        fake = FakeGitHub(scopes="repo, workflow")
        self.assertEqual(cp(fake).token_scopes(), {"repo", "workflow"})

    def test_fine_grained_token_no_scope_header_is_none_then_write_probes(self):
        fake = FakeGitHub(scopes=None, floor_met=False, rulesets=[])  # fine-grained: no scopes header
        result = cp(fake).apply(announce=quiet)
        self.assertIsNone(cp(fake).token_scopes())
        self.assertEqual(result.status, "applied")                 # capability proven by the write itself


class TestLabelsAndDisclosure(unittest.TestCase):
    def test_label_failure_is_disclosed_not_crashed(self):
        fake = FakeGitHub(floor_met=False, rulesets=[])
        result = cp(fake, issues=FakeIssues(fail=True)).apply(announce=quiet)
        self.assertEqual(result.status, "applied")                 # protection still applied
        self.assertFalse(result.labels_ok)                         # but the label gap is disclosed
        self.assertIn("issue label", bootstrap.render(result))


class TestCopySurface(unittest.TestCase):
    def test_template_carries_every_copy_section(self):
        # The template SURFACE must hold every heading the tool renders -> no silent drift to fallbacks.
        # Parse the template DIRECTLY (not load_copy, which substitutes the fallback for a missing section and
        # would make a dropped heading read as present — the very drift this guards). Assert each heading is a
        # real section in the template file with a non-empty body.
        with open(bootstrap.TEMPLATE_PATH, encoding="utf-8") as fh:
            sections = bootstrap._parse_sections(fh.read())
        for key, heading in bootstrap.COPY_HEADINGS.items():
            self.assertIn(heading, sections, f"copy section {key!r} ({heading!r}) missing from the template")
            self.assertTrue(sections[heading].strip(), f"copy section {key!r} is empty in the template")
        # And the template body, not the built-in fallback, is what load_copy returns.
        approve = bootstrap.load_copy(bootstrap.TEMPLATE_PATH)["before-you-approve"]
        self.assertIn("repo", approve)

    @staticmethod
    def _norm(text):
        # load_copy preserves the markdown paragraph's line wrapping, so normalize whitespace before a
        # word-level comparison — a wrap point is not a copy change, but a different word is.
        return " ".join(text.split())

    def test_before_you_approve_names_and_defuses_the_full_control_label(self):
        # The operator reads the TEMPLATE body at first run. It must NAME the sweeping,
        # full-control-sounding wording GitHub's screen uses and defuse it (scoped to repos the operator
        # already controls) — never the forbidden milder paraphrase that lets the scary label land
        # uninterpreted. This locks the operator-visible surface, which no test guarded before.
        approve = self._norm(bootstrap.load_copy(bootstrap.TEMPLATE_PATH)["before-you-approve"])
        self.assertIn("full control of your repositories", approve, "names the felt-sweeping label")
        self.assertIn("you already control", approve, "defuses it: scoped to repos the operator controls")
        self.assertNotIn("manage my repository", approve, "the forbidden milder paraphrase is gone")

    def test_template_and_fallback_before_you_approve_do_not_drift(self):
        # The operator normally reads the TEMPLATE body; the FALLBACK is the degraded path. Nothing guarded
        # that they say the same thing, so a copy fix could land in one and not the other (all tests still
        # green, the operator-visible copy unchanged). This guards it — word-for-word, tolerating only the
        # template's line wrapping.
        self.assertEqual(self._norm(bootstrap.load_copy(bootstrap.TEMPLATE_PATH)["before-you-approve"]),
                         self._norm(bootstrap.FALLBACK_COPY["before-you-approve"]))

    def test_missing_template_falls_back_not_crashes(self):
        copy = bootstrap.load_copy("/no/such/template.md")
        self.assertEqual(copy["before-you-approve"], bootstrap.FALLBACK_COPY["before-you-approve"])

    def test_render_picks_the_cause_matched_banner(self):
        r = bootstrap.Result("degraded", "main", ["x"], "not-admin")
        self.assertIn("administer", bootstrap.render(r))
        r2 = bootstrap.Result("degraded", "main", [], "didnt-save")
        self.assertIn("didn't save", bootstrap.render(r2))

    def test_copy_leaks_no_raw_api_token(self):
        # A raw GitHub-API / protocol token in the operator copy signals a leaked implementation detail (a
        # bug), not a word choice — this guards SYMBOLS, not vocabulary, so it is not a banned-word list.
        # Whether the prose leans on jargon is a judgment (the audit probe +
        # the per-PR review), never a filter.
        copy = bootstrap.load_copy(bootstrap.TEMPLATE_PATH)
        blob = " ".join(copy.values()).lower()
        for sym in ("ruleset", "oauth", "endpoint"):
            self.assertNotIn(sym, blob, f"raw API token {sym!r} leaked into operator copy")


class _RulesetFake:
    """An in-memory engine ruleset for the de-bootstrap seam — tracks the ruleset's CURRENT rules so
    floor_missing reflects what de_bootstrap / apply write, and supports DELETE. Records every call."""

    def __init__(self, present=True):
        self.rulesets = [{"id": 7, "name": bootstrap.ENGINE_RULESET_NAME}] if present else []
        self.rules = bootstrap.floor_ruleset(tier=bootstrap.SOLO)["rules"] if present else []
        self.calls = []

    def transport(self, method, path, body=None):
        self.calls.append((method, path, body))
        if method == "GET" and path == f"/repos/{REPO}":
            return 200, {"full_name": REPO}, {"X-OAuth-Scopes": "repo"}
        if method == "GET" and path == f"/repos/{REPO}/rules/branches/main":
            return 200, (self.rules if self.rulesets else []), {}
        if method == "GET" and path == f"/repos/{REPO}/rulesets":
            return 200, self.rulesets, {}
        if method == "PUT" and path.startswith(f"/repos/{REPO}/rulesets/"):
            self.rules = body["rules"]
            return 200, {"id": 7}, {}
        if method == "POST" and path == f"/repos/{REPO}/rulesets":
            self.rulesets = [{"id": 7, "name": body["name"]}]
            self.rules = body["rules"]
            return 201, {"id": 7}, {}
        if method == "DELETE" and path.startswith(f"/repos/{REPO}/rulesets/"):
            self.rulesets, self.rules = [], []
            return 204, None, {}
        return 404, None, {}

    def methods(self):
        return [c[0] for c in self.calls]


class TestRemainderRuleset(unittest.TestCase):
    def test_remainder_is_the_floor_minus_the_required_checks_rule(self):
        types = [r["type"] for r in bootstrap.remainder_ruleset()["rules"]]
        self.assertNotIn("required_status_checks", types)
        self.assertEqual(set(types), {"pull_request", "non_fast_forward", "deletion"})

    def test_remainder_keeps_code_owner_review_off(self):
        # The kept remainder must not reference a CODEOWNERS the removal deletes (adversarial NIT).
        pr = next(r for r in bootstrap.remainder_ruleset()["rules"] if r["type"] == "pull_request")
        self.assertFalse(pr["parameters"]["require_code_owner_review"])


class TestDeBootstrap(unittest.TestCase):
    def _cp(self, fake):
        return bootstrap.ControlPlane(REPO, "tok", transport=fake.transport)

    def test_keep_puts_the_checkless_remainder_never_deletes(self):
        fake = _RulesetFake(present=True)
        r = self._cp(fake).de_bootstrap(choice="keep", announce=quiet)
        self.assertEqual(r["status"], "kept")
        self.assertFalse(r["deleted"])
        self.assertIn("PUT", fake.methods())
        self.assertNotIn("DELETE", fake.methods())
        self.assertNotIn("required_status_checks", [x["type"] for x in fake.rules])

    def test_default_choice_is_keep_never_auto_deletes(self):
        fake = _RulesetFake(present=True)
        r = self._cp(fake).de_bootstrap(choice=None, announce=quiet)
        self.assertEqual(r["status"], "kept")
        self.assertNotIn("DELETE", fake.methods())

    def test_drop_deletes_the_engine_rule(self):
        fake = _RulesetFake(present=True)
        r = self._cp(fake).de_bootstrap(choice="drop", announce=quiet)
        self.assertEqual(r["status"], "dropped")
        self.assertTrue(r["deleted"])
        self.assertIn("DELETE", fake.methods())
        self.assertEqual(fake.rulesets, [])

    def test_no_engine_rule_is_a_no_op_disclosure(self):
        fake = _RulesetFake(present=False)
        r = self._cp(fake).de_bootstrap(choice="drop", announce=quiet)
        self.assertEqual(r["status"], "no-rule")
        self.assertFalse(r["ruleset_existed"])
        self.assertNotIn("DELETE", fake.methods())
        self.assertNotIn("PUT", fake.methods())

    def test_de_bootstrap_keep_then_apply_restores_the_floor(self):
        # The reversal pair: de_bootstrap removes the engine checks; re-running setup restores the floor.
        fake = _RulesetFake(present=True)
        cp = self._cp(fake)
        cp.de_bootstrap(choice="keep", announce=quiet)
        self.assertNotEqual(
            protection_guard.missing_floor(fake.rules, protection_guard.REQUIRED_CHECKS), [],
            "after de-bootstrap the engine checks should be gone from the rule")
        result = cp.apply(announce=quiet)
        self.assertTrue(result.is_protected())
        self.assertEqual(
            protection_guard.missing_floor(fake.rules, protection_guard.REQUIRED_CHECKS), [],
            "re-running setup should restore the full floor")

    def test_de_bootstrap_never_renames_the_frozen_check_names(self):
        before = list(protection_guard.REQUIRED_CHECKS)
        self._cp(_RulesetFake(present=True)).de_bootstrap(choice="keep", announce=quiet)
        self.assertEqual(protection_guard.REQUIRED_CHECKS, before)


# ====================================================================================================
# Brownfield augment + de-bootstrap. A purpose-built fake enforces the REAL list-vs-detail
# split — the list endpoint returns summaries WITHOUT rules, the full object (with rules) comes only from
# GET /rulesets/{id}, and the evaluated per-branch endpoint tags each in-force rule with the ruleset_id it
# came from — so these tests genuinely exercise ruleset_detail and the whitelist projection rather than
# reading rules off the list (which would mask a verbatim-echo bug).
# ====================================================================================================

def product_ruleset(rid=9, name="team protections", checks=("product-ci",), with_pr=True,
                    with_nff=True, with_deletion=True, bypass=None, thread_resolution=True):
    """A FULL product ruleset object (as GET /rulesets/{id} returns it), carrying the read-only metadata a
    real GET echoes and a writable body, so the projection (which must strip the former) is exercised."""
    rules: list = []
    if with_pr:
        rules.append({"type": "pull_request",
                      "parameters": {"required_approving_review_count": 1,
                                     "required_review_thread_resolution": thread_resolution,
                                     "dismiss_stale_reviews_on_push": False},
                      "ruleset_source_type": "Repository", "ruleset_id": rid})
    rules.append({"type": "required_status_checks",
                  "parameters": {"required_status_checks": [{"context": c} for c in checks],
                                 "strict_required_status_checks_policy": False},
                  "ruleset_source_type": "Repository", "ruleset_id": rid})
    if with_nff:
        rules.append({"type": "non_fast_forward", "ruleset_id": rid})
    if with_deletion:
        rules.append({"type": "deletion", "ruleset_id": rid})
    return {"id": rid, "name": name, "target": "branch", "enforcement": "active",
            "node_id": "RRS_x", "_links": {"self": {"href": "x"}}, "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-02T00:00:00Z", "source": REPO, "source_type": "Repository",
            "current_user_can_bypass": "always",
            "bypass_actors": bypass if bypass is not None
            else [{"actor_id": 5, "actor_type": "Team", "bypass_mode": "always"}],
            "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}},
            "rules": rules}


class AugmentGitHub:
    """In-memory GitHub modeling a repo that already protects main with its OWN ruleset(s). Enforces the real
    list-vs-detail split and tags evaluated rules with their ruleset_id. `drop_writes` simulates a concurrent
    overwrite (a PUT accepted but not applied); `mangle_on_put` drops a product rule on store (a
    preservation-violating server); `detail_partial` returns a rules-less detail (a partial read);
    `classic` is a list of in-force rules with NO ruleset_id (classic branch protection)."""

    def __init__(self, products=None, scopes="repo", drop_writes=0, mangle_on_put=False,
                 detail_partial=False, classic=None, drop_rule_types=None, normalize=False,
                 change_param=False):
        self.rulesets = {p["id"]: p for p in (products or [])}
        self.scopes = scopes
        self.drop_writes = drop_writes
        self.mangle_on_put = mangle_on_put
        self.detail_partial = detail_partial
        self.classic = classic or []
        self.drop_rule_types = set(drop_rule_types or [])   # server silently rejects these rule types
        self.normalize = normalize                          # server echoes back a default param (GitHub does)
        self.change_param = change_param                    # server alters an operator param (a violation)
        self.calls: list = []
        self._next_id = 900

    def _evaluated(self):
        out = list(self.classic)
        for rid, rs in self.rulesets.items():
            for r in rs.get("rules", []):
                out.append({**r, "ruleset_id": rid, "ruleset_source_type": "Repository"})
        return out

    def transport(self, method, path, body=None):
        self.calls.append((method, path, body))
        headers = {} if self.scopes is None else {"X-OAuth-Scopes": self.scopes}
        if method == "GET" and path == f"/repos/{REPO}":
            return 200, {"full_name": REPO}, headers
        if method == "GET" and path == f"/repos/{REPO}/rules/branches/main":
            return 200, self._evaluated(), headers
        if method == "GET" and path == f"/repos/{REPO}/rulesets":
            return 200, [{"id": rid, "name": rs.get("name")} for rid, rs in self.rulesets.items()], headers
        if method == "GET" and path.startswith(f"/repos/{REPO}/rulesets/"):
            rid = int(path.rsplit("/", 1)[1])
            rs = self.rulesets.get(rid)
            if rs is None:
                return 404, None, headers
            if self.detail_partial:
                return 200, {"id": rid, "name": rs.get("name")}, headers   # malformed: no rules array
            return 200, dict(rs), headers
        if method == "PUT" and path.startswith(f"/repos/{REPO}/rulesets/"):
            rid = int(path.rsplit("/", 1)[1])
            if self.drop_writes > 0:
                self.drop_writes -= 1
                return 200, {"id": rid}, headers          # accepted but NOT applied (someone else won)
            stored = {**self.rulesets.get(rid, {}), **body, "id": rid}
            rules = [dict(r) for r in stored.get("rules", [])]
            if self.drop_rule_types:                       # server rejects a rule type (org policy / tier)
                rules = [r for r in rules if r.get("type") not in self.drop_rule_types]
            for r in rules:                                # server-side parameter behaviors
                if r.get("type") == "pull_request":
                    p = dict(r.get("parameters") or {})
                    if self.normalize:                     # echoes back a default param it filled in
                        p.setdefault("dismiss_stale_reviews_on_push", False)
                    if self.change_param and "required_approving_review_count" in p:
                        p["required_approving_review_count"] = 0   # alters an operator setting (a violation)
                    r["parameters"] = p
            if self.mangle_on_put:
                rules = [r for r in rules if r.get("type") != "pull_request"]
            stored["rules"] = rules
            self.rulesets[rid] = stored
            return 200, {"id": rid}, headers
        if method == "POST" and path == f"/repos/{REPO}/rulesets":
            rid = self._next_id
            self._next_id += 1
            self.rulesets[rid] = {**body, "id": rid}
            return 201, {"id": rid, "name": body.get("name")}, headers
        if method == "DELETE" and path.startswith(f"/repos/{REPO}/rulesets/"):
            self.rulesets.pop(int(path.rsplit("/", 1)[1]), None)
            return 204, None, headers
        return 404, None, headers

    def detail(self, rid):
        return self.rulesets.get(rid)

    def writes(self):
        return [c for c in self.calls if c[0] in ("POST", "PUT", "DELETE")]

    def checks_of(self, rid):
        return bootstrap._bound_checks((self.rulesets.get(rid) or {}).get("rules", []))

    def types_of(self, rid):
        return {r.get("type") for r in (self.rulesets.get(rid) or {}).get("rules", [])}


ENGINE = list(protection_guard.REQUIRED_CHECKS)


class TestAugmentPayload(unittest.TestCase):
    """The pure read-modify helper: strictly additive, read-only fields stripped, residual gaps reported."""

    def test_unions_engine_checks_into_existing_rule(self):
        payload, added, residual = bootstrap.augment_payload(product_ruleset(), tier=bootstrap.SOLO)
        ctx = bootstrap._bound_checks(payload["rules"])
        self.assertEqual(ctx, {"product-ci", *ENGINE})
        self.assertEqual(set(added["checks"]), set(ENGINE))
        self.assertEqual(added["rules"], [])            # all floor rule types already present
        self.assertEqual(residual, [])

    def test_creates_checks_rule_when_absent(self):
        prod = product_ruleset(checks=())
        prod["rules"] = [r for r in prod["rules"] if r["type"] != "required_status_checks"]
        payload, added, _ = bootstrap.augment_payload(prod, tier=bootstrap.SOLO)
        self.assertEqual(bootstrap._bound_checks(payload["rules"]), set(ENGINE))
        self.assertIn("required_status_checks", added["rules"])
        self.assertEqual(added["checks"], [])           # the created rule's removal covers the checks

    def test_adds_wholly_missing_floor_rule_types(self):
        _payload, added, residual = bootstrap.augment_payload(
            product_ruleset(with_nff=False, with_deletion=False), tier=bootstrap.SOLO)
        self.assertIn("non_fast_forward", added["rules"])
        self.assertIn("deletion", added["rules"])
        self.assertEqual(residual, [])

    def test_preserves_bypass_and_conditions_and_strips_readonly(self):
        prod = product_ruleset()
        payload, _added, _residual = bootstrap.augment_payload(prod, tier=bootstrap.SOLO)
        self.assertEqual(payload["bypass_actors"], prod["bypass_actors"])
        self.assertEqual(payload["conditions"], prod["conditions"])
        for k in ("id", "node_id", "_links", "source", "source_type", "created_at",
                  "updated_at", "current_user_can_bypass"):
            self.assertNotIn(k, payload)
        for r in payload["rules"]:                      # per-rule read-only metadata stripped
            self.assertNotIn("ruleset_id", r)
            self.assertNotIn("ruleset_source_type", r)

    def test_existing_weak_pr_rule_is_disclosed_not_modified(self):
        prod = product_ruleset(thread_resolution=False)
        payload, added, residual = bootstrap.augment_payload(prod, tier=bootstrap.SOLO)
        self.assertTrue(any("unresolved" in m for m in residual))   # the gap is reported
        pr = next(r for r in payload["rules"] if r["type"] == "pull_request")
        self.assertFalse(pr["parameters"]["required_review_thread_resolution"])   # NOT flipped
        self.assertEqual(added["rules"], [])            # no rule type added; PR rule already present

    def test_already_augmented_is_a_noop(self):
        _payload, added, _residual = bootstrap.augment_payload(
            product_ruleset(checks=("product-ci", *ENGINE)), tier=bootstrap.SOLO)
        self.assertEqual(added["checks"], [])
        self.assertEqual(added["rules"], [])


class TestApplyAugmentsProductRuleset(unittest.TestCase):
    def _cp(self, fake):
        return cp(fake)

    def test_augments_the_single_product_ruleset_and_creates_no_second(self):
        fake = AugmentGitHub(products=[product_ruleset()])
        res = self._cp(fake).apply(branch="main", announce=quiet)
        self.assertEqual(res.status, "applied")
        self.assertEqual(res.mode, "augmented")
        self.assertEqual(len(fake.rulesets), 1)                  # NO second ruleset created
        self.assertNotIn("POST", [c[0] for c in fake.writes()])
        self.assertEqual(fake.checks_of(9), {"product-ci", *ENGINE})
        self.assertEqual(res.marker["ruleset_mode"], "augmented")
        self.assertEqual(res.marker["augmented_ruleset_id"], 9)
        self.assertEqual(set(res.marker["added"]["checks"]), set(ENGINE))

    def test_augment_preserves_the_operators_bypass_and_other_check(self):
        prod = product_ruleset(bypass=[{"actor_id": 7, "actor_type": "Integration", "bypass_mode": "always"}])
        fake = AugmentGitHub(products=[prod])
        self._cp(fake).apply(branch="main", announce=quiet)
        self.assertEqual(fake.rulesets[9]["bypass_actors"], prod["bypass_actors"])
        self.assertIn("product-ci", fake.checks_of(9))

    def test_partial_floor_is_disclosed_not_rewritten(self):
        fake = AugmentGitHub(products=[product_ruleset(thread_resolution=False)])
        res = self._cp(fake).apply(branch="main", announce=quiet)
        self.assertEqual(res.mode, "augmented-partial")
        self.assertTrue(res.missing)                            # the residual gap is carried for disclosure
        pr = next(r for r in fake.rulesets[9]["rules"] if r["type"] == "pull_request")
        self.assertFalse(pr["parameters"]["required_review_thread_resolution"])   # left as the operator set it

    def test_preservation_violation_degrades_never_false_applied(self):
        fake = AugmentGitHub(products=[product_ruleset()], mangle_on_put=True)   # server drops a product rule
        res = self._cp(fake).apply(branch="main", announce=quiet)
        self.assertEqual(res.status, "degraded")
        self.assertEqual(res.cause, "preserve-failed")

    def test_server_dropping_an_added_floor_rule_does_not_claim_applied(self):
        # The engine adds non_fast_forward, but the server (an org policy / plan tier) silently rejects it.
        # The engine must NOT report the floor as on — it verifies against the re-read object, not its payload.
        fake = AugmentGitHub(products=[product_ruleset(with_nff=False)], drop_rule_types=["non_fast_forward"])
        res = self._cp(fake).apply(branch="main", announce=quiet)
        self.assertEqual(res.status, "degraded")
        self.assertEqual(res.cause, "verify-failed")
        self.assertTrue(any("force-push" in m for m in res.missing))   # the unmet piece is named

    def test_preservation_tolerates_server_parameter_normalization(self):
        # GitHub echoes back default params it fills in; the additive write must NOT false-alarm on that.
        fake = AugmentGitHub(products=[product_ruleset()], normalize=True)
        res = self._cp(fake).apply(branch="main", announce=quiet)
        self.assertEqual(res.status, "applied")
        self.assertEqual(res.mode, "augmented")

    def test_preservation_catches_a_changed_operator_parameter(self):
        # If the server (or anything) alters an operator-set parameter, that IS a weakening — fail closed.
        fake = AugmentGitHub(products=[product_ruleset()], change_param=True)   # drops approvals 1 -> 0
        res = self._cp(fake).apply(branch="main", announce=quiet)
        self.assertEqual(res.status, "degraded")
        self.assertEqual(res.cause, "preserve-failed")

    def test_concurrent_overwrite_retries_once_then_succeeds(self):
        fake = AugmentGitHub(products=[product_ruleset()], drop_writes=1)
        res = self._cp(fake).apply(branch="main", announce=quiet)
        self.assertEqual(res.status, "applied")
        self.assertEqual(len([c for c in fake.writes() if c[0] == "PUT"]), 2)   # retried exactly once

    def test_persistent_overwrite_does_not_claim_applied(self):
        fake = AugmentGitHub(products=[product_ruleset()], drop_writes=5)
        res = self._cp(fake).apply(branch="main", announce=quiet)
        self.assertEqual(res.status, "unverified")
        self.assertLessEqual(len([c for c in fake.writes() if c[0] == "PUT"]), 2)   # bounded

    def test_partial_detail_read_fails_closed_with_no_write(self):
        fake = AugmentGitHub(products=[product_ruleset()], detail_partial=True)
        res = self._cp(fake).apply(branch="main", announce=quiet)
        self.assertEqual(res.status, "unverified")
        self.assertEqual([c for c in fake.writes() if c[0] == "PUT"], [])   # never PUT on a doubtful read

    def test_more_than_one_product_ruleset_creates_own_and_discloses(self):
        said = []
        fake = AugmentGitHub(products=[product_ruleset(rid=9), product_ruleset(rid=10, name="other")])
        res = cp(fake).apply(branch="main", announce=said.append)
        self.assertEqual(res.mode, "created")
        self.assertIn("POST", [c[0] for c in fake.writes()])               # created its own
        self.assertTrue(any("more than one" in s for s in said))           # disclosed the ambiguity

    def test_classic_protection_creates_own_and_discloses(self):
        said = []
        # classic branch protection: in-force rules with NO ruleset_id, partially meeting the floor.
        fake = AugmentGitHub(products=[], classic=[{"type": "pull_request",
                             "parameters": {"required_review_thread_resolution": True}}])
        res = cp(fake).apply(branch="main", announce=said.append)
        self.assertEqual(res.mode, "created")
        self.assertIn("POST", [c[0] for c in fake.writes()])
        self.assertTrue(any("already protected" in s for s in said))

    def test_own_engine_ruleset_present_repairs_in_place(self):
        fake = AugmentGitHub(products=[product_ruleset(rid=3, name=bootstrap.ENGINE_RULESET_NAME, checks=())])
        res = cp(fake).apply(branch="main", announce=quiet)
        self.assertEqual(res.mode, "repaired")
        self.assertNotIn("POST", [c[0] for c in fake.writes()])            # repaired, not created


class TestDeBootstrapAugmented(unittest.TestCase):
    # The fixtures here represent the POST-augment state: the product ruleset as it stands AFTER arrival
    # augmented it (the engine's checks unioned in, and — per AUG.added.rules — a non_fast_forward rule the
    # engine added because the product lacked one). The marker records exactly that, so removal reverses it.
    AUG = {"ruleset_mode": "augmented", "augmented_ruleset_id": 9,
           "added": {"checks": list(protection_guard.REQUIRED_CHECKS), "rules": ["non_fast_forward"]}}

    def test_reverses_exactly_the_marked_additions_never_deletes(self):
        prod = product_ruleset(checks=("product-ci", *ENGINE))   # post-augment: engine checks + (nff) present
        fake = AugmentGitHub(products=[prod])
        r = cp(fake).de_bootstrap(marker=self.AUG, announce=quiet)
        self.assertEqual(r["status"], "unaugmented")
        self.assertEqual(fake.checks_of(9), {"product-ci"})              # engine checks gone, theirs kept
        self.assertNotIn("non_fast_forward", fake.types_of(9))           # engine-added rule removed
        self.assertIn("pull_request", fake.types_of(9))                 # their rule untouched
        self.assertNotIn("DELETE", [c[0] for c in fake.writes()])      # never deletes a product rule

    def test_preserves_operator_bypass_on_strip(self):
        prod = product_ruleset(checks=("product-ci", *ENGINE))
        fake = AugmentGitHub(products=[prod])
        cp(fake).de_bootstrap(marker=self.AUG, announce=quiet)
        self.assertEqual(fake.rulesets[9]["bypass_actors"], prod["bypass_actors"])

    def test_marker_absent_falls_back_to_bounded_name_strip(self):
        prod = product_ruleset(checks=("product-ci", *ENGINE), with_nff=True)
        fake = AugmentGitHub(products=[prod])
        r = cp(fake).de_bootstrap(marker=None, announce=quiet)
        self.assertEqual(r["status"], "unaugmented")
        self.assertEqual(fake.checks_of(9), {"product-ci"})            # only the frozen engine names stripped
        self.assertIn("non_fast_forward", fake.types_of(9))            # NOT removed (authorship unknown)
        self.assertNotIn("DELETE", [c[0] for c in fake.writes()])

    def test_marker_absent_and_no_engine_checks_is_no_rule(self):
        fake = AugmentGitHub(products=[product_ruleset(checks=("product-ci",))])
        r = cp(fake).de_bootstrap(marker=None, announce=quiet)
        self.assertEqual(r["status"], "no-rule")
        self.assertEqual([c for c in fake.writes()], [])              # nothing of the operator's touched


if __name__ == "__main__":
    unittest.main()
