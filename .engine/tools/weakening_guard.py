#!/usr/bin/env python3
"""Guardrail-weakening classifier (stage-0 seed; re-homed onto custom/script).

Runs on pull_request_target so its logic is read from the protected base branch
— a pull request cannot tamper with the guard that judges it. It READS THE DIFF
ONLY via the API and NEVER checks out or executes the pull request's head code.
This is an authoring invariant: the trigger grants the privilege, this script
enforces the restraint (it makes no use of the head ref and the workflow checks
out only the base).

It flags a change that removes, renames, or modifies a guardrail file — a file
that constitutes or configures an enforcement gate (a CI workflow, a check rule,
a check rule's enforcement SCRIPT discovered by presence, an enforcement HOOK or
the config that wires it, the validator, the ruleset-applying operation, or
CODEOWNERS) OR ships a traveling security-floor provision (the committed
`dependabot.yml` the control plane sends to every generated repo — it gates no
merge, but silently dropping it downgrades a safety pillar the operator relied
on), defined by that PROPERTY rather than a path-prefix list, so
benign edits to non-gate tooling no longer demand the ack — AND a REPOINT of the
engine's update home in the manifest (`home_repository` in .engine/engine.json) —
which changes where executable engine code is fetched from at the next update, a
supply-chain weakening (StarshipSuperjam/engine-template#367) — AND a SHRINK of the deployment's own instance floor
(`.engine/operator-guarded-paths.json`, StarshipSuperjam/engine-template#532): a repo may declare extra product-side paths
to guard (a scanner the engine cannot discover by presence), UNIONED with the engine's set and
read from the trusted base; removing a declared path is a weakening, while adding one is a
strengthening that never stops here.

A match answers in one of TWO TIERS (eADR-0040). The KILLSWITCH tier — the value
detectors, the directional detectors, removal/rename of any guarded file, the
hard-floor members, and every fail-closed path — blocks the merge until the
operator applies the distinct, deliberate acknowledgment (the `guardrail-ack`
label) after reading, in plain language, what protection could weaken. Every
OTHER guarded modification emits a plain-language DISCLOSURE at soft severity —
which enforcement files changed and why they matter — and the check passes: the
operator's protected-branch merge review judges the change, and the notice says
plainly that it needs no action and must never shape a design. The ack label
DOWNGRADES a killswitch finding to a disclosure; it never erases the record.

It now runs as a frozen-named `custom/script` check rule (engine/check/guardrail-weakening),
invoked BY ID from engine-guard.yml (`validate.py --check`), NOT as part of the CI
suite — so its execution stays on the trusted-base pull_request_target workflow and
never moves into the head-checkout engine-ci context (the trusted-base isolation). It emits
finding.v1 JSON on stdout (the custom/script machine channel) and returns 0 on a
successful evaluation: an empty array when nothing weakens; a KILLSWITCH finding
at the rule's tier (ENGINE_RULE_TIER, passed by the kind) — carrying the
plain-language ack guidance — on an unacknowledged hard-tier change, downgraded
to soft when the `guardrail-ack` label is present (the ack is an INPUT to this
one guard, and it downgrades, never erases); a DISCLOSURE finding at soft
severity whenever soft-tier enforcement files are modified; and a fail-closed finding when
the pull-request context cannot be read, or when the full changed-file list cannot be
retrieved (it paginates the diff to completion and cross-checks what it read against the
pull request's authoritative `changed_files` count, failing closed on a partial view so a
weakening edit cannot hide past GitHub's file-listing cap). An internal crash returns non-zero, which
the custom/script kind turns into a hard fail-closed finding (defense in depth).

Honest bound: in solo the operator holds admin and could bypass the ruleset, so
this makes weakening NON-SILENT and DELIBERATE ("cannot weaken silently"), not
impossible ("cannot weaken at all" needs a distinct team identity).

Superseded by the control-plane weakening guard once that module lands.
"""
from __future__ import annotations
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # the sibling tools dir, for github_client
from github_client import get_json, get_page, next_link  # noqa: E402 — sibling import after the path insert

ACK_LABEL = "guardrail-ack"
# The guarded set is defined by a PROPERTY, not a path-prefix list: a committed file that constitutes
# or configures an enforcement gate — one whose change could remove, disable, rename, or loosen a check, a
# permission/enforcement hook, or a branch protection. Non-gate tooling (session boot, memory, telemetry, the
# self-review renderer) is NOT guarded — flagging benign edits there trained the rubber-stamping that the guardrail-weakening guard and its trusted-base isolation exist
# to prevent. The concrete roster is realized as: these two prefixes + the permanent floor below + every check
# rule's script DISCOVERED BY PRESENCE (see _derive_check_scripts). Fail-safe: an uncertain file resolves as
# covered. The blanket `.engine/tools/` prefix was REMOVED here — its enforcement scripts are now guarded by
# presence, its enforcement hooks by the floor, and everything else is correctly non-gate.
GUARDRAIL_PREFIXES = (".github/workflows/", ".engine/check/")

