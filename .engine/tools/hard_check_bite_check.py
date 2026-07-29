#!/usr/bin/env python3
"""The negative-fixture meta-check (issue #286) — the custom/script entry for
engine/check/hard-check-bite. The "checker-of-checkers".

A hard check can be present, registered, runnable, and green-on-everything while its logic is a no-op or
mis-aimed. To the maintainer at the merge, a green required check reads as *verified* — so a no-op check is
posture dressed as enforcement at the one signal he can independently corroborate. This meta-check closes that
hole: it runs each in-scope hard check against a committed, deliberately-broken **negative fixture** and confirms
the check actually CATCHES it (the check "bites"). A check that fails to bite its own fixture — or has no fixture
and no recorded reason it can't — is itself a hard finding.

How a unit is proven to bite: the meta-check runs the unit against its fixture via
`validate.run_unit` and asserts **by set-membership** that a finding of the expected severity, carrying a
distinctive token of the unit's INTENDED finding, is present — never by order or count (the finding stream is not
source-deterministic). The token (`expect.json`'s `message_contains`) is what stops a *wrong-reason* bite from
passing: a fixture that fail-closes for an unrelated reason (e.g. malformed JSON instead of a schema violation)
fires a hard finding but not the intended one, so it does not satisfy the assertion — the same "green by doing
nothing" failure this check exists to defeat, refused one level down.

The roster (the set proven):
  - the closed check KINDS — `validate.REGISTRY` minus `custom/script` — each with one fixture under
    `<fixtures>/kind-<kind>/`. A data rule of a proven kind inherits the kind's proof (so coverage is per kind,
    not per data-rule).
  - the `custom/script` KIND's three fail-closed modes (missing script / non-zero exit / unreadable output),
    under `<fixtures>/kind-custom-script/<mode>/` — there the fail-close IS the aimed behavior.
  - the `custom/script` INSTANCES — every `*.json` in the check directory whose kind is `custom/script` — each
    with a fixture under `<fixtures>/<check-id-stem>/` (the rule id minus `engine/check/`).

The only admissible carve-outs are bounded and disclosed, never an author's silent self-classification:
  - a unit with **no statically-decidable failure path in the CI environment** — disclosed by a
    `not-applicable.json` carrying that exact bounded reason, listed loudly here as a soft note;
  - a **home-scoped** unit (#512) — disclosed by a `construction-scoped.json` (a frozen format name; see the
    constant below) carrying the exact bounded reason, and honored ONLY where the ambient repo state confirms
    it: the unit is still run, and only a
    silent failed bite (no hard finding at all — a crash or fail-closed verdict is evidence, never excused) in a
    repo whose git origin is not the update home its manifest records — a copy of the engine, not its home —
    collapses to a loud soft note. In the engine's own home repo the declaration is inert and the bite stays
    required — so the declaration can never exempt a check where its failure path is reachable;
  - a unit whose bite is witnessable only with a **declared live environment** (#531) — disclosed by a
    `requires.json` naming the environment variables the witness needs. Ignored entirely in CI (where the
    declared environment is provided and the bite stays enforced); on a local machine missing one of the named
    variables, a failed bite collapses to a loud soft note instead of a false red.
The meta-check is **self-covering**: it is itself a
`custom/script` instance, and a unit test drives it against a seeded mini-scenario where a unit is missing or
non-biting, proving the checker-of-checkers is itself falsifiable — terminating the regress with no meta-meta-check.

The CORE is `evaluate(...)`, parameterized on (root, check_dir, fixture_root, registry, kinds) so a test can drive
it against a CONTROLLED roster without enumerating the live, not-yet-covered checks. `main()` binds the live
defaults and prints the finding.v1 JSON array; it reads optional env overrides (ENGINE_ROSTER_DIR /
ENGINE_FIXTURE_ROOT / ENGINE_ROSTER_KINDS) so a self-coverage run can be pointed at a mini-scenario. A crash
returns non-zero, which the kind turns into a hard fail-closed finding (a guard can never silently pass).
"""
from __future__ import annotations
import glob as _glob
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate  # noqa: E402  (finding.v1, run_unit, REGISTRY, ROOT, CHECK_DIR)
import repo_identity  # noqa: E402  (is_home_repo — the shared origin==home recognizer, #323)

