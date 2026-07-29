#!/usr/bin/env python3
"""Module coherence — the first consumer of the validation foundation's coherence legs.

After any install / uninstall / upgrade the module manager confirms the installed module
set is consistent by calling the validation foundation's coherence legs directly — a
library call, not a suite trigger.
The permanent module manager lands later; this is the seed consumer it inherits.
It also runs as a CLI so the coherence behaviour has an operator-runnable demonstration:

  uv run --directory .engine -- python tools/module_coherence.py          # plain language
  uv run --directory .engine -- python tools/module_coherence.py --json   # finding.v1 JSON

It reads the present module manifests (.engine/modules/*/manifest.json) — "installed means
present", a directory listing, never a hand-authored registry — and the engine manifest
(.engine/engine.json), then reports five coherence legs:

  - DEPENDENCY (reused from the validation foundation, validate.coherence_findings): every
    declared dependency is installed and within its version range, and the graph is acyclic.
  - OWNERSHIP (validate.ownership_findings): every engine file under .engine/ is claimed by
    exactly one module's `provides`, or is a named foundation infrastructure artifact, or is
    a module manifest (owned by its own module by construction). An unclaimed file is an
    orphan; a doubly-claimed file is a conflict. The ownership inventory is the COMMITTED tree
    (git-tracked): an untracked file — sync-conflict cruft a file-sync tool dropped, or an
    intended new file not yet committed — is not yet an ownership concern, so it is not read as a
    spurious local orphan (#281); the untracked-surface detector (untracked_surface_findings, a
    separate soft custom/script check) names it instead.
  - WIRING — BIDIRECTIONAL "declared <-> applied". FORWARD declared->applied (validate.wiring_findings
    over wiring.is_applied): every `wires` directive a present manifest declares is applied in its shared
    target file. An mcp wire is APPROVAL-BLIND — it checks the committed .mcp.json definition, never the
    operator's runtime approval (a server not live for a session shows up as an ABSENT tool and is
    surfaced to the operator by boot's AI-observed live-helper check / the control plane's PR-Validation
    surface, not here — availability subsumes approval). REVERSE applied->declared, the orphan-wire leg (validate.orphan_wire_findings over
    wiring.applied_engine_wires + declared_wire_identities): nothing engine-identified applied in the
    PLATFORM-SHARED files matches no present manifest's `wires` (a stale leftover after an incomplete
    uninstall). The reverse leg covers the three shared-file seams (hook / mcp / gitignore) — the only
    place an orphan has no other governance; PERMISSION (not engine-identifiable) and ONTOLOGY-ENTRY (the
    engine-owned catalog, covered by the OWNERSHIP leg + the separate catalog-coverage gate) are excluded.
    A drifted same-identity entry is reported once, by the forward leg (not double-flagged). The foundation
    `.gitignore` block IS a keyed fence (FOUNDATION_IGNORES_FENCE), but a library-helper one no
    manifest declares, so the reverse leg carves it out at wiring.applied_engine_wires (never an orphan).
  - BLOCK-BUDGET (validate.block_budget_findings over the declared block registry): every block an
    owning system declares sits on a block-eligible event — only PreToolUse and Stop may hard-block
    (the block-budget law). The registry is ASSEMBLED from each owner's declaration
    (hooks names none): modes' explore write-gate (PreToolUse) and close's findings-disposition
    gate (Stop). Both events are eligible, so the leg is green over the two real members (and
    would fire the moment any owner declared a block on a non-eligible event).

Deferred (named): the uncatalogued-surface leg belongs to catalog coverage (validators-core); the one
pathological ontology residue a botched uninstall could leave (a catalog record whose home dir exists but
is empty, so neither catalog-coverage nor the ownership leg fires) is a catalog-coverage hardening
concern, not module coherence's (owes -> catalog-coverage).

Discovery and loading are exposed as reusable functions (discover_manifests,
load_engine_manifest) so the self-map and the module manager read the
present set from here rather than re-walking it — one present-set reader, no drift.
"""
from __future__ import annotations
import glob as _glob
import json
import os
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate  # noqa: E402
import repo_identity  # noqa: E402  (the dependency-light home-repo identity seam; slug/home primitives re-exported below)
import wiring     # noqa: E402  (the wiring library: is_applied per directive for the forward wiring leg)
import hooks      # noqa: E402  (the block-eligible invariant registry the block-budget leg checks)
import modes      # noqa: E402  (modes declares its explore write-gate block; the consumer assembles it)
import close      # noqa: E402  (close declares its findings-disposition Stop block; assembled here too)

ENGINE_MANIFEST_REL = ".engine/engine.json"

