"""Control-plane bootstrap — the permanent, re-runnable operation that turns the protected-branch
ruleset ON (provisioning permanent primitive).

The branch ruleset is a SETTING, not a file, so it does not travel with "Use this template" and must be
applied once per repository by an operator-privileged actor (the #1 trust dependency). The control plane locks the CONTRACT (operator-privileged actor; the default
Actions token cannot create a ruleset; consent at the authorization screen; verify-after; degrade-never-
fake; the protection floor); provisioning owns the MECHANISM, which lives here so it survives the self-
deleting instantiator and the operator can re-run it any time.

This tool is permanent and re-runnable: the instantiator calls it at its apply phase, the
operator verb wraps it, and it is idempotent on its own. It surfaces only its OWN first-run
attempt outcome; the STANDING cross-session "your safety gate is off" surfacing is boot's (already built —
boot.protected_branch_signal).

CORRECTED BUILD-SPEC LEAF (token handling): the locked spec illustrates the required permission as
`admin:repo_ruleset`. Verified against GitHub's live OAuth-scopes documentation, the rulesets REST API, and
the operator's own `gh` login, NO such scope exists; creating/editing a repository ruleset requires the
standard classic `repo` scope (which a `gh` login carries by default) or a fine-grained "Administration:
write" permission. control-plane explicitly defers token handling to a provisioning build-spec leaf, so this
tool implements the correct mechanism; the locked prose's scope name is flagged for amendment (a filed doc
note). The locked CONTRACT is unchanged.

Extended for the clean-removal control-plane leg: `de_bootstrap` + `remainder_ruleset` — the
inverse of apply, removing the engine's required checks from its own safety rule (keep the floor remainder, or
drop the rule) so a clean removal can delete the engine workflows without deadlocking on checks that no longer
run. Operator-privileged; called by module_manager.remove_engine (the whole-engine removal entry).

Extended for the brownfield arrival (in-place ruleset augment): when the engine arrives on a project that
already protects its main branch with its OWN ruleset, `apply` AUGMENTS that rule in place — a fail-closed,
whitelist-projected read-modify-write that UNIONs the engine's required checks into it and ADDs any
wholly-missing floor protection, preserving everything of the operator's (bypass_actors, conditions, and
every existing rule, verified byte-identical after the write) and disclosing any floor piece it can't add
without modifying a rule the operator set themselves. The exact pieces added are recorded in engine.json so
`de_bootstrap` reverses precisely them on clean removal, never deleting a rule the engine did not create
("the ruleset is augmented, never weakened"). Greenfield
(no product ruleset) is unchanged: the engine creates and owns its own ruleset.

Scope OUT of this slice (named, deferred):
  - module manager add/remove + group-scoped uv-sync derivation + the orphan-wire reverse coherence leg
  - engine updater/upgrade + migrations (module_manager); CODEOWNERS renderer (wiring), with its
    live first-run/upgrade wire owed to the instantiator (which captures the operator handle)
  - the operator verb + boot's one-click-fix copy update
  - the second engine-scheme (spec-marker) label, deferred to the product-design module (core ensures only the
    engine-domain label here)
"""
# `from __future__ import annotations` (PEP 563) is LOAD-BEARING, not cosmetic: the first-run
# instantiator imports this module and runs it on the operator's SYSTEM python during the
# apply phase — BEFORE it bootstraps the engine's own 3.11+ tool-runtime. macOS ships python
# 3.9, where an *evaluated* `X | None` annotation (e.g. the Result/ControlPlane signatures below)
# raises `TypeError: unsupported operand type(s) for |`. Deferring annotations to strings makes this
# module import + run on 3.9, which is the precondition for `instantiator apply` to start on a bare
# adopter machine and reach the `uv sync` that materializes the 3.11+ venv. (The other engine tools
# already carry this; bootstrap was the lone gap — a `test_instantiator` regression guard now holds it.)
from __future__ import annotations

import argparse
import copy
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate  # noqa: E402
import github_client  # noqa: E402  (the shared authenticated GitHub API client; request-build)
import boot  # noqa: E402  (repo_slug, gh_token, PROTECTED_BRANCH — the shared GitHub-context helpers)
import protection_guard  # noqa: E402  (REQUIRED_CHECKS + missing_floor — the SINGLE home of the floor)
import telemetry  # noqa: E402  (GitHubIssues.ensure_label — the minimal ensure this inherits)
import weakening_guard  # noqa: E402  (ACK_LABEL — reuse the frozen guardrail-ack name, never re-decide it)

USER_AGENT = "engine-control-plane-bootstrap"
TEMPLATE_PATH = os.path.join(validate.ENGINE_DIR, "templates", "control-plane-bootstrap.md")

# The engine's own named ruleset. The engine maintains THIS ruleset; idempotence is decided by the
# evaluated floor (so a product ruleset already meeting the floor is a clean no-op and the engine never
# creates a duplicate). Operator-visible in repo settings, so the name is plain language.
ENGINE_RULESET_NAME = "engine: protected main"

# The classic OAuth scope that grants repository administration (creating/editing branch rulesets). See
# the module docstring: the locked spec's `admin:repo_ruleset` is not a real scope; `repo` is the correct
# classic scope (or fine-grained "Administration: write", which carries no classic scope string).
RULESET_SCOPE = "repo"

# The guardrail-acknowledgment label, bootstrap-provisioned so the operator APPLIES it but never hand-creates it
# (control-plane label scheme). The NAME is reused from the guard that reads it (frozen, single-sourced — never
# re-typed here). Color + description are the operator-facing build-spec leaves: a deliberate amber, distinct from
# the engine label's grey (and never alarm-red), and a plain-language description under GitHub's 100-char label
# cap that frames it as the operator's own consent act.
ACK_LABEL_COLOR = "fbca04"  # a deliberate amber — a conscious "I accept this", not an alarm
ACK_LABEL_DESCRIPTION = "You add this to approve a change the engine flagged as weakening a built-in safety protection."

# Two more required labels the engine's guards depend on, MIRRORED here so first-run provisioning creates the COMPLETE
# set in one auditable place. bootstrap runs on the operator's SYSTEM python BEFORE the venv exists (see the future-import
# note above), so it must NOT import these labels' owning modules — issue_conformance_ci and memory.erasure_observer pull
# heavier import chains onto that pre-venv path. Instead the identity is re-typed here and a drift-test in test_bootstrap
# holds each mirror equal to its canonical home; the owning module stays the single source of truth.
NEEDS_REAUTHORING_LABEL = "needs-reauthoring"            # canonical: issue_conformance_ci.NEEDS_REAUTHORING_LABEL
NEEDS_REAUTHORING_LABEL_COLOR = "d4c5f9"                 # canonical: issue_conformance_ci.NEEDS_REAUTHORING_LABEL_COLOR
NEEDS_REAUTHORING_LABEL_DESCRIPTION = "Engine Issue not yet in the engine's standard format — the engine will re-file it."
ERASURE_LABEL = "engine-erasure"                         # canonical: memory.erasure_observer.ERASURE_LABEL
ERASURE_LABEL_COLOR = "1d76db"                           # canonical: memory.erasure_observer.ERASURE_LABEL_COLOR
ERASURE_LABEL_DESCRIPTION = "The engine puts this on a pull request whose merge permanently forgets the notes it lists."