_FIXTURES_REL = os.path.join(".engine", "_fixtures")
# The closed kinds drive against a single fixture by a path/data target; custom/script is covered by its three
# fail-closed modes instead (see _cover_custom_script_kind), so it is handled separately, never here.
_PATH_KINDS = {"presence", "schema", "shape"}
# The exact bounded carve-out property (fixed VERBATIM precisely so a compressed slug cannot reopen
# the self-classification escape). A not-applicable.json must carry this exact string.
_NA_PROPERTY = "no statically-decidable failure path in the CI environment"
# The home-scoped property (#512), same VERBATIM discipline. The declaration file must carry it, and is honored
# only where the ambient root confirms the repo is NOT the engine's own home.
#
# THIS FILENAME AND THIS STRING ARE A FROZEN ON-DISK FORMAT — do not "tidy" them to match the engine's prose.
# Both travel into every deployed repo, and the two arrive by DIFFERENT delivery paths: the script through the
# engine overlay, the declaration through the fixture-namespace reconcile. A repo running an engine old enough
# to predate that reconcile receives the new script and keeps the old declaration — so changing either side
# turns this check hard-red in that repo, over a file the engine itself shipped, with nothing its operator can
# do. The fixture namespace is also a never-delete carve-out of the reconcile, so a renamed file's old copy
# would strand there permanently. The engine's prose vocabulary moved on from "construction"; this format did
# not, and the mismatch is deliberate. TestLiveDeclarationsAreHonored pins both sides against the shipped files.
_HS_DECLARATION = "construction-scoped.json"
_CS_PROPERTY = "no reachable failure path outside the construction repository"
# The declared-environment property (#531), same VERBATIM discipline for the same reason: a requires.json
# must carry it alongside the named variables, so this class of declaration can no more be minted casually
# than the other two.
_REQ_PROPERTY = "the aimed bite is witnessable only with a live repository connection"
# The home recognizer is the shared origin==home seam (repo_identity.is_home_repo) — the SAME signal the two
# home-scoped checks' own `_in_home_repo()` gates on, so the harness and the checks can never disagree about
# what "the home repo" is (a disagreement would let a check no-op in a copy while the harness still demanded
# its bite — a false red). Called directly at the one site that needs it, keyed on the PASSED root so a test
# can drive both branches (the live run passes validate.ROOT).


def _load(path: str):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _bit(findings: list, expect: dict) -> bool:
    """Set-membership: a finding of the expected severity carrying the expected message token is present. Never
    order/count. The token (required) is what distinguishes the unit's INTENDED finding from an unrelated bite."""
    severity = expect.get("severity", "hard")
    token = expect.get("message_contains", "")
    return any(f.get("severity") == severity and token in (f.get("message") or "") for f in findings)


def _summary(findings: list) -> str:
    """A short, plain rendering of what the fixture actually produced, for the did-not-bite message."""
    if not findings:
        return "no finding at all (the check passed the broken input)"
    return "; ".join(f"[{f.get('severity')}] {(f.get('message') or '')[:120]}" for f in findings[:3])


def _build_closed_unit(kind: str, fdir_abs: str, root: str):
    """Build the (rule, target) that drives one closed-kind callable against the fixture in fdir_abs. The rule is
    the fixture's own `rule.json`; the target points the REAL callable at the fixture's data, by kind. Any
    fixture-relative path the kind resolves under ROOT (schema's params.schema) is rewritten to ROOT-relative, so
    the fixture is relocatable (it does not hard-code its own committed path)."""
    rule = _load(os.path.join(fdir_abs, "rule.json"))
    if kind in _PATH_KINDS:
        inputs = sorted(_glob.glob(os.path.join(fdir_abs, "input.*")))
        if not inputs:
            raise FileNotFoundError(f"{kind} fixture has no input.* file in {fdir_abs}")
        target = {"path": os.path.relpath(inputs[0], root)}
        if kind == "schema":
            sch = (rule.get("params") or {}).get("schema")
            if sch:  # fixture-relative schema file -> ROOT-relative (how kind_schema resolves params.schema)
                rule.setdefault("params", {})["schema"] = os.path.relpath(os.path.join(fdir_abs, sch), root)
        return rule, target
    if kind == "coverage":
        return rule, {"coverage_catalog": os.path.join(fdir_abs, "catalog.json"),
                      "coverage_root": os.path.join(fdir_abs, "tree")}
    if kind == "coherence":
        return rule, {"manifests": _load(os.path.join(fdir_abs, "manifests.json"))}
    raise ValueError(f"no fixture driver for kind '{kind}'")


