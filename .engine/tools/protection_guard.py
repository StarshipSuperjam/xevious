#!/usr/bin/env python3
"""Protection-detection guard (stage-0 seed).

Reads the EVALUATED per-branch rules for the protected branch and fails loud
until the protected-branch ruleset AND its required-check bindings are actually
in force. The evaluated-rules endpoint omits rules left in 'evaluate' or
'disabled' mode, so a ruleset that protects the branch but does not actually
bite reads as absent here — "is protection on?" is answered by what bites, not
by configuration.

Runs as a `custom/script` check rule in the CI suite,
so an unprotected branch turns engine-ci red. It emits finding.v1 JSON on stdout
(the custom/script machine channel): a hard finding when the gate is not in force,
and a soft "not checked here" note when no token is available (locally — fail open;
the CI run, which has a token, performs the real check). The default GITHUB_TOKEN
(Metadata: read) can read this endpoint; it never reads the admin-gated
ruleset-configuration endpoints.

Superseded by the control-plane bootstrap guard once that module lands.
"""
from __future__ import annotations
import json
import os
import sys
import urllib.error
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # the sibling tools dir, for github_client
from github_client import get_json  # noqa: E402 — sibling import after the path insert
import repo_identity  # noqa: E402  (resolve_default_branch — the shared, env-authoritative default-branch resolver)

# Frozen required-check names this guard expects the ruleset to bind. These are
# the literal job names of the seed's two required checks; renaming either one,
# anywhere, is a guardrail-weakening change.
REQUIRED_CHECKS = ["engine-ci", "engine-guard"]

UA = "engine-seed-protection-guard"  # this guard's GitHub API User-Agent; boot reuses it for the same protected-branch probe

# The identity-tier vocabulary lives HERE (the floor's home), not in bootstrap: bootstrap imports protection_guard,
# so this is the one module both the ruleset builder and this CI guard can share the tier from without a cycle.
SOLO, TEAM = "solo", "team"  # mirror engine.v1.json's `identity` enum
_ENGINE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # .engine, two dirs up from tools/


def _load_manifest(engine_dir: str | None = None) -> dict | None:
    """The engine manifest (engine.json) as a dict, or None when it is absent/unreadable/not-an-object — the
    single committed-manifest reader this module shares (resolve_tier and recorded_posture both call it, so
    neither opens the file independently). Deliberately robust; never raises."""
    engine_dir = engine_dir if engine_dir is not None else _ENGINE_DIR
    try:
        with open(os.path.join(engine_dir, "engine.json"), encoding="utf-8") as fh:
            manifest = json.load(fh)
    except (OSError, ValueError):
        return None
    return manifest if isinstance(manifest, dict) else None  # a list/string/number honors the never-raises contract


def resolve_tier(engine_dir: str | None = None) -> str:
    """Resolve the repo's identity tier from its committed manifest — the SINGLE place the tier is read, so no
    ruleset/verify call site defaults it independently (a defaulted tier spread across sites is fail-open: an
    omission silently builds or verifies the weaker floor). Returns SOLO for an absent/missing/unreadable manifest
    or an absent/unknown `identity` (the documented default; a malformed manifest is caught loudly by the engine.v1
    schema check, an intentional team->solo downgrade by the weakening guard's identity detector — neither is this
    read's job). Returns TEAM only when the manifest explicitly records it. Deliberately robust; never raises."""
    manifest = _load_manifest(engine_dir)
    if manifest is None:
        return SOLO
    # TEAM is real only when the distinct identity that makes it real is ALSO recorded. This is the deadlock
    # guard: the team floor (1 required approval) is unsatisfiable without a distinct identity to author the PRs
    # (a sole owner cannot approve their own PR), so any team-WITHOUT-identity state — a first-run tier preference
    # recorded before the switch, or a half-completed switch — fail-safes to the SOLO floor here rather than
    # applying an unsatisfiable ruleset. The team-switch operation writes `identity` and `engine_identity`
    # together, so a genuinely-switched repo resolves TEAM.
    if manifest.get("identity") == TEAM and (manifest.get("engine_identity") or {}).get("login"):
        return TEAM
    return SOLO


