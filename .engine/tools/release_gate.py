"""Release-cut deployment gate — the cut-time proof that a release OPERATES and UPGRADES when deployed.

A deployed repo runs a projected shape of this engine (the first-run-only setup files retired, the optional
modules a deployment declined absent). Two failure classes ride on that shape and never show up in the home
repo's own per-PR suite: a self-test that asserts a construction-only invariant with no deployed-skip guard
(the StarshipSuperjam/engine-template#599 class — it *operates* wrong when deployed), and a wiring-map regeneration that fails closed on an
optional module's absent subtree (the StarshipSuperjam/engine-template#663 class — the upgrade *reconcile* reds and stalls half-applied). This
gate catches both at CONSTRUCTION cut time, before a release pull request is ever opened:

- **Arm A — operates when deployed.** Project the release candidate to the deployed shape and run the
  validator + the whole self-test suite against it, in two configurations: the default install (every shipped
  module) and an optional-modules-declined install (each `default-on` module and the files it owns removed —
  the exact shape StarshipSuperjam/engine-template#663 broke on). A red here means the release would not operate on a real deployment.
- **Arm B — upgrades AND rolls back when deployed.** For each released baseline at or above the clean-upgrade
  floor, project that past release to its deployed shape and run a REAL practice upgrade to the candidate — the
  same child tail, the same seven-check structural gate (including the wiring-map coverage check
  StarshipSuperjam/engine-template#663 failed), no pull request opened — and then a REAL undo of that staged
  update (the operator's `rollback`), asserting the projected copy is cleanly restored to the baseline. A red
  means a deployed engine could not reconcile cleanly onto this release, or could not cleanly undo a stalled
  update from it (the StarshipSuperjam/engine-template#599 rollback-refusal class). The per-baseline outcomes
  are recorded as the supported-version transition matrix (StarshipSuperjam/engine-template#703).

**Where deployed-shape protection now lives.** This gate REPLACES the inline `test_deployed_selftests.py` belt,
which ran Arm A's default configuration on every home-repo pull request (~44% of the suite's wall time). That
protection now runs at each release cut (and on demand via the `release-gate` workflow), NOT per pull request —
so a deployment-shape regression that lands on the default branch is caught at the next cut or manual run, not
on the pull request that introduced it. This is the deliberate StarshipSuperjam/engine-template#664 trade (the reopened StarshipSuperjam/engine-template#649 decision).

**Fail CLOSED.** A gate that cannot even build its projection, or that hits any unexpected error, BLOCKS the
cut — it never waves a release through unverified. The only clean pass is "ran, both arms green". The tool is
home-repo-only (`is_home_repo`): it ships committed but is inert on a deployed repo, whose own `engine-ci`
runs the suite directly. On a genuine engine cut the workflow asserts the gate actually RAN (not skipped), so
an origin-signal misdetection can never silently ship an engine release ungated.

**No bypass.** A red gate always blocks the cut (operator decision, 2026-07-28). A transient flake is cleared
by re-running the `release-gate` workflow; a real red is fixed and the release re-cut.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate                              # noqa: E402
import module_manager                        # noqa: E402  (retire_set safe reader, _archive_tree, upgrade)
import census_completeness_check as _ccc     # noqa: E402  (shared home-repo predicate; monkeypatched in tests)

# Set on every nested run this gate spawns (the in-projection suite and the upgrade driver) so a projected
# copy of the suite never re-enters the gate. Shared name with the retired belt's guard; `test_release_gate.py`
# also skips its gate-driving cases when it is set (belt-and-suspenders alongside the home-repo skip).
_NESTED_ENV = "ENGINE_NESTED_SELFTEST"

# A deployed origin that differs from the recorded home, so the projection reads as a downstream copy
# (`is_home_repo` -> False), exactly as the retired belt relied on.
_DEPLOYED_ORIGIN = "https://github.com/acme/deployed-product.git"

# The env var the in-projection driver reads to (1) verify it is running against the projection, not home
# (the ROOT-isolation guard), and (2) find the candidate release tree to inject.
_DRIVER_EXPECT_ROOT = "ENGINE_GATE_EXPECT_ROOT"
_DRIVER_CANDIDATE = "ENGINE_GATE_CANDIDATE"


def _nested_env(**extra) -> dict:
    """The environment for every process this gate spawns INSIDE a projection — the in-projection validator and
    self-test suite, the module-remove/regen steps, and the upgrade driver. A projection is a synthetic deployed
    tree with NO real pull request, so each nested run must have the same offline posture as a LOCAL developer
    run — never carrying this release workflow's GitHub-Actions identity. Leaking the ambient CI/PR env
    (`GITHUB_EVENT_PATH`, `GITHUB_ACTIONS`, `CI`, `GITHUB_TOKEN`, …) makes the PR-context checks fire against a
    projection that has no PR: `pr-body-completeness` reads a no-PR event's empty body as "sections missing" —
    the false red that blocked the first live cut (StarshipSuperjam/engine-template#676's first exercise). Strip the Actions/CI harness vars BY PREFIX (so a future GITHUB_*/RUNNER_*
    -keyed check stays neutralised too) and keep everything else (PATH, HOME, UV_*, locale — none of which the
    nested `git`/`sys.executable` runs need from Actions). This silences ONLY the no-PR context checks: gating is
    static suite config and the structural operate/upgrade checks red off the file tree, not the environment, so
    a genuine deployed-shape failure still blocks the cut. `GITHUB_TOKEN` is among the stripped keys, so the
    practice-upgrade child is denied the repo token here too — the driver keeps an explicit belt-and-suspenders
    pop so that property stays legible at its own spawn. EVERY nested spawn must build its env through this
    helper; a bare `{**os.environ}` at a new spawn would re-open the leak."""
    keep = {k: v for k, v in os.environ.items()
            if k != "CI" and not k.startswith(("GITHUB_", "RUNNER_", "ACTIONS_"))}
    return {**keep, _NESTED_ENV: "1", **extra}


class GateError(RuntimeError):
    """A gate step that could not complete — a setup/projection failure, or an unexpected error. It is a
    BLOCK, never a skip: the caller reports the cut as gated. Distinct from an arm finding a real red (also a
    block) only in the message; both stop the cut."""


def _run(cmd: list, cwd: str | None = None, env: dict | None = None, timeout: int = 600):
    return subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout)


def _tail(text: str | None, n: int = 600) -> str:
    return (text or "").strip()[-n:]


# --------------------------------------------------------------------------- projection

def _archive_candidate(dest: str) -> str:
    """Capture the current working tree as the release candidate — EXACTLY the bytes the cut will commit —
    through a THROWAWAY git index, so the real index is never touched. At the `release.yml` insertion point the
    candidate (version bumps + regenerated maps) is written but not yet committed; staging it into a temp index
    and archiving that tree yields the same content `git add -A && git commit` would, and shares the single
    'archive of a tree' source with the baselines (`_archive_tree`). Raises GateError on any git failure."""
    os.makedirs(dest, exist_ok=True)
    with tempfile.TemporaryDirectory() as idx_dir:
        env = {**os.environ, "GIT_INDEX_FILE": os.path.join(idx_dir, "index")}
        staged = _run(["git", "-C", validate.ROOT, "add", "-A"], env=env, timeout=120)
        if staged.returncode != 0:
            raise GateError(f"could not stage the candidate working tree ({_tail(staged.stderr)})")
        tree = _run(["git", "-C", validate.ROOT, "write-tree"], env=env, timeout=60)
        if tree.returncode != 0:
            raise GateError(f"could not capture the candidate tree ({_tail(tree.stderr)})")
        tree_sha = tree.stdout.strip()
    try:
        return module_manager._archive_tree(tree_sha, dest)
    except Exception as exc:                              # noqa: BLE001 — surface as a clean block
        raise GateError(f"could not archive the candidate tree ({exc})")


def _candidate_tree_sha() -> "str | None":
    """The git tree sha of the current working tree — the exact bytes the cut will commit, captured through a
    THROWAWAY index so the real index is never touched (the same capture `_archive_candidate` archives). Stamped
    into the gate result as the candidate identity, so the evidence can be tied to the tree it describes: the
    release-PR renderer re-derives this sha and refuses to present the transition matrix if it does not match
    (stale or mismatched gate JSON). Best-effort: returns None on any git failure rather than blocking the gate
    on an identity read (the arms are the gate's real verdict)."""
    try:
        with tempfile.TemporaryDirectory() as idx_dir:
            env = {**os.environ, "GIT_INDEX_FILE": os.path.join(idx_dir, "index")}
            if _run(["git", "-C", validate.ROOT, "add", "-A"], env=env, timeout=120).returncode != 0:
                return None
            tree = _run(["git", "-C", validate.ROOT, "write-tree"], env=env, timeout=60)
        return tree.stdout.strip() or None if tree.returncode == 0 else None
    except Exception:                                        # noqa: BLE001 — identity is advisory, never a block
        return None


def _archive_baseline(tag: str, dest: str) -> str:
    """Materialize a released tag's committed tree offline (`git archive`). Raises GateError if the tag's tree
    object is absent — a shallow checkout with no tags fails the cut rather than silently skipping a baseline."""
    try:
        return module_manager._archive_tree(tag, dest)
    except Exception as exc:                              # noqa: BLE001
        raise GateError(f"could not archive baseline {tag} offline ({exc}) — is the checkout shallow / tag-less?")


def _decline_optional_modules(tree: str) -> list:
    """Model a deployment that DECLINED every declinable add-on — both `default-on` and `optional` status —
    using the engine's OWN per-module removal run inside the tree (`module_manager.py remove`), so the files,
    the `engine.json` packages entry, the tool-runtime dependency groups, the wiring, and coherence are all
    reconciled exactly as a real decline leaves them, never a hand-rolled deletion that drifts (e.g. a stale
    `default-groups` the uv-group-drift check would then red on). The required substrate that lazily imports an
    optional subtree stays — so this still contains the exact StarshipSuperjam/engine-template#663 shape (a declined default-on module) — and
    declining the `optional` add-ons on top is the StarshipSuperjam/engine-template#646 shape (a deployment whose self-test suite must stay
    green when an add-on is absent). Returns the declined module ids. Raises GateError on any failure."""
    modules_dir = os.path.join(tree, ".engine", "modules")
    if not os.path.isdir(modules_dir):
        raise GateError("the candidate tree has no .engine/modules directory to project a declined shape from")
    declinable = []
    for mid in sorted(os.listdir(modules_dir)):
        man_path = os.path.join(modules_dir, mid, "manifest.json")
        if not os.path.isfile(man_path):
            continue
        try:
            status = validate.load_json(man_path).get("status")
        except Exception as exc:                          # noqa: BLE001
            raise GateError(f"could not read module manifest for {mid} ({exc})")
        if status in ("default-on", "optional"):          # every add-on the operator may decline at setup
            declinable.append(mid)
    if not declinable:
        # A declined projection that declined NOTHING is identical to the default one — the StarshipSuperjam/engine-template#663/StarshipSuperjam/engine-template#646 shapes
        # would silently stop being tested (e.g. if the status vocabulary were renamed). Fail closed, loudly,
        # rather than let the gate keep reporting green while the declined arm covers nothing.
        raise GateError("could not project a module-declined deployment: found no installed declinable "
                        "(default-on or optional) module to decline — the #663/#646 shapes would go untested. "
                        "This is likely a module-manifest vocabulary change; the gate must be updated before "
                        "the next cut.")
    env = _nested_env()
    for mid in declinable:
        r = _run([sys.executable, os.path.join("tools", "module_manager.py"), "remove", mid, "--json"],
                 cwd=os.path.join(tree, ".engine"), env=env, timeout=300)
        if r.returncode != 0:
            raise GateError(f"could not project a declined shape (removing {mid}: {_tail(r.stderr or r.stdout)})")
    return declinable


def _project_to_deployed(dest: str, *, decline_optional: bool = False) -> list:
    """Turn an archived home-repo tree at `dest` into the shape a deployed repo actually runs — the same
    projection first-run provisioning applies: RETIRE the first-run-only assets (through `retire_set`, the
    fail-loud safe reader — NOT a naive re-read), optionally DECLINE the optional modules, git-init with a
    deployed origin so it reads as a copy, and REGENERATE the deployed-state indexes (self-map + knowledge
    graph) that now describe the reduced surface. Any failure raises GateError (the cut is blocked, never
    skipped). Returns the declined module ids (empty unless `decline_optional`)."""
    try:
        r_files, r_dirs = module_manager.retire_set(dest)     # the safe reader; raises on a bad manifest
    except Exception as exc:                                  # noqa: BLE001
        raise GateError(f"could not read the release's setup-file list to project the deployed shape ({exc})")
    for rel in list(r_files) + list(r_dirs):
        p = os.path.join(dest, rel)
        if os.path.isfile(p):
            os.remove(p)
        elif os.path.isdir(p):
            shutil.rmtree(p, ignore_errors=True)
    declined = _decline_optional_modules(dest) if decline_optional else []
    env = _nested_env()
    for cmd in (["init", "-b", "main"],
                ["remote", "add", "origin", _DEPLOYED_ORIGIN],
                ["-c", "user.email=t@t", "-c", "user.name=t", "add", "-A"],
                ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", "deployed"]):
        r = _run(["git", "-C", dest, *cmd], timeout=120)
        if r.returncode != 0:
            raise GateError(f"could not build the deployed projection (git {cmd[0]}: {_tail(r.stderr)})")
    for gen in ("self_map.py", "knowledge_gen.py"):
        r = _run([sys.executable, os.path.join("tools", gen), "generate"],
                 cwd=os.path.join(dest, ".engine"), env=env, timeout=300)
        if r.returncode != 0:
            # On a declined projection this regen IS the StarshipSuperjam/engine-template#663 operation — a failure here is the real defect.
            raise GateError(f"the deployed projection could not regenerate its wiring map "
                            f"({gen} on {'a module-declined' if decline_optional else 'the default'} shape: "
                            f"{_tail(r.stderr)})")
    return declined


def _assert_isolated(projection: str) -> None:
    """Refuse to drive a mutating upgrade unless the projection is a real temporary directory distinct from the
    home checkout — the belt-and-suspenders half of the ROOT-isolation guarantee (the driver also asserts its
    own resolved ROOT). A practice upgrade overlays and deletes files at its ROOT; running it against home
    would corrupt the release mid-cut."""
    proj = os.path.realpath(projection)
    home = os.path.realpath(validate.ROOT)
    if proj == home or not proj.startswith(os.path.realpath(tempfile.gettempdir()) + os.sep):
        raise GateError(f"refusing to run an upgrade against a non-throwaway tree ({proj})")


def _worktree_digest() -> str:
    """A cheap fingerprint of the home working tree's tracked+staged state (`git status` porcelain + the index
    tree), used to prove the gate wrote nothing to the tree the cut is about to commit."""
    status = _run(["git", "-C", validate.ROOT, "status", "--porcelain"], timeout=60)
    with tempfile.TemporaryDirectory() as idx_dir:
        env = {**os.environ, "GIT_INDEX_FILE": os.path.join(idx_dir, "index")}
        _run(["git", "-C", validate.ROOT, "add", "-A"], env=env, timeout=120)
        tree = _run(["git", "-C", validate.ROOT, "write-tree"], env=env, timeout=60)
    return f"{status.stdout}\n{tree.stdout}"


# --------------------------------------------------------------------------- arms

def _validate_in(tree: str, label: str) -> dict:
    """Run the CI validator suite inside a projected tree. Returns {passed, detail}."""
    env = _nested_env()
    r = _run([sys.executable, os.path.join("tools", "validate.py"), "--suite", "CI"],
             cwd=os.path.join(tree, ".engine"), env=env, timeout=300)
    # report() prints the verbose (disclosed-no-op) "notes (…)" section FIRST, then the "FAIL (…)" hard-finding
    # section — so keep the FAIL section (the actual reason) and drop the notes preamble that precedes it. A red
    # WITHOUT that exact section — a CONFIG ERROR (returncode 2), a traceback, or the non-gating advisory render
    # — has no marker, so fall back to the full tail rather than blanking the reason (the empty-log symptom this
    # replaces: splitting on "\nnotes (" discarded the whole FAIL section that follows it).
    combined = r.stdout + r.stderr
    marker = "\nFAIL ("
    detail_body = combined[combined.index(marker):] if marker in combined else combined
    return {"passed": r.returncode == 0,
            "detail": "" if r.returncode == 0 else f"{label}: validator red\n{_tail(detail_body, 3000)}"}


def _suite_in(tree: str, label: str) -> dict:
    """Run the whole self-test suite inside a projected tree. Returns {passed, detail}."""
    env = _nested_env()
    r = _run([sys.executable, "-m", "unittest", "discover", "-s", "tools", "-p", "test_*.py", "-b"],
             cwd=os.path.join(tree, ".engine"), env=env, timeout=900)
    if r.returncode == 0:
        return {"passed": True, "detail": ""}
    # Surface the FULL FAIL/ERROR roster up front: several declined-shape tests can break together, and the
    # 3000-char tail keeps only the last traceback — the earlier failing test ids would vanish with no marker,
    # forcing a slow one-at-a-time diagnostic loop across multi-minute gate re-runs.
    summary = [ln for ln in (r.stderr or "").splitlines() if ln.startswith(("FAIL:", "ERROR:"))]
    roster = (f"failing tests ({len(summary)}):\n" + "\n".join(summary) + "\n\n") if summary else ""
    return {"passed": False, "detail": f"{label}: self-tests red\n{roster}last-failure detail:\n{_tail(r.stderr, 3000)}"}


def _arm_operates() -> dict:
    """Arm A. Project the candidate to the deployed shape (default and add-on-declined) and assert it operates.
    Default: validator + full suite (subsumes the retired belt). Declined: validator (whose knowledge-coverage
    check is the StarshipSuperjam/engine-template#663 detector, exercised by the declined projection's own regen) AND the full self-test suite
    (the StarshipSuperjam/engine-template#646 detector — a shipped test that assumes an optional add-on is installed reds the declined
    projection's own suite, which a deployment that declined that add-on would hit). Each projection needs its
    OWN mutable copy (projecting/declining rewrites the tree in place), so each arm captures a fresh candidate
    archive from the (unchanged) working tree rather than sharing one."""
    failures: list = []
    with tempfile.TemporaryDirectory() as d:
        default_tree = _archive_candidate(os.path.join(d, "default"))
        _project_to_deployed(default_tree, decline_optional=False)
        for res in (_validate_in(default_tree, "operate/default"),
                    _suite_in(default_tree, "operate/default")):
            if not res["passed"]:
                failures.append(res["detail"])
    with tempfile.TemporaryDirectory() as d:
        declined_tree = _archive_candidate(os.path.join(d, "declined"))
        declined = _project_to_deployed(declined_tree, decline_optional=True)
        label = f"operate/declined({','.join(declined) or 'none'})"
        for res in (_validate_in(declined_tree, label), _suite_in(declined_tree, label)):
            if not res["passed"]:
                failures.append(res["detail"])
    return {"passed": not failures, "failures": failures}


def _driver_source() -> str:
    """The in-projection driver: run as `python -c` with the working directory inside the projection's own
    `.engine/tools`, so `import module_manager` binds the PROJECTION's copy and `validate.ROOT` (derived from
    that file's own path) resolves to the projection — never home. It asserts that isolation itself before
    mutating anything, injects the candidate as `release_tree` alone (practice mode -> the real seven-check
    child gate, no pull request), and prints the result JSON."""
    return (
        "import json, os, sys\n"
        "sys.path.insert(0, os.getcwd())\n"
        "import validate, module_manager\n"
        "expect = os.path.realpath(os.environ['%s'])\n"
        "here = os.path.realpath(validate.ROOT)\n"
        "assert here == expect, 'ROOT isolation breach: %%r != %%r' %% (here, expect)\n"
        "res = module_manager.upgrade(release_tree=os.environ['%s'])\n"
        "sys.stdout.write('GATE_RESULT:' + json.dumps(res))\n"
    ) % (_DRIVER_EXPECT_ROOT, _DRIVER_CANDIDATE)


def _rollback_driver_source() -> str:
    """The in-projection ROLLBACK driver — a SECOND fresh child, run after the practice upgrade with the working
    directory inside the projection's own `.engine/tools`, so `import module_manager` binds the CANDIDATE'S
    just-overlaid rollback code (the true post-upgrade reality — the upgrade child had imported the baseline's
    copy before the overlay, so rollback could not run there). It re-asserts its own resolved ROOT before
    mutating anything (the same belt as the upgrade driver — `rollback` runs real `git checkout`/branch
    operations, the most destructive surface here), then undoes the staged update through the operator's own
    `rollback(confirm=True)` with the two side-effect boundaries seamed exactly as `demo_594_rollback_discard`
    does: `resync=lambda: True` (no `uv sync` of a projection that has no venv) and `transport=None`. The memory
    put-back is provably a no-op in a projection: `rollback` restores memory only when `detect_migration_revert`
    finds the GITIGNORED store's migration stamp ahead of the code, and a `git archive` projection carries no
    gitignored store — so `transport` is never reached (the leg also asserts the no-offer degrade as a
    postcondition). Prints the full result JSON."""
    return (
        "import json, os, sys\n"
        "sys.path.insert(0, os.getcwd())\n"
        "import validate, module_manager\n"
        "expect = os.path.realpath(os.environ['%s'])\n"
        "here = os.path.realpath(validate.ROOT)\n"
        "assert here == expect, 'ROOT isolation breach: %%r != %%r' %% (here, expect)\n"
        "res = module_manager.rollback(confirm=True, resync=lambda: True, transport=None)\n"
        "sys.stdout.write('ROLLBACK_RESULT:' + json.dumps(res))\n"
    ) % (_DRIVER_EXPECT_ROOT,)


def _upgrade_leg(proj: str, baseline_tag: str, candidate: str) -> dict:
    """Arm B, one baseline — the UPGRADE leg. Run a REAL practice upgrade of the already-projected baseline
    `proj` to the candidate, driven by the PROJECTION's own module_manager (phase-1 runs as the baseline's
    shipped code, exactly as a real deployment would; the tail runs as the overlaid candidate code). Assert
    the upgrade completed with NO refusal reason (a reconcile/migration refusal sets `reason` and leaves an
    early `applied=True` with empty findings — it must NOT read as a pass), no hard structural finding, and
    that it took the practice child path (not a silent network fetch). Returns {passed, detail}. Leaves the
    projection STAGED (the practice tail `git add -A`s but never commits/opens) so the rollback leg can undo
    it."""
    env = _nested_env(**{_DRIVER_EXPECT_ROOT: os.path.abspath(proj),
                         _DRIVER_CANDIDATE: os.path.abspath(candidate)})
    env.pop("GITHUB_TOKEN", None)                    # already stripped by _nested_env; kept in place so the
                                                     # "practice mode opens no PR, deny the token outright"
                                                     # property stays legible at this sensitive spawn
    run = _run([sys.executable, "-c", _driver_source()],
               cwd=os.path.join(proj, ".engine", "tools"), env=env, timeout=600)
    if run.returncode != 0 or "GATE_RESULT:" not in run.stdout:
        return {"passed": False,
                "detail": f"upgrade/{baseline_tag}: the practice upgrade did not complete\n"
                          f"{_tail(run.stderr or run.stdout, 3000)}"}
    result = json.loads(run.stdout.split("GATE_RESULT:", 1)[1])
    problems = []
    # A refusal at ANY step — phase-1 (`refused`) OR the tail (a `reason` with an early `applied=True` and no
    # findings) — means the deployed upgrade did not reconcile cleanly. Reading `reason` catches the tail case.
    if result.get("reason"):
        problems.append(f"the upgrade did not reconcile cleanly: {result['reason']}")
    hard = [f for f in result.get("findings", []) if (f or {}).get("severity") == "hard"]
    if hard:
        problems.append("the structural gate found blocking problem(s): "
                        + "; ".join(str((f or {}).get("id") or (f or {}).get("message")) for f in hard))
    if not result.get("reason"):
        # Only meaningful when the upgrade did NOT already refuse: a clean-looking result must have applied AND
        # carry the practice-path note (else it may have fetched a real release instead of the candidate).
        if not result.get("applied"):
            problems.append("the upgrade did not apply to the projected deployment")
        if module_manager.PRACTICE_RUN_NOTE not in (result.get("notes") or []):
            problems.append("the upgrade did not take the expected practice path (it may have fetched a real "
                            "release instead of testing the candidate)")
    return {"passed": not problems, "detail": "" if not problems
            else f"upgrade/{baseline_tag}: " + "; ".join(problems)}


def _rollback_leg(proj: str, baseline_tag: str) -> dict:
    """Arm B, one baseline — the ROLLBACK leg. Undo the staged practice upgrade the upgrade leg just left in
    `proj`, through a SECOND fresh child running the candidate's overlaid `rollback` (`_rollback_driver_source`).
    Assert the PARSED result, never the exit code: `rollback --confirm` exits 0 on `state:"none"` (nothing to
    undo) and an in-projection git failure degrades to `state:"none"` too — either would be a vacuous pass. The
    bar is the `demo_594` bar: a STAGED update was seen, it was UNDONE, a recovery point was saved first, and no
    refusal/partial. The foreign-work guard NOT tripping is the standing regression for the
    StarshipSuperjam/engine-template#599 rollback-refusal class (an `_upgrade_footprint` that disagreed with the
    reconcile deliver-set would flag the freshly-delivered files as foreign work and refuse). After the undo the
    projection tree must be clean (the discarded overlay lives only on the recovery branch). Returns
    {passed, detail}."""
    env = _nested_env(**{_DRIVER_EXPECT_ROOT: os.path.abspath(proj)})
    env.pop("GITHUB_TOKEN", None)                    # rollback opens no PR and reaches no network — deny outright
    run = _run([sys.executable, "-c", _rollback_driver_source()],
               cwd=os.path.join(proj, ".engine", "tools"), env=env, timeout=600)
    if run.returncode != 0 or "ROLLBACK_RESULT:" not in run.stdout:
        return {"passed": False,
                "detail": f"rollback/{baseline_tag}: the undo did not complete\n"
                          f"{_tail(run.stderr or run.stdout, 3000)}"}
    result = json.loads(run.stdout.split("ROLLBACK_RESULT:", 1)[1])
    problems = []
    if result.get("state") != "staged":
        problems.append(f"the engine saw no staged update to undo (state={result.get('state')!r}) — a vacuous "
                        "pass, not a real rollback")
    if result.get("refused"):
        problems.append(f"the undo refused (the foreign-work guard, the "
                        f"StarshipSuperjam/engine-template#599 class): {result.get('reason')}")
    if result.get("partial"):
        problems.append(f"the undo only partly completed: {result.get('reason')}")
    if not result.get("undone"):
        problems.append("the undo did not report the staged update discarded")
    if not (result.get("recovery_point") or "").startswith("engine-rescue/"):
        problems.append("the undo did not save a recovery point before discarding")
    if result.get("resync_failed"):
        problems.append("the tool-runtime rebuild after the undo reported a failure")
    # The memory put-back must have been the no-op degrade a projection guarantees (no gitignored store -> no
    # migration-revert offer -> transport never reached). A real restore, or a vault-reach attempt, means the
    # leg touched state a projection should never reach.
    if result.get("restored") is True:
        problems.append("the undo unexpectedly restored memory in a projection (it should be a no-op there)")
    note = result.get("memory_note") or ""
    if note.startswith("couldn't reach your backup"):
        problems.append("the undo attempted to reach a memory backup from a projection")
    # The tree must be clean after the undo — the discarded overlay lives only on the recovery branch now.
    st = _run(["git", "-C", proj, "status", "--porcelain"], timeout=60)
    if st.returncode != 0:
        problems.append("could not confirm the projected tree was clean after the undo")
    elif st.stdout.strip():
        problems.append(f"the undo left changes in the projected tree: {_tail(st.stdout, 400)}")
    return {"passed": not problems, "detail": "" if not problems
            else f"rollback/{baseline_tag}: " + "; ".join(problems)}


def _upgrade_from(baseline_tag: str, candidate: str) -> dict:
    """Arm B, one baseline — one supported-version transition. Project the baseline release to its deployed
    shape, run the practice UPGRADE leg, then (only if it passed) the ROLLBACK leg against the same staged
    projection. If the upgrade did not complete the rollback leg is NOT run — a rollback attempt on a half-
    applied tree would obscure the real upgrade failure (this is also the shape `demo_664` drives with a
    deliberately broken candidate). Returns the transition record
    `{baseline, upgrade:{passed,detail}, rollback:{passed,detail}, passed}`, where a not-run rollback carries
    `passed: None`. The whole transition happens inside one tempdir so the projection lives across both legs."""
    with tempfile.TemporaryDirectory() as d:
        proj = _archive_baseline(baseline_tag, os.path.join(d, "old"))
        _project_to_deployed(proj, decline_optional=False)
        _assert_isolated(proj)
        upgrade = _upgrade_leg(proj, baseline_tag, candidate)
        if not upgrade["passed"]:
            return {"baseline": baseline_tag, "upgrade": upgrade,
                    "rollback": {"passed": None, "detail": "not run — the upgrade did not complete"},
                    "passed": False}
        rollback = _rollback_leg(proj, baseline_tag)
        return {"baseline": baseline_tag, "upgrade": upgrade, "rollback": rollback,
                "passed": bool(upgrade["passed"] and rollback["passed"])}


def _baseline_selection() -> dict:
    """The Arm B baseline set, as {floor, baselines, excluded}. `baselines` = every released version tag at or
    above the candidate's clean-upgrade floor, deduped and sorted; `excluded` = the version tags BELOW the floor
    (recorded so the evidence shows the matrix wasn't silently shrunk by a floor bump or a deleted tag). Tags
    are `v`-prefixed; the floor is bare — strip the `v` before the version compare. Below-floor sources are not
    tested here: they predate the floor-preflight code and cannot self-refuse — which is why the floor exists
    (see the supported-upgrade-matrix policy)."""
    floor = None
    try:
        floor = (validate.load_json(os.path.join(validate.ROOT, ".engine", "engine.json")) or {}).get(
            "min_upgradeable_from")
    except Exception:                                    # noqa: BLE001 — no floor -> take all version tags
        floor = None
    tags = _run(["git", "-C", validate.ROOT, "tag", "--list", "v*"], timeout=60)
    if tags.returncode != 0:
        raise GateError(f"could not list release tags to pick upgrade baselines ({_tail(tags.stderr)})")
    baselines, excluded = [], []
    for line in tags.stdout.split():
        m = re.match(r"^v(\d+\.\d+\.\d+)$", line.strip())
        if not m:
            continue
        if floor and validate._ver_tuple(m.group(1)) < validate._ver_tuple(floor):
            excluded.append(line.strip())
            continue
        baselines.append(line.strip())
    key = lambda t: validate._ver_tuple(t[1:])           # noqa: E731 — a one-line sort key reads clearest inline
    return {"floor": floor,
            "baselines": sorted(set(baselines), key=key),
            "excluded": sorted(set(excluded), key=key)}


def _upgrade_baselines() -> list:
    """The released baselines Arm B upgrades FROM (the `baselines` field of `_baseline_selection`)."""
    return _baseline_selection()["baselines"]


def _arm_upgrades(candidate: str) -> dict:
    """Arm B. Run the upgrade+rollback transition for each in-range released baseline and record the matrix.
    `transitions` is the per-baseline record (the executable supported-version matrix); `floor`/`baselines`/
    `excluded` state the matrix's shape so a reviewer can see it was not silently shrunk; `failures` is the
    plain-text detail list the operator log surfaces."""
    sel = _baseline_selection()
    baselines = sel["baselines"]
    if not baselines:
        raise GateError("found no released baseline at or above the clean-upgrade floor to test upgrades from")
    transitions = [_upgrade_from(tag, candidate) for tag in baselines]
    failures = []
    for t in transitions:
        if t["passed"]:
            continue
        for leg in ("upgrade", "rollback"):
            detail = (t.get(leg) or {}).get("detail")
            if (t.get(leg) or {}).get("passed") is False and detail:
                failures.append(detail)
    return {"passed": not failures, "floor": sel["floor"], "baselines": baselines,
            "excluded": sel["excluded"], "transitions": transitions, "failures": failures}


# --------------------------------------------------------------------------- entrypoint

def run_gate() -> dict:
    """Run both arms against the candidate captured from the working tree. Returns a structured result:
    `ran` (False only when inert on a non-home checkout), `passed`, per-arm detail. Fails CLOSED — any
    GateError becomes `passed=False` with a plain reason; the caller must treat a non-pass as a blocked cut."""
    if not _ccc._in_home_repo():
        return {"ran": False, "passed": True, "reason": "inert: not the engine's home repo (a deployed repo "
                "runs the suite directly in its own engine-ci)"}
    before = _worktree_digest()
    arm_a = _arm_operates()                              # Arm A captures its own projection copies internally
    with tempfile.TemporaryDirectory() as d:
        candidate = _archive_candidate(os.path.join(d, "candidate"))
        arm_b = _arm_upgrades(candidate)
    after = _worktree_digest()
    result = {"ran": True, "operates": arm_a, "upgrades": arm_b,
              "passed": bool(arm_a["passed"] and arm_b["passed"]),
              # Evidence identity — the candidate this result describes, and when it was produced — so the
              # release-PR renderer can tie the transition matrix to the tree it was run against (and refuse a
              # stale/mismatched gate JSON) rather than asserting deployed-upgrade evidence for some other tree.
              "candidate_tree": _candidate_tree_sha(),
              "generated_at": datetime.datetime.now(datetime.timezone.utc).replace(
                  microsecond=0).isoformat()}
    if before != after:                                  # the gate must have written nothing to the home tree
        result["passed"] = False
        result["home_tree_mutated"] = True
    return result


def _render(result: dict) -> str:
    """Plain-language operator copy for the cut (never a check id or arm token). A blocked cut says what would
    not work when deployed and that nothing was changed."""
    if not result.get("ran"):
        return "The deployment gate is inert here (this is not the engine's home repo); nothing to check."
    if result.get("passed"):
        n = len((result.get("upgrades") or {}).get("transitions") or [])
        matrix = (f" (upgrade and rollback verified from {n} supported source version"
                  f"{'' if n == 1 else 's'})") if n else ""
        return ("The deployment gate passed: this release operates when deployed, and upgrades then cleanly "
                f"rolls back{matrix}.")
    if result.get("home_tree_mutated"):
        return ("The deployment gate was stopped because it detected an unexpected change to the release "
                "working copy while checking. No release pull request was opened and nothing was changed; "
                "this is an engine defect to report.")
    parts = ["This release would not work correctly when deployed, so no release pull request was opened and "
             "nothing was changed:"]
    if not (result.get("operates") or {}).get("passed", True):
        parts.append("  - it does not OPERATE cleanly on a deployed shape (a self-test or consistency check "
                     "failed against a projected deployment).")
    if not (result.get("upgrades") or {}).get("passed", True):
        parts.append("  - a deployed engine could not UPGRADE cleanly onto it from a supported version, or "
                     "could not cleanly UNDO that update.")
    parts.append("Fix the problem and cut the release again; a transient infrastructure hiccup clears by "
                 "re-running the `release-gate` workflow.")
    return "\n".join(parts)


def _summary_md(result: dict) -> str:
    """A plain-markdown per-transition summary for `$GITHUB_STEP_SUMMARY` — rendered HERE (never assembled in
    workflow bash) so the one home for this rendering is the tool, and STRUCTURED FIELDS ONLY: the baseline tag
    and per-leg outcome, never a raw `detail` string (those are unsanitized nested stderr — local paths,
    tracebacks, and `::`-prefixed text that a workflow-command stream or markdown table would mis-parse)."""
    if not result.get("ran"):
        return "### Deployment gate: not applicable here (this repository runs its own engine-ci directly)\n"
    up = result.get("upgrades") or {}
    transitions = up.get("transitions") or []
    # This block reports the upgrade/rollback matrix (Arm B) — its header reflects THAT arm's status, not the
    # overall gate verdict (which also covers the separate operate arm), so a green matrix is never mislabelled
    # BLOCKED because a different arm failed.
    head = "passed" if up.get("passed") else "BLOCKED"
    lines = [f"### Deployed upgrade and rollback check: {head}", ""]
    if up.get("floor"):
        n = len(transitions)
        excl = up.get("excluded") or []
        extra = f"; below the floor and not tested: {', '.join(excl)}" if excl else ""
        lines.append(f"Supported source versions: every released version at or above the clean-upgrade floor "
                     f"`{up['floor']}` ({n} transition{'' if n == 1 else 's'}{extra}).")
        lines.append("")
    if transitions:
        lines += ["| from version | practice upgrade | undo (rollback) |", "| --- | --- | --- |"]
        mark = {True: "pass", False: "FAIL", None: "not run"}
        for t in transitions:
            up_state = mark.get((t.get("upgrade") or {}).get("passed"), "unknown")
            rb_state = mark.get((t.get("rollback") or {}).get("passed"), "unknown")
            lines.append(f"| `{t.get('baseline')}` | {up_state} | {rb_state} |")
        lines.append("")
    lines.append("_A mechanical deploy-and-undo check on a projected deployed copy — not a readiness judgment._")
    return "\n".join(lines) + "\n"


def main(argv: list | None = None) -> int:
    ap = argparse.ArgumentParser(description="Release-cut deployment gate (operate + upgrade/rollback when "
                                             "deployed).")
    ap.add_argument("--json", action="store_true",
                    help="emit the structured result as JSON on stdout instead of the plain-language render")
    ap.add_argument("--json-out", metavar="PATH",
                    help="also write the structured result as JSON to PATH (stdout stays plain-language) — so a "
                         "caller can read machine fields while the operator log gets plain words")
    ap.add_argument("--summary-out", metavar="PATH",
                    help="also write a plain-markdown per-transition summary to PATH (for $GITHUB_STEP_SUMMARY) "
                         "— structured fields only, never raw failure detail")
    args = ap.parse_args(argv)
    try:
        result = run_gate()
    except GateError as exc:                             # fail CLOSED: a setup failure blocks the cut
        result = {"ran": True, "passed": False, "reason": str(exc)}
    except Exception as exc:                             # noqa: BLE001 — any unexpected error also blocks
        result = {"ran": True, "passed": False, "reason": f"the deployment gate hit an unexpected error ({exc})"}
    if args.json_out:
        try:
            with open(args.json_out, "w", encoding="utf-8") as fh:
                json.dump(result, fh)
        except OSError as exc:
            sys.stderr.write(f"(could not write the gate result to {args.json_out}: {exc})\n")
    if args.summary_out:
        try:
            with open(args.summary_out, "w", encoding="utf-8") as fh:
                fh.write(_summary_md(result))
        except OSError as exc:
            sys.stderr.write(f"(could not write the gate summary to {args.summary_out}: {exc})\n")
    if args.json:
        sys.stdout.write(json.dumps(result) + "\n")
    else:
        # Plain-language render is the operator's signal (never a check id / arm token); machine detail goes to
        # stderr and --json-out, so a workflow log leads with plain words even when it also captures the JSON.
        sys.stdout.write(_render(result) + "\n")
        if result.get("reason") and not result.get("passed"):
            sys.stderr.write(result["reason"] + "\n")
        for arm in ("operates", "upgrades"):
            for detail in (result.get(arm) or {}).get("failures", []):
                sys.stderr.write(detail + "\n")
    return 0 if result.get("passed") else 1


if __name__ == "__main__":
    sys.exit(main())