# The PERMANENT FLOOR — exact paths that constitute or configure an enforcement gate and are NOT discoverable as
# a check rule's `params.script`. Grouped by why each is guarded.
_FLOOR_ENFORCEMENT_CONFIG = (
    ".github/CODEOWNERS",         # the review-ownership wall (reserved)
    ".engine/pyproject.toml",     # the tool-runtime the validator + every guard execute in
    ".engine/uv.lock",            # (foundation artifacts — a change here changes what code runs)
    ".engine/suites.json",        # decides WHICH suite blocks the merge — loosening it (CI -> local-nudge) is a killswitch
    ".claude/settings.json",      # wires the PreToolUse write-gate + the other enforcement hooks (was ABSENT —
    #                               a live hole; a PR gutting those hooks passed the guard with NO ack)
    ".codex/hooks.json",          # the Codex runtime's mirror of settings.json — the SAME hole, closed on
    #                               arrival (guarded whole-file; its engine entries wire the same gates)
    ".codex/config.toml",         # the Codex helper-server registration (guarded whole-file, mirroring the
    #                               settings.json posture)
    ".engine/policies/provider-exceptions.json",  # the parity check's sanctioned-exception ledger — the file
    #                               that grants exemptions from an enforcement check is itself guarded, or
    #                               widening an exception would be the quiet way around the check (eADR-0034)
)
# The validator + this guard. validate.py is ALSO the sole home of the 5 built-in HARD check kinds
# (presence/schema/shape/coverage/coherence): those carry no `params.script`, so the derived clause below
# structurally cannot reach them — but the hard-check-bite meta-check proves each hard check still catches its
# planted violation, which is the mechanical correlate that makes validate.py's DISCLOSURE tier honest
# (eADR-0040: soft on modification, conditional on hard_check_bite_check.py staying in _HARD_EXACT). weakening_guard.py is
# additionally a check-script (doubly guarded), keeping the guard's own set-defining code in-set (the
# self-protection property: the guard is not falsifiable by the change it judges).
_FLOOR_VALIDATOR = (".engine/tools/validate.py", ".engine/tools/weakening_guard.py")
# The provisioning ruleset-applying operation — the guardrail-weakening "ruleset-affecting file". The branch ruleset does not
# travel as a file, so its APPLYING CODE is the guarded proxy: gutting it could apply a weakened ruleset
# with no on-disk correlate to surface it.
_FLOOR_RULESET_PROXY = (".engine/tools/bootstrap.py", ".engine/tools/team_switch.py")
# Enforcement-HOOK logic: files whose weakening loosens a live RUNTIME gate with NO on-disk floored correlate to
# surface it (unlike CODEOWNERS/settings.json CONTENT, whose weakenings appear as flagged diffs to those floored
# files). Hand-listed because they are not check-scripts and CANNOT be derived from settings.json: it wires gate
# hooks (modes.py, close.py) and non-gate hooks (boot/memory/telemetry) IDENTICALLY, so deriving all of them would
# re-guard the non-gate hooks and reintroduce the over-firing already fixed. Both block-budget members are here:
# modes.py (PreToolUse write-gate) and close.py (Stop finding-disposition gate) — the only two hooks that can emit
# a merge-relevant deny. A drift-detector test (test_seed.py) fails CI if a NEW PreToolUse/Stop hook whose code
# can emit a block (via hooks.block or hooks.decide) is wired in settings.json but not floored here — the
# gate-vs-non-gate call is DERIVED from the hook's own code, not a hand-maintained allowlist that could rot.
_FLOOR_ENFORCEMENT_HOOKS = (
    ".engine/tools/modes.py",          # the Explore/Build write-gate (PreToolUse block-budget member)
    ".engine/tools/close.py",          # the finding-disposition gate (Stop block-budget member; HARD-BLOCKS the turn)
    ".engine/tools/hook-runner.sh",    # the launcher EVERY hook runs through
    ".engine/tools/hooks.py",          # the hook-law substrate: block budget + fail-open harness
    ".engine/tools/issue_gate.py",     # the engine-Issue reroute matcher the write-gate consults
    ".engine/tools/github_client.py",  # the off-host/auth substrate BOTH guardrail-weakening guards depend on
    ".engine/tools/wiring.py",         # the sole mutator of settings.json / CODEOWNERS / hook registrations
    ".engine/tools/security_floor.py", # configures secret-scanning / push-protection
    ".engine/tools/providers.py",      # the provider-normalization seam EVERY gate's payload flows through —
    #                                    weakening normalize() un-recognizes the other runtime's edits
    ".engine/tools/codex-hook-runner.sh",  # the Codex launcher every Codex hook runs through (hook-runner's twin)
    ".engine/tools/codex_gen.py",      # renders the reviewer permission floors (read-only sandbox, policy files)
    #                                    the codex coherence checks then verify — weakening the renderer weakens
    #                                    what "in sync" means
    ".engine/tools/mechanic_build.py", # the engine-mechanic cross-repo-write gate (eADR-0026): its fail-closed,
    #                                    host-anchored belt (product_checkout_matches) authorizes running a
    #                                    SEPARATE checkout's own .engine tools and opening a PR against it — a
    #                                    weakening (fail-open on doubt, or an unanchored host parse accepting a
    #                                    look-alike origin) is local code execution + a write to the wrong repo,
    #                                    with NO on-disk floored correlate any check catches. A unit test is the
    #                                    wrong instrument (the same PR can flip the belt AND its test), so a change
    #                                    here routes through the guardrail-ack (hard tier, eADR-0040).
    ".engine/tools/repo_identity.py",  # the origin==home seam that SCOPES the two hard public-safety checks
    #                                    (memory-pointer-public-safety, census-completeness) and the negative-
    #                                    fixture harness. Flipping is_home_repo's fail-direction turns both
    #                                    checks into no-ops in the very repo they exist to protect — with NO
    #                                    diff to any guarded check script
    #                                    and no other on-disk floored correlate to surface it. Same criterion as
    #                                    mechanic_build.py above: a live runtime gate whose weakening is
    #                                    otherwise invisible, so it routes through the guardrail-ack.
)
# Traveling security-floor provisions — NOT enforcement gates (they check nothing and gate no merge), so they
# do not belong with _FLOOR_ENFORCEMENT_CONFIG above. They are the git-native security floor the control plane
# ships to EVERY generated repo: deleting or weakening one silently drops a
# safety pillar the operator was relying on, which the "disclose, never downgrade silently" law forbids — so a
# removal/weakening must route through the ack. `dependabot.yml` sits at the repo root, so (unlike its twin
# `secret-scan.yml`, a workflow already covered by the `.github/workflows/` prefix) it has no prefix basis and is
# floored here by exact path. Presence-SEEDING this file and disclosing a missing floor stay provisioning's job;
# this entry only gates its removal/weakening via a pull request.
_FLOOR_SECURITY_PROVISION = (".github/dependabot.yml",)
# Schema files that are the TEETH of a hard, merge-blocking (CI) schema-kind check (StarshipSuperjam/engine-template#467): the check rule names
# no schema (it is resolved through the surface catalog's `governing_schema`, or a `params.schema` override), so
# loosening the schema loosens that HARD gate with NO other on-disk correlate — the `.engine/check/` rule itself
# may be untouched. Guarded here by EXACT PATH, deliberately NOT by a blanket `.engine/schemas/` prefix: that
# would re-introduce the over-firing already removed, because ~half the files in `.engine/schemas/` are agent/tool
# OUTPUT contracts (plan-review-finding, audit-finding, conformance-verdicts, attention-result, knowledge, …)
# that back only a fixture unit test, gate no merge, and are correctly NOT guarded. The set is exactly the
# schemas a `kind: schema`, `tier: hard`, CI-suite check resolves to. A drift detector
# (test_seed.py::TestSchemaGateGuardCoverage) recomputes it from the LIVE check rules via the validator's own
# resolver and FAILS CI if a hard CI schema-kind check ever resolves to a schema not floored here — so this
# hand-list cannot rot as checks are added, while the guard stays import-light under pull_request_target (no
# catalog resolver imported here). A brand-new schema file is a pure addition (WEAKENING_STATUS excludes
# 'added'), so first-install is ungated; only a later weakening of a floored gate schema is held.
_FLOOR_GATE_SCHEMAS = (
    ".engine/schemas/agent.v1.json",
    ".engine/schemas/codex-agent.v1.json",
    ".engine/schemas/codex-hooks.v1.json",
    ".engine/schemas/codex-skill.v1.json",
    ".engine/schemas/concern-list.v1.json",
    ".engine/schemas/conduct.v1.json",
    ".engine/schemas/contract.v1.json",
    ".engine/schemas/doc.v1.json",
    ".engine/schemas/engine.v1.json",
    ".engine/schemas/execution-state.v1.json",
    ".engine/schemas/first-run-assets.v1.json",
    ".engine/schemas/interface.v1.json",
    ".engine/schemas/model-bindings.v1.json",
    ".engine/schemas/module.v1.json",
    ".engine/schemas/operation.v1.json",
    ".engine/schemas/policy.v1.json",
    ".engine/schemas/provider-exceptions.v1.json",
    ".engine/schemas/provisioning-catalog.v1.json",
    ".engine/schemas/skill.v1.json",
    ".engine/schemas/state.v1.json",
)
GUARDRAIL_EXACT = (_FLOOR_ENFORCEMENT_CONFIG + _FLOOR_VALIDATOR + _FLOOR_RULESET_PROXY
                   + _FLOOR_ENFORCEMENT_HOOKS + _FLOOR_SECURITY_PROVISION + _FLOOR_GATE_SCHEMAS)
# A pure addition strengthens; removal/rename/modification/copy can weaken.
# 'copied' is in GitHub's file-status enum — without it, a weakened *copy* of a
# guardrail file would slip through ungated.
WEAKENING_STATUS = {"removed", "renamed", "modified", "changed", "copied"}

# The base check-rule directory on disk. Like _BASE_MANIFEST, this reads from the TRUSTED BASE checkout (the guard
# runs on pull_request_target with only the base checked out), NEVER the PR head/diff — so a PR cannot repoint or
# delete a check rule to un-guard the very script it is weakening in the same PR: the base copy still points at
# that script, and the `.engine/check/` edit is independently flagged. A future change to scan the head/diff copy
# would REOPEN that hole. `<repo>/.engine/check`, three dirnames up from `<repo>/.engine/tools/weakening_guard.py`
# — the same anchor as _read_base_home, kept local so the guard stays import-light under pull_request_target
# (github_client + stdlib only; it deliberately does NOT import the validate.py dispatcher).
_BASE_CHECK_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), ".engine", "check")
_BLANKET_TOOLS_PREFIX = ".engine/tools/"  # the fail-safe fallback coverage (the prior blanket)
_DERIVE = object()  # sentinel: is_guardrail/flagged_changes derive the check-script set from disk (the default)
# A module-provided check-kind callable: `.engine/tools/<module>/kind_<name>.py` runs a
# validation kind's enforcement in CI but carries NO `params.script`, so the check-script derivation above
# structurally cannot reach it — yet neutering it (a `check` that always passes) silently disables every rule of
# that kind. It is guarded here by a PATH PROPERTY (the one-level filename↔kind convention the validator
# discovers by), not a disk scan: fail-safe with nothing to read, and it covers the file even when it is added.
# One level deep matches module-subdir ownership; a top-level `.engine/tools/kind_*.py` is not a discovered kind
# (and validate.py itself is floored). Add-vs-modify caveat: a brand-new kind file is a pure addition, so the
# FIRST install still enters with no ack (WEAKENING_STATUS excludes 'added') — only a later weakening is gated.
_KIND_CALLABLE_RE = re.compile(r"^\.engine/tools/[^/]+/kind_[^/]+\.py$")

# The INSTANCE-EXTENSIBLE floor (StarshipSuperjam/engine-template#532). A deployment can stand up its own guardrail in PRODUCT territory —
# e.g. a containment scanner whose enforcement CODE the engine cannot discover by presence (it is not a
# `.engine/check/` rule). Such a deployment declares the extra paths to guard in a committed operator-config
# file the guard UNIONS with its own set. The engine's own floor above is evaluated FIRST and independently,
# so an instance declaration can only ADD guarded paths, never subtract one — a broken or empty declaration
# never weakens the engine's floor. Read from the TRUSTED BASE checkout (like `_BASE_CHECK_DIR`/`_BASE_MANIFEST`),
# never the PR head/diff, so a PR cannot both weaken a declared path AND un-declare it in the same stroke: the
# base declaration still guards it. A malformed/degenerate declaration cannot reach base — the CI check
# `engine/check/operator-guarded-paths` (hard) blocks it at merge — so this reader defensively degrades an
# unreadable/absent file to the empty pair (no engine-floor effect) and lets that gate do the shape enforcement.
INSTANCE_DECL_REL = ".engine/operator-guarded-paths.json"
_BASE_INSTANCE_GUARDS = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    ".engine", "operator-guarded-paths.json")
_READ_INSTANCE = object()  # sentinel: is_guardrail/flagged_changes read the instance pair from base (the default)