# The COMPLETE set of labels first-run provisioning ensures — one auditable home, each row (name, color, description).
# The engine label sources its whole triple live from telemetry; guardrail-ack's name from the guard that reads it
# (color/desc are its leaves above); the other two from the drift-tested mirrors above. Adding a future guard-depended
# label is one new row here — the single place the required set is declared, so none can be silently forgotten.
REQUIRED_LABELS = [
    (telemetry.ENGINE_DOMAIN_LABEL, telemetry.ENGINE_DOMAIN_LABEL_COLOR, telemetry.ENGINE_DOMAIN_LABEL_DESCRIPTION),
    (weakening_guard.ACK_LABEL, ACK_LABEL_COLOR, ACK_LABEL_DESCRIPTION),
    (NEEDS_REAUTHORING_LABEL, NEEDS_REAUTHORING_LABEL_COLOR, NEEDS_REAUTHORING_LABEL_DESCRIPTION),
    (ERASURE_LABEL, ERASURE_LABEL_COLOR, ERASURE_LABEL_DESCRIPTION),
]


class BootstrapError(Exception):
    """A GitHub read/transport failure during bootstrap — surfaced and degraded, never swallowed."""


# ---- the protection-floor payload --------------------------------------------------------------

SOLO, TEAM = protection_guard.SOLO, protection_guard.TEAM  # re-export the tier vocabulary (single home: protection_guard)


def _pull_request_params(tier: str) -> dict:
    """The `pull_request` rule parameters for a tier. SOLO: zero required approvals — a sole owner cannot
    approve their own PR — so the enforced gate is the required checks. TEAM: a distinct
    non-admin identity authors the engine's commits, so the operator becomes the enforced code-owner
    reviewer — one code-owner approval, AND that approval must survive the last push (require_last_push_approval)
    so the authoring identity cannot push a fresh commit past an already-given approval, which would hollow out
    the 'genuine second-party review' the team tier is sold on."""
    if tier == TEAM:
        return {
            "required_approving_review_count": 1,
            "required_review_thread_resolution": True,
            "dismiss_stale_reviews_on_push": True,
            "require_code_owner_review": True,
            "require_last_push_approval": True,
            "required_reviewers": [],
            "allowed_merge_methods": ["merge", "squash", "rebase"],
        }
    return {
        "required_approving_review_count": 0,
        "required_review_thread_resolution": True,
        "dismiss_stale_reviews_on_push": False,
        "require_code_owner_review": False,
        "require_last_push_approval": False,
        "required_reviewers": [],
        "allowed_merge_methods": ["merge", "squash", "rebase"],
    }


def floor_ruleset(name: str = ENGINE_RULESET_NAME, *, tier: str) -> dict:
    """The ruleset object that satisfies protection_guard.missing_floor EXACTLY for the given tier (verified
    against the live evaluated floor): a pull request before merging (tier-specific review requirement above,
    plus conversation resolution), the engine's required checks, no force-push, no deletion. Targets the
    default branch via the ~DEFAULT_BRANCH ref condition so it follows a rename. `tier` is keyword-only and
    REQUIRED — never defaulted, so no call site can silently build the weaker floor (the tier is resolved once,
    via resolve_tier, and threaded explicitly)."""
    return {
        "name": name,
        "target": "branch",
        "enforcement": "active",
        "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}},
        "rules": [
            {
                "type": "pull_request",
                "parameters": _pull_request_params(tier),
            },
            {
                "type": "required_status_checks",
                "parameters": {
                    "strict_required_status_checks_policy": False,
                    "do_not_enforce_on_create": False,
                    # The required-check names come from the SINGLE frozen home (never copied here).
                    "required_status_checks": [
                        {"context": c} for c in protection_guard.REQUIRED_CHECKS
                    ],
                },
            },
            {"type": "non_fast_forward"},
            {"type": "deletion"},
        ],
    }


def remainder_ruleset(name: str = ENGINE_RULESET_NAME) -> dict:
    """The protection floor MINUS the engine's required-status-checks rule — what de-bootstrap PUTs back
    when the operator keeps protection. Dropping that one rule removes the engine checks (engine-ci /
    engine-guard) that would otherwise 'wait forever' once their workflows are deleted, while keeping the
    rest of the floor in force: a pull request before merging, no force-push, no deletion. Derived from
    floor_ruleset() (the single source) so it can never drift from the floor it strips one rule from. The
    pull_request rule keeps require_code_owner_review False, so the kept remainder never references the
    (separately removed) CODEOWNERS file. Pinned to the SOLO shape regardless of the repo's tier: de-bootstrap
    is removing the engine, so the team-specific protections (code-owner review, required approval) depend on
    the engine identity + the engine's CODEOWNERS block that are being removed alongside it — the minimal
    keep-protection floor is the honest remainder."""
    rs = floor_ruleset(name, tier=SOLO)
    rs["rules"] = [r for r in rs["rules"] if r.get("type") != "required_status_checks"]
    return rs


# ---- augmenting a pre-existing PRODUCT ruleset in place (brownfield) ----------------------------
#
# When the engine arrives on a project that already protects its main branch with its OWN ruleset, it
# adds its checks (and any wholly-missing floor protection) INTO that rule rather than standing up a
# second one — "the ruleset is augmented, never weakened". Because the GitHub rulesets API replaces a ruleset object WHOLESALE on PUT, the
# augment is a read-modify-write computed entirely client-side. These helpers keep it strictly ADDITIVE:
# the operator's bypass_actors, conditions, and every existing rule are preserved verbatim; the engine
# only UNIONs its required checks and ADDs floor rule types the product's ruleset lacks. An existing
# product rule's parameters are NEVER modified (that would alter the operator's own protection) — a
# residual gap that could only be closed by such a modification is disclosed, not acted on. The exact
# pieces added are recorded so a later de-bootstrap reverses precisely them and nothing else.


def _floor_rule_templates(tier: str) -> dict:
    """{rule_type: rule_dict} for every floor rule at the repo's tier, sourced from floor_ruleset(tier) so the
    shapes the engine ADDs on augment never drift from the floor the engine creates on greenfield (single
    source) — a team repo augmenting a product ruleset adds the team pull_request shape, not the solo one."""
    return {r["type"]: r for r in floor_ruleset(tier=tier)["rules"]}


def _project_rule(rule: dict) -> dict:
    """A ruleset rule reduced to the fields a PUT accepts — `type` (+ `parameters`). The GET echoes
    read-only per-rule metadata (`ruleset_source_type` / `ruleset_id`) that a PUT does not accept, so it
    is dropped rather than echoed back."""
    out = {"type": rule.get("type")}
    if rule.get("parameters") is not None:
        out["parameters"] = copy.deepcopy(rule["parameters"])   # deep-copy: augment must never mutate input
    return out


