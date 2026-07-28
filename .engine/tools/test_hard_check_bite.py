"""Tests for the negative-fixture meta-check (#286) — hard_check_bite_check.

The meta-check is LIVE in the CI suite (S5): its rule ships suites: ["CI"], so it runs at the merge gate. These
tests drive the testable `evaluate(...)` core against controlled rosters, drive the whole script through the real
`validate.run_unit` subprocess path for self-coverage, and assert the FULL live roster (every committed fixture +
the self-entry via its committed target.json) is clean — the go-live regression. Assertions are by set-membership
on (severity, message token), never order/count.
"""
from __future__ import annotations
import glob as _glob
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate  # noqa: E402
import repo_identity  # noqa: E402  (the shared origin==home seam the harness gates on)
import hard_check_bite_check as hcb  # noqa: E402

ROOT = validate.ROOT
LIVE_FIXTURES = os.path.join(ROOT, ".engine", "_fixtures")
CHECK_DIR = os.path.join(ROOT, ".engine", "check")
RULE_PATH = os.path.join(ROOT, ".engine", "check", "hard-check-bite.json")
CLOSED_KINDS = sorted(k for k in validate.REGISTRY if k != "custom/script")

# The harness reads the shared origin==home seam, so a fixture root is PLACED by its git origin + recorded
# home. HOME_URL -> the engine's own home repo; ADOPTER_URL -> a downstream copy.
HOME_SLUG = "StarshipSuperjam/engine-template"
HOME_URL = f"https://github.com/{HOME_SLUG}.git"
ADOPTER_URL = "https://github.com/adopter/their-product.git"


def _git(root: str, *args: str) -> None:
    subprocess.run(["git", "-C", root, *args], capture_output=True, text=True, check=False)


def _placed_root(origin: "str | None", home: str = HOME_SLUG) -> str:
    """A throwaway git checkout with an `origin` remote (omitted when None) and a manifest recording `home`, so
    `repo_identity.is_home_repo(root)` can place it. `origin=None` leaves the origin unreadable -> fails toward
    home (the safe direction the checks and the harness share)."""
    root = tempfile.mkdtemp()
    _git(root, "init", "-q")
    if origin:
        _git(root, "remote", "add", "origin", origin)
    os.makedirs(os.path.join(root, ".engine"), exist_ok=True)
    with open(os.path.join(root, ".engine", "engine.json"), "w", encoding="utf-8") as fh:
        json.dump({"engine_release": "0.0.0", "home_repository": home}, fh)
    return root


def _live_hard_script_rules() -> list:
    """Every live hard custom/script rule (the meta-check's roster(b) population)."""
    rules = []
    for rp in sorted(_glob.glob(os.path.join(CHECK_DIR, "*.json"))):
        r = validate.load_json(rp)
        if r.get("kind") == "custom/script" and r.get("tier") == "hard":
            rules.append(r)
    return rules


def _write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


class TestLiveFixturesBite(unittest.TestCase):
    """The committed fixtures all bite — the real-repo regression. An empty check_dir keeps roster(b) out (the
    live custom/script instances are backfilled in a later slice), so this proves roster(a) + the custom/script
    fail-closed modes against the shipped fixtures."""

    def test_every_kind_and_failclosed_mode_bites(self):
        with tempfile.TemporaryDirectory() as empty:
            found = hcb.evaluate(check_dir=empty, fixture_root=LIVE_FIXTURES)
        self.assertEqual(found, [], f"a committed fixture failed to bite: {found}")

    def test_each_closed_kind_bites_individually(self):
        # Per-kind, so a regression localizes to the kind whose fixture stopped biting.
        for kind in CLOSED_KINDS:
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as empty:
                found = hcb.evaluate(check_dir=empty, fixture_root=LIVE_FIXTURES, kinds=[kind])
                self.assertEqual(found, [], f"the '{kind}' fixture did not bite")

    def test_custom_script_failclosed_modes_bite(self):
        with tempfile.TemporaryDirectory() as empty:
            found = hcb.evaluate(check_dir=empty, fixture_root=LIVE_FIXTURES, kinds=["custom/script"])
        self.assertEqual(found, [])


class TestMissingAndNonBiting(unittest.TestCase):
    """The two failure modes the meta-check must itself catch: a unit with no fixture, and a unit whose
    fixture does not bite."""

    def test_missing_fixture_fails_closed(self):
        with tempfile.TemporaryDirectory() as empty_fix, tempfile.TemporaryDirectory() as empty_chk:
            found = hcb.evaluate(check_dir=empty_chk, fixture_root=empty_fix, kinds=["presence"])
        self.assertTrue(any(f["severity"] == "hard" and "no negative test fixture" in f["message"]
                            for f in found), found)

    def test_non_biting_fixture_fails_closed(self):
        # A presence fixture whose input is COMPLETE (the check will not fire) but whose expect.json demands a
        # bite → the meta-check must report it did not catch.
        with tempfile.TemporaryDirectory() as fix, tempfile.TemporaryDirectory() as chk:
            fdir = os.path.join(fix, "kind-presence")
            _write(os.path.join(fdir, "rule.json"), json.dumps(
                {"id": "f/p", "kind": "presence", "tier": "hard", "target": {},
                 "params": {"sections": ["Purpose"]}, "message": "m"}))
            _write(os.path.join(fdir, "input.md"), "## Purpose\n\nComplete — nothing missing.\n")
            _write(os.path.join(fdir, "expect.json"),
                   json.dumps({"severity": "hard", "message_contains": "Required section"}))
            found = hcb.evaluate(check_dir=chk, fixture_root=fix, kinds=["presence"])
        self.assertTrue(any(f["severity"] == "hard" and "did NOT catch" in f["message"] for f in found), found)