def _read_instance_guards(path: str | None = None) -> tuple:
    """The deployment-declared extra guarded set, read from the BASE checkout: `(exact_paths, prefix_tuple)`.
    ABSENT/unreadable -> the EMPTY pair, SILENTLY (absent is the normal steady state — the construction repo and
    every deployment before its first declaration have no file; mirror `_read_base_home`, which treats absent as a
    silent None). Defensive parse: only non-empty string members survive, and a degenerate prefix (empty after
    strip, or bare `.`/`/`/`./`) is dropped so it can never be the `startswith("")`-guards-everything footgun —
    belt-and-braces behind the hard CI shape gate, which blocks such a declaration from ever reaching base."""
    path = path if path is not None else _BASE_INSTANCE_GUARDS
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            return (set(), ())
        exact = {p for p in data.get("guarded_paths", []) if isinstance(p, str) and p.strip()}
        prefixes = tuple(p for p in data.get("guarded_prefixes", [])
                         if isinstance(p, str) and p.strip() and p.strip() not in {".", "/", "./"})
        return (exact, prefixes)
    except Exception:  # noqa: BLE001 — absent / unreadable / malformed -> empty pair (the CI shape gate is the teeth)
        return (set(), ())


# ---------------------------------------------------------------------------
# TIER (eADR-0040). The guarded SET is unchanged (D-268's property); what changed is what a match does:
# "hard" blocks the merge pending the operator's deliberate `guardrail-ack`; "soft" is a plain-language
# disclosure and the check passes — the protected-branch merge review judges the change. The hard criterion is
# a PROPERTY, not a roster: a weakening with NO mechanical on-disk correlate to catch it and no
# operator-readable diff — plus the guard's own set-defining machinery. Everything else guarded discloses.
# What supplies the correlates that make disclosure honest: the hard-check-bite meta-check proves every hard
# check still catches its planted violation (so a neutered check script fails CI), and the operator reads
# every diff at the merge. AUTHORING INVARIANT: this tier machinery — _HARD_EXACT, classify(), and the
# directional detectors below — lives IN weakening_guard.py, a hard-floor member judged from the trusted
# base, so reclassifying a hard event as soft is itself a hard event. Do not extract it to a sibling module
# or a data file (either would land in the soft else-branch).
# FAIL-SAFE DIRECTION: anything the tier machinery cannot cleanly classify resolves HARD, never soft.
_HARD_EXACT = (
    # a demotion here is a silent killswitch with no correlate:
    ".engine/suites.json",             # decides WHICH suite blocks the merge (CI -> report-only is global)
    ".engine/uv.lock",                 # what code the validator + every guard RUN; its diff is unreadable
    #                                    hashes, and dependency-review covers product deps, NOT engine tooling
    ".github/CODEOWNERS",              # defines WHO performs the binding review — the criterion "the merge
    #                                    review reads every diff" is circular for this one file (zero churn)
    ".codex/hooks.json",               # the Codex gate-hook wiring twin (near-zero churn; the recorded hole)
    ".codex/config.toml",              # the Codex helper registration (same posture)
    # the guard's own set-defining machinery (self-protection):
    ".engine/tools/weakening_guard.py",
    # ruleset/write-target seams whose one-line fail-direction flip no diff read reliably catches:
    ".engine/tools/team_switch.py",
    ".engine/tools/mechanic_build.py",
    ".engine/tools/repo_identity.py",
    # the meta-check that makes demoting validate.py and the check scripts honest — it must itself block:
    ".engine/tools/hard_check_bite_check.py",
    # check scripts with a recorded not-applicable bite declaration — the meta-check has NO witness for
    # these, so neutering them has no mechanical correlate (the N/A census in test_hard_check_bite.py):
    ".engine/tools/protection_guard.py",
    ".engine/tools/dependency_discipline/review.py",
    ".engine/tools/product_design/lock_integrity.py",
)


def classify(path: str, status: str, prev: str = "", instance_guards=_READ_INSTANCE) -> str:
    """The tier of one flagged guardrail change: "hard" (blocks pending the ack) or "soft" (disclosure).
    Removal or rename of ANY guarded file is hard — rare, unambiguous, and never the measured noise. A
    hard-floor member is hard on any weakening status. A path a DEPLOYMENT declared in its instance floor is
    hard — the operator opted into that friction themselves, so their declaration keeps its old meaning
    (StarshipSuperjam/engine-template#532). Everything else guarded is soft: the disclosure tier. The directional detectors below escalate
    specific gate-shaped MODIFICATIONS of soft-tier files back to hard; they run separately in main()."""
    if instance_guards is _READ_INSTANCE:
        instance_guards = _read_instance_guards()
    if status in ("removed", "renamed"):
        return "hard"
    inst_exact, inst_prefixes = instance_guards
    for p in (path, prev):
        if not p:
            continue
        if p in _HARD_EXACT:
            return "hard"
        if p in inst_exact or (inst_prefixes and p.startswith(inst_prefixes)):
            return "hard"
    return "soft"



def _derive_check_scripts(check_dir: str | None = None) -> set | None:
    """The enforcement scripts guarded BY PRESENCE: every `.engine/check/*.json` rule's
    `params.script` path, read from the base checkout. Returns the set of repo-relative script paths, or None on
    ANY read/parse failure — the fail-safe sentinel telling the caller to fall back to guarding ALL of
    `.engine/tools/`. The failure is ALL-OR-NOTHING: a single unreadable/corrupt rule collapses the WHOLE
    derivation to the blanket fallback, never a partial set, so a broken rule can never silently drop its own
    script from the guarded set (the fail-open the design rejects)."""
    check_dir = check_dir if check_dir is not None else _BASE_CHECK_DIR
    scripts: set = set()
    try:
        for fn in sorted(os.listdir(check_dir)):
            if not fn.endswith(".json"):
                continue
            with open(os.path.join(check_dir, fn), encoding="utf-8") as fh:
                data = json.load(fh)
            script = (data.get("params") or {}).get("script")
            if isinstance(script, str) and script.strip():
                scripts.add(script)
    except Exception:  # noqa: BLE001 — ANY failure -> None -> caller guards the whole tools dir (fail-safe)
        return None
    return scripts


def is_guardrail(path: str, derived_scripts=_DERIVE, instance_guards=_READ_INSTANCE) -> bool:
    """True iff `path` is a guarded file: a floor member, under a guarded prefix, an enforcement script
    discovered by presence in the base check rules, or a path a DEPLOYMENT declared in its instance floor (StarshipSuperjam/engine-template#532).
    `derived_scripts` defaults to deriving from disk; `instance_guards` defaults to reading the base instance
    declaration; tests pass an explicit set / `(exact, prefixes)` pair (or None derived-set for the fail-safe
    sentinel). A None derived set -> also guard all of `.engine/tools/` (fail-safe when the check dir could not be
    read). The engine floor is checked FIRST and independently — the instance clause can only ADD, never subtract."""
    if derived_scripts is _DERIVE:
        derived_scripts = _derive_check_scripts()
    if instance_guards is _READ_INSTANCE:
        instance_guards = _read_instance_guards()
    if path.startswith(GUARDRAIL_PREFIXES) or path in GUARDRAIL_EXACT:
        return True
    if _KIND_CALLABLE_RE.match(path):  # a module-provided check-kind callable (enforcement logic, no params.script)
        return True
    inst_exact, inst_prefixes = instance_guards
    if path in inst_exact or (inst_prefixes and path.startswith(inst_prefixes)):
        return True                     # a deployment-declared product guardrail (StarshipSuperjam/engine-template#532) — union, never subtraction
    if derived_scripts is None:
        return path.startswith(_BLANKET_TOOLS_PREFIX)  # fail-safe: derivation failed -> guard the whole dir
    return path in derived_scripts


def _flagged_with_prev(files: list, derived_scripts, instance_guards) -> list:
    """THE single matching condition for a guardrail change — the one place the filter lives, so main()'s
    enforcement path and flagged_changes()'s public seam can never drift apart. Returns
    (status, name, prev) triples."""
    out = []
    for f in files:
        name = f.get("filename", "")
        status = f.get("status", "")
        prev = f.get("previous_filename", "")
        if status in WEAKENING_STATUS and (is_guardrail(name, derived_scripts, instance_guards)
                                           or (prev and is_guardrail(prev, derived_scripts, instance_guards))):
            out.append((status, name, prev))
    return out


def flagged_changes(files: list, derived_scripts=_DERIVE, instance_guards=_READ_INSTANCE) -> list:
    """Classifier: the guardrail files this diff removes, renames, modifies, or copies. Returns a list of
    (status, shown_path). Derives the check-script set AND the instance pair ONCE and threads them through
    is_guardrail (one disk scan per run, not per file). This is the public set-membership seam —
    knowledge_gen.py's `guarded` field and the shipped demos consume it; main() consumes the same filter
    through _flagged_with_prev and layers classify() on top (eADR-0040)."""
    if derived_scripts is _DERIVE:
        derived_scripts = _derive_check_scripts()
    if instance_guards is _READ_INSTANCE:
        instance_guards = _read_instance_guards()
    return [(status, name if not prev else f"{prev} -> {name}")
            for status, name, prev in _flagged_with_prev(files, derived_scripts, instance_guards)]