def _project_ruleset(full: dict) -> dict:
    """A full ruleset object (from GET /rulesets/{id}) reduced to the writable PUT body — name, target,
    enforcement, bypass_actors, conditions, rules — preserving the operator's bypass_actors and conditions
    VERBATIM and dropping the read-only/metadata fields a PUT rejects (id, node_id, _links, source,
    source_type, created_at, updated_at, current_user_can_bypass). Echoing those back is what 422s or
    silently drops an operator's deploy-bot bypass, so the projection is a whitelist, never a verbatim echo."""
    return {
        "name": full.get("name"),
        "target": full.get("target", "branch"),
        "enforcement": full.get("enforcement", "active"),
        "bypass_actors": full.get("bypass_actors", []),
        "conditions": full.get("conditions", {}),
        "rules": [_project_rule(r) for r in (full.get("rules") or [])],
    }


def _bound_checks(rules: list) -> set:
    """The status-check contexts bound by the required_status_checks rule in `rules` (empty if none)."""
    for r in rules:
        if r.get("type") == "required_status_checks":
            p = r.get("parameters") or {}
            return {c.get("context") for c in p.get("required_status_checks", []) if c.get("context")}
    return set()


def augment_payload(product_full: dict, required_checks: list | None = None, *, tier: str) -> tuple:
    """Pure read-modify of a product ruleset object into the PUT body that ADDS the engine's protection
    without touching anything of the operator's. Returns (payload, added, residual_gaps):

    - payload: the writable projection (bypass_actors / conditions / existing rules preserved verbatim) with
      the engine's required checks UNIONed into the required_status_checks rule (the rule created if absent)
      and any WHOLLY-MISSING floor rule type ADDed. An existing product rule's parameters are never changed.
    - added: {"checks": [...], "rules": [...]} — the exact pieces the engine added (engine check contexts
      added to a pre-existing checks rule; floor rule types added wholesale, incl. "required_status_checks"
      when the engine created that rule). Recorded so de-bootstrap reverses precisely this set.
    - residual_gaps: floor pieces still unmet AFTER the additive merge (e.g. the product's own pull_request
      rule allows merging with unresolved comments) — disclosed to the operator, never fixed by modifying
      their rule. Computed by protection_guard.missing_floor (the single floor home), never re-derived here."""
    required_checks = required_checks if required_checks is not None else protection_guard.REQUIRED_CHECKS
    payload = _project_ruleset(product_full)
    rules = payload["rules"]
    templates = _floor_rule_templates(tier)
    added_checks: list = []
    added_rules: list = []

    # 1. Union the engine's required checks into the required_status_checks rule (create it if absent).
    rsc = next((r for r in rules if r.get("type") == "required_status_checks"), None)
    created_rsc = rsc is None
    if created_rsc:
        rsc = {"type": "required_status_checks",
               "parameters": {"required_status_checks": [],
                              "strict_required_status_checks_policy": False,
                              "do_not_enforce_on_create": False}}
        rules.append(rsc)
        added_rules.append("required_status_checks")
    rsc.setdefault("parameters", {}).setdefault("required_status_checks", [])
    bound = {c.get("context") for c in rsc["parameters"]["required_status_checks"] if c.get("context")}
    for name in required_checks:
        if name not in bound:
            rsc["parameters"]["required_status_checks"].append({"context": name})
            if not created_rsc:  # when the engine created the rule, its removal covers these checks
                added_checks.append(name)

    # 2. Add any WHOLLY-MISSING floor rule type (strengthen-to-floor) — never modify an existing one.
    present_types = {r.get("type") for r in rules}
    for rtype in ("pull_request", "non_fast_forward", "deletion"):
        if rtype not in present_types and rtype in templates:
            rules.append(_project_rule(templates[rtype]))
            added_rules.append(rtype)

    added = {"checks": added_checks, "rules": added_rules}
    residual_gaps = protection_guard.missing_floor(rules, required_checks, tier=tier)
    return payload, added, residual_gaps


def _strip_engine_additions(rules: list, added: dict) -> list:
    """The inverse of augment's additions: drop every rule type the engine added wholesale and remove the
    engine's added check contexts from a pre-existing required_status_checks rule — leaving every other
    product rule, and every product check, exactly as it was. Never empties or deletes a product rule it
    did not add."""
    add_checks = set((added or {}).get("checks") or [])
    add_rules = set((added or {}).get("rules") or [])
    out: list = []
    for r in rules:
        t = r.get("type")
        if t in add_rules:
            continue
        if t == "required_status_checks" and add_checks:
            p = dict(r.get("parameters") or {})
            p["required_status_checks"] = [c for c in p.get("required_status_checks", [])
                                           if c.get("context") not in add_checks]
            r = {**r, "parameters": p}
        out.append(r)
    return out


def _is_submap(small: dict, big: dict) -> bool:
    """Every key/value in `small` is present, with the same value, in `big`. Tolerates keys `big` adds —
    GitHub normalizes a rule on write by echoing back its default parameters — while still catching a
    parameter the operator set that was REMOVED or CHANGED (its (key, value) would be absent from `big`)."""
    return all(big.get(k) == v for k, v in (small or {}).items())


def _product_preserved(pre: dict, post: dict, added: dict) -> bool:
    """VERIFY (not trust) that the augment was additive: every operator-owned piece in `pre` (the projected
    product ruleset before the write) survives in `post` (the projection re-read after the write). The
    engine's own additions are ignored; anything of the operator's that changed or vanished fails the check
    (fail-closed). The comparison is by sub-mapping rather than byte-equality so GitHub's server-side
    parameter normalization (echoing default params it filled in) does not false-alarm — but a removed or
    changed operator parameter, a dropped rule type, a dropped check, or a removed bypass actor all fail it.
    Every operator bypass actor must survive (order-insensitive); conditions must survive."""
    for b in (pre.get("bypass_actors") or []):
        if b not in (post.get("bypass_actors") or []):
            return False
    if not _is_submap(pre.get("conditions") or {}, post.get("conditions") or {}):
        return False
    post_by_type: dict = {}
    for r in post.get("rules", []):
        post_by_type.setdefault(r.get("type"), []).append(r)
    for pr in pre.get("rules", []):
        t = pr.get("type")
        cands = post_by_type.get(t, [])
        if not cands:
            return False                      # a whole product rule type vanished
        post_rule = cands[0]
        if t == "required_status_checks":
            pre_ctx = {c.get("context") for c in (pr.get("parameters") or {}).get("required_status_checks", [])}
            post_ctx = {c.get("context") for c in (post_rule.get("parameters") or {}).get("required_status_checks", [])}
            if not pre_ctx.issubset(post_ctx):
                return False                  # a product check vanished
            pre_p = {k: v for k, v in (pr.get("parameters") or {}).items() if k != "required_status_checks"}
            post_p = {k: v for k, v in (post_rule.get("parameters") or {}).items() if k != "required_status_checks"}
            if not _is_submap(pre_p, post_p):
                return False                  # an operator parameter changed/vanished
        elif not _is_submap(pr.get("parameters") or {}, post_rule.get("parameters") or {}):
            return False                      # an operator parameter changed/vanished
    return True