class TestNotApplicableCarveOut(unittest.TestCase):
    """The only admissible carve-out is the exact bounded property; anything else is rejected (no silent
    self-classification)."""

    def _scenario(self, disclosure: dict):
        fix = tempfile.mkdtemp()
        fdir = os.path.join(fix, "kind-presence")
        _write(os.path.join(fdir, "not-applicable.json"), json.dumps(disclosure))
        return fix

    def test_exact_property_is_honored_as_a_soft_note(self):
        fix = self._scenario({"property": hcb._NA_PROPERTY, "reason": "presence has a CI failure path; this is a test."})
        with tempfile.TemporaryDirectory() as chk:
            found = hcb.evaluate(check_dir=chk, fixture_root=fix, kinds=["presence"])
        self.assertTrue(any(f["severity"] == "soft" and "NOT APPLICABLE" in f["message"] for f in found))
        self.assertFalse(any(f["severity"] == "hard" for f in found))

    def test_wrong_property_is_rejected_hard(self):
        fix = self._scenario({"property": "too-hard-to-write-a-fixture", "reason": "lazy"})
        with tempfile.TemporaryDirectory() as chk:
            found = hcb.evaluate(check_dir=chk, fixture_root=fix, kinds=["presence"])
        self.assertTrue(any(f["severity"] == "hard" and "only admissible reason" in f["message"] for f in found))


class TestRosterBInstances(unittest.TestCase):
    """Roster (b): every custom/script INSTANCE in the check directory is proven, by id-stem fixture."""

    def test_instance_with_no_fixture_fails_closed(self):
        with tempfile.TemporaryDirectory() as chk, tempfile.TemporaryDirectory() as fix:
            _write(os.path.join(chk, "foo.json"), json.dumps(
                {"id": "engine/check/foo", "kind": "custom/script", "tier": "hard", "target": {"context": "x"},
                 "params": {"script": ".engine/tools/validate.py"}, "suites": [], "message": "m"}))
            found = hcb.evaluate(check_dir=chk, fixture_root=fix, kinds=[])
        self.assertTrue(any(f["severity"] == "hard" and "engine/check/foo" in f["message"]
                            and "no negative test fixture" in f["message"] for f in found), found)

    def test_instance_bites_its_fixture(self):
        # A controlled instance whose script is a committed fail-closed fixture script (it exits non-zero), with
        # an id-stem fixture asserting that token → the instance bites. Proves roster(b) witnesses a real bite.
        crash = ".engine/_fixtures/kind-custom-script/nonzero/crash.py"
        with tempfile.TemporaryDirectory() as chk, tempfile.TemporaryDirectory() as fix:
            _write(os.path.join(chk, "bar.json"), json.dumps(
                {"id": "engine/check/bar", "kind": "custom/script", "tier": "hard", "target": {"context": "x"},
                 "params": {"script": crash}, "suites": [], "message": "m"}))
            _write(os.path.join(fix, "bar", "expect.json"),
                   json.dumps({"severity": "hard", "message_contains": "exited with an error"}))
            found = hcb.evaluate(check_dir=chk, fixture_root=fix, kinds=[])
        self.assertEqual(found, [], f"the instance fixture did not bite: {found}")


class TestSelfCoverageThroughSubprocess(unittest.TestCase):
    """The meta-check runs as one more roster entry through the REAL run_unit subprocess path, pointed at its
    committed self-fixture mini-scenario — proving self-falsifiability the way it runs live (S5), and that the
    run terminates (the mini-scenario contains no custom/script instance and not the meta-check itself). It drives
    the SAME committed target.json the live self-entry uses, so the test and the live configuration cannot drift."""

    def test_meta_check_bites_its_own_self_fixture_via_committed_target(self):
        rule = validate.load_json(RULE_PATH)
        target = validate.load_json(os.path.join(LIVE_FIXTURES, "hard-check-bite", "target.json"))
        passed, found = validate.run_unit(rule, target, {})
        self.assertFalse(passed)
        self.assertTrue(any(f["severity"] == "hard" and "did NOT catch" in f["message"] for f in found), found)

    def test_committed_roster_dir_is_instance_free(self):
        """The committed ENGINE_ROSTER_DIR must hold no top-level *.json — the termination guarantee rests on it,
        so a stray check file dropped there (which would re-enter the self-run) is caught here, not in CI."""
        target = validate.load_json(os.path.join(LIVE_FIXTURES, "hard-check-bite", "target.json"))
        roster_dir = os.path.join(ROOT, target["env"]["ENGINE_ROSTER_DIR"])
        self.assertEqual(_glob.glob(os.path.join(roster_dir, "*.json")), [],
                         "the self-run roster dir must stay free of top-level *.json (see its README)")