# FOUNDATION_INFRA — the wholly-engine-owned files that belong to no module's `provides`: the engine
# manifest, the root CLAUDE.md, the tool-runtime lockfiles, and the engine-owned .github/ control-plane
# artifacts (the two required-check workflows; the advisory secret-scan workflow + dependabot.yml that
# form the git-native security floor; the advisory actionlint
# workflow that grammar-checks every workflow file; the scheduled audit-prep workflow that runs the
# engine's self-review; the PR template, the issue templates, and CODEOWNERS
# itself). This is the foundation infrastructure-artifact set — the high-trust files a bare `provides`-union
# would leave unowned. It is the SINGLE SOURCE for four derived consumers, so they cannot drift apart:
#   - NAMED_INFRA (below) — the .engine/-only subset, the ownership-walk carve-out.
#   - engine_owned_paths()/foundation_infra_paths() — the CODEOWNERS engine-owned path set.
#   - codeowners_path_set() — engine_owned_paths + the CODEOWNERS self-add, the one path set BOTH the
#     first-run render and the upgrade re-render use, so the two render sites cannot drift.
#   - module_manager.FOUNDATION_CODE — the upgrade overlay-replace set (minus the manifest, which is
#     version-bumped in place, and CODEOWNERS, which is rendered locally, not fetched from a release).
# A member may be a glob (.github/ISSUE_TEMPLATE/*.md); consumers that need concrete paths expand it
# against the live tree (foundation_infra_paths) or the release tree (the overlay loop).
FOUNDATION_INFRA = (
    ENGINE_MANIFEST_REL,
    ".engine/pyproject.toml",
    ".engine/uv.lock",
    "CLAUDE.md",
    "AGENTS.md",           # the Codex floor — CLAUDE.md's exact sibling: engine-owned foundation, keyed
    #                        `floor` fence, OUT of FOUNDATION_CODE (keyed-merged on upgrade, never
    #                        overlay-replaced) + block-reversed in remove_engine.
    ".gitignore",          # a platform-shared keyed file like CLAUDE.md/CODEOWNERS — carries the engine's
    #                        foundation-ignores fence; OUT of FOUNDATION_CODE + block-reversed
    #                        in remove_engine (never overlay-replaced / wholesale-deleted — #409).
    ".github/workflows/engine-ci.yml",
    ".github/workflows/engine-guard.yml",
    ".github/workflows/secret-scan.yml",
    ".github/workflows/actionlint.yml",
    ".github/workflows/audit-prep.yml",
    ".github/workflows/engine-issue-conformance.yml",
    ".github/workflows/engine-overlay-disclosure.yml",
    # the release lifecycle (#516): engine-owned workflows that cut + publish a release — the engine's version
    # in the construction repo, the deployed repo's own product version once deployed. Foundation like every
    # other engine workflow, so an engine improvement to the release path (and the deployed-repo product wording)
    # reaches a deployed repo on its next upgrade rather than freezing at whatever the initial copy carried.
    ".github/workflows/release.yml",
    ".github/workflows/release-publish.yml",
    ".github/dependabot.yml",
    ".github/pull_request_template.md",
    ".github/ISSUE_TEMPLATE/*.md",
    ".github/CODEOWNERS",
)

# NAMED_INFRA — the .engine/ subset of FOUNDATION_INFRA: the foundation artifacts that live under
# .engine/ and so take part in the .engine/ ownership walk (the only corner where "is this file
# owned?" is well-defined). The root CLAUDE.md and the engine-owned .github/ control-plane files are
# also foundation artifacts, but they live OUTSIDE .engine/ in containers the product co-occupies, so
# the ownership leg never reads them. DERIVED (not hand-listed) so it cannot drift from
# FOUNDATION_INFRA; identical membership to the historical literal {engine.json, pyproject, uv.lock}.
NAMED_INFRA = {p for p in FOUNDATION_INFRA if p.startswith(".engine/")}

# OPERATOR_CONFIG — committed operator-authored config the ownership leg must NOT read as orphans: the
# per-deployment operator policy-override of tunable policy values (.engine/operator-overrides.json, written
# by /engine-tune) and the per-deployment instance guarded-paths declaration (.engine/operator-guarded-paths.json,
# #532 — the deployment's own extra product-side paths for the weakening guard to watch, unioned in by
# weakening_guard.is_guardrail and shape-gated by engine/check/operator-guarded-paths) and the per-deployment
# local-reference vocabulary (.engine/operator-local-references.json — the references that mean something only
# inside this deployment, read by local_references.py so an outbound contribution can be checked against them
# and shape-gated by engine/check/operator-local-references). It is operator-owned config preserved across an engine update — in NO module's
# `provides` and NOT a FOUNDATION_INFRA artifact (that set is overlay-REPLACED on upgrade, which would clobber
# the operator's tuning). This is the LOCKED carve-out of module coherence: "Operator- and
# deployment-authored committed content is outside this leg ... coherence does not read them as orphans, the
# same shape of carve-out by which CODEOWNERS and the foundation .gitignore block sit [outside it]". Absent until the first tune, so it never appears in this construction repo; fixture-tested. The
# conduct operator-override (.engine/conduct/operator.md), the maintainer's conduct seed
# (.engine/provisioning/conduct-seed.md), the maintainer's SECURITY.md disclosure seed
# (.engine/provisioning/security-seed.md, security floor), and the maintainer's product-starter README seed
# (.engine/provisioning/readme-seed.md, the front-door seed) are the same kind of carve-out:
# maintainer/operator-authored content, preserved across overlay, in no `provides`, so the ownership leg must
# not read them as orphans either. (The SEEDED root files — SECURITY.md, README.md — need no carve-out: they
# live outside .engine/, so the ownership walk never reaches them; they are product territory preserved by the
# overlay's "never touch product".)
OPERATOR_CONFIG = {".engine/operator-overrides.json", ".engine/operator-guarded-paths.json",
                   ".engine/operator-local-references.json",
                   ".engine/conduct/operator.md",
                   ".engine/provisioning/conduct-seed.md", ".engine/provisioning/security-seed.md",
                   ".engine/provisioning/readme-seed.md"}

# Directories under .engine/ that are regenerable derivatives or caches — never owned files. The
# inventory's contract is "every COMMITTED engine file"; these hold gitignored regenerable artifacts
# (the uv venv, Python bytecode, the pytest run-cache, and knowledge's derived `.cache/` query index).
# Pruning them keeps the ownership leg from flagging a derived cache as an unowned orphan.
# Matched by bare directory NAME, because these caches recur at any depth in the tree.
PRUNE_DIRS = {".venv", "__pycache__", ".cache", ".pytest_cache"}