# ---- operator-facing copy (the template SURFACE is primary; built-in fallbacks keep the #1 trust
#      tool working even if the template is absent/damaged — degrade-loud, never crash) -----------

# Built-in fallbacks. The plain-language SURFACE source is .engine/templates/control-plane-bootstrap.md;
# a test asserts the template carries each of these so they cannot silently drift.
FALLBACK_COPY = {
    "before-you-approve": (
        "I'm about to turn on your safety gate — the branch protection that keeps work from reaching "
        "your main branch without passing checks and your review. To do that I need permission to manage "
        "this repository's settings. GitHub will show an authorization screen for `repo` access, and it "
        "describes that access in sweeping terms — it reads as full control of your repositories. That "
        "sounds far broader than what happens here: it's the standard GitHub permission for turning on the "
        "review gate, and it reaches only repositories you already control — not your wider account, and "
        "nothing you don't already own. Approving it lets me set the protection rules; I can't grant it to "
        "myself, which is the point. Nothing changes until you approve."
    ),
    "degraded-not-admin": (
        "I couldn't turn on branch protection — this account doesn't administer the repository. Protection "
        "is not active, so work can merge unreviewed. Next step: ask whoever owns the repository to run "
        "this setup and approve the screen. I'll keep reminding you until it's on."
    ),
    "degraded-org-policy": (
        "I couldn't turn on branch protection — your organization's settings blocked the permission it "
        "needs. Protection is not active, so work can merge unreviewed. The way forward is to ask your "
        "organization's admin to allow it — creating branch protection here needs an admin's permission, "
        "which I can't grant myself. I'll keep reminding you until it's on."
    ),
    "degraded-didnt-save": (
        "The authorization screen completed but the permission didn't save (some sign-in methods do "
        "this). Protection is still off, so work can merge unreviewed. Let's try once more, or sign in "
        "again first. I'll keep reminding you until it's on."
    ),
    "applied": (
        "Your safety gate is on. The main branch now requires a pull request, passing checks, and resolved "
        "review comments before anything merges — and it can't be force-pushed or deleted."
    ),
    "already": (
        "Your safety gate is already on — nothing to change. (Safe to run any time; it never weakens "
        "protection that's already in place.)"
    ),
    "unverified": (
        "I set up branch protection but couldn't read back to confirm it actually took (GitHub didn't "
        "answer just now). Don't assume it's on — check your repository's branch settings, or run this "
        "again in a moment."
    ),
    # -- de-bootstrap (removing the engine's protection from the main branch) --
    "debootstrap-choose": (
        "I set up a safety rule on your main branch that requires checks to pass and a pull request before "
        "anything merges. Removing the engine takes my checks out of that rule. I can keep the rule — your "
        "main branch stays protected, just without my checks — or remove it entirely. Keep it unless you're "
        "sure you want it gone; I'll never remove protection without you choosing."
    ),
    "debootstrap-kept": (
        "I took my checks out of your main-branch safety rule and kept the rule itself, so your main branch "
        "still requires a pull request and can't be force-pushed or deleted."
    ),
    "debootstrap-dropped": (
        "I removed the main-branch safety rule entirely — the one I had set up. Your main branch is no "
        "longer protected. To turn protection back on later, run the engine setup again."
    ),
    "debootstrap-none": (
        "There was no engine safety rule on your main branch to remove — nothing to change here."
    ),
    # -- brownfield augment (the engine adds its checks INTO your existing rule, not a second rule) --
    "applied-augmented": (
        "Your main branch was already protected by your own rule, so I added my two checks to that rule "
        "rather than creating a second one — and where your rule was missing a piece of the safety floor "
        "(blocking force-pushes, blocking branch deletion, or requiring a pull request), I added that too. "
        "I changed nothing else of yours: your rule's other settings are exactly as you had them."
    ),
    "applied-augmented-partial": (
        "Your main branch was already protected by your own rule, so I added my two checks to it (and any "
        "missing force-push/deletion/pull-request protection). One part of the safety floor I couldn't turn "
        "on without changing a rule you set yourself, so I left it exactly as you have it: {gaps}. You can "
        "switch that on yourself in your branch's rules if you want it — leaving it as is means a change can "
        "still reach your main branch under that rule's current terms."
    ),
    "augment-ambiguous": (
        "Your project has more than one rule covering your main branch, so I didn't change any of them — I "
        "couldn't be sure which one to add my checks to without risking your setup. Instead I added my own "
        "rule with my two checks, alongside yours. Your branch is protected by both; if you'd rather have my "
        "checks in one of your existing rules, tell me which and I'll move them."
    ),
    "augment-classic": (
        "Your main branch is already protected by your own settings, so I added my two checks in my own rule "
        "alongside what you have — I didn't touch your existing protection. Your branch is now covered by "
        "both; nothing of yours changed."
    ),
    "debootstrap-product": (
        "I took my two checks — and any force-push/deletion/pull-request protection I had added — back out "
        "of your own branch-protection rule, and left the rest of that rule exactly as it was. There's no "
        "keep-or-remove choice here because the rule is yours, not one I created; I only added to it, so I "
        "only removed what I added."
    ),
}

# Maps each copy key to its `##` heading in the template surface.
COPY_HEADINGS = {
    "before-you-approve": "Before you approve",
    "degraded-not-admin": "If it couldn't turn on — you don't administer this repository",
    "degraded-org-policy": "If it couldn't turn on — your organization blocks the permission",
    "degraded-didnt-save": "If it couldn't turn on — the approval didn't save",
    "applied": "When it's on",
    "already": "When it was already on",
    "unverified": "When it couldn't be confirmed",
    "debootstrap-choose": "Removing the engine — keep or remove your safety rule",
    "debootstrap-kept": "When the safety rule is kept",
    "debootstrap-dropped": "When the safety rule is removed",
    "debootstrap-none": "When there was no engine safety rule",
    "applied-augmented": "When your own rule was there — I added my checks to it",
    "applied-augmented-partial": "When I added my checks but one floor piece is yours to decide",
    "augment-ambiguous": "When you have more than one rule covering main",
    "augment-classic": "When your main branch was already protected your own way",
    "debootstrap-product": "When the rule was yours — I only removed what I added",
}


def _parse_sections(text: str) -> dict:
    """Split a markdown body into {heading: body-text} by `## ` headings (frontmatter and the lead-in
    comment are ignored — only `## ` sections are read)."""
    sections: dict = {}
    current = None
    buf: list = []
    for line in text.splitlines():
        if line.startswith("## "):
            if current is not None:
                sections[current] = "\n".join(buf).strip()
            current = line[3:].strip()
            buf = []
        elif current is not None:
            buf.append(line)
    if current is not None:
        sections[current] = "\n".join(buf).strip()
    return sections


