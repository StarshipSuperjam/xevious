#!/usr/bin/env python3
"""Behavioral FALSIFICATION for issue #759 — an engine update INSTALLS the new modules a release adds that the
deployment needs (a `required` capability mandatorily, a NET-NEW `default-on` add-on opt-out) and OFFERS the
optional ones, as part of the update's OWN pull request — the addition mirror of #688's whole-module removal.
Before #759 the upgrade only ever touched modules the deployment already had, so a module a release added stayed
silently off (the concrete 0.4.1 case: `memory-semantic-recall` was default-on, never installed, never surfaced).

FAIL-THEN-PASS driving the REAL upgrade tail. Three arms run the practice-child path (a local release injected,
no opener → a fresh child interpreter runs the freshly-overlaid tail and the REAL structural gate, exactly as a
live upgrade would); the negative control runs the in-process path so it can disable the new behavior. Synthetic
modules are injected into the RELEASE clone (and, for the decline arm, the LIVE clone's catalog) so the arms are
cheap and isolate exactly the classification under test — the real modules already present in both clones are
version-unchanged no-ops:

  * POSITIVE (the fix): the release adds a net-new `required`, a net-new `default-on`, and a net-new `optional`
    module. The update INSTALLS the required and the default-on (files, package entry, coherent), OFFERS the
    optional (never installed), the structural gate is clean, and the pull-request body discloses all three.
  * REQUIRED-FAILS-CLOSED (the guarantee): the release adds a `required` module whose dependency is absent, so
    `add()` refuses it. No structural check compares the deployed set to the release's required set, so the tail
    itself REFUSES cleanly (nothing opened) rather than shipping a green pull request missing a required
    capability — the #759 primary-path guarantee.
  * RESPECT-DECLINE (the critical safety property): the release adds a `default-on` module the deployment already
    KNEW (it is in the deployment's pre-overlay catalog) but does not have installed — i.e. previously declined.
    The update must NOT resurrect it: it is OFFERED, never installed. Auto-installing only NET-NEW default-on is
    what keeps an update from turning back on a capability the operator deliberately switched off.
  * NEGATIVE CONTROL (reproduces #759): with the classifier disabled, the same net-new default-on module stays
    OFF — the exact symptom the fix removes.

This is a PERMANENT regression (forever-relevant upgrade behaviour, like demo_757): its companion test
`test_module_manager.TestUpgradeInstallsNewModules.test_the_759_falsification_demo_passes` runs it, so it travels
with the engine and guards this in every generated repo. Run it directly:
`uv run --directory .engine -- python tools/demo_759_upgrade_installs_new_modules.py`.
"""
from __future__ import annotations
import json
import os
import re
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import engine_fixture           # noqa: E402  (the shared tracked-only fixture clone)
import module_manager as mm     # noqa: E402  (the real upgrade + install + structural gate under test)