# The engine's update HOME lives in the manifest as a single key. A change to its VALUE (a repoint)
# redirects where executable engine code is fetched from at the next update — a supply-chain weakening
# that needs the deliberate ack (StarshipSuperjam/engine-template#367). The manifest is deliberately NOT whole-file guarded:
# it legitimately churns on every upgrade/add (version bumps) and on first-run setup, so blanket-guarding
# it would demand an ack on routine updates. Instead the detector compares the diff against the home
# recorded in the TRUSTED BASE manifest and FAILS CLOSED — so it cannot be falsified by the change it judges.
ENGINE_MANIFEST_REL = ".engine/engine.json"
_HOME_VALUE_RE = re.compile(r'"home_repository"\s*:\s*"([^"]*)"')
# The base manifest on disk. The guard runs on pull_request_target with ONLY the trusted base checked out,
# so this reads the base value (never head) — the authoritative "what the home is now" the repoint compares
# against. `<repo>/.engine/engine.json`, three dirnames up from `<repo>/.engine/tools/weakening_guard.py`.
_BASE_MANIFEST = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), ".engine", "engine.json")


def _read_base_home() -> str | None:
    """The `home_repository` recorded in the BASE manifest, read from disk (the trusted base checkout, never
    head). None when absent/unreadable — i.e. no home is recorded yet, so a home appearing in the diff is a
    first recording, not a repoint."""
    try:
        with open(_BASE_MANIFEST, encoding="utf-8") as fh:
            home = json.load(fh).get("home_repository")
        return home if isinstance(home, str) and home.strip() else None
    except Exception:  # noqa: BLE001 — absent / unreadable base manifest -> treated as no home recorded
        return None


def _diff_lines(patch: str) -> list:
    """The unified-diff `patch` split into lines on `\\n` ONLY — GitHub's sole line delimiter. NEVER
    `str.splitlines()`, which also breaks on CR, VT, FF, the FS/GS/RS separators, NEL, and U+2028/U+2029 —
    characters a GitHub diff and JSON treat as ordinary content, so `splitlines` would fragment one `+`
    line into pieces, and every fragment past the separator loses its `+`/`-` marker and goes invisible to
    the checks below. Splitting on `\\n` keeps each diff line whole, so an embedded separator stays inside
    its line where `_added_line_is_anomalous` can catch it (StarshipSuperjam/engine-template#550 review)."""
    return patch.split("\n")


def _added_line_is_anomalous(ln: str) -> bool:
    """True iff an ADDED manifest diff line carries content the plain-ASCII engine manifest never
    legitimately holds and which could disguise a key/value from the value checks: a backslash (a JSON
    string escape — `home_repositor\\u0079` folds back to the real key, last value wins), or an EMBEDDED
    line-separator that `str.splitlines` splits on but a GitHub `\\n`-delimited diff does not (CR, VT, FF,
    FS/GS/RS, NEL, U+2028/9 — the fragment past it would lose its `+` marker and hide a second key/value).
    A TRAILING CRLF `\\r` is deliberately NOT flagged — `"…"\\r`.splitlines() yields one piece — so a
    Windows-checkout diff never false-alarms; only an INTERNAL separator (which yields >1 piece) does."""
    return "\\" in ln or len(ln.splitlines()) > 1


def _touches_home_key(patch: str) -> bool:
    """True iff the unified-diff `patch` adds or removes any line mentioning the `home_repository` key — a
    SUBSTRING test (not a value regex), so a duplicate-key injection (JSON's last value wins, but the added
    key line still shows), a value split across lines, and any reformatting of the home line all register as
    a touch. The `+++`/`---` file headers are excluded."""
    for line in _diff_lines(patch):
        plus = line.startswith("+") and not line.startswith("+++")
        minus = line.startswith("-") and not line.startswith("---")
        if (plus or minus) and "home_repository" in line:
            return True
    return False


def home_repoint(files: list, base_home: str | None) -> tuple | None:
    """A change to the engine's update home when one is ALREADY recorded (`base_home`) is a supply-chain
    repoint — returns `(base_home, new_value_or_None, reason)` to flag, else None. FAILS CLOSED so the guard
    cannot be falsified by the change it judges. `reason` drives the operator message and is one of:
      - "changed" — a new home value was parsed (`new_value` carries it);
      - "deletion" — the home line is REMOVED and not re-added. A removal is not harmless: with no home
        recorded, the guard's own rule makes the NEXT change that adds one back a first recording, unflagged
        — so a deletion + a later add would compose into a silent two-PR repoint. A deletion therefore always
        keeps the flag (the review of StarshipSuperjam/engine-template#515 proved this composition against the first draft, which cleared it);
      - "unclear" — the home line is touched with an added line, but no clean single-line value could be read;
      - "unreadable-patch" — the whole manifest diff was too large for GitHub to return;
      - "escaped" — an ADDED manifest line carries a JSON string escape (a backslash), which the plain-ASCII
        engine manifest never legitimately needs and which can disguise the home key past the substring
        touch-test (JSON folds e.g. `home_repositor\\u0079` back to the real key, last value wins).

    ONE provably-benign carve-out (StarshipSuperjam/engine-template#515): the flag is suppressed ONLY when EVERY touched home line (added and
    removed alike) is EXACTLY a one-line `"home_repository": "<base>"` entry — bare key, base value, optional
    trailing comma, nothing else — AND at least one such line is ADDED (the home must SURVIVE the change).
    That admits the first-run trailing-comma reformat (which always re-adds the line) and nothing wider: a
    duplicate-key injection with a differing value, a value split across lines, a trailing fragment on the
    line, a pure deletion, an escaped key, and a patch too large to inspect all still flag. The carve-out is
    deliberately dumb — no patch application, no head reconstruction — and strictly narrower than the
    fail-closed default. A first recording (no `base_home`) is never a repoint; a version-only bump (no home
    line touched) does not flag."""
    if not base_home:
        return None                        # no home recorded yet -> establishing one is not a repoint
    # A touched home line is benign only as the EXACT one-line entry at the base value (bare key, optional
    # trailing comma, nothing else) — a full-line anchor, so a trailing fragment or split value fails it.
    benign_re = re.compile(r'^[+-]\s*"home_repository"\s*:\s*"' + re.escape(base_home) + r'"\s*,?\s*$')
    for f in files:
        if f.get("filename") != ENGINE_MANIFEST_REL:
            continue
        if f.get("status") not in WEAKENING_STATUS:
            continue
        patch = f.get("patch")
        if not patch:
            return (base_home, None, "unreadable-patch")   # a manifest change we cannot inspect -> fail closed
        added = [ln for ln in _diff_lines(patch) if ln.startswith("+") and not ln.startswith("+++")]
        # The engine manifest holds only plain-ASCII values (versions, package names, identity, the home
        # slug, the control-plane marker's ids + enumerated rule names), so a backslash escape OR an
        # embedded line-separator in an ADDED line is anomalous — either can disguise the home key or a
        # second hidden value past the checks below. Fail closed on it.
        if any(_added_line_is_anomalous(ln) for ln in added):
            return (base_home, None, "escaped")
        if _touches_home_key(patch):
            touched = [ln for ln in _diff_lines(patch)
                       if ((ln.startswith("+") and not ln.startswith("+++"))
                           or (ln.startswith("-") and not ln.startswith("---")))
                       and "home_repository" in ln]
            added_home = [ln for ln in touched if ln.startswith("+")]
            if touched and added_home and all(benign_re.match(ln) for ln in touched):
                continue                    # formatting churn around an unchanged, SURVIVING home value
            new = None
            for line in added_home:
                m = _HOME_VALUE_RE.search(line)
                if m and m.group(1) != base_home:
                    new = m.group(1)
            if new:
                reason = "changed"
            elif not added_home:
                reason = "deletion"          # the home line is removed and not re-added
            else:
                reason = "unclear"           # touched with an added line but no clean single-line value
            return (base_home, new, reason)
    return None


# The identity tier the manifest records; `team` carries the stronger floor (1 approval + code-owner review),
# so a change back to `solo` is a guardrail-weakening — the exact shape home_repoint guards for `home_repository`.
_TEAM = "team"
_IDENTITY_VALUE_RE = re.compile(r'"identity"\s*:\s*"([^"]*)"')


def _read_base_tier() -> str | None:
    """The `identity` tier recorded in the BASE manifest (trusted base checkout, never head). None when
    absent/unreadable — no tier recorded, so there is nothing to downgrade FROM."""
    try:
        with open(_BASE_MANIFEST, encoding="utf-8") as fh:
            tier = json.load(fh).get("identity")
        return tier if isinstance(tier, str) and tier.strip() else None
    except Exception:  # noqa: BLE001 — absent / unreadable base manifest -> treated as no tier recorded
        return None


def _touches_identity_key(patch: str) -> bool:
    """True iff the unified-diff `patch` adds or removes any line mentioning the `identity` key — a SUBSTRING
    test (mirrors _touches_home_key), so a duplicate-key injection, a value split across lines, and any
    reformatting of the identity line all register. The `+++`/`---` file headers are excluded."""
    for line in _diff_lines(patch):
        plus = line.startswith("+") and not line.startswith("+++")
        minus = line.startswith("-") and not line.startswith("---")
        if (plus or minus) and '"identity"' in line:
            return True
    return False