class TestRuleIsLiveAndWellFormed(unittest.TestCase):
    """The committed rule is the LIVE (suites: ["CI"]) custom/script meta-check, conforming to check.v1.json."""

    def test_rule_conforms_and_is_live(self):
        from jsonschema import Draft202012Validator
        rule = validate.load_json(RULE_PATH)
        schema = validate.load_json(os.path.join(ROOT, ".engine", "schemas", "check.v1.json"))
        self.assertEqual(list(Draft202012Validator(schema).iter_errors(rule)), [])
        self.assertEqual(rule["suites"], ["CI"])
        self.assertEqual(rule["kind"], "custom/script")
        self.assertEqual(rule["params"]["script"], ".engine/tools/hard_check_bite_check.py")
        # S6: the meta-check is a TOKEN CONDUIT. It makes no API call itself, but it must pass GITHUB_TOKEN
        # to the disposition-issue-resolution grandchild it witnesses live (kind_custom_script strips the token
        # on every hop unless the rule sets pass_token). Do not "clean this up" — without it the disposition
        # unit runs token-less, emits unevaluable instead of the aimed unresolved bite, and this meta-check reds.
        self.assertTrue(rule["params"]["pass_token"])


class TestS4LiveRosterBackfill(unittest.TestCase):
    """Real-repo regression: every in-scope hard custom/script INSTANCE either bites its committed fixture or is
    honored as a disclosed not-applicable carve-out — the roster is whole. As of S5 the meta-check's own self-entry
    is INCLUDED: it flows through the generic fixture path, loading its committed target.json so the self-run is
    pointed at its mini-scenario (and terminates) rather than re-entering the live roster."""

    def test_every_hard_instance_bites_or_is_disclosed_na(self):
        for rule in _live_hard_script_rules():
            stem = rule["id"].split("engine/check/")[-1]
            na = os.path.join(LIVE_FIXTURES, stem, "not-applicable.json")
            with self.subTest(check=stem):
                if os.path.isfile(na):
                    found = hcb._cover_script_instance(rule, LIVE_FIXTURES, ROOT, "hard")
                    self.assertTrue(found and all(f["severity"] == "soft" for f in found),
                                    f"{stem}: a not-applicable carve-out must yield only soft notes: {found}")
                    self.assertTrue(any("NOT APPLICABLE" in (f.get("message") or "") for f in found), found)
                elif stem == "disposition-issue-resolution":
                    # The roster's first NON-OFFLINE unit (#292): its negative path is a live issue-API query
                    # against the cited sentinel, witnessable only with a repository + token. THREE ambient
                    # environments, asserted rather than skipped: fully-witnessed -> the live bite; CI without
                    # the token (this repo's own self-test step — the workflow passes the token only to the
                    # validator step, the enforcing surface) -> the carve-out is deliberately ignored and the
                    # fail-closed hard finding stands; a local machine missing the variables -> the
                    # declared-environment carve-out (#531) collapses the red to a loud soft note.
                    found = hcb._cover_script_instance(rule, LIVE_FIXTURES, ROOT, "hard")
                    witnessed = os.environ.get("GITHUB_REPOSITORY") and os.environ.get("GITHUB_TOKEN")
                    in_ci = bool(os.environ.get("GITHUB_ACTIONS") or os.environ.get("CI"))
                    if witnessed:
                        self.assertEqual(found, [], f"{stem}: the live witness did not bite")
                    elif in_ci:
                        self.assertTrue(any(f["severity"] == "hard" and "did NOT catch" in f["message"]
                                            for f in found), found)
                    else:
                        self.assertTrue(found and all(f["severity"] == "soft" for f in found), found)
                        self.assertTrue(any("NOT WITNESSED HERE" in (f.get("message") or "") for f in found),
                                        found)
                elif stem == "census-completeness":
                    # Construction-scoped (#512): required to bite here in the construction repo; in a
                    # deployed repo the ambient-verified carve-out yields the loud NOT APPLICABLE HERE note.
                    found = hcb._cover_script_instance(rule, LIVE_FIXTURES, ROOT, "hard")
                    if repo_identity.is_home_repo(ROOT):
                        self.assertEqual(found, [], f"{stem}: did not bite in the construction repo")
                    else:
                        self.assertTrue(found and all(f["severity"] == "soft" for f in found), found)
                        self.assertTrue(any("NOT APPLICABLE HERE" in (f.get("message") or "") for f in found),
                                        found)
                elif stem == "memory-pointer-public-safety":
                    # Construction-scoped (#512) AND it reads its fixture via `git show HEAD:`, so in the
                    # construction repo it bites only once the fixture is committed at HEAD; in a deployed
                    # repo the ambient-verified carve-out yields the loud NOT APPLICABLE HERE note.
                    if not repo_identity.is_home_repo(ROOT):
                        found = hcb._cover_script_instance(rule, LIVE_FIXTURES, ROOT, "hard")
                        self.assertTrue(found and all(f["severity"] == "soft" for f in found), found)
                        self.assertTrue(any("NOT APPLICABLE HERE" in (f.get("message") or "") for f in found),
                                        found)
                        continue
                    committed = subprocess.run(
                        ["git", "cat-file", "-e",
                         "HEAD:.engine/_fixtures/memory-pointer-public-safety/pointer.json"],
                        cwd=ROOT, capture_output=True).returncode == 0
                    if not committed:
                        self.skipTest("memory-pointer fixture not yet committed at HEAD (git show HEAD: read)")
                    self.assertEqual(hcb._cover_script_instance(rule, LIVE_FIXTURES, ROOT, "hard"), [])
                else:
                    self.assertEqual(hcb._cover_script_instance(rule, LIVE_FIXTURES, ROOT, "hard"), [],
                                     f"{stem}: did not bite its committed fixture")