def _cover_closed_kind(kind: str, fixture_root: str, root: str, tier: str) -> list:
    """Prove one closed kind bites its fixture (or honor a disclosed not-applicable)."""
    label = f"`{kind}` check"
    fdir = os.path.join(fixture_root, f"kind-{kind}")
    na = os.path.join(fdir, "not-applicable.json")
    if not os.path.isdir(fdir):
        return [validate.finding(tier, _missing_msg(label, fdir, root))]
    if os.path.isfile(na):
        return _na_note(label, na, tier)
    try:
        rule, target = _build_closed_unit(kind, fdir, root)
        expect = _load(os.path.join(fdir, "expect.json"))
        _passed, found = validate.run_unit(rule, target, {})
    except Exception as exc:  # a malformed fixture is itself a failure to prove the bite (fails closed)
        return [validate.finding(tier, f"The {label} could not be proven to bite: its negative fixture "
                f"under {os.path.relpath(fdir, root)} is malformed or unreadable ({exc}). Fix the fixture so the "
                f"engine can confirm this check catches a bad input.")]
    if _bit(found, expect):
        return []
    return [validate.finding(tier, _no_bite_msg(label, fdir, expect, found, root))]


def _cover_custom_script_kind(fixture_root: str, root: str, tier: str) -> list:
    """Prove the custom/script KIND fail-closes on each of its three modes (missing script / non-zero exit /
    unreadable output). Each mode is a sub-fixture whose rule's script is rewritten to ROOT-relative; the
    fail-close IS the aimed behavior, so each expect.json asserts that mode's own token."""
    base = os.path.join(fixture_root, "kind-custom-script")
    if not os.path.isdir(base):
        return [validate.finding(tier, f"The custom/script check kind has no fail-closed fixtures under "
                f"{os.path.relpath(base, root)}, so the engine cannot prove it fails closed on a broken script. "
                f"Add the missing-script / non-zero-exit / unreadable-output fixtures.")]
    findings = []
    for mode in sorted(os.listdir(base)):
        mdir = os.path.join(base, mode)
        if not os.path.isdir(mdir):
            continue
        try:
            rule = _load(os.path.join(mdir, "rule.json"))
            sc = (rule.get("params") or {}).get("script")
            if sc:  # fixture-relative script -> ROOT-relative (a missing one stays missing, which is the point)
                rule.setdefault("params", {})["script"] = os.path.relpath(os.path.join(mdir, sc), root)
            expect = _load(os.path.join(mdir, "expect.json"))
            _passed, found = validate.run_unit(rule, {}, {})
        except Exception as exc:
            findings.append(validate.finding(tier, f"The custom/script fail-closed fixture '{mode}' under "
                            f"{os.path.relpath(mdir, root)} is malformed ({exc}); cannot prove the mode."))
            continue
        if not _bit(found, expect):
            findings.append(validate.finding(tier, _no_bite_msg(f"custom/script '{mode}' mode", mdir,
                            expect, found, root)))
    return findings


def _cover_script_instance(rule: dict, fixture_root: str, root: str, tier: str) -> list:
    """Prove one custom/script INSTANCE bites its fixture. The fixture dir (`<id-stem>/`) holds an expect.json and
    an optional target.json (the run_unit target — e.g. a seeded `env`); a missing dir with no disclosure fails
    closed. The instance's own rule (its real script) runs unchanged — only the target is substituted. A failed
    bite consults the fixture's bounded applicability declarations (home-scoped / declared environment)
    LAST — the unit always runs first, so a check that unexpectedly bites is simply covered, never excused."""
    stem = (rule.get("id") or "").split("engine/check/")[-1]
    fdir = os.path.join(fixture_root, stem)
    na = os.path.join(fdir, "not-applicable.json")
    if not os.path.isdir(fdir):
        return [validate.finding(tier, _missing_msg(f"custom/script check '{rule.get('id')}'", fdir, root))]
    if os.path.isfile(na):
        return _na_note(rule.get("id"), na, tier)
    try:
        expect = _load(os.path.join(fdir, "expect.json"))
        target_path = os.path.join(fdir, "target.json")
        target = _load(target_path) if os.path.isfile(target_path) else {}
        _passed, found = validate.run_unit(rule, target, {})
    except Exception as exc:
        return [validate.finding(tier, f"The check '{rule.get('id')}' could not be proven to bite: its fixture "
                f"under {os.path.relpath(fdir, root)} is malformed ({exc}).")]
    if _bit(found, expect):
        return []
    # The declarations may only excuse a unit that ran and STAYED SILENT (no hard finding at all) — the true
    # signature of a structurally-inert check. A run that produced any hard finding (a crash, a non-zero exit,
    # unreadable output, or the check's own fail-closed verdict) is evidence, and evidence is never excused:
    # a genuinely home-scoped check cannot crash in a deployed repo, because it returns at its gate
    # before doing anything.
    if not any(f.get("severity") == "hard" for f in found):
        alt = _failed_bite_applicability(rule.get("id"), fdir, root, tier)
        if alt is not None:
            return alt
    return [validate.finding(tier, _no_bite_msg(f"check '{rule.get('id')}'", fdir, expect, found, root))]