def identity_downgrade(files: list, base_tier: str | None) -> bool:
    """Lowering the identity tier from `team` back to `solo` is a guardrail-weakening — it drops the required-approval
    + code-owner floor the team tier enforces, a protection a non-engineer cannot see removed by reading a diff —
    so it needs the ack. Returns True to flag, else False. Only a repo whose BASE is already `team` can be
    downgraded (solo->team and a first `team` recording are STRENGTHENINGS, never gated). FAILS CLOSED like
    home_repoint: once base is team, a manifest change that touches the `identity` key and does not provably keep
    `team`, or a manifest change too large to inspect at all, both require the ack — defeating a duplicate-key
    injection or a value split across lines that a naive value-diff would miss."""
    if base_tier != _TEAM:
        return False                       # solo base (or none) -> not a downgrade
    for f in files:
        if f.get("filename") != ENGINE_MANIFEST_REL:
            continue
        if f.get("status") not in WEAKENING_STATUS:
            continue
        patch = f.get("patch")
        if not patch:
            return True                    # a manifest change we cannot inspect on a team repo -> fail closed
        added_lines = [ln for ln in _diff_lines(patch) if ln.startswith("+") and not ln.startswith("+++")]
        # Same fail-closed anomaly guard as home_repoint: a backslash escape or an embedded line-separator
        # in an added line could hide a second `"identity": "solo"` value past the value read below.
        if any(_added_line_is_anomalous(ln) for ln in added_lines):
            return True
        if _touches_identity_key(patch):
            # findall, not search — collect EVERY identity value on each added line, so a duplicate-key
            # injection on ONE line (`"identity": "team", "identity": "solo"`, last value wins) cannot hide
            # the downgrade behind the first (team) value.
            added = [v for ln in added_lines for v in _IDENTITY_VALUE_RE.findall(ln)]
            # touched the tier key: a downgrade unless every added `identity` value provably stays `team`
            if not added or any(v != _TEAM for v in added):
                return True
    return False


# The engine's EXECUTABLE build target (eADR-0026 engine-mechanic): the repo the engine may branch, commit, and
# open a pull request against. This detector is deliberately INVERTED from home_repoint on the axis that matters.
# home_repository is born-present (template-seeded, carried forward at first-run), so only a REPOINT ever happens
# and a first recording is benign. `product_build_target` is ABSENT by default and is first written by the
# mechanic itself LATER — so its first-set (absent -> present) IS the eADR-0011 re-classification (arming a
# previously-inert engine to write against an external repo) and MUST fire the ack. A repoint fires too; a
# DELETION (present -> absent) is benign (it reverts to the safe self-building default) and is safe to skip
# because any later re-add is itself a flagged first-set. The manifest is deliberately NOT whole-file guarded
# (version churn), so this value detector is the SOLE gate on a manifest-only arming PR — hence fail-closed.
_PRODUCT_BUILD_TARGET_VALUE_RE = re.compile(r'"product_build_target"\s*:\s*"([^"]*)"')
# Unlike the WEAKENING_STATUS siblings, THIS detector must ALSO evaluate an `added` manifest. Because first-set
# is the arming event, a manifest that ARRIVES (or is re-added after a deletion) carrying a target is a first-set
# that must fire — WEAKENING_STATUS excludes `added` (correct where first recording is benign, wrong here). A
# first-run manifest with NO target still passes cleanly (the key-touch test finds nothing and returns None).
_ARM_MANIFEST_STATUS = WEAKENING_STATUS | {"added"}


def _read_base_product_build_target() -> str | None:
    """The `product_build_target` recorded in the BASE manifest (trusted base checkout, never head). None when
    absent/unreadable — the normal self-building state, with no executable target set. UNLIKE a missing home,
    a missing target is NOT a licence to skip: a first-set (None -> value) is precisely the arming event the
    detector must flag."""
    try:
        with open(_BASE_MANIFEST, encoding="utf-8") as fh:
            v = json.load(fh).get("product_build_target")
        return v if isinstance(v, str) and v.strip() else None
    except Exception:  # noqa: BLE001 — absent / unreadable base manifest -> no target recorded
        return None


def _touches_product_build_target_key(patch: str) -> bool:
    """True iff the unified-diff `patch` adds or removes any line mentioning the `product_build_target` key — a
    SUBSTRING test (mirrors _touches_home_key), so a duplicate-key injection or a reformat all register."""
    for line in _diff_lines(patch):
        plus = line.startswith("+") and not line.startswith("+++")
        minus = line.startswith("-") and not line.startswith("---")
        if (plus or minus) and '"product_build_target"' in line:
            return True
    return False


def product_build_target_arm(files: list, base_target: str | None) -> tuple | None:
    """Arming (or repointing) the executable build target is a guardrail-weakening — it authorizes the engine to
    branch, commit, and open pull requests against an external repo, inheriting home_repository's supply-chain and
    command surface. Returns `(base_or_None, new_or_None, reason)` to flag, else None. INVERTED from home_repoint:
    a FIRST-SET (base absent -> a value appears) FIRES (the arming); a value change FIRES (a repoint); a DELETION
    (the target line removed and not re-added) is BENIGN — it reverts to the safe self-building default, needing no
    flag because any later re-add is itself a flagged first-set. FAILS CLOSED: a manifest change too large to
    inspect, or an anomalous added line that could hide the key, both flag REGARDLESS of base (a first-set could
    hide there — the fail-closed is intentionally broader than home's, which is safe because the manifest is tiny
    and never legitimately produces an unreadable patch). `reason`:
      - "set"              — a target value appears where the base had none (the arming);
      - "changed"          — the target value differs from the base (a repoint);
      - "unreadable-patch" — the manifest diff was too large for GitHub to return;
      - "escaped"          — an added manifest line carries a JSON escape or embedded separator that could hide
                             the key past the substring touch-test;
      - "unclear"          — the key is touched with an added line but no clean single-line value could be read.
    A version-only bump (no target line touched) does not flag; a same-value formatting touch does not flag; an
    `added` manifest WITH a target fires (a first-set arming), one WITHOUT a target passes."""
    for f in files:
        if f.get("filename") != ENGINE_MANIFEST_REL:
            continue
        if f.get("status") not in _ARM_MANIFEST_STATUS:   # WEAKENING_STATUS + 'added' (an added first-set arms)
            continue
        patch = f.get("patch")
        if not patch:
            return (base_target, None, "unreadable-patch")   # a manifest change we cannot inspect -> fail closed
        added = [ln for ln in _diff_lines(patch) if ln.startswith("+") and not ln.startswith("+++")]
        if any(_added_line_is_anomalous(ln) for ln in added):
            return (base_target, None, "escaped")
        if _touches_product_build_target_key(patch):
            added_target = [ln for ln in added if '"product_build_target"' in ln]
            if not added_target:
                continue                        # the key appears only on removed lines -> deletion -> benign
            # findall, not search — collect EVERY value on the added lines, so a duplicate-key injection
            # (JSON's last value wins) cannot hide the effective target behind a benign first value.
            values = [v for ln in added_target for v in _PRODUCT_BUILD_TARGET_VALUE_RE.findall(ln)]
            if not values:
                return (base_target, None, "unclear")   # touched + added but no clean single-line value
            new = None
            for v in values:
                if v != base_target:
                    new = v
            if new is None:
                continue                        # every added value equals base -> formatting churn -> benign
            reason = "changed" if base_target else "set"
            return (base_target, new, reason)
    return None


_QUOTED_RE = re.compile(r'"([^"]*)"')