class TestS5GoLive(unittest.TestCase):
    """The go-live regression: the meta-check, run over the FULL LIVE roster with no overrides (every committed
    fixture, every disclosed carve-out, AND its own self-entry via the committed target.json), is clean. This is
    what runs at the merge gate now that the rule is in the CI suite — proving the live check is green over the
    real repo and that the self-entry both terminates and is non-vacuous through the real subprocess path."""

    def test_full_live_roster_is_clean(self):
        # The self-entry is in the live roster and is exercised here through its committed target.json.
        stems = {r["id"].split("engine/check/")[-1] for r in _live_hard_script_rules()}
        self.assertIn("hard-check-bite", stems, "the meta-check must be in its own live roster")
        # The full evaluate() includes the NON-OFFLINE disposition-issue-resolution unit (#292), whose live
        # witness needs a repository + token. Three ambient environments (see the S4 disposition branch):
        # fully-witnessed and plain-local runs produce no hard finding; CI WITHOUT the token (this repo's own
        # self-test step) keeps exactly the disposition fail-closed hard — the carve-out is deliberately
        # ignored there, and the enforcing validator step (which has the token) witnesses the live bite.
        findings = hcb.evaluate()
        # A merge is blocked only by a HARD finding; the disclosed carve-outs are loud SOFT notes by design.
        hard = [f for f in findings if f["severity"] == "hard"]
        witnessed = os.environ.get("GITHUB_REPOSITORY") and os.environ.get("GITHUB_TOKEN")
        in_ci = bool(os.environ.get("GITHUB_ACTIONS") or os.environ.get("CI"))
        if witnessed or not in_ci:
            self.assertEqual(hard, [], "the live meta-check must produce no hard finding over the real repo "
                             "(every covered check bites or is a disclosed carve-out, and the self-entry "
                             "terminates)")
        else:
            self.assertEqual(len(hard), 1, hard)
            self.assertIn("disposition-issue-resolution", hard[0]["message"])
        # The carve-outs are surfaced loudly so the reviewer can re-derive them at the gate (not silently
        # skipped). The 4 static N/A disclosures are a fixed census; in a DEPLOYED repo (root CLAUDE.md no
        # longer the construction body) the two construction-scoped checks (#512) add their ambient-derived
        # NOT APPLICABLE HERE notes on top.
        na_notes = [f for f in findings if "NOT APPLICABLE" in (f.get("message") or "")]
        expected_na = 4 if repo_identity.is_home_repo(ROOT) else 6
        self.assertEqual(len(na_notes), expected_na,
                         f"expected {expected_na} disclosed N/A notes to be surfaced, got: {na_notes}")


class TestS4TierFilter(unittest.TestCase):
    """Roster(b) is scoped to HARD instances: a soft
    custom/script with no fixture is NOT enumerated — so a soft no-op is never escalated to a hard 'missing
    fixture' meta-finding — while a hard one with no fixture still fails closed."""

    def _rule(self, tier: str) -> dict:
        return {"id": f"engine/check/x-{tier}", "kind": "custom/script", "tier": tier,
                "target": {"context": "x"}, "params": {"script": ".engine/tools/validate.py"},
                "suites": [], "message": "m"}

    def test_soft_instance_without_fixture_yields_no_finding(self):
        with tempfile.TemporaryDirectory() as chk, tempfile.TemporaryDirectory() as fix:
            _write(os.path.join(chk, "x-soft.json"), json.dumps(self._rule("soft")))
            found = hcb.evaluate(check_dir=chk, fixture_root=fix, kinds=[])
        self.assertEqual(found, [], f"a soft instance must not be enumerated: {found}")

    def test_hard_instance_without_fixture_fails_closed(self):
        with tempfile.TemporaryDirectory() as chk, tempfile.TemporaryDirectory() as fix:
            _write(os.path.join(chk, "x-hard.json"), json.dumps(self._rule("hard")))
            found = hcb.evaluate(check_dir=chk, fixture_root=fix, kinds=[])
        self.assertTrue(any(f["severity"] == "hard" and "no negative test fixture" in f["message"]
                            for f in found), found)


