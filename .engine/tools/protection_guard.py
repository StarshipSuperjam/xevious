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

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # the sibling tools dir, for github_client
from github_client import get_json  # noqa: E402 — sibling import after the path insert

# Frozen required-check names this guard expects the ruleset to bind. These are
# the literal job names of the seed's two required checks; renaming either one,
# anywhere, is a guardrail-weakening change.
REQUIRED_CHECKS = ["engine-ci", "engine-guard"]

UA = "engine-seed-protection-guard"  # this guard's GitHub API User-Agent; boot reuses it for the same protected-branch probe

# The identity-tier vocabulary lives HERE (the floor's home), not in bootstrap: bootstrap imports protection_guard,
# so this is the one module both the ruleset builder and this CI guard can share the tier from without a cycle.
SOLO, TEAM = "solo", "team"  # mirror engine.v1.json's `identity` enum
_ENGINE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # .engine, two dirs up from tools/


def resolve_tier(engine_dir: str | None = None) -> str:
    """Resolve the repo's identity tier from its committed manifest — the SINGLE place the tier is read, so no
    ruleset/verify call site defaults it independently (a defaulted tier spread across sites is fail-open: an
    omission silently builds or verifies the weaker floor). Returns SOLO for an absent/missing/unreadable manifest
    or an absent/unknown `identity` (the documented default; a malformed manifest is caught loudly by the engine.v1
    schema check, an intentional team->solo downgrade by the weakening guard's identity detector — neither is this
    read's job). Returns TEAM only when the manifest explicitly records it. Deliberately robust; never raises."""
    engine_dir = engine_dir if engine_dir is not None else _ENGINE_DIR
    try:
        with open(os.path.join(engine_dir, "engine.json"), encoding="utf-8") as fh:
            manifest = json.load(fh)
    except (OSError, ValueError):
        return SOLO
    if not isinstance(manifest, dict):
        return SOLO   # valid JSON that isn't an object (a list/string/number) -> honor the never-raises contract
    # TEAM is real only when the distinct identity that makes it real is ALSO recorded. This is the deadlock
    # guard: the team floor (1 required approval) is unsatisfiable without a distinct identity to author the PRs
    # (a sole owner cannot approve their own PR), so any team-WITHOUT-identity state — a first-run tier preference
    # recorded before the switch, or a half-completed switch — fail-safes to the SOLO floor here rather than
    # applying an unsatisfiable ruleset. The team-switch operation writes `identity` and `engine_identity`
    # together, so a genuinely-switched repo resolves TEAM.
    if manifest.get("identity") == TEAM and (manifest.get("engine_identity") or {}).get("login"):
        return TEAM
    return SOLO


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
    branch = os.environ.get("PROTECTED_BRANCH", "main")
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
        # notes (#322); the marker rides through the custom/script boundary's allow-list.
        return emit([{"severity": "soft", "location": None, "not_applicable": True,
                      "message": "Branch protection was not checked here — no repository "
                      "access token is available, which is normal on your own machine. The "
                      "check that can actually block a bad merge runs in CI."}])
    try:
        rules = get_json(f"/repos/{repo}/rules/branches/{branch}", token, user_agent=UA)
    except Exception as e:  # token present but the API could not be read -> fail closed in CI
        return emit([{"severity": tier, "location": None,
                      "message": f"Branch protection could not be verified for '{branch}' "
                      f"({e}); treating it as not in force until confirmed."}])
    missing = missing_floor(rules, REQUIRED_CHECKS, tier=identity_tier)
    if missing:
        return emit([{"severity": tier, "location": None,
                      "message": f"The protected-branch safety gate on '{branch}' is not fully "
                      "in force: " + "; ".join(missing) + ". Until this is on, an unreviewed "
                      "change could reach the protected branch. Apply the ruleset using the "
                      "setup recipe you were handed, then re-run."}])
    return emit([])  # protection is fully in force


if __name__ == "__main__":
    sys.exit(main())