def load_copy(path: str = TEMPLATE_PATH) -> dict:
    """Operator copy, the template surface preferred, built-in fallbacks where a section is missing or
    the template is unreadable. The returned dict is keyed by the stable COPY_HEADINGS keys."""
    by_heading: dict = {}
    try:
        with open(path, encoding="utf-8") as fh:
            by_heading = _parse_sections(fh.read())
    except OSError:
        by_heading = {}
    out = {}
    for key, heading in COPY_HEADINGS.items():
        body = by_heading.get(heading, "").strip()
        out[key] = body if body else FALLBACK_COPY[key]
    return out


# ---- the GitHub authorization refresh (the operator's consent screen) ---------------------------

def gh_auth_refresh(scope: str = RULESET_SCOPE) -> bool:
    """Open the operator's GitHub authorization screen to add `scope` to their `gh` login (web flow).
    The engine CANNOT grant itself the scope — the screen is the consent gate, consistent with the
    merge-as-consent model. Returns True if `gh` exited 0. Injectable for tests/demo."""
    try:
        out = subprocess.run(
            ["gh", "auth", "refresh", "-s", scope], timeout=300, check=False
        )
        return out.returncode == 0
    except Exception:  # noqa: BLE001 — missing binary / timeout / OS error -> treated as not refreshed
        return False


# ---- the result of an apply attempt -------------------------------------------------------------

class Result:
    """The outcome of an apply attempt, plain-language-renderable for the operator."""

    def __init__(self, status: str, branch: str, missing: list, cause: str | None,
                 labels_ok: bool = True, mode: str = "created", marker: dict | None = None):
        self.status = status          # "applied" | "already" | "degraded" | "unverified"
        self.branch = branch
        self.missing = missing        # floor pieces still not in force (degraded) or disclosed (partial)
        self.cause = cause            # "not-admin" | "org-policy" | "didnt-save" | "verify-failed" |
        #                               "preserve-failed" | None
        self.labels_ok = labels_ok
        # How the floor was put in force, for the right operator copy: "created" (the engine's own ruleset,
        # greenfield), "repaired" (the engine's own ruleset already existed), "augmented" (the engine added
        # its checks into a pre-existing PRODUCT ruleset), "augmented-partial" (augmented, but a floor piece
        # is left to the operator), "already".
        self.mode = mode
        # The control-plane state the arrival persists into engine.json so a later clean removal reverses
        # exactly what the engine did: {"ruleset_mode": "created"|"augmented", "augmented_ruleset_id": id|None,
        # "added": {"checks": [...], "rules": [...]}|None}. None for a read-only/degraded outcome.
        self.marker = marker

    def is_protected(self) -> bool:
        return self.status in ("applied", "already")


# ---- the control-plane bootstrap ----------------------------------------------------------------