def _failed_bite_applicability(unit, fdir: str, root: str, tier: str) -> "list | None":
    """The bounded applicability declarations, consulted ONLY after a unit failed to bite. Returns the finding
    list that replaces the did-not-bite red (a loud soft note, or a hard rejection of a malformed declaration),
    or None when no declaration applies and the failed bite must stand. Two declarations exist:

      - the home-scoped declaration (#512): the check's failure path is reachable only in the engine's own home.
        Honored only where the AMBIENT root confirms the repo is not the construction one — in the construction
        repo the declaration is inert and the failed bite stands, so it can never exempt a reachable check.
      - requires.json (#531): the bite is witnessable only with the named environment variables (a live-API
        witness). IGNORED in CI, where the declared environment is provided and the bite stays enforced; outside
        CI a missing named variable collapses the red to a loud soft note so a local rehearsal reads honestly.
    """
    cs_path = os.path.join(fdir, _HS_DECLARATION)
    if os.path.isfile(cs_path):
        try:
            disclosure = _load(cs_path)
        except Exception as exc:  # noqa: BLE001 — an unreadable declaration is a failure to prove, fails closed
            return [validate.finding(tier, f"The home-scoped disclosure for '{unit}' ({_HS_DECLARATION}) is "
                    f"unreadable ({exc}); the failed bite stands until it is fixed.")]
        if not isinstance(disclosure, dict):
            return [validate.finding(tier, f"The home-scoped disclosure for '{unit}' ({_HS_DECLARATION}) is not "
                    f"a JSON object; the failed bite stands until it is fixed.")]
        if disclosure.get("property") != _CS_PROPERTY:
            # Quoting the property verbatim is right HERE: this message is read by whoever authors a
            # declaration, and it must hand them the exact string the format demands.
            return [validate.finding(tier, f"The home-scoped disclosure for '{unit}' ({_HS_DECLARATION}) does "
                    f"not carry the only admissible reason (\"{_CS_PROPERTY}\"); a check may be home-scoped only "
                    f"when its failure path is unreachable outside the engine's own home repository. Either fix "
                    f"the check or correct the disclosure's recorded reason.")]
        # The SAME origin==home seam the two home-scoped checks gate on, so the harness honors an exemption in
        # EXACTLY the repos where those checks no-op — the two can never disagree. Keyed on the passed root so
        # tests can drive both branches; the live run passes validate.ROOT. An unreadable origin/home fails
        # TOWARD home, matching the checks' own fail-direction (both treat an unplaceable repo as home).
        if not repo_identity.is_home_repo(root):
            reason = disclosure.get("reason", "")
            # A plain soft finding (never the collapsible no-op class): this note is the loud disclosure the
            # carve-out promises, and it must render in full in the suite output, not fold into a
            # "nothing to do" summary line.
            # The property is NOT quoted here: this reader is an operator looking at their own repository, and
            # the format's frozen wording would only puzzle them. State the real reason in plain words instead.
            return [validate.finding("soft", f"NOT APPLICABLE HERE — '{unit}' only has a failure path in the "
                    f"engine's own home repository. This repository is a copy of the engine: its git origin is "
                    f"not the update home recorded in .engine/engine.json, so the check stops at that gate "
                    f"before doing anything. There is nothing here for its self-test to catch and nothing for "
                    f"you to fix. It is still required to catch its own broken example where the engine itself "
                    f"is built. Recorded reason: {reason} This carve-out is disclosed here and re-derived when "
                    f"the change is reviewed; it is not a proof that the check works.")]
        # The engine's own home: the declaration is inert — fall through to any other declaration.
    req_path = os.path.join(fdir, "requires.json")
    if os.path.isfile(req_path):
        try:
            req = _load(req_path)
        except Exception as exc:  # noqa: BLE001
            return [validate.finding(tier, f"The environment-requirements disclosure for '{unit}' is unreadable "
                    f"({exc}); the failed bite stands until it is fixed.")]
        if not isinstance(req, dict):
            return [validate.finding(tier, f"The environment-requirements disclosure for '{unit}' is not a "
                    f"JSON object; the failed bite stands until it is fixed.")]
        if req.get("property") != _REQ_PROPERTY:
            return [validate.finding(tier, f"The environment-requirements disclosure for '{unit}' does not "
                    f"carry the only admissible reason (\"{_REQ_PROPERTY}\"); a check's bite may be excused "
                    f"locally only when its aimed failure needs a live repository witness. Either add a real "
                    f"offline fixture, or correct the disclosure's recorded reason.")]
        names = req.get("env")
        if not (isinstance(names, list) and names and all(isinstance(n, str) and n for n in names)):
            return [validate.finding(tier, f"The environment-requirements disclosure for '{unit}' must name the "
                    f"environment variables its live witness needs (a non-empty `env` list of names); the failed "
                    f"bite stands until it is fixed.")]
        in_ci = bool(os.environ.get("GITHUB_ACTIONS") or os.environ.get("CI"))
        missing = [n for n in names if not os.environ.get(n)]
        if missing and not in_ci:
            reason = req.get("reason", "")
            # A plain soft finding for the same render-in-full reason as the home-scoped note.
            return [validate.finding("soft", f"NOT WITNESSED HERE — '{unit}' declares its bite needs "
                    f"{', '.join(missing)}, absent on this machine. In CI the declaration is ignored and the "
                    f"bite stays enforced; this note only keeps a local rehearsal honest instead of falsely "
                    f"red. Recorded reason: {reason}")]
    return None