def recorded_posture(engine_dir: str | None = None) -> dict | None:
    """The operator-consented protection posture recorded in engine.json, or None. Returns the posture dict
    ONLY when it is well-formed and records the unsupported-platform status; anything else reads as no posture
    (fail toward the HARD check, never toward a false soften). Written solely by `bootstrap.py
    accept-unprotected` after it re-verifies the platform limitation; its mere presence never softens the gate —
    the standing check also demands a live plan-limitation 403 (platform_forbids_rulesets), so a stale or
    hand-forged posture is inert on any repo whose plan can host protection. Deliberately robust; never raises."""
    manifest = _load_manifest(engine_dir)
    if manifest is None:
        return None
    posture = manifest.get("protection_posture")
    if isinstance(posture, dict) and posture.get("status") == "unsupported-platform":
        return posture
    return None


def _forbidden_body(err: urllib.error.HTTPError) -> dict:
    """Best-effort parse of an HTTPError's JSON body (GitHub returns an object with a `message`). Returns a
    dict, or {} when the body is absent/unreadable/not-JSON. Never raises — a body we cannot read simply
    can't match the plan-limitation signature, so the gate stays HARD."""
    try:
        raw = err.read()
    except Exception:  # noqa: BLE001 — an unreadable error body must not crash the gate
        return {}
    if not raw:
        return {}
    try:
        data = json.loads(raw.decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray)) else raw)
    except (ValueError, AttributeError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def platform_forbids_rulesets(status: int, body, headers=None) -> bool:
    """The SINGLE definition of "this repository's GitHub PLAN cannot host branch rulesets at all" — the one
    403 that is a permanent platform limitation rather than a transient or a permission failure. It is the
    load-bearing gate on the whole unsupported-platform posture: the standing check softens to a warning, boot
    reports it calmly, and the accept-unprotected verb records it, ONLY when this returns True. Every other 403
    — a rate-limit/secondary-limit throttle, a service incident, an ordinary not-admin or org-policy block —
    stays a HARD, unresolved failure, because treating any of those as an accepted limitation would silence the
    safety gate on a repo that genuinely CAN be protected (a repo whose plan hosts rulesets returns 200 for any
    token; only writes 403 there). Shared by the standing guard, boot's signal, and bootstrap's arrival/verb so
    the recognition lives in exactly one place and cannot drift between them.

    Grounded in GitHub's real response: on a plan that cannot host rulesets the rules read returns 403 with an
    upgrade-oriented message ('Upgrade to GitHub Team/Enterprise to enable this feature', 'rulesets won't be
    enforced on this private repository until you upgrade …'). A transient rate-limit 403 instead carries
    rate-limit headers/message, excluded FIRST so an induced or coincidental throttle can never masquerade as a
    plan limit. When the wording is unrecognizable we return False — the safe direction is a red gate the
    operator can re-file, never a silently softened one."""
    if status != 403:
        return False
    msg = ""
    if isinstance(body, dict):
        msg = (body.get("message") or "").lower()
    elif isinstance(body, str):
        msg = body.lower()
    hdrs = {}
    try:
        hdrs = {str(k).lower(): str(v).lower() for k, v in dict(headers or {}).items()}
    except (TypeError, ValueError):
        hdrs = {}
    # Exclude the transient/abuse 403s first — these are NOT plan limitations, and are the inducible cases a
    # forged posture would try to ride to a false soften.
    if "retry-after" in hdrs or hdrs.get("x-ratelimit-remaining") == "0":
        return False
    if "rate limit" in msg or "secondary rate" in msg or "abuse" in msg:
        return False
    # Positive plan-limitation signature: an upgrade-to-a-paid-tier message about rulesets / this feature.
    return "upgrade" in msg and any(
        token in msg for token in ("ruleset", "team", "enterprise", "feature", "private repositor"))


def http_error_forbids_rulesets(err: urllib.error.HTTPError) -> bool:
    """platform_forbids_rulesets for the raising read model (get_json raises HTTPError unwrapped) — used by
    the standing check's main() and by boot's protected_branch_signal so both branch on the genuine
    plan-limitation 403 through exactly one recognition. Never raises."""
    return platform_forbids_rulesets(err.code, _forbidden_body(err), err.headers)


def missing_floor(rules: list, required_checks: list, *, tier: str = SOLO) -> list:
    """Pure evaluation of the protection floor against the EVALUATED per-branch rules (which already omit rules in
    evaluate/disabled mode), for the given identity `tier`. Returns the list of floor pieces not in force — empty
    means the gate fully bites. In TEAM the floor additionally requires a code-owner approval that survives the last
    push — the distinct-identity review the tier is sold on. The default is SOLO: the ENFORCEMENT paths (the standing
    CI check `main()` and bootstrap's apply/verify) resolve the real tier once via resolve_tier and pass it
    explicitly, so team protection is continuously verified; the default only serves an un-migrated informational
    caller (boot's orientation card — a tracked follow-up to make tier-aware), and under-reports team-specific rules
    there rather than mis-enforcing them."""
    types = {r.get("type") for r in rules}
    bound: set[str] = set()
    pr_thread_resolution = False
    pr_params: dict = {}
    for r in rules:
        p = r.get("parameters") or {}
        if r.get("type") == "required_status_checks":
            for c in p.get("required_status_checks", []):
                if c.get("context"):
                    bound.add(c["context"])
        elif r.get("type") == "pull_request":
            pr_thread_resolution = bool(p.get("required_review_thread_resolution"))
            pr_params = p

    missing: list[str] = []
    if "pull_request" not in types:
        missing.append("a pull request is not required before merging")
    elif tier == TEAM:
        # The team floor's whole point: a distinct non-admin identity authors the engine's commits, so the operator
        # is the enforced code-owner reviewer — and that approval must not be bypassable by a post-approval push.
        if int(pr_params.get("required_approving_review_count") or 0) < 1:
            missing.append("in team mode, a change can merge without anyone's review approval")
        if not pr_params.get("require_code_owner_review"):
            missing.append("in team mode, a change can merge without a code-owner's approval")
        if not pr_params.get("require_last_push_approval"):
            missing.append("in team mode, a commit pushed after approval can merge without a fresh approval")
    # The required-checks floor is conditional on there being checks to require. In the brownfield-arrival
    # CHECKLESS bootstrap (required_checks == []), the engine deliberately binds no checks until its workflows
    # are on the branch (finalize), so an ABSENT required_status_checks rule is the intended state, not a floor
    # gap — reporting it would make a checkless apply falsely read as degraded. The enforcement paths (the
    # standing CI check and steady-state bootstrap/verify) always pass the frozen REQUIRED_CHECKS (non-empty),
    # so this stays fully enforced there; only the checkless bootstrap passes an empty set.
    if required_checks:
        if "required_status_checks" not in types:
            missing.append("status checks are not required to pass")
        else:
            for name in required_checks:
                if name not in bound:
                    missing.append(f"the required check '{name}' is not bound")
    if not pr_thread_resolution:
        missing.append("unresolved review conversations do not block merging")
    if "non_fast_forward" not in types:
        missing.append("force-pushes are not blocked")
    if "deletion" not in types:
        missing.append("branch deletion is not restricted")
    return missing


def emit(findings: list) -> int:
    """Write the finding.v1 array to stdout (the custom/script machine channel) and return
    0 — a successful evaluation, whatever it found. Each finding carries its own severity;
    the dispatcher's custom/script kind decides where the teeth land. Human-readable prose
    lives inside each finding's `message`, so stdout stays pure JSON."""
    print(json.dumps(findings))
    return 0


def main() -> int:
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    # The branch this merge-gate verifies: the workflow sets PROTECTED_BRANCH from the repo's AUTHORITATIVE
    # live default (github.event.repository.default_branch), which the resolver reads first; recorded ->
    # origin/HEAD -> "main" are the local/degraded fallbacks. Never raises (fail-soft) so the gate can only
    # emit a finding, never crash to an ambiguous disposition.
    branch = repo_identity.resolve_default_branch()
    token = os.environ.get("GITHUB_TOKEN", "")
    tier = os.environ.get("ENGINE_RULE_TIER", "hard")  # the FINDING SEVERITY (hard/soft), passed by the kind
    identity_tier = resolve_tier()  # the repo's solo/team IDENTITY tier — DISTINCT from the severity above; decides
    #                                 which floor the standing CI check verifies, so a team repo's stronger floor is
    #                                 continuously enforced (not just the solo baseline).
    if not repo or not token:
        # Local / no credentials: FAIL OPEN with a soft note — a soft finding never blocks,
        # and the CI run (which has a token) performs the real check. Mirrors the presence
        # kind's fail-open-locally posture; never a false local block.
        # A disclosed not-applicable: on a local run there is no token, so the real check runs in CI
        # and there is nothing to do here. Marked so the validator collapses it away from actionable
        # notes (StarshipSuperjam/engine-template#322); the marker rides through the custom/script boundary's allow-list.
        return emit([{"severity": "soft", "location": None, "not_applicable": True,
                      "message": "Branch protection was not checked here — no repository "
                      "access token is available, which is normal on your own machine. The "
                      "check that can actually block a bad merge runs in CI."}])
    posture = recorded_posture()  # an operator-consented 'this plan can't host protection' acceptance, or None
    try:
        rules = get_json(f"/repos/{repo}/rules/branches/{urllib.parse.quote(branch, safe='')}",
                         token, user_agent=UA)
    except urllib.error.HTTPError as e:
        # The read failed with an HTTP status. Softens to an honest WARNING ONLY when BOTH the operator
        # recorded an unsupported-platform posture AND this 403 genuinely carries GitHub's plan-limitation
        # signature (platform_forbids_rulesets excludes rate-limit/incident/permission 403s). Any other
        # failure — no posture, or a 403 that isn't a plan limit — stays HARD, exactly as before.
        if posture and http_error_forbids_rulesets(e):
            when = posture.get("recorded_on") or "an earlier date"
            who = posture.get("operator_login") or "the operator"
            return emit([{"severity": "soft", "location": None,
                          "message": f"Branch protection isn't available on this repository's GitHub plan, so "
                          f"the safety gate can't be enforced on '{branch}'. Running without it was accepted "
                          f"on {when} (recorded by {who}) — a known, accepted limitation, not a failure to "
                          f"fix. If your plan later supports branch rulesets, run `python "
                          f".engine/tools/bootstrap.py apply` and this note stops applying."}])
        return emit([{"severity": tier, "location": None,
                      "message": f"Branch protection could not be verified for '{branch}' "
                      f"({e}); treating it as not in force until confirmed."}])
    except Exception as e:  # token present but the API could not be read (network, etc.) -> fail closed in CI
        return emit([{"severity": tier, "location": None,
                      "message": f"Branch protection could not be verified for '{branch}' "
                      f"({e}); treating it as not in force until confirmed."}])
    if not isinstance(rules, list) or not all(isinstance(r, dict) for r in rules):
        # A 200 with an unexpected body is NOT a confirmation that protection is in force — fail CLOSED
        # (mirrors boot's twin guard). This checks BOTH the outer container AND the elements: a list of
        # non-dicts (e.g. [1, 2, 3]) would otherwise crash missing_floor's `r.get("type")` into an uncaught
        # exception (missing_floor runs below, outside the read's try) and an ambiguous disposition.
        return emit([{"severity": tier, "location": None,
                      "message": f"Branch protection could not be verified for '{branch}' (the rules "
                      "response was not in the expected form); treating it as not in force until confirmed."}])
    missing = missing_floor(rules, REQUIRED_CHECKS, tier=identity_tier)
    if missing:
        # The read SUCCEEDED, which proves this plan CAN host rulesets — so a posture recorded here is now
        # stale (e.g. the plan was upgraded) and must NOT soften anything: this stays a HARD finding, and we
        # nudge the operator to clear the stale record. This is the "should be available but missing" case the
        # design preserves as red.
        stale = ""
        if posture:
            stale = (" (This repository also carries a recorded 'protection unavailable on this plan' "
                     "acceptance, but its plan now supports branch protection — that record is stale; turning "
                     "protection on with the command above clears it.)")
        return emit([{"severity": tier, "location": None,
                      "message": f"The protected-branch safety gate on '{branch}' is not fully "
                      "in force: " + "; ".join(missing) + ". Until this is on, an unreviewed "
                      "change could reach the protected branch. If the engine was just added to "
                      "this project, run `python .engine/tools/bootstrap.py finalize` to turn its "
                      "required checks on now that their workflows are on the branch; otherwise "
                      "complete the branch-protection setup you were handed, then re-run." + stale}])
    return emit([])  # protection is fully in force


if __name__ == "__main__":
    sys.exit(main())