# Repo-relative directory PATHS (not bare names) that hold gitignored RUNTIME state — never committed,
# so by the inventory's "every COMMITTED engine file" contract they are not owned files. Distinct
# justification from PRUNE_DIRS above: this is not a regenerable derivative/cache but the memory
# substrate's live, non-regenerable per-instance store (the NDJSON ledger, the search index, the
# capture-state file, the lock) under `.engine/memory/`, created the moment the memory hooks run and
# gitignored by the memory module's `gitignore` wire. It is excluded by the SAME walk-prune mechanism.
# Anchored on the exact PATH `.engine/memory`, never the bare name `memory`, so the committed memory
# CODE package `.engine/tools/memory/` (owned via the module's `provides` glob) stays ownership-checked.
# `.engine/projects-sync` is the same shape: the github-projects-sync module's gitignored per-instance
# board-coordinate config + debounce stamp (its `gitignore` wire keys it out of version control), created
# the moment that module's sync hook runs. Anchored on the exact path, so the committed CODE package
# `.engine/tools/projects_sync/` (owned via that module's `provides` glob) stays ownership-checked.
PRUNE_PATHS = {".engine/memory", ".engine/projects-sync"}

# Repo-relative directory PATHS holding COMMITTED test data that is deliberately NOT a governed surface: the
# reserved negative-fixture namespace (`.engine/_fixtures/`). These are seeded
# bad inputs the negative-fixture meta-check runs each hard check against to prove it bites. Distinct
# justification from both sets above: unlike PRUNE_DIRS (regenerable caches) and PRUNE_PATHS (gitignored
# runtime state), fixtures ARE committed — but they are excluded from the ownership leg because no module
# `provides` them and they must never read as an unowned orphan or an uncatalogued surface (the check-system
# "fixtures are test data, not a surface" rule). Anchored on the exact path, so a committed tool or surface
# elsewhere is unaffected. Pruned by the SAME walk mechanism as PRUNE_PATHS — and at the SHARED walk
# (_walk_engine_files), so the namespace is excluded from BOTH the ownership leg and the #281
# untracked-surface detector intentionally (a committed fixture is not cruft to flag; fixtures are never
# fingerprinted into the graph, so the #281 "a regen would pull it in" risk does not apply to them).
FIXTURE_PATHS = {".engine/_fixtures"}

# Repo-relative directory PATHS holding the deployment's COMMITTED per-instance eADR stream — the
# deployment-authored decision records on the contracts surface (the contracts-surface topology rule). The engine's own
# foundational eADR CANON rides core's non-recursive `.engine/contracts/*.md` glob (which never descends into
# a subdirectory); this deployment stream lives one level down, in NO module's `provides`, preserved across an
# engine overlay like operator config. Distinct justification from every set above: it is neither a regenerable
# cache (PRUNE_DIRS), gitignored runtime state (PRUNE_PATHS), nor test data (FIXTURE_PATHS) — it is committed
# deployment content that must not read as an unowned orphan. Excluded by the SAME shared walk-prune as
# FIXTURE_PATHS, so it sits outside BOTH the OWNERSHIP leg and the #281 untracked-surface detector. It IS a
# graph entity, though — knowledge_gen's own presence walk (`deployment_contract_inventory` / Pass 1b) entitizes
# it as a NON-CANON contract, told apart from the canon by the ABSENCE of a `provided_by` edge (by
# provides-membership, never a path/marker), so a deployment's decisions are graph-visible but not engine-OWNED
# surface. Anchored on the exact subtree path, so the engine canon at `.engine/contracts/*.md` stays fully
# ownership-checked.
DEPLOYMENT_CONTRACTS = {".engine/contracts/instance"}

MODULES_GLOB = ".engine/modules/*/manifest.json"


def _rel(abs_path: str) -> str:
    return os.path.relpath(abs_path, validate.ROOT).replace(os.sep, "/")


def _git_lines(args: list) -> list | None:
    """Run a read-only `git -C ROOT <args> -z` and return the NUL-split, ROOT-relative relpaths
    (forward-slash, matching _rel), or None on any non-zero exit / missing binary / timeout. Never
    raises — a degraded git read returns None so the caller fails safe. Mirrors checkout_health._run.
    Reads validate.ROOT at call time, so a test that redirects ROOT is honored. Fixed argv, no shell —
    no injection surface; `-z` is verbatim NUL-terminated, so a filename with spaces/quotes is safe."""
    try:
        out = subprocess.run(["git", "-C", validate.ROOT, *args, "-z"],
                             capture_output=True, text=True, timeout=30, check=False)
    except Exception:  # noqa: BLE001 — missing binary / timeout / OS error all degrade to "unavailable"
        return None
    if out.returncode != 0:
        return None
    return [p for p in out.stdout.split("\0") if p]


def _tracked_paths() -> set | None:
    """The git-tracked relpaths (`git ls-files`), or None when git is unavailable. engine_file_inventory
    intersects with this so an UNTRACKED file is not read as a committed-ownership concern (#281)."""
    lines = _git_lines(["ls-files"])
    return set(lines) if lines is not None else None


def _untracked_surface_paths() -> set | None:
    """The relpaths git neither tracks NOR ignores (`git ls-files --others --exclude-standard`) — genuine
    sync-conflict cruft, or a new file not yet committed; gitignored files are deliberately excluded (they
    are intentional, not cruft). None when git is unavailable. The untracked-surface detector's input."""
    lines = _git_lines(["ls-files", "--others", "--exclude-standard"])
    return set(lines) if lines is not None else None


def discover_manifests() -> list:
    """The present module manifests as (relpath, manifest) pairs — installed-means-present,
    read from the .engine/modules/ directory listing. Sorted by path for stable output."""
    out = []
    for abs_path in sorted(_glob.glob(os.path.join(validate.ROOT, MODULES_GLOB))):
        out.append((_rel(abs_path), validate.load_json(abs_path)))
    return out