def _cover_module_kind(kind: str, fixture_root: str, root: str, tier: str) -> list:
    """Prove one MODULE-provided kind bites its fixture. The closed core kinds each have a bespoke target
    driver (_build_closed_unit); a module kind is generic, so its fixture dir `kind-<name>/` declares its own
    run: a transient `rule.json` (the rule to run, of this kind), an optional `target.json` (the run_unit
    target — a seeded `ctx`/`path`/`env` overlay), and an `expect.json`. Mirrors _cover_script_instance but
    keyed on the kind name; a present kind with no fixture fails closed via _missing_msg."""
    label = f"`{kind}` check kind"
    fdir = os.path.join(fixture_root, f"kind-{kind}")
    na = os.path.join(fdir, "not-applicable.json")
    if not os.path.isdir(fdir):
        return [validate.finding(tier, _missing_msg(label, fdir, root))]
    if os.path.isfile(na):
        return _na_note(label, na, tier)
    try:
        rule = _load(os.path.join(fdir, "rule.json"))
        expect = _load(os.path.join(fdir, "expect.json"))
        target_path = os.path.join(fdir, "target.json")
        target = _load(target_path) if os.path.isfile(target_path) else {}
        _passed, found = validate.run_unit(rule, target, {})
    except Exception as exc:
        return [validate.finding(tier, f"The {label} could not be proven to bite: its negative fixture "
                f"under {os.path.relpath(fdir, root)} is malformed or unreadable ({exc}). Fix the fixture so the "
                f"engine can confirm this check catches a bad input.")]
    if _bit(found, expect):
        return []
    return [validate.finding(tier, _no_bite_msg(label, fdir, expect, found, root))]


def _na_note(unit, na_path: str, tier: str) -> list:
    """Honor a disclosed not-applicable as a loud SOFT note (the unit is treated as covered). The disclosure must
    carry the exact bounded property; anything else is rejected as a hard finding (no silent self-classification)."""
    try:
        disclosure = _load(na_path)
    except Exception as exc:
        return [validate.finding(tier, f"The not-applicable disclosure for '{unit}' is unreadable ({exc}).")]
    if disclosure.get("property") != _NA_PROPERTY:
        return [validate.finding(tier, f"The not-applicable disclosure for '{unit}' does not carry the only "
                f"admissible reason (\"{_NA_PROPERTY}\"); a check may be exempted from a negative fixture only "
                f"when it has no failure path that can be triggered in CI. Either add a real fixture, or correct "
                f"the disclosure's recorded reason.")]
    reason = disclosure.get("reason", "")
    return [validate.disclosed_noop(f"NOT APPLICABLE — '{unit}' is exempt from a negative fixture: "
            f"{_NA_PROPERTY}. Recorded reason: {reason} This carve-out is disclosed here and re-derived at the "
            f"review gate; it is not a proof that the check bites.")]