def instance_declaration_shrink(files: list) -> tuple | None:
    """Removing a path from the deployment's instance floor (`.engine/operator-guarded-paths.json`, StarshipSuperjam/engine-template#532) is a
    guardrail-WEAKENING — it stops the guard flagging future edits to a path the deployment chose to protect, a
    protection a non-engineer cannot see removed by reading a diff. So a SHRINK of the declaration needs the ack,
    while a pure ADDITION (strengthening — declaring MORE) passes unflagged. This is the directional detector for
    the declaration file itself (mirroring `home_repoint`); the file is deliberately NOT whole-file floored, which
    would fire the ack on every strengthening add. Returns `(reason, dropped_list)` to flag, else None. `reason`:
      - "removed"          — the whole declaration file is deleted (every declared guard dropped);
      - "unreadable-patch" — the declaration diff was too large for GitHub to return (fail closed);
      - "escaped"          — an added/removed line carries a JSON escape or an embedded separator that could hide
                             a removal past the substring diff (fail closed);
      - "shrink"           — one or more quoted entries present in the base diff are removed and not re-added.
    Dumb by design (like home_repoint's carve-out): it compares quoted strings on `-` vs `+` lines, never applies
    the patch. A rename of an ENTRY (remove old + add new) reads as a removal of the old entry and flags — the safe
    direction, since the old path is no longer guarded. A pure reformat that keeps every entry present drops
    nothing. The re-add-under-an-inert-key decoy is closed upstream: the shape check forbids unknown top-level
    keys, so a re-added string is always a genuine `guarded_paths`/`guarded_prefixes` member — still guarded."""
    for f in files:
        name = f.get("filename", "")
        prev = f.get("previous_filename", "")
        # A rename OFF the canonical path is functionally a delete: the reader loads a FIXED path, so renaming the
        # declaration away silently drops every guard post-merge, exactly like `status: removed`. Catch both.
        renamed_away = prev == INSTANCE_DECL_REL and name != INSTANCE_DECL_REL
        if name != INSTANCE_DECL_REL and not renamed_away:
            continue
        if f.get("status") not in WEAKENING_STATUS:
            continue                       # an ADDED declaration is a strengthening — never a shrink
        if renamed_away or f.get("status") == "removed":
            return ("removed", [])         # the declaration is gone from its canonical path -> every guard dropped
        patch = f.get("patch")
        if not patch:
            return ("unreadable-patch", [])  # a declaration change we cannot inspect -> fail closed
        removed_lines = [ln for ln in _diff_lines(patch) if ln.startswith("-") and not ln.startswith("---")]
        added_lines = [ln for ln in _diff_lines(patch) if ln.startswith("+") and not ln.startswith("+++")]
        if any(_added_line_is_anomalous(ln) for ln in removed_lines + added_lines):
            return ("escaped", [])         # an escape/embedded separator could disguise a removal -> fail closed
        removed_strs = {s for ln in removed_lines for s in _QUOTED_RE.findall(ln)}
        added_strs = {s for ln in added_lines for s in _QUOTED_RE.findall(ln)}
        # The two array KEYS are not guarded ENTRIES, so filter them from the reported list — a change that only
        # reflows the key lines but keeps every entry drops nothing.
        dropped = sorted((removed_strs - added_strs) - {"guarded_paths", "guarded_prefixes"})
        if dropped:
            return ("shrink", dropped)
    return None


# ---------------------------------------------------------------------------
# DIRECTIONAL DETECTORS (eADR-0040): gate-shaped MODIFICATIONS of soft-tier files that escalate back to
# hard. Each is dumb by design (patch-line reading, never patch application), and FAILS CLOSED: an
# unreadable patch, an anomalous line, or a shape it cannot cleanly classify escalates. Each returns a list
# of (path, reason) to escalate, or None.

_CHECK_RULE_RE = re.compile(r"^\.engine/check/[^/]+\.json$")
_MSG_LINE_RE = re.compile(r'^[+-]\s*"message"\s*:\s*"(.*)"\s*,?\s*$')


def _is_closed_json_string(body: str) -> bool:
    """True iff `body` (the captured text between the outer quotes of a one-line JSON string) contains no
    unescaped quote — i.e. the line really is ONE closed string, with no second key smuggled after it.
    Escape-aware: strip `\\\\` pairs, then `\\"`, then any remaining `"` is unescaped."""
    return '"' not in body.replace("\\\\", "").replace('\\"', "")


def check_rule_demotion(files: list) -> list | None:
    """A modification INSIDE a check rule (`.engine/check/*.json`) can demote the gate the rule defines with
    the file still present: flip `tier` hard->soft, drop "CI" from (or delete) `suites`, repoint
    `params.script` or `kind` at something that passes, widen `ci_author_exempt`/`ci_label_exempt`, or flip
    the lifecycle `status`. Rather than enumerate those (a list that rots — and `params` keys share no
    naming convention), this detector is the fail-closed INVERSION, mirroring home_repoint's single narrow
    carve-out: a rule modification is benign ONLY when every touched line is provably the rule's operator-
    facing `message` string and nothing else. Reasons: "unreadable-patch", "escaped" (an embedded line
    separator), "structural" (a non-message line touched), "unclear" (a message line that is not one closed
    string — a smuggled second key). Removal/rename of a rule file is already hard via classify()."""
    hits = []
    for f in files:
        name = f.get("filename", "")
        if not _CHECK_RULE_RE.match(name) or f.get("status") not in ("modified", "changed", "copied"):
            continue
        patch = f.get("patch")
        if not patch:
            hits.append((name, "unreadable-patch"))
            continue
        verdict = None
        for ln in _diff_lines(patch):
            plus = ln.startswith("+") and not ln.startswith("+++")
            minus = ln.startswith("-") and not ln.startswith("---")
            if not (plus or minus):
                continue
            if len(ln.splitlines()) > 1:
                verdict = "escaped"
                break
            m = _MSG_LINE_RE.match(ln)
            if not m:
                verdict = "structural"
                break
            if not _is_closed_json_string(m.group(1)):
                verdict = "unclear"
                break
        if verdict:
            hits.append((name, verdict))
    return hits or None


_WORKFLOW_RE = re.compile(r"^\.github/workflows/[^/]+\.ya?ml$")
# Tokens whose presence on a touched workflow line marks a gate-shaped edit: the trigger (a
# pull_request_target -> pull_request flip runs the PR's own copy of a guard — the exact defeat eADR-0011's
# trusted-base isolation exists to prevent), the token/permission grants, a checkout repoint at the PR head,
# a conditional skip, and the validator invocation. A `uses:` line is judged separately by ACTION PATH so a
# same-action version pin (the routine dependabot bump) stays soft while an action swap escalates.
_WF_GATE_TOKENS = ("pull_request", "permissions", "secrets.", "GITHUB_TOKEN", "if:", "ref:",
                   "repository:", "head.sha", "validate.py", "--check")
_WF_USES_RE = re.compile(r"^[+-]\s*(?:-\s*)?uses\s*:\s*([^@\s]+)")


def workflow_gate_edit(files: list) -> list | None:
    """A one-line workflow MODIFICATION can disarm a gate with the file still present. Escalates when a
    touched line carries a gate token, when a `uses:` line changes its ACTION PATH (not merely its pinned
    version), or fail-closed on an unreadable patch or an embedded separator. Ordinary step-body and comment
    churn — and the routine dependabot same-action version bump — stays soft."""
    hits = []
    for f in files:
        name = f.get("filename", "")
        if not _WORKFLOW_RE.match(name) or f.get("status") not in ("modified", "changed", "copied"):
            continue
        patch = f.get("patch")
        if not patch:
            hits.append((name, "unreadable-patch"))
            continue
        verdict = None
        removed_actions, added_actions = set(), set()
        for ln in _diff_lines(patch):
            plus = ln.startswith("+") and not ln.startswith("+++")
            minus = ln.startswith("-") and not ln.startswith("---")
            if not (plus or minus):
                continue
            if len(ln.splitlines()) > 1:
                verdict = "escaped"
                break
            m = _WF_USES_RE.match(ln)
            if m:
                (added_actions if plus else removed_actions).add(m.group(1))
                continue
            if any(tok in ln for tok in _WF_GATE_TOKENS):
                verdict = "gate-line"
                break
        if not verdict and (removed_actions != added_actions):
            verdict = "action-swap"  # an action path appeared or vanished — not a same-action version pin
        if verdict:
            hits.append((name, verdict))
    return hits or None


# The gate-hook wires in `.claude/settings.json`: removing one un-wires a live runtime gate (the recorded
# "live hole"). Additions and other settings churn (new non-gate hooks, env, permissions style) stay soft —
# all measured churn there is "wire a new hook". `.codex/hooks.json` needs no twin detector: it is whole-file
# hard in _HARD_EXACT (near-zero churn).
_SETTINGS_PATH = ".claude/settings.json"
# '"matcher"' is here because a PreToolUse/Stop block's matcher REWRITE (not just a hook-line removal)
# silently un-scopes every gate hook in the block — the write-gate stays wired but never fires.
_SETTINGS_GATE_TOKENS = ("modes.py", "close.py", "hook-runner.sh", "PreToolUse", '"Stop"', '"matcher"')


def settings_gate_unwire(files: list) -> list | None:
    """Escalates a `.claude/settings.json` modification when a REMOVED line names a gate hook or its
    lifecycle block; fail-closed on an unreadable patch or an embedded separator."""
    hits = []
    for f in files:
        if f.get("filename") != _SETTINGS_PATH or f.get("status") not in ("modified", "changed", "copied"):
            continue
        patch = f.get("patch")
        if not patch:
            hits.append((_SETTINGS_PATH, "unreadable-patch"))
            continue
        verdict = None
        for ln in _diff_lines(patch):
            plus = ln.startswith("+") and not ln.startswith("+++")
            minus = ln.startswith("-") and not ln.startswith("---")
            if not (plus or minus):
                continue
            if len(ln.splitlines()) > 1:
                verdict = "escaped"
                break
            if minus and any(tok in ln for tok in _SETTINGS_GATE_TOKENS):
                verdict = "gate-unwire"
                break
        if verdict:
            hits.append((_SETTINGS_PATH, verdict))
    return hits or None


