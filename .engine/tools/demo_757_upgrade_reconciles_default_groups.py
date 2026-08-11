#!/usr/bin/env python3
"""Behavioral FALSIFICATION for issue #757 — an engine update RECONCILES its own tool-runtime dependency-group
selection so the update's OWN pull request is born green, never red on `uv-group-drift`. The upgrade overlay
replaces `.engine/pyproject.toml` WHOLESALE with the release's `default-groups` (the release's construction
set), but the deployment's installed module set may derive a different selection — so without a reconcile the
committed `[tool.uv] default-groups` drifts from what the modules derive, and the update opens a pull request
that immediately fails the `uv-group-drift` CI check. The operator then has to hand-run `sync-groups` (#757).

FAIL-THEN-PASS driving the REAL upgrade tail (the practice-child path: a local release injected, no opener —
so the version-sensitive tail runs as the freshly-overlaid code and the REAL structural gate runs, exactly as
a live upgrade would). Three arms, each from the same pristine clone:
  * POSITIVE (the fix): the overlay wrote a `default-groups` that drifts from the deployed set (a single-line
    array the reconciler can rewrite). The tail re-derives from the installed modules and rewrites the line, so
    committed == derived, the structural gate is clean, and the update opens for review — here the reconcile
    silently restores the deployment's own selection that the overlay had clobbered, so there is NO
    operator-facing change and the pull-request body must stay silent (the false-positive guard: no crying wolf).
  * GENUINE CHANGE: the deployment's OWN committed selection was stale (missing a group its installed modules
    derive). The update genuinely changes it, so `groups_changed` is True, the delta is measured against the
    operator's TRUE prior (not the overlay's transient value), and the body surfaces it (the false-negative
    guard: a real supply-chain-relevant change is never silently omitted).
  * NEGATIVE CONTROL (the failure it guards): the release's committed `default-groups` is a MULTI-LINE array,
    which the single-line rewriter deliberately refuses (`rewrite_default_groups_text` raises) and so fails
    OPEN — the reconcile cannot fix the drift. The structural gate now carries `uv-group-drift`, so instead of
    opening a red pull request the update REFUSES cleanly (nothing opened), honouring the tail's "never open a
    broken PR" contract. Disable that gate membership and the same tree would open a self-red pull request —
    the exact #757 symptom.

The reconcile is direction-agnostic (add or remove a group); the pure rewrite/derive helpers and both
directions are also unit-covered by `test_module_manager.TestUpgradeDefaultGroupsReconcile`.

This is a PERMANENT regression (forever-relevant upgrade behaviour, like demo_599): its companion test
`test_module_manager.TestUpgradeDefaultGroupsReconcile.test_the_757_falsification_demo_passes` runs it, so it
travels with the engine and guards this in every generated repo. Run it directly:
`uv run --directory .engine -- python tools/demo_757_upgrade_reconciles_default_groups.py`.
"""
from __future__ import annotations
import os
import re
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import engine_fixture           # noqa: E402  (the shared tracked-only fixture clone)
import validate                 # noqa: E402
import module_manager as mm     # noqa: E402  (the real upgrade + reconcile + structural gate under test)

_PYPROJECT_REL = os.path.join(".engine", "pyproject.toml")
# The single-line default-groups array the overlay ships — anchored so we replace exactly that one line.
_SINGLE_LINE_RE = re.compile(r"(?m)^[ \t]*default-groups[ \t]*=[ \t]*\[[^\]\n]*\][ \t]*$")


def _set_release_default_groups(release: str, rendering: str) -> None:
    """Rewrite the release clone's committed `[tool.uv] default-groups` to `rendering` (a literal array text,
    single- or multi-line). This is the ONLY edit to the pristine clone, so the upgrade's structural gate sees
    a tree that is clean apart from the dependency-group drift we are isolating."""
    path = os.path.join(release, _PYPROJECT_REL)
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    new_text, n = _SINGLE_LINE_RE.subn(f"default-groups = {rendering}", text)
    if n != 1:
        raise AssertionError(f"fixture setup: expected exactly one default-groups line to replace, found {n}")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(new_text)