def load_engine_manifest():
    """The engine manifest (.engine/engine.json) as a dict, or None if it is absent."""
    path = os.path.join(validate.ROOT, ENGINE_MANIFEST_REL)
    return validate.load_json(path) if os.path.isfile(path) else None


# ---- home-repo identity primitives: single-homed in `repo_identity`, re-exported here ----------------
# These moved to the dependency-light `repo_identity` module so the lean HARD CI safety checks
# (memory-pointer-public-safety, census-completeness, hard-check-bite) can gate on the home-repo signal
# WITHOUT importing this module's modes/close/hooks lifecycle machinery into a `pull_request_target`-adjacent
# path. Re-exported under their long-standing `module_coherence.<name>` names so every existing caller
# (module_manager, first_run_health, release_cut, external_contribution/submit, instantiator) is unaffected,
# and this module keeps no copy of its own to drift from. (Other modules — first_run_health, boot,
# instantiator, weakening_guard — still carry their own origin/home readers with their own fail-directions;
# `repo_identity` is the single home for the primitives it defines, though not the tree's only reader of that
# state.) `load_engine_manifest` (above) stays
# here: it is this module's general engine-manifest reader, distinct from the identity seam. `home_repository`
# preserves its fail-LOUD-on-malformed contract (via `validate.load_json` in `repo_identity`).
home_repository = repo_identity.home_repository
normalize_slug = repo_identity.normalize_slug
slug_eq = repo_identity.slug_eq
is_downstream_copy = repo_identity.is_downstream_copy
_READ_HOME = repo_identity._READ_HOME


# ---- what may travel in a contribution back to the ENGINE'S OWN HOME (issue #556) -----------------
#
# When a deployment contributes back to the engine's home (a fork-native deployment escalating an engine fix to
# engine-template — the OWNED engine-mechanic takes the direct-PR path, not this), the engine's own SOURCE
# legitimately rides upstream — it IS the contribution. What must NEVER ride upstream, even to the home, is this deployment's ACCRETED STATE
# and the operator's private tuning. The leak check narrows to that never-travels set for the home case.
#
# SAFE DIRECTION (default-flag). `travels_to_engine_home` returns True ONLY for content proven safe to travel
# — the engine's source namespaces, the build/runtime config, and the two CI-REQUIRED derived indexes. Any
# path it does not positively recognise stays flagged (an over-flag is an operator-decidable nudge; an
# under-flag would leak). A future committed per-instance store therefore flags by default until it is
# explicitly classified — and the `test_engine_home_travel_classification` completeness test fails until it is.

# The two derived indexes the engine's HOME repo regenerates and whose freshness its CI hard-gates
# (`knowledge-coverage`, `self-map-drift` — both unbypassable at CI). A contribution that stripped them could
# not merge, and they regenerate correctly from the home checkout, so they MUST travel (not instance-state).
_CI_REQUIRED_INDEXES = frozenset({".engine/knowledge/graph.json", ".engine/self-map.md"})

# Committed, identical-across-instances engine build/runtime config (not under a surface namespace) that is
# product and travels: the tool-runtime pin/lock, the suite manifest, the repo ignore rules, and the governance
# floors (the root CLAUDE.md/AGENTS.md, which since #323 ARE the fenced adopter floor — the separate
# .deployed.md files retired with the greenfield swap).
_HOME_TRAVEL_FILES = frozenset({
    ".engine/pyproject.toml", ".engine/uv.lock", ".engine/suites.json",
    ".gitignore", "AGENTS.md", "CLAUDE.md",
})

# The engine's SOURCE namespaces — surface-catalogued kinds plus the module/template/provisioning trees and
# the assistant/Codex adapter roots. Everything an engine ships as code/config lives under one of these.
# KNOWN ASSUMPTION (bounded): these prefixes are broad, so "default-flag" is fully safe only for a path OUTSIDE
# them — a path UNDER one is positively recognised as travelling. That holds because these namespaces hold
# only identical-across-instances code/config today (swept: no per-instance data store lives under one). If a
# future committed per-instance store were placed under a source namespace (e.g. `.engine/tools/x/state.json`)
# it would travel unflagged, and neither the runtime default-flag nor `test_no_owned_path_is_left_ambiguous`
# (which recognises data by the instance-DIR prefixes below) would catch it. A new per-instance store must be
# kept OUT of these namespaces (or its module's `provides` + the instance-dir list extended). Hardening to an
# explicit source-file allowlist would be a stronger option, but is not needed while the sweep holds.
_HOME_TRAVEL_PREFIXES = (
    ".engine/tools/", ".engine/check/", ".engine/schemas/", ".engine/operations/",
    ".engine/policies/", ".engine/interfaces/", ".engine/docs/", ".engine/conduct/",
    ".engine/contracts/", ".engine/modules/", ".engine/templates/", ".engine/provisioning/",
    ".claude/", ".codex/", ".agents/", ".github/",
)


def travels_to_engine_home(path: str) -> bool:
    """True iff `path` is engine content that legitimately rides into a contribution to the engine's OWN home
    (source, build config, or a CI-required derived index) — False for this deployment's accreted state and
    the operator's private tuning, which never travel. OPERATOR_CONFIG and the per-instance eADR stream are
    checked FIRST, so an operator-authored file under a source namespace (e.g. `.engine/conduct/operator.md`,
    the provisioning seeds) never travels despite its prefix. Default is False (flag): only positively
    recognised source/index/config travels."""
    if path in OPERATOR_CONFIG or path.startswith(tuple(d + "/" for d in DEPLOYMENT_CONTRACTS)):
        return False  # operator/maintainer tuning + the deployment's own decision records — private, never travel
    if path in _CI_REQUIRED_INDEXES or path in _HOME_TRAVEL_FILES:
        return True
    return path.startswith(_HOME_TRAVEL_PREFIXES)