def _put_module(tree: str, mid: str, status: str, depends=None, provides=None) -> None:
    """Write a synthetic module manifest into `tree`'s .engine/modules/<mid>/ (and any provided files)."""
    mdir = os.path.join(tree, ".engine", "modules", mid)
    os.makedirs(mdir, exist_ok=True)
    man = {"id": mid, "version": "0.1.0", "status": status, "provides": provides or {}, "wires": [],
           "depends": depends or {"core": ""}}
    with open(os.path.join(mdir, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(man, fh, indent=2)
    for patterns in (provides or {}).values():
        for rel in patterns:
            if "*" in rel:
                continue
            dst = os.path.join(tree, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            with open(dst, "w", encoding="utf-8") as fh:
                fh.write(f"# {mid} fixture surface\n")


def _catalog_add(tree: str, mid: str, description: str = "a fixture add-on") -> None:
    """Append a catalog entry for `mid` to `tree`'s module-catalog.json (so the catalog-completeness gate is
    satisfied for a default-on/optional module, and so the deployment 'knows' it for the decline discriminator)."""
    p = os.path.join(tree, ".engine", "provisioning", "module-catalog.json")
    data = json.load(open(p, encoding="utf-8")) if os.path.isfile(p) else []
    data.append({"id": mid, "description": description, "category": "Verification & Validation", "status": "optional"})
    with open(p, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)


def _add_dep_group(tree: str, name: str) -> None:
    """Declare a `[dependency-groups]` entry named `name` in the release pyproject, so a module of that id
    contributes a Python-dependency group the reconcile must pick up ONCE the module is installed — exercising
    the install-before-reconcile ordering (#757 composes with #759): if the install ran after the group reconcile,
    the group would be erased because the module wasn't yet present when `derive_uv_groups()` ran."""
    p = os.path.join(tree, ".engine", "pyproject.toml")
    with open(p, encoding="utf-8") as fh:
        text = fh.read()
    new_text = re.sub(r"(?m)^(\[dependency-groups\]\s*\n)", rf"\g<1>{name} = []\n", text, count=1)
    if new_text == text:
        raise AssertionError("fixture setup: no [dependency-groups] table found to extend")
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(new_text)


def _break_manifest(tree: str, mid: str) -> None:
    """Corrupt module `mid`'s manifest.json in `tree` to invalid JSON — a malformed release manifest."""
    with open(os.path.join(tree, ".engine", "modules", mid, "manifest.json"), "w", encoding="utf-8") as fh:
        fh.write("{ this is not valid json ")


def _packages(tree: str) -> dict:
    return (json.load(open(os.path.join(tree, ".engine", "engine.json"), encoding="utf-8")).get("packages") or {})


def _installed_dir(tree: str, mid: str) -> bool:
    return os.path.isfile(os.path.join(tree, ".engine", "modules", mid, "manifest.json"))


def _hard(result: dict) -> list:
    return [f for f in (result.get("findings") or []) if f.get("severity") == "hard"]


def main() -> int:
    real_root = mm.validate.ROOT
    failures = []
    print("=" * 78)
    print("DEMO #759 — an engine update installs the NEW modules a release adds that the deployment needs")
    print("(required mandatorily, net-new default-on opt-out) and offers the optional ones, never resurrecting a")
    print("declined module. Same pristine clones, four arms.")
    print("=" * 78)

    # ---- POSITIVE: net-new required + default-on installed; net-new optional offered; gate clean; body discloses
    with tempfile.TemporaryDirectory() as d:
        live = engine_fixture.clone_engine(real_root, os.path.join(d, "live"))
        release = engine_fixture.clone_engine(real_root, os.path.join(d, "release"))
        _put_module(release, "fixture-req", "required")
        _put_module(release, "fixture-def", "default-on"); _catalog_add(release, "fixture-def")
        _add_dep_group(release, "fixture-def")           # the default-on carries a Python-dependency group
        _put_module(release, "fixture-opt", "optional"); _catalog_add(release, "fixture-opt")
        with mm._redirect_root(live):
            result = mm.upgrade(ref="v-demo", release_tree=release)   # practice-child: REAL install + gate
            body = mm.render_upgrade_pr_body(result.get("from") or {}, result.get("to") or {}, result)
            pkgs = _packages(live)
            req_dir, def_dir, opt_dir = _installed_dir(live, "fixture-req"), _installed_dir(live, "fixture-def"), _installed_dir(live, "fixture-opt")
        installed = {m["id"]: m for m in (result.get("modules_installed") or [])}
        offered = {m["id"] for m in (result.get("modules_offered") or [])}
        group_survived = "fixture-def" in (result.get("groups_after") or [])   # install ran BEFORE the reconcile
        desc_in_body = "a fixture add-on" in body                              # installed modules carry their description
        print("\n[POSITIVE — the release adds a required, a default-on, and an optional module]")
        print(f"  reached the open step (no refusal):           {result.get('reason') is None}")
        print(f"  required installed (fixture-req):             {'fixture-req' in installed} / dir {req_dir} / pkg {'fixture-req' in pkgs}")
        print(f"  default-on installed (fixture-def):           {'fixture-def' in installed} / dir {def_dir} / pkg {'fixture-def' in pkgs}")
        print(f"  optional OFFERED not installed (fixture-opt):  offered={'fixture-opt' in offered} / dir {opt_dir} / pkg {'fixture-opt' in pkgs}")
        print(f"  installed default-on's dependency group kept: {group_survived}")
        print(f"  installed modules carry their description:     {desc_in_body}")
        print(f"  structural gate clean (no hard finding):      {not _hard(result)}")
        print(f"  PR body names all three:                      {all(x in body for x in ('fixture-req', 'fixture-def', 'fixture-opt'))}")
        if result.get("reason") is not None:
            failures.append(f"POSITIVE: the update refused unexpectedly: {result.get('reason')}")
        if not group_survived:
            failures.append("POSITIVE: the newly-installed default-on module's dependency group was erased by the reconcile (install must run before it)")
        if not desc_in_body:
            failures.append("POSITIVE: the PR body did not carry the installed modules' catalog description")
        if not (installed.get("fixture-req", {}).get("status") == "required" and req_dir and "fixture-req" in pkgs):
            failures.append("POSITIVE: the net-new required module was not installed")
        if not (installed.get("fixture-def", {}).get("status") == "default-on" and def_dir and "fixture-def" in pkgs):
            failures.append("POSITIVE: the net-new default-on module was not installed")
        if "fixture-opt" not in offered or opt_dir or "fixture-opt" in pkgs:
            failures.append("POSITIVE: the optional module was not offered-not-installed as expected")
        if _hard(result):
            failures.append(f"POSITIVE: the structural gate hard-flagged the rebuilt tree: {[mm.validate.fmt(f) for f in _hard(result)]}")
        if not all(x in body for x in ("fixture-req", "fixture-def", "fixture-opt")):
            failures.append("POSITIVE: the pull-request body did not disclose the installed/offered modules")

    # ---- REQUIRED-FAILS-CLOSED: a required module the release adds cannot install -> the tail refuses cleanly
    with tempfile.TemporaryDirectory() as d:
        live = engine_fixture.clone_engine(real_root, os.path.join(d, "live"))
        release = engine_fixture.clone_engine(real_root, os.path.join(d, "release"))
        _put_module(release, "fixture-badreq", "required", depends={"core": "", "no-such-module": ""})
        with mm._redirect_root(live):
            result = mm.upgrade(ref="v-demo", release_tree=release)
            pkgs = _packages(live)
        refused = result.get("reason") is not None and result.get("pr") is None
        names_cause = "REQUIRES" in (result.get("reason") or "")
        print("\n[REQUIRED-FAILS-CLOSED — a required module the release adds cannot be installed]")
        print(f"  the update REFUSED cleanly (no PR opened):     {refused}")
        print(f"  the refusal names the required-capability cause: {names_cause}")
        print(f"  the broken required module was NOT recorded:   {'fixture-badreq' not in pkgs}")
        if not refused:
            failures.append("REQUIRED-FAILS-CLOSED: the update did not refuse — it would ship missing a required module (#759)")
        if not names_cause:
            failures.append("REQUIRED-FAILS-CLOSED: the refusal did not name the required-capability cause")
        if "fixture-badreq" in pkgs:
            failures.append("REQUIRED-FAILS-CLOSED: the un-installable required module was recorded anyway")

    # ---- RESPECT-DECLINE: a default-on the deployment KNEW (catalogued) but lacks is offered, never resurrected
    with tempfile.TemporaryDirectory() as d:
        live = engine_fixture.clone_engine(real_root, os.path.join(d, "live"))
        release = engine_fixture.clone_engine(real_root, os.path.join(d, "release"))
        _catalog_add(live, "fixture-def")                 # the deployment KNEW it (pre-overlay catalog) -> declined
        _put_module(release, "fixture-def", "default-on"); _catalog_add(release, "fixture-def")
        with mm._redirect_root(live):
            result = mm.upgrade(ref="v-demo", release_tree=release)
            pkgs = _packages(live)
            def_dir = _installed_dir(live, "fixture-def")
        installed_ids = {m["id"] for m in (result.get("modules_installed") or [])}
        offered_ids = {m["id"] for m in (result.get("modules_offered") or [])}
        print("\n[RESPECT-DECLINE — a default-on the deployment previously declined must NOT be resurrected]")
        print(f"  NOT installed (fixture-def):                  installed={('fixture-def' in installed_ids)} / dir {def_dir} / pkg {'fixture-def' in pkgs}")
        print(f"  offered instead:                              {'fixture-def' in offered_ids}")
        print(f"  structural gate clean:                        {not _hard(result)}")
        if "fixture-def" in installed_ids or def_dir or "fixture-def" in pkgs:
            failures.append("RESPECT-DECLINE: a previously-declined default-on module was RESURRECTED (the safety property failed)")
        if "fixture-def" not in offered_ids:
            failures.append("RESPECT-DECLINE: the declined default-on module was not offered")
        if _hard(result):
            failures.append(f"RESPECT-DECLINE: the structural gate hard-flagged the tree: {[mm.validate.fmt(f) for f in _hard(result)]}")

    # ---- NEGATIVE CONTROL: disable the classifier (in-process) -> the net-new default-on stays OFF (#759 symptom)
    with tempfile.TemporaryDirectory() as d:
        live = engine_fixture.clone_engine(real_root, os.path.join(d, "live"))
        release = engine_fixture.clone_engine(real_root, os.path.join(d, "release"))
        _put_module(release, "fixture-def", "default-on"); _catalog_add(release, "fixture-def")
        saved = mm.classify_available_modules
        mm.classify_available_modules = lambda *a, **k: {"install": [], "offered": []}
        try:
            with mm._redirect_root(live):
                # in-process (release + an injected opener) so the disabled classifier in THIS process is the one
                # the tail calls — a practice child would run the real committed code and ignore the monkeypatch.
                result = mm.upgrade(ref="v-demo", release_tree=release, opener=lambda **kw: {"number": 1})
                pkgs = _packages(live)
                def_dir = _installed_dir(live, "fixture-def")
        finally:
            mm.classify_available_modules = saved
        stayed_off = "fixture-def" not in pkgs and not def_dir
        print("\n[NEGATIVE CONTROL — classifier disabled; the net-new default-on module stays off (#759 symptom)]")
        print(f"  the net-new default-on stayed OFF:            {stayed_off}")
        if not stayed_off:
            failures.append("NEGATIVE: with the classifier disabled the module was still installed (the arm proves nothing)")

    # ---- MALFORMED-MANIFEST: a broken manifest for a REQUIRED module must NOT be silently skipped -> refuse
    with tempfile.TemporaryDirectory() as d:
        live = engine_fixture.clone_engine(real_root, os.path.join(d, "live"))
        release = engine_fixture.clone_engine(real_root, os.path.join(d, "release"))
        _put_module(release, "fixture-req", "required")
        _break_manifest(release, "fixture-req")           # a net-new required module could hide behind a bad manifest
        with mm._redirect_root(live):
            preview = mm.plan_upgrade(ref="v-demo", release_tree=release, target_ref="v-demo", available="v-demo")
            result = mm.upgrade(ref="v-demo", release_tree=release)
            pkgs = _packages(live)
        refused = result.get("reason") is not None and result.get("pr") is None
        names_cause = "malformed" in (result.get("reason") or "")
        preview_refused = preview.get("refused") and preview.get("status") == "broken-release"
        print("\n[MALFORMED-MANIFEST — a broken module manifest in the release must refuse, never silently skip]")
        print(f"  the update REFUSED cleanly (no PR opened):     {refused}")
        print(f"  the refusal names the malformed cause:         {names_cause}")
        print(f"  the PREVIEW also refuses (no preview/apply drift): {preview_refused}")
        print(f"  the broken module was NOT installed:           {'fixture-req' not in pkgs}")
        if not refused:
            failures.append("MALFORMED-MANIFEST: the update did not refuse — a required module hid behind a bad manifest (#759)")
        if not names_cause:
            failures.append("MALFORMED-MANIFEST: the refusal did not name the malformed-release cause")
        if not preview_refused:
            failures.append("MALFORMED-MANIFEST: the read-only preview did not also refuse (preview/apply drift on a malformed release)")

    # ---- BAD-INSTALL: add() returns applied=True with a hard finding (a wire it couldn't apply) -> NOT a success
    with tempfile.TemporaryDirectory() as d:
        live = engine_fixture.clone_engine(real_root, os.path.join(d, "live"))
        release = engine_fixture.clone_engine(real_root, os.path.join(d, "release"))
        _put_module(release, "fixture-def", "default-on"); _catalog_add(release, "fixture-def")
        saved_add = mm.add
        # Simulate the real hazard the security review found: add() reports applied=True but leaves a hard
        # coherence finding (a wire the dispatcher turned into a finding, not an exception). The in-process path
        # runs THIS process's monkeypatch (a practice child would run the committed add()).
        mm.add = lambda mid, release_tree=None, ref=None: {
            "module_id": mid, "applied": True, "reason": None,
            "findings": [{"severity": "hard", "message": "a wire could not be applied"}]}
        try:
            with mm._redirect_root(live):
                result = mm.upgrade(ref="v-demo", release_tree=release, opener=lambda **kw: {"number": 1})
        finally:
            mm.add = saved_add
        installed_ids = {m["id"] for m in (result.get("modules_installed") or [])}
        offered_ids = {m["id"] for m in (result.get("modules_offered") or [])}
        print("\n[BAD-INSTALL — add() reports applied but with a hard finding; must NOT count as installed]")
        print(f"  NOT recorded as installed:                    {'fixture-def' not in installed_ids}")
        print(f"  demoted to an offer instead (default-on):     {'fixture-def' in offered_ids}")
        if "fixture-def" in installed_ids:
            failures.append("BAD-INSTALL: a module with a hard install finding was falsely recorded as installed (the security hole)")
        if "fixture-def" not in offered_ids:
            failures.append("BAD-INSTALL: the failed default-on install was not demoted to an offer")

    # ---- UNTRUSTED-CATALOG (end-to-end): an unreadable pre-overlay catalog fails default-on CLOSED to offer-only
    with tempfile.TemporaryDirectory() as d:
        live = engine_fixture.clone_engine(real_root, os.path.join(d, "live"))
        release = engine_fixture.clone_engine(real_root, os.path.join(d, "release"))
        _put_module(release, "fixture-def", "default-on"); _catalog_add(release, "fixture-def")
        with open(os.path.join(live, ".engine", "provisioning", "module-catalog.json"), "w", encoding="utf-8") as fh:
            fh.write("{ not a catalog ")           # corrupt the deployment's pre-overlay catalog
        with mm._redirect_root(live):
            result = mm.upgrade(ref="v-demo", release_tree=release)
            pkgs = _packages(live)
        installed_ids = {m["id"] for m in (result.get("modules_installed") or [])}
        offered_ids = {m["id"] for m in (result.get("modules_offered") or [])}
        catalog_noted = any("could not read the module catalog" in n for n in (result.get("notes") or []))
        print("\n[UNTRUSTED-CATALOG — an unreadable pre-overlay catalog fails default-on closed to offer-only]")
        print(f"  net-new default-on NOT installed:             {'fixture-def' not in pkgs and 'fixture-def' not in installed_ids}")
        print(f"  offered instead:                              {'fixture-def' in offered_ids}")
        print(f"  the degraded-catalog fallback is disclosed:   {catalog_noted}")
        if "fixture-def" in pkgs or "fixture-def" in installed_ids:
            failures.append("UNTRUSTED-CATALOG: a default-on was auto-installed despite an unreadable catalog (fail-open safety hole)")
        if "fixture-def" not in offered_ids:
            failures.append("UNTRUSTED-CATALOG: the default-on was not offered under an unreadable catalog")
        if not catalog_noted:
            failures.append("UNTRUSTED-CATALOG: the degraded-catalog fallback was not disclosed to the operator")

    # ---- MISSING-MANIFEST-DIR: a module directory with NO manifest at all must refuse, not silently skip
    with tempfile.TemporaryDirectory() as d:
        live = engine_fixture.clone_engine(real_root, os.path.join(d, "live"))
        release = engine_fixture.clone_engine(real_root, os.path.join(d, "release"))
        os.makedirs(os.path.join(release, ".engine", "modules", "fixture-req"))   # a module dir, but no manifest.json
        with mm._redirect_root(live):
            result = mm.upgrade(ref="v-demo", release_tree=release)
            pkgs = _packages(live)
        refused = result.get("reason") is not None and result.get("pr") is None
        print("\n[MISSING-MANIFEST-DIR — a module directory with no manifest at all must refuse, not skip]")
        print(f"  the update REFUSED cleanly:                   {refused}")
        print(f"  the (manifest-less) module was NOT installed: {'fixture-req' not in pkgs}")
        if not refused:
            failures.append("MISSING-MANIFEST-DIR: a manifest-less module dir was silently skipped (a required module could hide there, #759)")

    # ---- MISATTRIBUTION-GUARD: a PRE-EXISTING unrelated hard finding must NOT be blamed on a clean install
    with tempfile.TemporaryDirectory() as d:
        live = engine_fixture.clone_engine(real_root, os.path.join(d, "live"))
        release = engine_fixture.clone_engine(real_root, os.path.join(d, "release"))
        _put_module(release, "fixture-clean-req", "required")     # a clean required module with no wires
        orphan = [{"severity": "hard", "message": "a pre-existing unrelated orphan (not this module's fault)"}]
        saved_cc = mm.module_coherence.check_coherence
        # A pre-existing tree-wide finding present throughout: the clean install adds NO NEW finding, so the
        # per-module delta must attribute nothing to it — it installs, and the orphan surfaces at the gate instead
        # of the install being rolled back and the release falsely blamed.
        mm.module_coherence.check_coherence = lambda tier="hard": list(orphan)
        try:
            with mm._redirect_root(live):
                result = mm.upgrade(ref="v-demo", release_tree=release, opener=lambda **kw: {"number": 1})
        finally:
            mm.module_coherence.check_coherence = saved_cc
        installed_ids = {m["id"] for m in (result.get("modules_installed") or [])}
        misblamed = "REQUIRES" in (result.get("reason") or "")   # the required-completeness (false) refusal
        print("\n[MISATTRIBUTION-GUARD — a pre-existing unrelated finding must not roll back a clean install]")
        print(f"  the clean required module WAS installed:      {'fixture-clean-req' in installed_ids}")
        print(f"  NOT falsely blamed as a required-install fail: {not misblamed}")
        if "fixture-clean-req" not in installed_ids:
            failures.append("MISATTRIBUTION-GUARD: a clean install was rolled back because of a pre-existing unrelated finding (the security regression)")
        if misblamed:
            failures.append("MISATTRIBUTION-GUARD: a pre-existing finding was falsely reported as a required-install failure (misdirects the operator)")

    print("\n" + "=" * 78)
    if failures:
        print("DEMO #759 FAILED:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("DEMO #759 PASSED: an engine update installs the net-new modules a release adds that the deployment "
          "needs — a required capability mandatorily (refusing cleanly if it can't), a net-new default-on add-on "
          "opt-out — offers the optional ones, and never resurrects a previously-declined module. The classifier, "
          "the install leg, and the required-completeness refusal are load-bearing.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