def _missing_msg(unit, fdir: str, root: str) -> str:
    return (f"The {unit} has no negative test fixture (and no recorded reason it cannot have one), so the engine "
            f"cannot prove this check actually catches a bad input — it could be green while enforcing nothing. "
            f"Add a deliberately-broken example under {os.path.relpath(fdir, root)} that the check should catch, "
            f"or, only if the check has no failure path that can be triggered in CI, record that as a "
            f"not-applicable disclosure there.")


def _no_bite_msg(unit, fdir: str, expect: dict, found: list, root: str) -> str:
    return (f"The {unit} did NOT catch its own deliberately-broken example — running the check against the "
            f"fixture under {os.path.relpath(fdir, root)} should have produced a "
            f"'{expect.get('severity', 'hard')}' finding mentioning \"{expect.get('message_contains', '')}\", "
            f"but it produced: {_summary(found)}. The check may not be enforcing what it claims; fix the check, "
            f"or the fixture if the example is no longer the right one.")


def _roster_kinds(registry, kinds) -> list:
    if kinds is not None:
        return list(kinds)
    return sorted(registry)


def evaluate(*, root: str | None = None, check_dir: str | None = None, fixture_root: str | None = None,
             registry=None, kinds=None, tier: str = "hard") -> list:
    """The core: prove every in-scope hard check bites its negative fixture, returned as a finding.v1 list (empty
    = every unit proven). Parameterized so a test can drive a CONTROLLED roster. `kinds` overrides the kind roster
    (default: every kind in `registry`); `check_dir` is enumerated for custom/script instances; `fixture_root` is
    where the fixtures live."""
    root = root or validate.ROOT
    # Default to the RESOLVED registry (core + module-provided kinds discovered by presence), the SAME seam
    # the dispatcher reads, so a module kind the validator would run is a kind this meta-check demands a fixture
    # for — the two rosters cannot desync (a desync would let a green no-op enforcement escape the bite proof).
    registry = registry if registry is not None else validate.resolved_registry()
    check_dir = check_dir if check_dir is not None else validate.CHECK_DIR
    fixture_root = fixture_root if fixture_root is not None else os.path.join(root, _FIXTURES_REL)
    findings = []
    for kind in _roster_kinds(registry, kinds):
        if kind == "custom/script":
            findings.extend(_cover_custom_script_kind(fixture_root, root, tier))
        elif kind in validate._CLOSED_CORE_KINDS:  # a closed core kind — bespoke per-kind fixture driver
            findings.extend(_cover_closed_kind(kind, fixture_root, root, tier))
        else:  # a module-provided kind — the generic driver (its fixture declares its own target)
            findings.extend(_cover_module_kind(kind, fixture_root, root, tier))
    if os.path.isdir(check_dir):
        for rule_path in sorted(_glob.glob(os.path.join(check_dir, "*.json"))):
            try:
                rule = _load(rule_path)
            except Exception:
                continue  # a malformed check rule is another check's job, not this one's
            # Scope is the in-scope HARD check: a soft
            # custom/script is not a merge gate, so it is not required to carry a negative fixture
            # — and emitting a hard "no fixture" finding for one would escalate a soft concern to a
            # hard meta-finding. Only hard instances are in the roster.
            if rule.get("kind") == "custom/script" and rule.get("tier") == "hard":
                findings.extend(_cover_script_instance(rule, fixture_root, root, tier))
    return findings


def main() -> int:
    root = validate.ROOT
    tier = os.environ.get("ENGINE_RULE_TIER", "hard")
    kinds_env = os.environ.get("ENGINE_ROSTER_KINDS")
    kinds = None if kinds_env is None else [k for k in kinds_env.split(",") if k]

    def _resolve(value):  # a relative env path is resolved against the repo root, not the process CWD
        if not value:
            return value
        return value if os.path.isabs(value) else os.path.join(root, value)

    # A self-coverage run sets ENGINE_ROSTER_DIR to a controlled (possibly empty/absent) path so roster(b) is the
    # mini-scenario, never the live check dir — so distinguish "set" (use it as-is) from "unset" (live default).
    check_dir = _resolve(os.environ["ENGINE_ROSTER_DIR"]) if "ENGINE_ROSTER_DIR" in os.environ else None
    fixture_root = _resolve(os.environ.get("ENGINE_FIXTURE_ROOT") or None)
    print(json.dumps(evaluate(root=root, check_dir=check_dir, fixture_root=fixture_root, kinds=kinds, tier=tier)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