def main() -> int:
    real_root = validate.ROOT   # capture the REAL repo before any redirect
    failures = []
    print("=" * 78)
    print("DEMO #757 — an engine update reconciles its OWN tool-runtime dependency-group selection, so the")
    print("update's pull request is born green (never red on uv-group-drift), disclosing a genuine change to the")
    print("operator but staying silent when it merely restores their selection. Same pristine clone, three arms.")
    print("=" * 78)

    # ---- POSITIVE: a single-line drift the reconcile rewrites; committed == derived, the gate is clean ----
    with tempfile.TemporaryDirectory() as d:
        live = engine_fixture.clone_engine(real_root, os.path.join(d, "live"))
        release = engine_fixture.clone_engine(real_root, os.path.join(d, "release"))
        # The release UNDER-lists default-groups (drops a group the full deployed set derives): committed drifts
        # BELOW derived. Single-line, so the reconciler can rewrite it.
        _set_release_default_groups(release, '["core"]')
        with mm._redirect_root(live):
            derived = mm.derive_uv_groups()                         # what the installed modules derive
            result = mm.upgrade(ref="v-demo", release_tree=release)  # practice-child: REAL reconcile + gate
            committed_after = mm.committed_default_groups()          # after the tail wrote pyproject
        in_sync = committed_after == derived
        recorded = (result.get("groups_after") or []) == derived
        gate_clean = not [f for f in (result.get("findings") or []) if f.get("severity") == "hard"]
        opened_or_practice = result.get("reason") is None    # no refusal — the update reached the open step
        no_false_alarm = result.get("groups_changed") is False   # net-zero for the operator -> must NOT cry wolf
        print("\n[POSITIVE — the release under-listed default-groups; the reconcile rewrites the single line]")
        print(f"  derived selection (installed modules):        {derived}")
        print(f"  committed default-groups after the update:    {committed_after}")
        print(f"  committed now equals derived (no drift):      {in_sync}")
        print(f"  new selection recorded for the PR body:       {recorded}")
        print(f"  structural gate clean (no hard finding):      {gate_clean}")
        print(f"  update did not refuse (reached the open step): {opened_or_practice}")
        print(f"  no operator-facing change announced (correct): {no_false_alarm}")
        if not in_sync:
            failures.append(f"POSITIVE: committed default-groups {committed_after} != derived {derived} after the update")
        if not recorded:
            failures.append("POSITIVE: the reconciled selection was not recorded in the result for the PR body")
        if not gate_clean:
            failures.append("POSITIVE: the structural gate hard-flagged the reconciled tree")
        if not opened_or_practice:
            failures.append(f"POSITIVE: the update refused despite a clean reconcile: {result.get('reason')}")
        if not no_false_alarm:
            failures.append("POSITIVE: a net-zero reconcile falsely reported an operator-facing group change (cries wolf)")

    # ---- GENUINE CHANGE: the deployment's OWN committed selection was stale; the update corrects it and says so ----
    with tempfile.TemporaryDirectory() as d:
        live = engine_fixture.clone_engine(real_root, os.path.join(d, "live"))
        release = engine_fixture.clone_engine(real_root, os.path.join(d, "release"))
        # The deployment's committed default-groups is genuinely stale (missing a group its installed modules
        # derive) — the field "false negative": a real change the update makes and MUST surface. Edit the LIVE
        # clone's PRE-upgrade committed value, leaving all modules present so the derived set is the fuller one.
        _set_release_default_groups(live, '["core"]')
        with mm._redirect_root(live):
            prior = mm.committed_default_groups()                     # ["core"] — what the operator actually had
            derived = mm.derive_uv_groups()
            result = mm.upgrade(ref="v-demo", release_tree=release)
            committed_after = mm.committed_default_groups()
        changed = result.get("groups_changed") is True
        before_is_operator_prior = (result.get("groups_before") or []) == prior     # baseline is the TRUE prior
        after_is_derived = (result.get("groups_after") or []) == derived
        corrected = committed_after == derived
        print("\n[GENUINE CHANGE — the deployment's committed selection was stale; the update corrects it]")
        print(f"  operator's prior committed selection:         {prior}")
        print(f"  committed default-groups after the update:    {committed_after}")
        print(f"  an operator-facing change WAS announced:      {changed}")
        print(f"  the delta baseline is the operator's prior:   {before_is_operator_prior}")
        if not changed:
            failures.append("GENUINE: a real selection change was NOT surfaced (the false-negative #757 guards against)")
        if not before_is_operator_prior:
            failures.append(f"GENUINE: groups_before {result.get('groups_before')} is not the operator's true prior {prior}")
        if not (after_is_derived and corrected):
            failures.append(f"GENUINE: committed {committed_after} != derived {derived} after the update")

    # ---- NEGATIVE CONTROL: a MULTI-LINE array the reconciler can't rewrite -> fail-open -> the gate refuses ----
    with tempfile.TemporaryDirectory() as d:
        live = engine_fixture.clone_engine(real_root, os.path.join(d, "live"))
        release = engine_fixture.clone_engine(real_root, os.path.join(d, "release"))
        # Same drift, but written as a multi-line array: valid TOML, but the single-line rewriter refuses it,
        # so the reconcile fails OPEN and the drift survives into the gate.
        _set_release_default_groups(release, '[\n    "core",\n]')
        with mm._redirect_root(live):
            result = mm.upgrade(ref="v-demo", release_tree=release)   # practice-child: REAL gate
        refused = result.get("reason") is not None and result.get("pr") is None
        drift_finding = any(f.get("source_rule") == "engine/check/uv-group-drift" and f.get("severity") == "hard"
                            for f in (result.get("findings") or []))
        reason_names_groups = "dependency groups" in (result.get("reason") or "")
        failed_open = any("Could not reconcile the tool-runtime dependency groups" in n
                          for n in (result.get("notes") or []))
        print("\n[NEGATIVE CONTROL — a multi-line default-groups array the reconciler can't rewrite]")
        print(f"  reconcile failed open (drift survived):       {failed_open}")
        print(f"  the gate carried uv-group-drift and hard-flagged it: {drift_finding}")
        print(f"  the update REFUSED cleanly (no PR opened):     {refused}")
        print(f"  the refusal names the dependency-group cause:  {reason_names_groups}")
        if not failed_open:
            failures.append("NEGATIVE: the reconcile did not fail open on the multi-line array (the setup is wrong)")
        if not drift_finding:
            failures.append("NEGATIVE: the structural gate did NOT carry/flag uv-group-drift on the drifting tree")
        if not refused:
            failures.append("NEGATIVE: the update did not refuse — it would have opened a self-red pull request (#757)")
        if not reason_names_groups:
            failures.append("NEGATIVE: the refusal did not name the dependency-group cause (a dead-end 'retry')")

    print("\n" + "=" * 78)
    if failures:
        print("DEMO #757 FAILED:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("DEMO #757 PASSED: the update re-derives and rewrites its own default-groups so committed == derived "
          "and the pull request is born green; when the reconcile can't fix the drift, the gate (now carrying "
          "uv-group-drift) refuses cleanly instead of opening a self-red pull request. The reconcile and the "
          "gate membership are load-bearing.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