# The ruleset-applying lines in bootstrap.py: the branch ruleset lives on GitHub with no on-disk correlate,
# so a weakened APPLY is invisible to every other check. Label/provisioning churn in the same file stays
# soft. team_switch.py (the other ruleset proxy) is whole-file hard in _HARD_EXACT (near-zero churn).
_BOOTSTRAP_PATH = ".engine/tools/bootstrap.py"
# Narrowed to the ruleset-CONFIG vocabulary (the GitHub ruleset API's own field names + the frozen
# required-check roster constant): bare "ruleset"/"protection" fired on provisioning copy in a file that is
# entirely about branch protection (backtest, eADR-0040). Escalation-only heuristic — narrowing it trades
# recall on exotic shapes for the measured noise, and removal of the file stays hard via classify().
# The FULL field literals of the ruleset vocabulary bootstrap.py actually writes — deliberately not the
# bare required_/require_ prefixes, which match the ordinary `self.required_checks` attribute threaded
# through the whole class and would escalate comment-only churn (re-audit, eADR-0040). The rule-type
# strings cover dropping force-push/deletion/pull-request protection rules themselves.
_BOOTSTRAP_GATE_TOKENS = ("required_status_checks", "required_approving_review",
                          "require_code_owner_review", "required_review_thread_resolution",
                          "require_last_push_approval", "dismiss_stale", "bypass", "enforcement",
                          "REQUIRED_CHECKS", "non_fast_forward", '"deletion"', '"pull_request"')


def bootstrap_ruleset_edit(files: list) -> list | None:
    """Escalates a bootstrap.py modification when a touched line carries ruleset vocabulary; fail-closed on
    an unreadable patch or an embedded separator."""
    hits = []
    for f in files:
        if f.get("filename") != _BOOTSTRAP_PATH or f.get("status") not in ("modified", "changed", "copied"):
            continue
        patch = f.get("patch")
        if not patch:
            hits.append((_BOOTSTRAP_PATH, "unreadable-patch"))
            continue
        verdict = None
        for ln in _diff_lines(patch):
            plus = ln.startswith("+") and not ln.startswith("+++")
            minus = ln.startswith("-") and not ln.startswith("---")
            if not (plus or minus):
                continue
            if len(ln.splitlines()) > 1:
                verdict = "escaped"
                break
            if any(tok in ln for tok in _BOOTSTRAP_GATE_TOKENS):
                verdict = "ruleset-line"
                break
        if verdict:
            hits.append((_BOOTSTRAP_PATH, verdict))
    return hits or None


_DIRECTIONAL_DETECTORS = (check_rule_demotion, workflow_gate_edit, settings_gate_unwire,
                          bootstrap_ruleset_edit)

# Rendering safety (eADR-0040): the disclosure is ALWAYS emitted, and its text reaches a markdown run
# summary and a stdout stream GitHub parses for `::` workflow commands — so every PR-controlled path is
# passed through this conservative whitelist (the overlay-disclosure discipline: strip, never
# backslash-escape). No colon survives, so a filename can never forge a `::notice::`/`::stop-commands::`
# command; no backtick or bracket survives, so it cannot break the summary's fencing.
_SAFE_PATH_RE = re.compile(r"[^A-Za-z0-9._/ >-]")
_MAX_LISTED = 50


def _safe_shown(shown: str) -> str:
    """A rendered path (or "prev -> name" pair), reduced to the conservative whitelist and capped."""
    return _SAFE_PATH_RE.sub("", shown)[:300]


# A generous page bound: ~10k files at 100/page, well past GitHub's ~3000-file listing
# cap. It exists only to halt a pathological Link cycle — exceeding it raises (the caller
# fails closed), never silently truncates the file list it then judges.
MAX_PAGES = 100

# This guard's GitHub API User-Agent (was inline in its own request builder, now homed in
# github_client). The authenticated request shape + the off-host guard the guardrail-weakening protection
# relies on now live in github_client; this guard reads the diff through the GET-only
# helpers below and never issues a write.
_UA = "engine-seed-weakening-guard"


def fetch_all_changed_files(repo: str, number, token: str) -> list:
    """The COMPLETE list of changed-file objects for the pull request, following Link
    pagination to exhaustion. Raises on a pathological Link cycle (more than MAX_PAGES
    pages) so the caller fails closed rather than judging a truncated set."""
    files = []
    url = f"/repos/{repo}/pulls/{number}/files?per_page=100"
    pages = 0
    while url:
        pages += 1
        if pages > MAX_PAGES:
            raise RuntimeError(f"changed-files pagination exceeded {MAX_PAGES} pages")
        page, link = get_page(url, token, user_agent=_UA)
        files.extend(page)
        url = next_link(link)
    return files


def changed_files_total(repo: str, number, token: str):
    """The pull request's authoritative changed-file count (GET /pulls/{n} -> changed_files).
    This count is the true total and is NOT subject to the files-listing cap, so it is the
    yardstick for whether the paginated listing was complete."""
    pr = get_json(f"/repos/{repo}/pulls/{number}", token, user_agent=_UA)
    return pr.get("changed_files")


def emit(findings: list) -> int:
    """Write the finding.v1 array to stdout (the custom/script machine channel) and return
    0 — a successful evaluation, whatever it found. Each finding carries its own severity;
    the dispatcher's custom/script kind decides where the teeth land. Human-readable prose
    — including the deliberate guardrail-ack guidance — lives inside each finding's
    `message`, so stdout stays pure JSON."""
    print(json.dumps(findings))
    return 0