def is_deployment_private(path: str) -> bool:
    """True iff `path` is operator/deployment-private committed content that must NOT ride into ANY upstream —
    the operator's tuning (OPERATOR_CONFIG) or the deployment's own decision records (DEPLOYMENT_CONTRACTS).
    These sit OUTSIDE `engine_owned_paths` (they are ownership carve-outs), so the leak check unions them in
    explicitly, closing the gap where a deployment's private content could ride upstream unflagged."""
    return path in OPERATOR_CONFIG or path.startswith(tuple(d + "/" for d in DEPLOYMENT_CONTRACTS))


def _walk_engine_files() -> list:
    """Every file under .engine/ on the live filesystem (relpaths), pruning regenerable cache dirs
    (PRUNE_DIRS, any depth), gitignored runtime roots (PRUNE_PATHS), the committed-but-non-surface
    fixtures namespace (FIXTURE_PATHS), and the deployment's committed per-instance eADR stream
    (DEPLOYMENT_CONTRACTS). The RAW walk — what is on disk, tracked or not.
    engine_file_inventory() narrows this to the committed set; the untracked-surface detector reads it raw
    (both therefore exclude the four pruned sets)."""
    out = []
    for dirpath, dirs, files in os.walk(validate.ENGINE_DIR):
        # Prune name-matched caches (PRUNE_DIRS, any depth), path-matched gitignored runtime roots
        # (PRUNE_PATHS), the committed-but-non-surface fixtures namespace (FIXTURE_PATHS), and the
        # deployment-owned per-instance eADR stream (DEPLOYMENT_CONTRACTS) — each by the exact repo-relative
        # path — so none of their contents are flagged as orphans.
        dirs[:] = [d for d in dirs
                   if d not in PRUNE_DIRS
                   and _rel(os.path.join(dirpath, d)) not in PRUNE_PATHS
                   and _rel(os.path.join(dirpath, d)) not in FIXTURE_PATHS
                   and _rel(os.path.join(dirpath, d)) not in DEPLOYMENT_CONTRACTS]
        out.extend(_rel(os.path.join(dirpath, f)) for f in files)
    return sorted(out)


def engine_file_inventory() -> list:
    """The COMMITTED engine files under .engine/ (relpaths) — the OWNERSHIP inventory. Ownership is a
    property of the committed tree, so the raw walk is intersected with git's tracked set: an untracked
    file (sync-conflict cruft, or an intended new file not yet committed) is not yet an ownership concern
    and must not raise a spurious local orphan / double-claim (#281). Fail-soft: when git is unavailable
    the raw walk is returned unchanged (the prior behavior); in CI (a clean checkout, all committed) the
    intersection is a no-op. Nothing is dropped silently — untracked_surface_findings names what is
    excluded. The product never owns a file under .engine/, so this is the exclusively-engine corner
    where file ownership is well-defined."""
    walked = _walk_engine_files()
    tracked = _tracked_paths()
    if tracked is None:
        return walked
    return [rel for rel in walked if rel in tracked]


def _platform_surface_roots() -> list:
    """The AI-runtime surface-location roots from the surface catalog — the catalogued homes that live
    OUTSIDE .engine/ because a runtime dictates them (today .claude/skills/ and .claude/agents/ for
    Claude Code, .agents/skills/ and .codex/agents/ for Codex) — read from the catalog so the set stays
    single-sourced, never hand-listed. Engine surface files live under .engine/ AND these roots; the
    untracked-surface detector walks both."""
    catalog = validate.load_json(validate.CATALOG_PATH) or {}
    roots = {(s or {}).get("location") for s in (catalog.get("surfaces") or {}).values()}
    return sorted(r for r in roots
                  if isinstance(r, str) and r.startswith((".claude/", ".codex/", ".agents/")))


def _surface_walk() -> list:
    """Every file on disk under the engine's surface territory: the raw .engine/ tree plus the catalogued
    runtime surface roots (so a duplicated skill/agent directory, e.g. `engine-help 2/SKILL.md`, is seen
    — a literal `provides` path never would). The untracked-surface detector cross-references this against
    git."""
    out = set(_walk_engine_files())
    for root in _platform_surface_roots():
        for dirpath, dirs, files in os.walk(os.path.join(validate.ROOT, root)):
            dirs[:] = [d for d in dirs if d not in PRUNE_DIRS]
            out.update(_rel(os.path.join(dirpath, f)) for f in files)
    return sorted(out)


def untracked_surface_findings(tier: str = "soft") -> list:
    """Issue #281 detector: every file under the engine surface (the .engine/ tree + the catalogued
    .claude/ surface roots) that git neither tracks nor ignores — sync-conflict cruft a file-sync tool
    dropped, or a new engine file not yet committed — as a `tier` (soft) finding naming the file. CI runs
    on a clean checkout, so this is silent there (the pollution it guards is local-only); it surfaces on a
    local validate run and the audit digest. When git is unavailable it cannot tell tracked from
    untracked, so it returns ONE finding saying the check was skipped — never a silent all-clear."""
    untracked = _untracked_surface_paths()
    if untracked is None:
        return [validate.finding(tier,
            "The untracked-surface check could not run because git was unavailable, so the engine could "
            "not tell which surface files are committed. If your working copy holds sync-conflict "
            "duplicates (names like `… 2.py`), they are not being caught right now.")]
    findings = []
    for rel in sorted(set(_surface_walk()) & untracked):
        findings.append(validate.finding(tier,
            f"'{rel}' is under the engine's surface but git is not tracking it. The shared checks run on "
            f"a clean copy of the repository and never see it, so it can cause a failure that only happens "
            f"on your machine — and regenerating the engine's map of itself would pull it in. If a "
            f"file-sync tool created it (names like `… 2.py`), delete it; if it is a new engine file "
            f"you meant to add, commit it.",
            validate.loc(os.path.join(validate.ROOT, rel))))
    return findings