class TestS4NotApplicableDisclosuresAreArgued(unittest.TestCase):
    """Each shipped N/A disclosure carries the exact locked property AND a reason that argues it (names the
    live external substrate and the false-witness point) — not a bare category assertion."""

    NA_CHECKS = ("protection", "dependency-review", "guardrail-weakening", "product-lock-integrity")

    def test_each_na_disclosure_is_honored_and_reasoned(self):
        for stem in self.NA_CHECKS:
            with self.subTest(check=stem):
                disclosure = validate.load_json(os.path.join(LIVE_FIXTURES, stem, "not-applicable.json"))
                self.assertEqual(disclosure["property"], hcb._NA_PROPERTY)
                reason = disclosure.get("reason", "")
                self.assertIn("false witness", reason.lower(),
                              f"{stem}: the reason must argue why fixturing the fail-closed path is a false witness")


class TestEnvOverridePathSeam(unittest.TestCase):
    """The one shared seam helper (validate.env_override_path): UNSET -> the caller's default (production
    byte-unchanged); a relative value -> resolved under ROOT; an absolute value -> used as-is."""

    VAR = "ENGINE_TEST_SEAM_VAR_DO_NOT_SET"

    def tearDown(self):
        os.environ.pop(self.VAR, None)

    def test_unset_returns_default(self):
        os.environ.pop(self.VAR, None)
        self.assertIsNone(validate.env_override_path(self.VAR))
        self.assertEqual(validate.env_override_path(self.VAR, "/x/default"), "/x/default")

    def test_relative_resolves_under_root(self):
        os.environ[self.VAR] = ".engine/foo.json"
        self.assertEqual(validate.env_override_path(self.VAR), os.path.join(validate.ROOT, ".engine/foo.json"))

    def test_absolute_used_as_is(self):
        os.environ[self.VAR] = "/abs/path.json"
        self.assertEqual(validate.env_override_path(self.VAR), "/abs/path.json")