def main() -> int:
    tier = os.environ.get("ENGINE_RULE_TIER", "hard")  # the rule's tier, passed by the kind
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    token = os.environ.get("GITHUB_TOKEN", "")
    event_path = os.environ.get("GITHUB_EVENT_PATH", "")
    if not (repo and token and event_path and os.path.exists(event_path)):
        # Fail closed: a required check that cannot read the PR context blocks until it can.
        return emit([{"severity": tier, "location": None,
                      "message": "GUARDRAIL CHECK: could not read the pull request "
                      "context; failing closed."}])
    with open(event_path, encoding="utf-8") as fh:
        event = json.loads(fh.read())
    pr = event.get("pull_request") or {}
    number = pr.get("number")
    labels = {l.get("name") for l in (pr.get("labels") or [])}
    if number is None:
        return emit([{"severity": tier, "location": None,
                      "message": "GUARDRAIL CHECK: no pull request number in the "
                      "event; failing closed."}])
    try:
        # Read ALL changed files (paginated to completion) AND the authoritative count —
        # both inside this fail-closed block, so any read failure, an off-host Link, or a
        # pathological Link cycle becomes the plain-language fail-closed finding below,
        # never an unhandled path.
        files = fetch_all_changed_files(repo, number, token)
        expected = changed_files_total(repo, number, token)
    except Exception as e:  # fail closed — never wave a change through unjudged
        return emit([{"severity": tier, "location": None,
                      "message": f"GUARDRAIL CHECK: could not read the changed files "
                      f"({e}); failing closed."}])

    # Completeness gate (the guardrail-weakening non-falsifiability property): a guardrail-
    # weakening edit must not hide past GitHub's file-listing cap. If the guard could not
    # read EVERY changed file — fewer files seen than the pull request's authoritative
    # changed_files count, or no count at all — it fails closed and asks for the deliberate
    # acknowledgment; it never judges a pull request from a partial view. The cause here is
    # PR SIZE, not a detected weakening, so the message says so plainly and stays distinct
    # from the change-detected message below — the operator must never be told a guard
    # weakened when none was confirmed.
    # Count DISTINCT filenames — the same way GitHub's changed_files counts — so a
    # duplicate listing entry (or a pagination overlap) can never inflate the tally to
    # match the authoritative count while a real file goes unseen (the guard must not
    # be falsifiable by the change it judges).
    seen = len({f.get("filename", "") for f in files})
    if not isinstance(expected, int) or seen < expected:
        if isinstance(expected, int):
            detail = (f"changes {expected} files — more than the safety check can read in "
                      f"one pass (it could read {seen}; GitHub limits how many files it "
                      "lists at once)")
        else:
            detail = ("did not report how many files it changes, so the safety check "
                      f"cannot confirm it read them all (it read {seen})")
        if ACK_LABEL in labels:
            # eADR-0040: the ack DOWNGRADES, never erases — the fail-closed record survives as a
            # disclosure. (Before the tier split this path never honored the label at all, despite its
            # own message promising it would — fixed here.)
            return emit([{"severity": "soft", "location": None,
                          "message": "ACKNOWLEDGED (guardrail-ack applied) — kept as a record, no longer "
                          "blocking: this pull request " + detail + ". "
                          "The safety check could not read every changed file, and you approved "
                          "proceeding by applying the label."}])
        return emit([{"severity": tier, "location": None,
                      "message": "GUARDRAIL CHECK — this pull request " + detail + ".\n\n"
                      "Rather than judge your safety gates from a partial view, this check "
                      "is blocking.\n"
                      f"To approve this deliberately, apply the `{ACK_LABEL}` label to this "
                      "pull request (one deliberate action, distinct from the merge click). "
                      "Splitting the change into smaller pull requests also lets the check "
                      "read every file. Until then, this check blocks the merge."}])

    # Tier split (eADR-0040): classify every flagged file, then let the directional detectors escalate
    # gate-shaped modifications of soft-tier files back to hard. One derivation, threaded through.
    derived = _derive_check_scripts()
    inst = _read_instance_guards()
    hard_files, soft_files = [], []
    for status, name, prev in _flagged_with_prev(files, derived, inst):
        shown = name if not prev else f"{prev} -> {name}"
        bucket = hard_files if classify(name, status, prev, inst) == "hard" else soft_files
        bucket.append((status, shown))
    esc_reason = {}
    for det in _DIRECTIONAL_DETECTORS:
        for path, reason in (det(files) or []):
            esc_reason.setdefault(path, reason)
    if esc_reason:
        promoted = [(s, shown) for s, shown in soft_files if shown.split(" -> ")[-1] in esc_reason]
        soft_files = [(s, shown) for s, shown in soft_files if shown.split(" -> ")[-1] not in esc_reason]
        hard_files.extend(promoted)
    repoint = home_repoint(files, _read_base_home())
    downgrade = identity_downgrade(files, _read_base_tier())
    shrink = instance_declaration_shrink(files)
    arm = product_build_target_arm(files, _read_base_product_build_target())
    if not hard_files and not soft_files and not repoint and not downgrade and not shrink and not arm:
        return emit([])  # nothing weakens
    acked = ACK_LABEL in labels
    findings = []

    def _listing(pairs):
        shown_lines = [f"  - {status}: {_safe_shown(shown)}" for status, shown in pairs[:_MAX_LISTED]]
        if len(pairs) > _MAX_LISTED:
            shown_lines.append(f"  - …and {len(pairs) - _MAX_LISTED} more")
        return "\n".join(shown_lines)

    parts = ["GUARDRAIL CHANGE DETECTED — this pull request changes protection you rely on:\n"]
    if hard_files:
        listing = _listing(hard_files)
        reasons = {shown.split(" -> ")[-1]: esc_reason.get(shown.split(" -> ")[-1])
                   for _, shown in hard_files}
        esc_note = ""
        if any(reasons.values()):
            esc_note = ("  (at least one of these changed a line that configures the gate itself — a "
                        "trigger, a tier, a suite, an exemption, or a ruleset line — the kind of one-line "
                        "change a diff read can miss)\n")
        parts.append("Files that enforce your safety gates, where this change could turn the protection "
                     "off in a way a diff read can miss:\n" + listing + "\n" + esc_note + "\n"
                     "If merged unwatched, a safety check could be turned off, renamed, or loosened — "
                     "letting future changes reach the protected branch without being checked.\n")
    if repoint:
        old, new, reason = repoint
        old, new = _safe_shown(old or ""), (_safe_shown(new) if new else new)
        if reason == "changed":
            lead = f"Your engine's update home is being changed from {old} to {new}."
        elif reason == "deletion":
            lead = (f"Your engine's update home ({old}) is being REMOVED from `.engine/engine.json`. "
                    f"Removing the recorded home is not harmless: once no home is recorded, the next change "
                    f"that adds one back is treated as a first-time setup and is not re-checked — so this "
                    f"removal is where the safety check has to stop and ask you.")
        elif reason == "escaped":
            lead = (f"A change to `.engine/engine.json` (where your update home, {old}, is recorded) adds an "
                    f"unusual character — a backslash escape or a hidden line break — where the engine's "
                    f"settings are normally plain text. This check can't safely read what it does, and such "
                    f"a character can hide a change to the home, so it stops and asks you.")
        elif reason == "unreadable-patch":
            lead = (f"A change to `.engine/engine.json` (where your update home, {old}, is recorded) was too "
                    f"large for this check to read in full, so it can't confirm whether the home changed — "
                    f"confirm this change before merging.")
        else:  # "unclear" — the home line was touched but no clean value could be read
            lead = (f"The `home_repository` line in `.engine/engine.json` was changed in a way this check "
                    f"couldn't cleanly read (it is {old} today) — confirm the value in this pull request's "
                    f"changed files before merging.")
        parts.append(lead + " This matters because that setting decides WHERE "
                     "your engine's own code is fetched from when it updates — a supply-chain change: a "
                     "wrong or look-alike home could feed your engine altered code at its next update. The "
                     "engine cannot itself tell a genuine home from a convincing look-alike — only you can "
                     "confirm this is the home you intend.\n")
    if downgrade:
        parts.append("Your engine is being switched from team mode back to on-your-own (solo) mode. In team mode a "
                     "separate identity's approval is required before anything merges — switching back removes "
                     "that required approval, so future changes could merge with only the automatic checks and no "
                     "second sign-off. Only you can confirm you mean to give up that protection.\n")
    if shrink:
        reason, dropped = shrink
        if reason == "removed":
            lead = (f"Your list of extra protected files (`{INSTANCE_DECL_REL}`) is being REMOVED entirely. "
                    f"That list is where your project adds its OWN files to the ones this safety check watches — "
                    f"deleting it stops the check from flagging future edits to every path it named.")
        elif reason == "unreadable-patch":
            lead = (f"A change to your list of extra protected files (`{INSTANCE_DECL_REL}`) was too large for this "
                    f"check to read in full, so it cannot confirm whether a protected path was dropped — confirm "
                    f"this change before merging.")
        elif reason == "escaped":
            lead = (f"A change to your list of extra protected files (`{INSTANCE_DECL_REL}`) adds an unusual "
                    f"character — a backslash escape or a hidden line break — where the list is normally plain "
                    f"text. That can hide a removed path from this check, so it stops and asks you.")
        else:  # "shrink"
            listing = ", ".join(_safe_shown(d) for d in dropped)
            lead = (f"Your list of extra protected files (`{INSTANCE_DECL_REL}`) is having entries REMOVED: "
                    f"{listing}. That list is where your project adds its OWN files to the ones this safety check "
                    f"watches — removing an entry stops the check from flagging future edits to it.")
        parts.append(lead + " Adding to this list is fine and never stops here; only REMOVING a protection does, "
                     "because a protection you added is being taken away. Only you can confirm you mean to.\n")
    if arm:
        old, new, reason = arm
        old, new = (_safe_shown(old) if old else old), (_safe_shown(new) if new else new)
        if reason == "set":
            lead = (f"Your engine is being pointed at a build target it can ACT ON: `{new}`. Until now this engine "
                    f"opened pull requests only against its own repository; recording this target authorizes it to "
                    f"branch, commit, and open pull requests against `{new}`.")
        elif reason == "changed":
            lead = (f"Your engine's executable build target is being changed from `{old}` to `{new}` — the "
                    f"repository the engine branches, commits, and opens pull requests against.")
        elif reason == "escaped":
            lead = ("A change to `.engine/engine.json` (where your engine's executable build target is recorded) "
                    "adds an unusual character — a backslash escape or a hidden line break — where the engine's "
                    "settings are normally plain text. That can hide a build-target change, so this check stops "
                    "and asks you.")
        elif reason == "unreadable-patch":
            lead = ("A change to `.engine/engine.json` (where your engine's executable build target is recorded) "
                    "was too large for this check to read in full, so it cannot confirm whether the target was set "
                    "or changed — confirm this change before merging.")
        else:  # "unclear" — the target line was touched but no clean value could be read
            lead = ("The `product_build_target` line in `.engine/engine.json` was changed in a way this check "
                    "couldn't cleanly read — confirm the value in this pull request's changed files before merging.")
        parts.append(lead + " This matters because that setting decides WHICH repository your engine may open "
                     "pull requests against and run against — the same command-and-supply-chain surface as your "
                     "update home: a wrong or tampered value redirects where your engine writes and runs code. "
                     "Only you can confirm this is the repository you intend the engine to build.\n")
    parts.append(f"To approve this deliberately, apply the `{ACK_LABEL}` label to this pull request (one "
                 "deliberate action, distinct from the merge click). Until then, this check blocks the merge.")

    hard_present = bool(hard_files or repoint or downgrade or shrink or arm)
    if hard_present:
        if acked:
            # eADR-0040: the ack DOWNGRADES a killswitch finding to a disclosure; it never erases the
            # record. (Before the tier split the label erased every finding.)
            findings.append({"severity": "soft", "location": None,
                             "message": "ACKNOWLEDGED (guardrail-ack applied) — kept as a record, no "
                             "longer blocking:\n\n" + "\n".join(parts[:-1])})
        else:
            findings.append({"severity": tier, "location": None, "message": "\n".join(parts)})
    if soft_files:
        findings.append({"severity": "soft", "location": None,
                         "message": "GUARDRAIL DISCLOSURE — this pull request modifies enforcement files. "
                         "No action is needed: this notice does not block the merge, needs no label, and "
                         "must never be a reason to change a design.\n\n"
                         "Enforcement files modified:\n" + _listing(soft_files) + "\n\n"
                         "Each of these constitutes or configures a safety gate, so the change deserves a "
                         "read at the merge — your review of this pull request is the gate that judges it. "
                         "A neutered check script would also be caught mechanically: every merge-blocking "
                         "check must still catch its planted violation in CI (the checker-of-checkers)."})
    return emit(findings)


if __name__ == "__main__":
    sys.exit(main())