def provides_claims(manifests: list) -> dict:
    """{relpath: [module-id, ...]} — for each present manifest, every file its `provides`
    globs select, mapped to the owning module id. Built against the live filesystem so it
    uses real glob semantics; the pure validate.ownership_findings consumes the result."""
    claims: dict = {}
    for _path, m in manifests:
        mid = m.get("id")
        for _group, patterns in (m.get("provides") or {}).items():
            for pattern in patterns:
                # sorted() so the claim order is filesystem-order-independent — a defense-in-depth,
                # matching discover_manifests/engine_file_inventory/foundation_infra_paths below.
                for abs_path in sorted(_glob.glob(os.path.join(validate.ROOT, pattern), recursive=True)):
                    if os.path.isfile(abs_path):
                        claims.setdefault(_rel(abs_path), []).append(mid)
    return claims


def foundation_infra_paths() -> list:
    """FOUNDATION_INFRA expanded to the concrete committed relpaths that exist in the live tree —
    each literal member that is present, plus every file a glob member (the issue templates) selects.
    `glob.glob` on a literal path returns it iff it exists, so one uniform pass handles both. This is
    the concrete-path form the CODEOWNERS path set needs (file-precise ownership, never a bare glob)."""
    out: set = set()
    for member in FOUNDATION_INFRA:
        for abs_path in _glob.glob(os.path.join(validate.ROOT, member), recursive=True):
            if os.path.isfile(abs_path):
                out.add(_rel(abs_path))
    return sorted(out)


def engine_owned_paths(manifests: list) -> list:
    """The engine-owned file set for the CODEOWNERS ownership block: every file a present module's
    `provides` claims, UNIONED with the foundation infrastructure artifacts (FOUNDATION_INFRA). The
    `∪` is load-bearing — the highest-trust engine files (the manifest, root CLAUDE.md, the
    tool-runtime lockfiles, the engine-owned .github/ files) are in no module's `provides`, so a bare
    provides-union would leave exactly those product-merge-able (the repository-topology wall).
    Returns concrete relpaths (globs expanded), sorted and de-duplicated."""
    paths = set(provides_claims(manifests).keys())
    paths.update(foundation_infra_paths())
    return sorted(paths)


def codeowners_path_set() -> list:
    """The exact path set the CODEOWNERS ownership block renders against: engine_owned_paths over the
    live present set, plus `.github/CODEOWNERS` itself. SINGLE-SOURCED here so the two render sites — the
    first-run instantiation and an engine upgrade's re-render (the
    engine.json `handle` field) — cannot drift; both call this. The self-add lives ONLY here, never in
    engine_owned_paths(): CODEOWNERS must own its own routing rule (or a product line could shadow it),
    but engine_owned_paths' other consumers — module_manager.FOUNDATION_CODE and remove_engine's
    outside-set / shadow-collision detector — must NOT carry that self-ownership. Read live, so after an
    overlay it reflects the new release's engine files."""
    co_rel = ".github/CODEOWNERS"
    path_set = engine_owned_paths(discover_manifests())
    if co_rel not in path_set:
        path_set = sorted(set(path_set) | {co_rel})
    return path_set


# The shared target file each seam's wire lands in — a plain-language LABEL for the wiring leg's
# finding only (the operative target paths are wiring.py's own constants, which is_applied actually
# reads). Derived from those constants so the label is SINGLE-HOMED and cannot drift from them.
WIRING_TARGETS = {
    "hook": _rel(wiring.SETTINGS_PATH),
    "permission": _rel(wiring.SETTINGS_PATH),
    "mcp": _rel(wiring.MCP_PATH),
    "gitignore": _rel(wiring.GITIGNORE_PATH),
    "ontology-entry": _rel(wiring.CATALOG_PATH),
    "codex-hook": _rel(wiring.CODEX_HOOKS_PATH),
    "codex-mcp": _rel(wiring.CODEX_CONFIG_PATH),
}


def wiring_status(manifests: list) -> list:
    """The forward wiring-leg input: (module_id, seam_type, target_label, is_applied) for every
    `wires` directive of every present manifest, with `is_applied` computed live by the wiring
    library over the real shared files. The pure validate.wiring_findings consumes the result —
    so the live filesystem reads live here, the policy stays pure there (the ownership split)."""
    out = []
    for _path, m in manifests:
        mid = m.get("id")
        for directive in (m.get("wires") or []):
            seam = directive.get("type") if isinstance(directive, dict) else None
            target = WIRING_TARGETS.get(seam, "its shared target file")
            out.append((mid, seam, target, wiring.is_applied(directive)))
    return out


def declared_wire_identities(manifests: list) -> set:
    """The set of (seam_type, identity_key) every present manifest's `wires` declares — the orphan-wire
    reverse leg's "allowed" set, compared against wiring.applied_engine_wires(). Built with
    wiring.declared_wire_identity so the keying is single-homed with the applied-side enumerator (no
    second copy of the identity rule). A permission or ontology-entry directive maps to None and is
    skipped — those seams are excluded from the reverse leg (see validate.orphan_wire_findings)."""
    ids = set()
    for _path, m in manifests:
        for directive in (m.get("wires") or []):
            key = wiring.declared_wire_identity(directive)
            if key is not None:
                ids.add(key)
    return ids