class ControlPlane:
    """The control-plane bootstrap boundary. Mirrors telemetry.GitHubIssues' injectable-transport
    pattern, EXTENDED to return response headers (so token capability can be read from X-OAuth-Scopes).
    `transport(method, path, body) -> (status, json, headers)` is injectable so tests/demo replace ONLY
    the network and run the real logic."""

    def __init__(self, repo: str, token: str, transport=None, refresh_fn=None, issues=None, tier=None):
        self.repo = repo
        self.token = token
        self._transport = transport or self._http
        self._refresh = refresh_fn or gh_auth_refresh
        # The label-ensure boundary, INHERITED from telemetry's first-producer mechanism. Injectable
        # so tests/demo replace the network; constructed lazily against the real transport otherwise.
        self._issues = issues
        # The identity tier is resolved ONCE here (the single boundary) and threaded through every ruleset
        # method as self.tier, so no method independently defaults it. Injectable for tests; resolved from the
        # committed manifest otherwise.
        self.tier = tier if tier is not None else protection_guard.resolve_tier()

    def _http(self, method: str, path: str, body=None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = github_client.request(path, self.token, user_agent=USER_AGENT, method=method, data=data)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode("utf-8")
                return resp.status, (json.loads(raw) if raw else None), dict(resp.headers)
        except urllib.error.HTTPError as exc:           # 4xx/5xx — keep status + headers + body
            headers = dict(exc.headers) if exc.headers else {}
            try:
                body_json = json.loads(exc.read().decode("utf-8"))
            except Exception:  # noqa: BLE001 — an empty/non-JSON error body is fine
                body_json = None
            return exc.code, body_json, headers
        except urllib.error.URLError as exc:             # network unreachable — a transport failure
            raise BootstrapError(f"GitHub is unreachable: {exc}") from exc

    # -- capability -------------------------------------------------------------------------------

    def token_scopes(self) -> set | None:
        """The classic-OAuth scopes on the operator token, read from the X-OAuth-Scopes response header
        of any API call. Returns a set of scope strings, or None when the header is absent (a fine-
        grained token, or an unreachable API) — capability is then decided by probing the write."""
        try:
            _status, _data, headers = self._transport("GET", f"/repos/{self.repo}", None)
        except BootstrapError:
            return None
        raw = None
        for k, v in headers.items():
            if k.lower() == "x-oauth-scopes":
                raw = v
                break
        if raw is None:
            return None
        return {s.strip() for s in raw.split(",") if s.strip()}

    # -- reads ------------------------------------------------------------------------------------

    def floor_missing(self, branch: str) -> list:
        """The protection-floor pieces NOT in force on the branch, via the EVALUATED per-branch rules
        endpoint (the default token can read it) and protection_guard's own evaluation. Raises
        BootstrapError on an unreadable response — never a false 'protected'."""
        status, data, _ = self._transport(
            "GET", f"/repos/{self.repo}/rules/branches/{branch}", None)
        if status >= 400 or not isinstance(data, list):
            raise BootstrapError(f"could not read evaluated branch rules (status {status})")
        return protection_guard.missing_floor(data, protection_guard.REQUIRED_CHECKS, tier=self.tier)

    def engine_ruleset(self) -> dict | None:
        """The engine's own ruleset, if it already exists (matched by ENGINE_RULESET_NAME). Returns None
        when absent. Raises BootstrapError if the admin rulesets endpoint cannot be listed."""
        status, data, _ = self._transport("GET", f"/repos/{self.repo}/rulesets", None)
        if status >= 400 or not isinstance(data, list):
            raise BootstrapError(f"could not list rulesets (status {status})")
        for r in data:
            if r.get("name") == ENGINE_RULESET_NAME:
                return r
        return None

    def product_rulesets(self, branch: str, own_id="__resolve__") -> list:
        """The ids of the repository's OWN branch rulesets that ACTUALLY apply to the branch (and bite),
        excluding the engine's own. Resolved from the evaluated per-branch endpoint — each evaluated rule
        carries the `ruleset_id` and `ruleset_source_type` it came from — so detection is exact and needs no
        ref-condition parsing, and rules in evaluate/disabled mode (which do not protect) are naturally
        excluded. Organization-level rulesets are skipped: they are not the repository's to edit here.
        `own_id` is the engine's own ruleset id when the caller already resolved it (apply does), to skip a
        redundant rulesets list; it is resolved here when not supplied. Raises BootstrapError on an
        unreadable response (fail-closed — never guess the set)."""
        status, data, _ = self._transport(
            "GET", f"/repos/{self.repo}/rules/branches/{branch}", None)
        if status >= 400 or not isinstance(data, list):
            raise BootstrapError(f"could not read evaluated branch rules (status {status})")
        if own_id == "__resolve__":
            own = self.engine_ruleset()
            own_id = own.get("id") if own else None
        ids: list = []
        for r in data:
            rid = r.get("ruleset_id")
            if rid is None or rid == own_id or rid in ids:
                continue
            if r.get("ruleset_source_type") not in (None, "Repository"):
                continue  # an org/enterprise ruleset is not ours to augment
            ids.append(rid)
        return ids

    def ruleset_detail(self, rid: int) -> dict:
        """The FULL ruleset object (incl. its `rules` array, which the list endpoint omits). Raises
        BootstrapError on any non-200 or malformed body — the read-modify-write must NEVER PUT on a doubtful
        read, or a partial GET would silently drop the operator's rules."""
        status, data, _ = self._transport("GET", f"/repos/{self.repo}/rulesets/{rid}", None)
        if status >= 400 or not isinstance(data, dict) or not isinstance(data.get("rules"), list):
            raise BootstrapError(f"could not read ruleset {rid} (status {status})")
        return data

    # -- writes -----------------------------------------------------------------------------------

    def _write_floor(self, existing: dict | None):
        """Create the engine ruleset (POST) or repair it in place (PUT). Returns (status, body)."""
        payload = floor_ruleset(tier=self.tier)
        if existing:
            status, body, _ = self._transport(
                "PUT", f"/repos/{self.repo}/rulesets/{existing['id']}", payload)
        else:
            status, body, _ = self._transport("POST", f"/repos/{self.repo}/rulesets", payload)
        return status, body

    @staticmethod
    def _forbidden_cause(body) -> str:
        """Best-effort classification of a 401/403 on the write: an organization-policy block (whose
        structural escape is the team identity) vs the operator simply not administering the repo. GitHub
        returns an informative message on a policy block; default to not-admin when it can't be told."""
        msg = ""
        if isinstance(body, dict):
            msg = (body.get("message") or "").lower()
        if "organization" in msg or "policy" in msg or "ruleset bypass" in msg:
            return "org-policy"
        return "not-admin"

    def ensure_labels(self) -> bool:
        """Idempotently ensure the COMPLETE set of labels the engine's guards depend on exists (REQUIRED_LABELS),
        INHERITING the minimal ensure (telemetry.GitHubIssues.ensure_named_label) — never re-deciding a string, never
        re-implementing the mechanism. The set: the engine-domain label; the `guardrail-ack` label the operator APPLIES
        to consent to a guardrail-weakening change but (per the control-plane label scheme) never hand-creates; the
        `needs-reauthoring` malformed-issue flag; and the `engine-erasure` erasure-consent marker — so a fresh generated
        repo's first safety-gate merge, first malformed-issue re-file, and first memory-erasure never block on (nor
        lazily mis-create) a label the operator was told they never make. Create-only and idempotent (never overwrites
        an operator's own label edits). Best-effort: returns False on any failure so the caller can disclose, never
        crashing the bootstrap (and if provisioning ever fails, the guard simply stays blocking — never a silent pass)."""
        try:
            issues = self._issues or telemetry.GitHubIssues(self.repo, self.token)
            for name, color, description in REQUIRED_LABELS:
                issues.ensure_named_label(name, color, description)
            return True
        except Exception:  # noqa: BLE001 — DegradedReadError / transport failure -> disclose, don't crash
            return False

    # -- orchestration ----------------------------------------------------------------------------

    def apply(self, branch: str | None = None, announce=None) -> Result:
        """Idempotently ensure the protection floor is in force on the branch, then ensure the engine
        labels. `announce(text)` surfaces operator copy at the right moments (default: print)."""
        branch = branch or boot.PROTECTED_BRANCH
        copy = load_copy()
        say = announce if announce is not None else (lambda text: print(text))

        # 1. Already in force? (the idempotent no-op — a product ruleset that meets the floor counts).
        try:
            missing = self.floor_missing(branch)
        except BootstrapError:
            missing = None  # unreadable -> fall through to the capability+apply path
        if missing == []:
            labels_ok = self.ensure_labels()
            return Result("already", branch, [], None, labels_ok)

        # 2. Capability: does the operator token carry repository administration?
        scopes = self.token_scopes()
        needs_grant = scopes is not None and RULESET_SCOPE not in scopes
        if needs_grant:
            say(copy["before-you-approve"])
            self._refresh(RULESET_SCOPE)            # the operator approves the authorization screen
            scopes = self.token_scopes()            # verify-after-refresh (web flow can fail to persist)
            if scopes is not None and RULESET_SCOPE not in scopes:
                labels_ok = self.ensure_labels()
                return Result("degraded", branch, missing or [], "didnt-save", labels_ok)

        # 3. Apply. Three paths, in order of decisiveness:
        #    (a) the engine's OWN ruleset already exists -> repair it in place (greenfield re-run / a prior
        #        install). The engine owns it, so a later removal offers keep/drop.
        #    (b) no engine ruleset, but exactly ONE product ruleset protects the branch -> AUGMENT it in
        #        place (add our checks + any wholly-missing floor protection), preserving everything of
        #        theirs, and record what we added so removal can reverse exactly it.
        #    (c) otherwise (no product ruleset, or more than one) -> create the engine's OWN ruleset, and
        #        disclose any pre-existing protection it now sits alongside (the residual two-rule state).
        try:
            own = self.engine_ruleset()
        except BootstrapError:
            own = None
        if own is None:
            try:
                prod_ids = self.product_rulesets(branch, own_id=None)  # own already resolved as absent
            except BootstrapError:
                prod_ids = []
            if len(prod_ids) == 1:
                return self._augment_ruleset(prod_ids[0], branch, missing, say, copy)
            if len(prod_ids) > 1:
                say(copy["augment-ambiguous"])
            elif self._pre_existing_protection(missing, self.tier):
                say(copy["augment-classic"])

        mode = "repaired" if own is not None else "created"
        status, body = self._write_floor(own)
        if status in (401, 403):
            # A fine-grained token without admin shows no classic scope; the write is the real probe.
            say(copy["before-you-approve"])
            self._refresh(RULESET_SCOPE)
            try:
                own = self.engine_ruleset()   # re-resolve: if the first attempt landed, repair it
            except BootstrapError:
                pass
            status, body = self._write_floor(own)
        if status >= 400:
            labels_ok = self.ensure_labels()
            cause = self._forbidden_cause(body) if status in (401, 403) else "verify-failed"
            return Result("degraded", branch, missing or [], cause, labels_ok, mode=mode)

        # 4. Verify the floor is now actually in force (never assume the write took). An UNREADABLE
        #    verify is NOT success — it degrades to 'unverified', never a false 'applied'.
        try:
            still_missing = self.floor_missing(branch)
        except BootstrapError:
            still_missing = None
        labels_ok = self.ensure_labels()
        marker = {"ruleset_mode": "created", "augmented_ruleset_id": None, "added": None}
        if still_missing is None:
            return Result("unverified", branch, [], "verify-unreadable", labels_ok, mode=mode)
        if still_missing:
            return Result("degraded", branch, still_missing, "verify-failed", labels_ok, mode=mode)
        return Result("applied", branch, [], None, labels_ok, mode=mode, marker=marker)

    def _augment_ruleset(self, rid: int, branch: str, missing, say, copy) -> Result:
        """AUGMENT a single pre-existing PRODUCT ruleset in place: add the engine's required checks and any
        wholly-missing floor protection, preserving everything of the operator's. A fail-closed
        read-modify-write VERIFIED against the ruleset object (immediately consistent, unlike the lagging
        evaluated endpoint), with a bounded re-read if a concurrent overwrite lands in the read-write window.
        Returns a Result carrying the de-bootstrap marker so a later removal reverses EXACTLY what was added."""
        attempts = 0
        added: dict = {"checks": [], "rules": []}
        residual: list = []
        while True:
            attempts += 1
            try:
                pre_full = self.ruleset_detail(rid)       # complete-or-BootstrapError: never PUT on doubt
            except BootstrapError:
                labels_ok = self.ensure_labels()
                return Result("unverified", branch, missing or [], "verify-unreadable", labels_ok,
                              mode="augmented")
            pre = _project_ruleset(pre_full)
            payload, added, residual = augment_payload(pre_full, tier=self.tier)
            if not added["checks"] and not added["rules"]:
                # Already augmented — a verified no-op. Record the engine checks in force so a later
                # de-bootstrap still strips them (and never deadlocks); leave added rule-types empty (safe).
                added = {"checks": [c for c in protection_guard.REQUIRED_CHECKS
                                    if c in _bound_checks(pre["rules"])], "rules": []}
                post = pre
                break
            status, body, _ = self._transport("PUT", f"/repos/{self.repo}/rulesets/{rid}", payload)
            if status in (401, 403):
                say(copy["before-you-approve"])
                self._refresh(RULESET_SCOPE)
                status, body, _ = self._transport("PUT", f"/repos/{self.repo}/rulesets/{rid}", payload)
            if status >= 400:
                labels_ok = self.ensure_labels()
                cause = self._forbidden_cause(body) if status in (401, 403) else "verify-failed"
                return Result("degraded", branch, missing or [], cause, labels_ok, mode="augmented")
            try:
                post = _project_ruleset(self.ruleset_detail(rid))   # immediately consistent with the PUT
            except BootstrapError:
                labels_ok = self.ensure_labels()
                return Result("unverified", branch, missing or [], "verify-unreadable", labels_ok,
                              mode="augmented")
            if not _product_preserved(pre, post, added):
                # The wholesale PUT altered something of the operator's — fail closed, never a false 'applied'.
                labels_ok = self.ensure_labels()
                return Result("degraded", branch, missing or [], "preserve-failed", labels_ok,
                              mode="augmented")
            if all(c in _bound_checks(post["rules"]) for c in protection_guard.REQUIRED_CHECKS):
                break                                     # our checks are in force
            if attempts < 2:
                continue                                  # a concurrent overwrite landed — re-read, retry once
            labels_ok = self.ensure_labels()
            return Result("unverified", branch, missing or [], "verify-unreadable", labels_ok,
                          mode="augmented")

        labels_ok = self.ensure_labels()
        marker = {"ruleset_mode": "augmented", "augmented_ruleset_id": rid, "added": added}
        # Verify the floor against what is ACTUALLY in force now (the re-read object), never the pre-write
        # payload — a server can accept the PUT but silently drop a rule type it disallows (an org policy or
        # plan tier), and the engine must not then claim that protection is on. `residual` is the floor pieces
        # the engine deliberately LEFT to the operator (gaps in their own rules it won't modify); anything
        # missing BEYOND that is something the engine tried to add but the server didn't apply.
        actual_missing = protection_guard.missing_floor(post["rules"], protection_guard.REQUIRED_CHECKS, tier=self.tier)
        unexpected = [m for m in actual_missing if m not in residual]
        if unexpected:
            return Result("degraded", branch, actual_missing, "verify-failed", labels_ok, mode="augmented")
        if residual:   # a floor piece the product's own rule leaves open — disclosed, not modified
            return Result("applied", branch, residual, None, labels_ok, mode="augmented-partial",
                          marker=marker)
        return Result("applied", branch, [], None, labels_ok, mode="augmented", marker=marker)

    @staticmethod
    def _pre_existing_protection(missing, tier: str) -> bool:
        """True if the branch already had SOME protection in force (a PROPER subset of the floor was missing)
        — classic branch protection, or a ruleset the engine didn't augment — so creating the engine's own
        ruleset leaves the operator with their protection plus the engine's, worth disclosing. The baseline is
        computed at the SAME tier as `missing` so the subset comparison stays consistent."""
        if not missing:
            return False
        baseline = set(protection_guard.missing_floor([], protection_guard.REQUIRED_CHECKS, tier=tier))
        return set(missing) < baseline   # proper subset => something was already in force

    # -- de-bootstrap (the inverse of apply — operator-privileged, for clean removal) -------------

    def de_bootstrap(self, choice: str | None = None, announce=None, marker: dict | None = None) -> dict:
        """Take the engine's protection back off the branch — the inverse of apply(), operator-privileged, on
        clean removal. It ALWAYS removes the engine's checks first: a required check whose workflow is being
        deleted would otherwise 'wait forever' and deadlock every pull request, so
        this must run BEFORE the removal pull request that deletes the engine workflows. Two shapes, told
        apart WITHOUT guessing by whether the engine's OWN named ruleset exists:

        - Engine CREATED its own ruleset (greenfield) -> disclose and let the operator choose: `keep`
          (default) PUTs the checkless protection-floor remainder so the branch STAYS protected (never
          auto-deleted); `drop` DELETEs the engine rule entirely.
        - Engine AUGMENTED a pre-existing PRODUCT ruleset (brownfield) -> reverse EXACTLY what was added,
          per the `marker` the arrival recorded (engine checks + any floor rules the engine added), leaving
          the rest of the operator's rule untouched. The product rule is NEVER deleted — it's theirs, not
          ours; there is no keep/drop choice. If the marker is absent/stale, fall back to stripping only the
          frozen engine check names (bounded), never deleting, and disclose.

        `choice` (keep/drop) applies only to the engine-created shape; `marker` is read by the caller from
        engine.json; `announce(text)` surfaces the outcome copy. Returns a structured result."""
        copy = load_copy()
        say = announce if announce is not None else (lambda text: print(text))
        existing = self.engine_ruleset()
        if existing is not None:
            rid = existing.get("id")
            # Default / None / anything other than explicit "drop" => keep. Protection is NEVER auto-deleted.
            if choice == "drop":
                status, _body, _ = self._transport("DELETE", f"/repos/{self.repo}/rulesets/{rid}", None)
                if status >= 400:
                    raise BootstrapError(f"could not remove the engine safety rule (status {status})")
                say(copy["debootstrap-dropped"])
                return {"status": "dropped", "ruleset_existed": True, "choice": "drop", "deleted": True}
            status, _body, _ = self._transport(
                "PUT", f"/repos/{self.repo}/rulesets/{rid}", remainder_ruleset())
            if status >= 400:
                raise BootstrapError(f"could not update the engine safety rule (status {status})")
            say(copy["debootstrap-kept"])
            return {"status": "kept", "ruleset_existed": True, "choice": "keep", "deleted": False}

        # No engine-owned ruleset: the brownfield AUGMENTED shape. Reverse exactly what was recorded.
        mk = marker if isinstance(marker, dict) else {}
        if mk.get("ruleset_mode") == "augmented" and mk.get("augmented_ruleset_id") is not None:
            rid = mk["augmented_ruleset_id"]
            added = mk.get("added") or {"checks": [], "rules": []}
            try:
                full = self.ruleset_detail(rid)
            except BootstrapError:
                say(copy["debootstrap-none"])   # the product rule is already gone — nothing to reverse
                return {"status": "no-rule", "ruleset_existed": False, "choice": None, "deleted": False}
            return self._strip_product(rid, full, added, say, copy)

        # Marker absent/stale: don't guess authorship. Strip ONLY the frozen engine check names from any
        # product ruleset that carries them (bounded, never a product's own rules), never delete, disclose.
        # This is deadlock-prevention first: the engine's checks must come off before its workflows vanish.
        try:
            prod_ids = self.product_rulesets(branch=boot.PROTECTED_BRANCH)
        except BootstrapError:
            prod_ids = []
        for rid in prod_ids:
            try:
                full = self.ruleset_detail(rid)
            except BootstrapError:
                continue
            if any(c in _bound_checks(full.get("rules") or []) for c in protection_guard.REQUIRED_CHECKS):
                return self._strip_product(
                    rid, full, {"checks": list(protection_guard.REQUIRED_CHECKS), "rules": []}, say, copy)
        say(copy["debootstrap-none"])
        return {"status": "no-rule", "ruleset_existed": False, "choice": None, "deleted": False}

    def _strip_product(self, rid: int, full: dict, added: dict, say, copy) -> dict:
        """Remove exactly the engine's `added` pieces from a product ruleset and write the remainder back,
        preserving everything else of the operator's (bypass_actors / conditions / their rules). Never
        DELETEs the ruleset — it is the operator's, not the engine's."""
        payload = _project_ruleset(full)
        payload["rules"] = _strip_engine_additions(payload["rules"], added)
        status, _body, _ = self._transport("PUT", f"/repos/{self.repo}/rulesets/{rid}", payload)
        if status >= 400:
            raise BootstrapError(f"could not update your branch-protection rule (status {status})")
        say(copy["debootstrap-product"])
        return {"status": "unaugmented", "ruleset_existed": True, "choice": None, "deleted": False}


# ---- rendering an outcome for the operator ------------------------------------------------------

def render(result: Result, copy: dict | None = None) -> str:
    """Plain-language rendering of an apply outcome for the operator."""
    copy = copy or load_copy()
    if result.status == "applied" and result.mode == "augmented":
        msg = copy["applied-augmented"]
    elif result.status == "applied" and result.mode == "augmented-partial":
        gaps = "; ".join(result.missing) if result.missing else "the missing piece"
        msg = copy["applied-augmented-partial"].replace("{gaps}", gaps)
    elif result.status == "applied":
        msg = copy["applied"]
    elif result.status == "already":
        msg = copy["already"]
    elif result.status == "unverified":
        msg = copy["unverified"]
    elif result.cause == "preserve-failed":
        # The wholesale PUT changed something of the operator's — say so plainly, never a false 'applied'.
        msg = ("I tried to add my checks to your existing branch-protection rule, but couldn't confirm I "
               "left the rest of your rule exactly as it was, so I've stopped rather than risk changing "
               "your protection. Nothing of yours should have changed — please check your repository's "
               "rules, and tell me if anything looks off.")
    elif result.cause == "verify-failed":
        # The write reported success but the gate still isn't fully in force — honest, not "not-admin".
        detail = (": " + "; ".join(result.missing)) if result.missing else ""
        msg = ("I tried to turn on branch protection, but it still isn't fully in force" + detail +
               ". Work can merge unreviewed until it's on — please check your repository's branch "
               "settings, or run this again.")
    else:  # degraded — pick the cause-matched banner
        key = {
            "not-admin": "degraded-not-admin",
            "org-policy": "degraded-org-policy",
            "didnt-save": "degraded-didnt-save",
        }.get(result.cause or "", "degraded-not-admin")
        msg = copy[key]
    if not result.labels_ok:
        msg += ("\n(Note: I couldn't confirm the engine's issue label exists — I'll retry that next "
                "time I can reach GitHub.)")
    return msg


# ---- CLI ----------------------------------------------------------------------------------------

def _resolve_repo(arg_repo: str | None) -> str | None:
    return arg_repo or boot.repo_slug()


def cmd_status(args) -> int:
    """Read-only: report whether the safety gate is on for the branch (no writes, no consent screen)."""
    repo = _resolve_repo(args.repo)
    token = boot.gh_token()
    if not repo or not token:
        print("Can't check branch protection from here — no repository access is available. "
              "(This is normal on a machine without a logged-in `gh`.)")
        return 0
    cp = ControlPlane(repo, token)
    try:
        missing = cp.floor_missing(args.branch)
    except BootstrapError as e:
        print(f"Couldn't read branch protection for '{args.branch}' ({e}); treating it as not on.")
        return 0
    if not missing:
        print(f"Safety gate is ON for '{args.branch}'.")
    else:
        print(f"Safety gate is OFF for '{args.branch}': " + "; ".join(missing) + ".")
    return 0


def cmd_apply(args) -> int:
    """Turn the safety gate on for the branch (idempotent; surfaces the consent screen if needed)."""
    repo = _resolve_repo(args.repo)
    token = boot.gh_token()
    if not repo or not token:
        print("Can't turn on branch protection from here — no repository access is available. "
              "Run this where you're logged in to GitHub (`gh auth login`).")
        return 1
    cp = ControlPlane(repo, token)
    try:
        result = cp.apply(branch=args.branch)
    except BootstrapError as e:
        print(f"Couldn't reach GitHub to turn on branch protection ({e}). Nothing changed — try again "
              "when you're back online.")
        return 1
    print(render(result))
    return 0 if result.is_protected() else 1


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="bootstrap",
        description="Turn the protected-branch safety gate on (the control-plane bootstrap).")
    parser.add_argument("--repo", default=None, help="owner/repo (default: derived from the git remote)")
    parser.add_argument("--branch", default=boot.PROTECTED_BRANCH, help="the protected branch")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("status", help="report whether the safety gate is on (read-only)")
    sub.add_parser("apply", help="turn the safety gate on (idempotent)")
    args = parser.parse_args(argv)
    if args.cmd == "status":
        return cmd_status(args)
    if args.cmd == "apply":
        return cmd_apply(args)
    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