class TestModuleKindBite(unittest.TestCase):
    """Leg 3 (#405): a module-provided kind is rostered from the same resolved registry the dispatcher reads, and
    its bite is proven by a GENERIC driver (the fixture declares its own target) — not the closed-kind driver,
    which would raise on a non-core kind. Proven with a synthetic kind in a temp dir via the ENGINE_KIND_DIR seam;
    a present kind with no fixture fails closed."""

    _KIND = (
        "def check(rule, ctx):\n"
        "    if ctx.get('value') == 'bad':\n"
        "        return False, [{'severity': 'hard', 'message': 'foo caught bad input', 'location': None}]\n"
        "    return True, []\n"
    )

    def _kind_dir(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        d = os.path.join(tmp, "mymod")
        os.makedirs(d)
        _write(os.path.join(d, "kind_foo.py"), self._KIND)
        return tmp

    def test_discovered_kind_is_rostered_and_its_fixture_bites(self):
        kind_dir = self._kind_dir()
        with tempfile.TemporaryDirectory() as fix, tempfile.TemporaryDirectory() as chk:
            fdir = os.path.join(fix, "kind-foo")
            _write(os.path.join(fdir, "rule.json"),
                   json.dumps({"id": "f/foo", "kind": "foo", "tier": "hard", "message": "m"}))
            _write(os.path.join(fdir, "target.json"), json.dumps({"ctx": {"value": "bad"}}))
            _write(os.path.join(fdir, "expect.json"),
                   json.dumps({"severity": "hard", "message_contains": "foo caught bad input"}))
            with mock.patch.dict(os.environ, {"ENGINE_KIND_DIR": kind_dir}):
                # The kind IS in the resolved roster (evaluate's default registry = resolved_registry())...
                self.assertIn("foo", validate.resolved_registry())
                found = hcb.evaluate(check_dir=chk, fixture_root=fix, kinds=["foo"])
        self.assertEqual(found, [], f"the discovered kind's fixture should bite green: {found}")

    def test_discovered_kind_missing_fixture_fails_closed(self):
        kind_dir = self._kind_dir()
        with tempfile.TemporaryDirectory() as empty_fix, tempfile.TemporaryDirectory() as chk, \
                mock.patch.dict(os.environ, {"ENGINE_KIND_DIR": kind_dir}):
            found = hcb.evaluate(check_dir=chk, fixture_root=empty_fix, kinds=["foo"])
        self.assertTrue(any(f["severity"] == "hard" and "no negative test fixture" in f["message"]
                            for f in found), found)

    def test_discovered_kind_non_biting_fixture_fails_closed(self):
        kind_dir = self._kind_dir()
        with tempfile.TemporaryDirectory() as fix, tempfile.TemporaryDirectory() as chk:
            fdir = os.path.join(fix, "kind-foo")
            _write(os.path.join(fdir, "rule.json"),
                   json.dumps({"id": "f/foo", "kind": "foo", "tier": "hard", "message": "m"}))
            _write(os.path.join(fdir, "target.json"), json.dumps({"ctx": {"value": "fine"}}))  # not bad -> no bite
            _write(os.path.join(fdir, "expect.json"),
                   json.dumps({"severity": "hard", "message_contains": "foo caught bad input"}))
            with mock.patch.dict(os.environ, {"ENGINE_KIND_DIR": kind_dir}):
                found = hcb.evaluate(check_dir=chk, fixture_root=fix, kinds=["foo"])
        self.assertTrue(any(f["severity"] == "hard" and "did NOT catch" in f["message"] for f in found), found)

    def test_live_roster_is_unchanged_when_no_module_kind_is_present(self):
        # With no ENGINE_KIND_DIR, the meta-check rosters exactly the core kinds + custom/script — no module kind.
        env = {k: v for k, v in os.environ.items() if k != "ENGINE_KIND_DIR"}
        with mock.patch.dict(os.environ, env, clear=True):
            roster = sorted(validate.resolved_registry())
        self.assertEqual(roster, sorted(validate.REGISTRY))


class TestFailedBiteApplicability(unittest.TestCase):
    """The two bounded failed-bite carve-outs (#512 construction-scoped, #531 declared environment): honored only
    where ambient state confirms them, rejected on any malformed declaration, and inert wherever the failure path
    is reachable. Driven through the REAL _cover_script_instance -> run_unit subprocess path, using the real
    disposition script env-less (it exits 0 with its honest no-op finding — a genuine run-but-no-bite unit)."""

    _RULE = {"id": "engine/check/x-live", "kind": "custom/script", "tier": "hard",
             "target": {"context": "x"},
             "params": {"script": ".engine/tools/disposition_issue_resolution_check.py"},
             "suites": [], "message": "m"}

    def _fixture(self, declarations: dict) -> str:
        fix = tempfile.mkdtemp()
        fdir = os.path.join(fix, "x-live")
        _write(os.path.join(fdir, "expect.json"),
               json.dumps({"severity": "hard", "message_contains": "token-this-unit-never-produces"}))
        for name, data in declarations.items():
            _write(os.path.join(fdir, name), json.dumps(data))
        return fix

    def _root(self, *, origin: "str | None") -> str:
        root = _placed_root(origin)
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        return root

    def _cover(self, fix: str, root: str, extra_env: "dict | None" = None):
        # A clean ambient slate: no repo/token (the unit must not bite) and no CI markers unless seeded.
        env = {k: v for k, v in os.environ.items()
               if k not in ("GITHUB_REPOSITORY", "GITHUB_TOKEN", "GITHUB_ACTIONS", "CI")}
        env.update(extra_env or {})
        with mock.patch.dict(os.environ, env, clear=True):
            return hcb._cover_script_instance(self._RULE, fix, root, "hard")

    def test_construction_scoped_yields_loud_na_outside_the_construction_repo(self):
        fix = self._fixture({"construction-scoped.json": {"property": hcb._CS_PROPERTY, "reason": "test."}})
        found = self._cover(fix, self._root(origin=ADOPTER_URL))
        self.assertTrue(found and all(f["severity"] == "soft" for f in found), found)
        self.assertTrue(any("NOT APPLICABLE HERE" in f["message"] for f in found), found)

    def test_construction_scoped_is_inert_in_the_construction_repo(self):
        fix = self._fixture({"construction-scoped.json": {"property": hcb._CS_PROPERTY, "reason": "test."}})
        found = self._cover(fix, self._root(origin=HOME_URL))
        self.assertTrue(any(f["severity"] == "hard" and "did NOT catch" in f["message"] for f in found), found)

    def test_construction_scoped_wrong_property_is_rejected_hard(self):
        fix = self._fixture({"construction-scoped.json": {"property": "only-runs-here", "reason": "lazy"}})
        found = self._cover(fix, self._root(origin=ADOPTER_URL))
        self.assertTrue(any(f["severity"] == "hard" and "only admissible reason" in f["message"]
                            for f in found), found)

    def test_unreadable_origin_reads_as_home_so_the_exemption_is_inert(self):
        # #323 fail-direction: a root whose git origin can't be read is treated as HOME (is_home_repo fails
        # toward home) — the SAME direction the checks' own gate takes, so harness and checks never disagree.
        # The construction-scoped exemption is therefore inert here and the failed bite stands as a hard finding.
        fix = self._fixture({"construction-scoped.json": {"property": hcb._CS_PROPERTY, "reason": "test."}})
        found = self._cover(fix, self._root(origin=None))
        self.assertTrue(any(f["severity"] == "hard" and "did NOT catch" in f["message"] for f in found), found)

    def test_requires_env_missing_locally_yields_loud_soft_note(self):
        fix = self._fixture({"requires.json": {"property": hcb._REQ_PROPERTY,
                                               "env": ["GITHUB_TOKEN"], "reason": "test."}})
        found = self._cover(fix, self._root(origin=ADOPTER_URL))
        self.assertTrue(found and all(f["severity"] == "soft" for f in found), found)
        self.assertTrue(any("NOT WITNESSED HERE" in f["message"] and "GITHUB_TOKEN" in f["message"]
                            for f in found), found)

    def test_requires_is_ignored_in_ci(self):
        fix = self._fixture({"requires.json": {"property": hcb._REQ_PROPERTY,
                                               "env": ["GITHUB_TOKEN"], "reason": "test."}})
        found = self._cover(fix, self._root(origin=ADOPTER_URL), extra_env={"GITHUB_ACTIONS": "true"})
        self.assertTrue(any(f["severity"] == "hard" and "did NOT catch" in f["message"] for f in found), found)

    def test_requires_env_present_falls_through_to_the_failed_bite(self):
        fix = self._fixture({"requires.json": {"property": hcb._REQ_PROPERTY,
                                               "env": ["GITHUB_TOKEN"], "reason": "test."}})
        found = self._cover(fix, self._root(origin=ADOPTER_URL), extra_env={"GITHUB_TOKEN": "x"})
        self.assertTrue(any(f["severity"] == "hard" and "did NOT catch" in f["message"] for f in found), found)

    def test_requires_malformed_env_list_is_rejected_hard(self):
        fix = self._fixture({"requires.json": {"property": hcb._REQ_PROPERTY,
                                               "env": "GITHUB_TOKEN", "reason": "not a list"}})
        found = self._cover(fix, self._root(origin=ADOPTER_URL))
        self.assertTrue(any(f["severity"] == "hard" and "must name the environment variables" in f["message"]
                            for f in found), found)

    def test_requires_wrong_property_is_rejected_hard(self):
        fix = self._fixture({"requires.json": {"property": "needs-the-internet",
                                               "env": ["GITHUB_TOKEN"], "reason": "lazy"}})
        found = self._cover(fix, self._root(origin=ADOPTER_URL))
        self.assertTrue(any(f["severity"] == "hard" and "only admissible reason" in f["message"]
                            for f in found), found)

    def test_non_object_declaration_is_rejected_hard_not_a_crash(self):
        fix = self._fixture({"construction-scoped.json": ["not", "an", "object"]})
        found = self._cover(fix, self._root(origin=ADOPTER_URL))
        self.assertTrue(any(f["severity"] == "hard" and "not a JSON object" in f["message"]
                            for f in found), found)

    def test_unreadable_declaration_is_rejected_hard(self):
        fix = tempfile.mkdtemp()
        fdir = os.path.join(fix, "x-live")
        _write(os.path.join(fdir, "expect.json"),
               json.dumps({"severity": "hard", "message_contains": "token-this-unit-never-produces"}))
        _write(os.path.join(fdir, "construction-scoped.json"), "{not valid json")
        found = self._cover(fix, self._root(origin=ADOPTER_URL))
        self.assertTrue(any(f["severity"] == "hard" and "is unreadable" in f["message"]
                            for f in found), found)

    def test_construction_root_falls_through_to_the_requires_declaration(self):
        # Both declarations present, construction root: the construction-scoped one is inert here, and the
        # declared-environment one still applies (env missing, not CI) — the documented fall-through.
        fix = self._fixture({
            "construction-scoped.json": {"property": hcb._CS_PROPERTY, "reason": "test."},
            "requires.json": {"property": hcb._REQ_PROPERTY, "env": ["GITHUB_TOKEN"], "reason": "test."}})
        found = self._cover(fix, self._root(origin=HOME_URL))
        self.assertTrue(found and all(f["severity"] == "soft" for f in found), found)
        self.assertTrue(any("NOT WITNESSED HERE" in f["message"] for f in found), found)

    def test_a_crashing_check_is_never_excused_as_inert(self):
        # The masking hunt: a unit whose run produced a HARD finding (here: a script that emits no findings
        # array, so the kind fail-closes) is evidence of a live, broken check — a valid construction-scoped
        # declaration in a deployed-shape repo must NOT collapse that to the soft "structurally inert" note.
        rule = dict(self._RULE)
        rule["params"] = {"script": ".engine/tools/validate.py"}   # runs, but emits no findings JSON
        fix = self._fixture({"construction-scoped.json": {"property": hcb._CS_PROPERTY, "reason": "test."}})
        env = {k: v for k, v in os.environ.items()
               if k not in ("GITHUB_REPOSITORY", "GITHUB_TOKEN", "GITHUB_ACTIONS", "CI")}
        with mock.patch.dict(os.environ, env, clear=True):
            found = hcb._cover_script_instance(rule, fix, self._root(origin=ADOPTER_URL), "hard")
        self.assertTrue(any(f["severity"] == "hard" for f in found), found)
        self.assertFalse(any("NOT APPLICABLE HERE" in (f.get("message") or "") for f in found), found)


class TestDeclarationCensus(unittest.TestCase):
    """Drift canary (#512/#531 review): the bite-harness applicability declarations are exemption-granting
    data, and a NEW one arrives as a file addition — a change class the weakening guard structurally cannot
    flag (a pure addition never trips it). Binding the exact shipped set here makes any new declaration a
    visible, reviewable test edit instead of a quiet file drop."""

    @unittest.skipUnless(
        repo_identity.is_home_repo(ROOT),
        "construction-repo drift canary: it pins the SOURCE's exact declaration set so a newly-authored declaration "
        "is a visible test edit. It is scoped to the construction checkout because a deployed repo does not author "
        "these declarations, and a deployed repo's fixture surface can differ from the source's (installed modules, "
        "a fork-native product's own checks, accumulated state), so pinning the exact source set is not meaningful "
        "there. A fresh deployed repo carries the same committed declarations; this guard is the honest scope, not a "
        "claim that the set changes on deploy.")
    def test_exact_shipped_declaration_set(self):
        found = sorted(
            os.path.relpath(p, ROOT)
            for name in ("not-applicable.json", "construction-scoped.json", "requires.json")
            for p in _glob.glob(os.path.join(LIVE_FIXTURES, "*", name)))
        self.assertEqual(found, [
            ".engine/_fixtures/census-completeness/construction-scoped.json",
            ".engine/_fixtures/dependency-review/not-applicable.json",
            ".engine/_fixtures/disposition-issue-resolution/requires.json",
            ".engine/_fixtures/guardrail-weakening/not-applicable.json",
            ".engine/_fixtures/memory-pointer-public-safety/construction-scoped.json",
            ".engine/_fixtures/product-lock-integrity/not-applicable.json",
            ".engine/_fixtures/protection/not-applicable.json",
        ])


class TestLiveDeclarationsAreHonored(unittest.TestCase):
    """The COMMITTED construction-scoped declarations, driven through the real applicability path.

    This is the ONLY fast-suite enforcement of the two byte contracts a declaration rests on: the filename
    `_failed_bite_applicability` opens, and the exact `property` string it demands. Neither is otherwise
    exercised where the engine is developed — `_cover_script_instance` returns at its successful-bite branch
    BEFORE consulting a declaration, and both home-scoped checks DO bite in the engine's own home repo, so the
    committed files are never opened there. Every other test in this module builds a SYNTHETIC declaration out
    of `hcb._CS_PROPERTY`, which stays self-consistent for any value of it. Without this class, a desync between
    the constant and the shipped files reads green in the engine's home and lands as a hard red in every
    deployed repo — the only place the contract actually runs.

    Reads no environment: the construction-scoped branch consults none, so this is deterministic offline.
    """

    def _dirs(self) -> list:
        """The committed fixture dirs carrying the declaration — DERIVED, never pinned. A deployed repo's
        fixture surface can differ from the source's, and this must stay honest there rather than assert a
        source-tree set (the same scoping `TestDeclarationCensus` documents)."""
        return sorted(os.path.dirname(p)
                      for p in _glob.glob(os.path.join(LIVE_FIXTURES, "*", "construction-scoped.json")))

    def _root(self, *, origin: str) -> str:
        root = _placed_root(origin)
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        return root

    @staticmethod
    def _na(found) -> bool:
        return bool(found) and any("NOT APPLICABLE HERE" in f["message"] for f in found)

    def test_every_shipped_declaration_carries_the_exact_property(self):
        dirs = self._dirs()
        self.assertTrue(dirs, "no committed construction-scoped declaration found to check")
        for fdir in dirs:
            with self.subTest(fixture=os.path.basename(fdir)):
                data = validate.load_json(os.path.join(fdir, "construction-scoped.json"))
                self.assertEqual(
                    data.get("property"), hcb._CS_PROPERTY,
                    "a shipped declaration's property must byte-match the constant the check compares it "
                    "against; a desync is invisible here and a hard red in every deployed repo")

    def test_each_is_honored_as_a_loud_soft_note_in_a_deployed_repo(self):
        for fdir in self._dirs():
            stem = os.path.basename(fdir)
            with self.subTest(fixture=stem):
                found = hcb._failed_bite_applicability(
                    f"engine/check/{stem}", fdir, self._root(origin=ADOPTER_URL), "hard")
                self.assertIsNotNone(found, f"{stem}: the shipped declaration was not honored at all")
                self.assertTrue(all(f["severity"] == "soft" for f in found), found)
                self.assertTrue(self._na(found), found)

    def test_each_is_inert_in_the_engines_own_home_repo(self):
        # The carve-out may never excuse a check whose failure path IS reachable: in the engine's own home the
        # declaration is inert and the failed bite stands. Asserted as "no N/A note" rather than a strict None
        # so a fixture dir that also carries a requires.json stays correctly judged — and paired with the
        # adopter-root assertion, so a declaration the code cannot FIND (wrong filename) can never satisfy this
        # by returning None.
        for fdir in self._dirs():
            stem = os.path.basename(fdir)
            with self.subTest(fixture=stem):
                unit = f"engine/check/{stem}"
                self.assertTrue(
                    self._na(hcb._failed_bite_applicability(unit, fdir, self._root(origin=ADOPTER_URL), "hard")),
                    f"{stem}: the declaration must be FOUND and honored in a copy, or 'inert at home' below "
                    f"would pass merely because nothing was read")
                found = hcb._failed_bite_applicability(unit, fdir, self._root(origin=HOME_URL), "hard")
                self.assertFalse(self._na(found), found)


if __name__ == "__main__":
    unittest.main()