def block_eligible_registrations() -> list:
    """The block declarations the block-registry leg governs, ASSEMBLED from each owning system's own
    declaration — hooks names no invariant itself (the block-budget law), so the registry
    is the hooks-owned set (none) PLUS each owning lifecycle system's block: modes' explore write-gate
    (modes.BLOCK_INVARIANT) and its engine-Issue-conformance reroute (modes.REROUTE_BLOCK_INVARIANT) —
    both PreToolUse blocks modes' single handler composes, named a member by the
    block-budget law — and close's findings-disposition gate on Stop (close.BLOCK_INVARIANT). Each entry
    is {event, name, owner, modes}; the leg reads `event` and `modes`. These — NOT bare
    .claude/settings.json hook registrations — are the authoritative "this blocks" source: a wired hook
    command is opaque, so registration alone never implies a block (boot's SessionStart hook is wired yet
    declares none). So the leg validates three REAL members on block-eligible events (PreToolUse, Stop) →
    green; it would fire the moment any owner declared a block on a non-eligible event or without its
    modes. (owes → the module manager: if the block-owner set grows past 2–3 it may refactor this
    consumer-side assembly to a registry-discovery pattern.)"""
    return ([dict(inv) for inv in hooks.BLOCK_ELIGIBLE_INVARIANTS]
            + [dict(modes.BLOCK_INVARIANT), dict(modes.REROUTE_BLOCK_INVARIANT),
               dict(close.BLOCK_INVARIANT)])


def check_coherence(tier: str = "hard") -> list:
    """All coherence legs over the present set: dependency (reused) + ownership + the BIDIRECTIONAL
    wiring leg — forward (declared->applied) AND reverse (applied->declared, the orphan-wire leg) —
    + block-budget (only PreToolUse/Stop may hard-block) + kind-discovery (a module-provided check-kind
    file that shadows a core kind, collides with another, or cannot be imported). Returns a flat list
    of finding.v1 dicts. The library entry the module manager calls after any install / uninstall."""
    manifests = discover_manifests()
    dep = validate.coherence_findings(
        [m for _path, m in manifests], tier,
        "Install the missing module, adjust the version range, or break the dependency "
        "cycle, then re-check.")
    exempt = set(NAMED_INFRA) | {path for path, _m in manifests} | OPERATOR_CONFIG
    own = validate.ownership_findings(
        engine_file_inventory(), provides_claims(manifests), exempt, tier,
        "Every engine file must be owned by exactly one module.")
    wiring_leg = validate.wiring_findings(
        wiring_status(manifests), tier,
        "Each module's declared settings must be applied in the shared files.")
    orphan_leg = validate.orphan_wire_findings(
        wiring.applied_engine_wires(), declared_wire_identities(manifests), tier,
        "Each applied engine setting must belong to an installed module.")
    block = validate.block_budget_findings(
        block_eligible_registrations(), tier,
        "Only PreToolUse and Stop may hard-block, and every block must declare the stances it is active "
        "in; fix the block's event or its modes before merging.", stances=modes.STANCES)
    kinds = validate.kind_discovery_findings(
        tier,
        "A module-provided check kind is discovered by the name of its file; two kinds cannot share a "
        "name and none may reuse a core kind's.")
    return dep + own + wiring_leg + orphan_leg + block + kinds


# The artifact warrant for a coherence result: what a green check shows, what it does NOT, and
# what still needs a look. Printed on EVERY report — green or with findings — so the bound the operator
# reads never collapses to the green word alone. Adapted from the locked coherence warrant. Coherence is a STRUCTURAL
# attestation and the gap is wide, so this is the most prominent of the engine's warrants (proportionate).
COHERENCE_WARRANT = (
    "\nWhat this shows / what it does not:"
    "\n  - A green result shows the installed set is CONSISTENT — every dependency present and in range,"
    "\n    every declared wire applied, every engine file owned by exactly one module."
    "\n  - It does NOT show that the modules WORK. That a module does useful work shows in its own checks"
    "\n    and in the behaviour you observe — never here; a green coherence result is not a fitness check."
    "\n  - Whether an installed module still earns its place is the self-review's call, not coherence's."
)


def _print_report(findings: list, n_modules: int, n_files: int) -> None:
    """Plain-language-first, matching the validator's report() register — a human sentence
    per issue, never raw JSON (the operator reads this; --json is the machine channel)."""
    hard = [f for f in findings if f["severity"] == "hard"]
    soft = [f for f in findings if f["severity"] != "hard"]
    if soft:
        print(f"\nnotes ({len(soft)}):")
        for f in soft:
            print("  - " + validate.fmt(f))
    if hard:
        print(f"\nModule coherence found {len(hard)} issue(s):")
        for f in hard:
            print("  - " + validate.fmt(f))
    elif not soft:
        print(f"\nOK — the module set is coherent: {n_modules} module(s) installed, "
              f"{n_files} engine file(s), all owned.")
    print(COHERENCE_WARRANT)


def _demo(_argv: list) -> int:
    """Operator-runnable, MUTATION-FREE fail-then-pass for the orphan-wire reverse leg. On a private
    COPY of the real shared hook file (never the live one), it adds an engine setting that belongs to no
    installed module — exactly the leftover an incomplete removal would leave — shows coherence catch it,
    removes it, and shows coherence green again. The REAL check_coherence / applied_engine_wires logic
    runs; only the file it reads is a throwaway, so nothing real is changed."""
    saved = wiring.SETTINGS_PATH
    orphan = {"type": "hook", "event": "PostToolUse", "matcher": "",
              "hook": {"type": "command",
                       "command": ".engine/.venv/bin/python .engine/tools/boot.py --orphan-demo"}}

    def _hard():
        return [f for f in check_coherence() if f["severity"] == "hard"]

    with tempfile.TemporaryDirectory() as d:
        copy = os.path.join(d, "settings.json")
        if os.path.exists(saved):
            shutil.copyfile(saved, copy)
        else:
            with open(copy, "w", encoding="utf-8") as fh:
                fh.write("{}\n")
        wiring.SETTINGS_PATH = copy
        try:
            print("This demo touches only a throwaway copy of your settings — your real files are "
                  "never changed.\n")
            print("(1) Baseline — is the engine consistent?")
            base = _hard()
            print("    " + ("OK — no problems." if not base
                            else f"UNEXPECTED baseline issues: {[f['message'] for f in base]}"))
            print("\n(2) Adding a setting that belongs to no installed module "
                  "(the kind a half-finished removal leaves behind):")
            print("    " + validate.fmt(wiring.apply(orphan)))
            flagged = _hard()
            print("    Re-checking:")
            for f in flagged:
                print("      - " + validate.fmt(f))
            print(f"    -> {len(flagged)} problem(s) found (expected 1).")
            print("\n(3) Removing that leftover and checking again:")
            print("    " + validate.fmt(wiring.reverse(orphan)))
            after = _hard()
            print("    " + ("OK — consistent again." if not after
                            else f"UNEXPECTED remaining issues: {[f['message'] for f in after]}"))
            ok = (not base) and len(flagged) == 1 and (not after)
            print("\n" + ("DEMO PASSED: the leftover was caught, then cleared."
                          if ok else "DEMO DID NOT BEHAVE AS EXPECTED — see above."))
            return 0 if ok else 1
        finally:
            wiring.SETTINGS_PATH = saved


def _untracked_demo(_argv: list) -> int:
    """Operator-runnable fail-then-pass for the #281 untracked-surface guard, on a THROWAWAY git repo
    (never your real tree). It commits an owned engine file, drops an UNTRACKED duplicate under the
    surface, and shows (a) the ownership inventory excludes it (no spurious orphan) and (b) the detector
    names it — then `git add`s it and shows the detector go quiet. The REAL engine_file_inventory /
    untracked_surface_findings logic runs; only the repo it reads is a fixture."""
    saved = (validate.ROOT, validate.ENGINE_DIR, validate.CATALOG_PATH)
    dup_rel = ".engine/tools/real_tool 2.py"

    def _git(root, *a):
        subprocess.run(["git", "-C", root, *a], capture_output=True, text=True, check=False)

    with tempfile.TemporaryDirectory() as root:
        engine = os.path.join(root, ".engine")
        os.makedirs(os.path.join(engine, "tools"))
        os.makedirs(os.path.join(engine, "schemas"))
        with open(os.path.join(engine, "schemas", "surface-catalog.json"), "w", encoding="utf-8") as fh:
            json.dump({"surfaces": {"skill": {"location": ".claude/skills/"}}}, fh)
        with open(os.path.join(engine, "tools", "real_tool.py"), "w", encoding="utf-8") as fh:
            fh.write("# a committed engine tool\n")
        validate.ROOT, validate.ENGINE_DIR = root, engine
        validate.CATALOG_PATH = os.path.join(engine, "schemas", "surface-catalog.json")
        try:
            _git(root, "init")
            _git(root, "add", "-A")
            _git(root, "-c", "user.email=e@x", "-c", "user.name=e", "commit", "-m", "base")
            print("This demo uses a throwaway git repo — your real files are never touched.\n")
            with open(os.path.join(engine, "tools", "real_tool 2.py"), "w", encoding="utf-8") as fh:
                fh.write("# a sync-conflict duplicate a file-sync tool dropped\n")
            print(f"(1) A file-sync tool dropped an untracked duplicate: {dup_rel}")
            in_inv = dup_rel in engine_file_inventory()
            print(f"    ownership inventory includes it? {in_inv}  "
                  f"(expected False — tracked-only, so no spurious orphan)")
            flagged = untracked_surface_findings("soft")
            named = any(dup_rel in f["message"] for f in flagged)
            print(f"    the detector names it? {named}  ({len(flagged)} soft finding(s))")
            print("\n(2) Committing it (as if it were an intended new file), then re-checking:")
            _git(root, "add", "-A")
            cleared = not any(dup_rel in f["message"] for f in untracked_surface_findings("soft"))
            print(f"    detector quiet for it now? {cleared}")
            ok = (not in_inv) and named and cleared
            print("\n" + ("DEMO PASSED: the untracked duplicate was kept out of ownership, named by the "
                          "detector, and cleared once committed."
                          if ok else "DEMO DID NOT BEHAVE AS EXPECTED — see above."))
            return 0 if ok else 1
        finally:
            validate.ROOT, validate.ENGINE_DIR, validate.CATALOG_PATH = saved


def main(argv: list) -> int:
    if argv and argv[0] == "demo":
        return _demo(argv[1:])
    if argv and argv[0] == "demo-untracked":
        return _untracked_demo(argv[1:])
    try:
        manifests = discover_manifests()
        inventory = engine_file_inventory()
        findings = check_coherence()
    except Exception as exc:  # a malformed manifest/engine file halts loudly, in plain language
        print(f"\nCONFIG ERROR: cannot read the module manifests or the engine manifest: "
              f"{exc}", file=sys.stderr)
        return 2
    if "--json" in argv:
        print(json.dumps(findings, indent=2))
    else:
        _print_report(findings, len(manifests), len(inventory))
    return 1 if any(f["severity"] == "hard" for f in findings) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
