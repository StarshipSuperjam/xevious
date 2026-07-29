#!/usr/bin/env python3
"""Self-tests for the module manager — `remove` + the group-scoped uv-sync derivation, `add`
(fetch/overlay), and the engine `upgrade`/updater + the migrations machinery
(select/run, the no-backup guard, the version-stamp check, and the frozen-check-name invariant).

Run: uv run --directory .engine --frozen -- python tools/selftest.py

Pure policy (refusals, the derivation, migration select/order, version-stamp detection, the tightened
migrations schema) is tested directly on fixture data — no disk mutation; the live mutation glue (`remove`,
`add`, `upgrade`) is exercised end-to-end by the shipped fail-then-pass demos against throwaway fixture
engines, with ONLY the side-effect boundaries faked (release fetch, git/PR open, data backup) and the real
overlay / migration / coherence logic run. The deliverable-gate cold review attests each test's assertion
matches its name.
"""
from __future__ import annotations
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import module_manager  # noqa: E402
import module_coherence  # noqa: E402
import validate  # noqa: E402
import wiring  # noqa: E402


def _m(mid, status="optional", depends=None, version="0.0.0"):
    return (f".engine/modules/{mid}/manifest.json",
            {"id": mid, "version": version, "status": status, "provides": {},
             "depends": depends or {}})


def _cand(mid, version="0.1.0", status="optional", depends=None, provides=None, wires=None):
    """A fetched-module candidate manifest (the shape plan_add receives from the release tree)."""
    return {"id": mid, "version": version, "status": status,
            "provides": provides or {}, "depends": depends or {}, "wires": wires or []}


class TestRemoveRefusals(unittest.TestCase):
    """Pure refusal policy over an injected manifest list — no disk."""

    def test_absent_module_is_refused_plainly(self):
        plan = module_manager.plan_remove("nope", [_m("a")])
        self.assertTrue(plan["refused"])
        self.assertIn("no module named 'nope'", plan["reason"])

    def test_depended_on_module_is_refused_naming_the_dependent(self):
        manifests = [_m("a"), _m("b", depends={"a": ""})]
        plan = module_manager.plan_remove("a", manifests)
        self.assertTrue(plan["refused"])
        self.assertIn("'b'", plan["reason"])          # names the dependent
        self.assertIn("needs it", plan["reason"])     # singular verb agreement

    def test_two_dependents_are_both_named_with_plural_grammar(self):
        manifests = [_m("a"), _m("b", depends={"a": ""}), _m("c", depends={"a": ""})]
        plan = module_manager.plan_remove("a", manifests)
        self.assertTrue(plan["refused"])
        self.assertIn("'b'", plan["reason"])
        self.assertIn("'c'", plan["reason"])
        self.assertIn("need it", plan["reason"])      # plural verb

    def test_required_module_is_refused(self):
        plan = module_manager.plan_remove("base", [_m("base", status="required")])
        self.assertTrue(plan["refused"])
        self.assertIn("required", plan["reason"])

    def test_dependency_refusal_takes_precedence_over_required(self):
        # A module both required AND depended-on surfaces the dependency reason (the actionable one).
        manifests = [_m("base", status="required"), _m("ext", depends={"base": ""})]
        plan = module_manager.plan_remove("base", manifests)
        self.assertTrue(plan["refused"])
        self.assertIn("'ext'", plan["reason"])
        self.assertNotIn("required part", plan["reason"])

    def test_optional_with_no_dependents_is_allowed(self):
        plan = module_manager.plan_remove("a", [_m("a"), _m("b")])
        self.assertFalse(plan["refused"])
        self.assertIsNone(plan["reason"])


class TestRealRepoRefusals(unittest.TestCase):
    """The real required modules — read-only, no mutation."""

    def test_remove_core_is_refused_naming_validators_core(self):
        plan = module_manager.plan_remove("core")
        self.assertTrue(plan["refused"])
        self.assertIn("validators-core", plan["reason"])   # reverse-dependency refusal

    def test_remove_validators_core_is_refused_naming_audit_library(self):
        # audit-library is the first module to depend on validators-core, so its removal is now a
        # reverse-dependency refusal (audit-library needs it), ahead of its required-foundation status.
        plan = module_manager.plan_remove("validators-core")
        self.assertTrue(plan["refused"])
        self.assertIn("audit-library", plan["reason"])     # reverse-dependency refusal

    def test_remove_a_required_leaf_is_refused_as_required(self):
        # a required module that nothing depends on falls through to the required-foundation refusal.
        plan = module_manager.plan_remove("routine-mode")
        self.assertTrue(plan["refused"])
        self.assertIn("required", plan["reason"])          # required-foundation refusal


class TestUvGroupDerivation(unittest.TestCase):

    def test_pep735_normalization(self):
        self.assertEqual(module_manager.normalize_pep735("a_b.c"), "a-b-c")
        self.assertEqual(module_manager.normalize_pep735("Core"), "core")
        self.assertEqual(module_manager.normalize_pep735("validators-core"), "validators-core")
        self.assertEqual(module_manager.normalize_pep735("my__group..x"), "my-group-x")

    def test_derive_matches_committed_default_groups_on_the_real_repo(self):
        # The drift gate: the committed [tool.uv] default-groups equals what the present set derives. This
        # leg holds in any deployment — a declined module drops out of both sides together.
        self.assertEqual(module_manager.derive_uv_groups(), module_manager.committed_default_groups())
        # Core always carries dependencies; the semantic-recall module (numpy) carries a group only when it is
        # installed, so a deployment that DECLINED it legitimately has just ["core"] here (#646).
        import module_coherence
        installed = {m.get("id") for _p, m in module_coherence.discover_manifests()}
        groups = module_manager.committed_default_groups()
        self.assertIn("core", groups)
        if "memory-semantic-recall" in installed:
            self.assertIn("memory-semantic-recall", groups)
        else:
            self.assertNotIn("memory-semantic-recall", groups)

    def test_a_module_with_no_dependency_group_is_excluded(self):
        with tempfile.TemporaryDirectory() as d:
            py = os.path.join(d, "pyproject.toml")
            with open(py, "w") as fh:
                fh.write('[dependency-groups]\na = ["x"]\n[tool.uv]\ndefault-groups = ["a"]\n')
            # present ids {a, b, c}; only `a` declares a group -> b and c contribute nothing
            manifests = [_m("a"), _m("b"), _m("c")]
            self.assertEqual(module_manager.derive_uv_groups(manifests=manifests, pyproject_path=py), ["a"])

    def test_rewrite_default_groups_is_minimal_and_validates_shape(self):
        text = ('# a comment\n[project]\nname = "x"\n\n[dependency-groups]\n'
                'base = ["p"]\noptx = ["q"]\n\n[tool.uv]\ndefault-groups = ["base", "optx"]\n')
        new, changed = module_manager.rewrite_default_groups_text(text, ["base"])
        self.assertTrue(changed)
        self.assertIn('default-groups = ["base"]', new)
        self.assertIn("[dependency-groups]", new)            # the rest is preserved byte-for-byte
        self.assertIn('optx = ["q"]', new)
        # idempotent: rewriting to the same value reports no change
        _, changed2 = module_manager.rewrite_default_groups_text(new, ["base"])
        self.assertFalse(changed2)
        # a missing, duplicated, or MULTI-LINE array fails loud (the caller fails open, never blind-writes
        # nor silently reformats the operator's pyproject)
        with self.assertRaises(ValueError):
            module_manager.rewrite_default_groups_text("[tool.uv]\n", ["x"])
        with self.assertRaises(ValueError):
            module_manager.rewrite_default_groups_text(text + 'default-groups = ["base"]\n', ["base"])
        with self.assertRaises(ValueError):
            module_manager.rewrite_default_groups_text(
                '[tool.uv]\ndefault-groups = [\n  "base",\n]\n', ["base"])


class TestRemoveEndToEnd(unittest.TestCase):
    """The live `remove` mutation glue — reversal, deletion, engine.json update, group re-derivation,
    idempotence, and post-removal coherence — exercised by the shipped fail-then-pass demo against a
    throwaway fixture engine (real logic, fixture boundary)."""

    def test_run_demo_passes(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(module_manager.run_demo())


class TestRemoveDeletesProvidesAtAnyPath(unittest.TestCase):
    """#409: per-module remove() deletes a module's sole-owned provides regardless of path — so a removed
    module's .claude/ personas + skills do not orphan on disk (before this fix the deletion was gated to .engine/,
    leaving e.g. a removed /engine-design skill live and erroring). The manifest-derived reversal law
    deletes the engine-identified files a module provides, wherever
    they live — matching what whole-engine remove_engine already does."""

    def test_remove_deletes_a_claude_skill_provide_not_only_engine_files(self):
        with tempfile.TemporaryDirectory() as d:
            with module_manager._redirect_root(d):
                module_manager._build_fixture(d)
                # give optx a .claude/ skill provide (sole-owned) alongside its .engine/ tool
                mpath = os.path.join(d, ".engine", "modules", "optx", "manifest.json")
                module_manager._write_json(mpath, {
                    "id": "optx", "version": "0.0.0", "status": "optional",
                    "provides": {"tool": [".engine/tools/optx_tool.py"],
                                 "skill": [".claude/skills/optx/SKILL.md"]},
                    "wires": [{"type": "gitignore", "key": "optx-cache", "lines": [".engine/optx/.cache/"]},
                              {"type": "permission", "value": "Bash(optx-tool:*)"}],
                    "depends": {}})
                skill_abs = os.path.join(d, ".claude", "skills", "optx", "SKILL.md")
                os.makedirs(os.path.dirname(skill_abs), exist_ok=True)
                with open(skill_abs, "w", encoding="utf-8") as fh:
                    fh.write("# optx skill\n")
                res = module_manager.remove("optx")
                skill_gone = not os.path.exists(skill_abs)
                tool_gone = not os.path.exists(os.path.join(d, ".engine", "tools", "optx_tool.py"))
        self.assertFalse(res["refused"])
        self.assertTrue(skill_gone, "the removed module's .claude/ skill must be deleted, not orphaned")
        self.assertTrue(tool_gone, "the module's .engine/ tool is still deleted (the path-agnostic sweep)")
        self.assertIn(".claude/skills/optx/SKILL.md", res["deleted"])


class TestAddRefusals(unittest.TestCase):
    """Pure plan_add refusal policy over an injected present set + a fetched candidate — no disk, no fetch."""

    def test_already_installed_is_refused(self):
        plan = module_manager.plan_add("a", _cand("a"), [_m("a")])
        self.assertTrue(plan["refused"])
        self.assertIn("already installed", plan["reason"])

    def test_manifest_id_mismatch_is_refused(self):
        # the fetched files carry a different id than requested (a wrong/corrupt fetch)
        plan = module_manager.plan_add("a", _cand("b"), [_m("base", "required")])
        self.assertTrue(plan["refused"])
        self.assertIn("'a'", plan["reason"])
        self.assertIn("'b'", plan["reason"])          # names what was actually found

    def test_missing_dependency_is_refused_naming_it(self):
        plan = module_manager.plan_add("a", _cand("a", depends={"ghost": ""}), [_m("base", "required")])
        self.assertTrue(plan["refused"])
        self.assertIn("ghost", plan["reason"])         # the absent dependency is named

    def test_out_of_range_dependency_is_refused(self):
        present = [_m("base", "required", version="1.0.0")]
        plan = module_manager.plan_add("a", _cand("a", depends={"base": ">=2.0.0"}), present)
        self.assertTrue(plan["refused"])
        self.assertIn("base", plan["reason"])          # the range rule (reused from coherence) fires

    def test_satisfiable_add_is_allowed(self):
        present = [_m("base", "required", version="1.0.0")]
        plan = module_manager.plan_add("a", _cand("a", version="0.1.0", depends={"base": ">=1.0.0"}), present)
        self.assertFalse(plan["refused"])
        self.assertIsNone(plan["reason"])
        self.assertEqual(plan["version"], "0.1.0")


class TestAddEndToEnd(unittest.TestCase):
    """The live `add` overlay glue — fetch (faked, injected release tree), copy, wire, engine.json record,
    group re-derivation, coherence, and the missing-dependency / already-installed refusals — exercised by
    the shipped add demo against a throwaway fixture (real logic, faked fetch boundary)."""

    def test_add_demo_passes(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(module_manager.add_demo())


class TestAddSafety(unittest.TestCase):
    """The add path's defense-in-depth: a malformed id, a release that places files outside the engine
    (the topology-wall containment guard), and an unreachable release all refuse cleanly, changing nothing."""

    def test_invalid_module_id_is_refused_without_touching_disk(self):
        # refused at the id check, before any discover/fetch — safe on the real repo, no network
        res = module_manager.add("../evil")
        self.assertTrue(res["refused"])
        self.assertIn("not a valid module id", res["reason"])

    def test_escaping_provides_pattern_is_refused_before_any_copy(self):
        with tempfile.TemporaryDirectory() as d:
            live = os.path.join(d, "live")
            os.makedirs(live)
            release = os.path.join(d, "release")
            os.makedirs(os.path.join(release, ".engine", "modules", "evil"))
            module_manager._write_json(
                os.path.join(release, ".engine", "modules", "evil", "manifest.json"),
                {"id": "evil", "version": "0.1.0", "status": "optional",
                 "provides": {"tool": ["../sneak.py"]}, "depends": {}})   # climbs out of the engine tree
            with open(os.path.join(d, "sneak.py"), "w", encoding="utf-8") as fh:
                fh.write("# a file the escaping glob would match, OUTSIDE ROOT\n")
            with module_manager._redirect_root(live):
                module_manager._build_add_fixture(live)
                res = module_manager.add("evil", release_tree=release)
            self.assertTrue(res["refused"])
            self.assertIn("outside the engine", res["reason"])
            # nothing was written: the module folder was never created in the live tree
            self.assertFalse(os.path.isdir(os.path.join(live, ".engine", "modules", "evil")))

    def test_unreachable_release_is_a_plain_refusal_changing_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            live = os.path.join(d, "live")
            os.makedirs(live)
            with module_manager._redirect_root(live):
                module_manager._build_add_fixture(live)                  # engine_release "0.0.0"
                saved = module_manager._fetch_release_tree
                module_manager._fetch_release_tree = lambda *a, **k: (_ for _ in ()).throw(
                    RuntimeError("HTTP Error 404: Not Found"))
                try:
                    res = module_manager.add("feat")                    # real-fetch path -> raises
                finally:
                    module_manager._fetch_release_tree = saved
                engine = module_manager.module_coherence.load_engine_manifest()
            self.assertTrue(res["refused"])
            self.assertIn("Couldn't reach", res["reason"])              # plain, not a raw urllib error
            self.assertIn("Nothing was changed", res["reason"])
            self.assertNotIn("feat", (engine or {}).get("packages", {}))

    def test_add_fetches_from_the_recorded_home_never_origin(self):
        # A module's files come from the engine's recorded HOME, never this repo's own origin (#367).
        seen = {}
        with tempfile.TemporaryDirectory() as d:
            live = os.path.join(d, "live")
            os.makedirs(live)
            with module_manager._redirect_root(live):
                module_manager._build_add_fixture(live)                 # records home "acme/engine-home"
                saved = module_manager._fetch_release_tree

                def _spy(ref, dest, repo=None, token=None):
                    seen["repo"] = repo
                    raise RuntimeError("stop after capturing the source")
                module_manager._fetch_release_tree = _spy
                try:
                    module_manager.add("feat")
                finally:
                    module_manager._fetch_release_tree = saved
        self.assertEqual(seen.get("repo"), "acme/engine-home")          # the HOME, not boot.repo_slug()/origin

    def test_add_with_no_recorded_home_refuses_with_a_remedy_never_origin(self):
        called = {"n": 0}
        with tempfile.TemporaryDirectory() as d:
            live = os.path.join(d, "live")
            os.makedirs(live)
            with module_manager._redirect_root(live):
                module_manager._build_add_fixture(live)
                p = os.path.join(live, ".engine", "engine.json")        # strip the home -> a pre-field engine
                m = module_manager.validate.load_json(p)
                m.pop("home_repository", None)
                module_manager._write_json(p, m)
                saved = module_manager._fetch_release_tree
                module_manager._fetch_release_tree = lambda *a, **k: called.__setitem__("n", called["n"] + 1)
                try:
                    res = module_manager.add("feat")
                finally:
                    module_manager._fetch_release_tree = saved
        self.assertTrue(res["refused"])
        self.assertIn("no update home recorded", res["reason"])
        self.assertEqual(called["n"], 0)                                # never reached a fetch -> never origin

    def test_add_release_missing_at_the_home_is_refused_naming_the_home(self):
        import urllib.error
        with tempfile.TemporaryDirectory() as d:
            live = os.path.join(d, "live")
            os.makedirs(live)
            with module_manager._redirect_root(live):
                module_manager._build_add_fixture(live)
                saved = module_manager._fetch_release_tree
                module_manager._fetch_release_tree = lambda *a, **k: (_ for _ in ()).throw(
                    urllib.error.HTTPError("u", 404, "Not Found", {}, None))   # a REAL 404 at the home
                try:
                    res = module_manager.add("feat")
                finally:
                    module_manager._fetch_release_tree = saved
        self.assertTrue(res["refused"])
        self.assertIn("acme/engine-home", res["reason"])               # NAMES the home so the operator can check
        self.assertIn("Nothing was changed", res["reason"])


class TestCli(unittest.TestCase):

    def test_status_exits_zero(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(module_manager.main(["status"]), 0)

    def test_plan_remove_required_exits_one(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(module_manager.main(["plan-remove", "validators-core"]), 1)

    def test_demo_exits_zero(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(module_manager.main(["demo"]), 0)

    def test_sync_groups_exits_zero(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(module_manager.main(["sync-groups"]), 0)

    def test_add_already_installed_exits_one_without_fetching(self):
        # `core` is already installed, so add refuses BEFORE any release fetch (no network in this test).
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(module_manager.main(["add", "core"]), 1)


def _man(mid, version="0.0.0", migrations=None, depends=None, retired_capabilities=None):
    """A manifest DICT (the shape select_migrations / select_retired_capabilities / topological_order consume)."""
    return {"id": mid, "version": version, "status": "required",
            "provides": {}, "depends": depends or {}, "migrations": migrations or {},
            "retired_capabilities": retired_capabilities or {}}


class TestSelectMigrations(unittest.TestCase):
    """PURE migration selection + ordering — no disk, no network."""

    def test_only_in_range_migrations_are_selected(self):
        m = _man("a", migrations={
            "0.1.0": {"description": "x", "run": "migrations/a1.py", "kind": "config"},
            "0.2.0": {"description": "y", "run": "migrations/a2.py", "kind": "data"},
            "0.3.0": {"description": "z", "run": "migrations/a3.py", "kind": "config"}})
        sel = module_manager.select_migrations({"a": "0.1.0"}, {"a": "0.2.0"}, [m])
        self.assertEqual([s["version"] for s in sel], ["0.2.0"])   # > from(0.1.0), <= target(0.2.0)

    def test_within_a_module_versions_order_numerically_not_lexically(self):
        # the "0.10.0" < "0.9.0" string-sort bug guard: 0.9.0 must precede 0.10.0
        m = _man("a", migrations={
            "0.10.0": {"description": "ten", "run": "migrations/x.py", "kind": "config"},
            "0.9.0": {"description": "nine", "run": "migrations/y.py", "kind": "config"}})
        sel = module_manager.select_migrations({"a": "0.0.0"}, {"a": "0.10.0"}, [m])
        self.assertEqual([s["version"] for s in sel], ["0.9.0", "0.10.0"])

    def test_modules_run_in_dependency_order(self):
        base = _man("base", migrations={"1.0.0": {"description": "b", "run": "migrations/b.py",
                                                  "kind": "config"}})
        ext = _man("ext", depends={"base": ""},
                   migrations={"1.0.0": {"description": "e", "run": "migrations/e.py", "kind": "config"}})
        # input order puts ext BEFORE base; topological order must still emit base first (ext needs base)
        sel = module_manager.select_migrations({"base": "0.0.0", "ext": "0.0.0"},
                                               {"base": "1.0.0", "ext": "1.0.0"}, [ext, base])
        self.assertEqual([s["module_id"] for s in sel], ["base", "ext"])

    def test_empty_migrations_select_nothing(self):
        self.assertEqual(
            module_manager.select_migrations({"a": "0.0.0"}, {"a": "1.0.0"}, [_man("a")]), [])

    def test_a_two_part_boundary_key_is_selected_not_skipped(self):
        # #689: a two-part target ('0.4') must include a three-part boundary key ('0.4.0') — they are the SAME
        # version. Un-normalized, (0,4,0) <= (0,4) is False (a shorter tuple sorts BELOW), so the boundary step
        # was wrongly skipped; the length-normalized comparison includes it. Fails on main, passes after the fix.
        m = _man("a", migrations={"0.4.0": {"description": "x", "run": "migrations/a.py", "kind": "config"}})
        sel = module_manager.select_migrations({"a": "0.3.0"}, {"a": "0.4"}, [m])
        self.assertEqual([s["version"] for s in sel], ["0.4.0"])


class TestRunMigrations(unittest.TestCase):
    """The runner's execution + the no-backup guard. A migration .py is loaded by path and run; only the
    backup seam (a side-effect boundary) is faked."""

    def setUp(self):
        # A data migration now raises an in-flight marker under memory's dir (#396); isolate ENGINE_MEMORY_DIR
        # to a throwaway so the window never touches the real store (nor flakes on a concurrent session's lock).
        self._memtmp = tempfile.TemporaryDirectory()
        self._prev_mem = os.environ.get("ENGINE_MEMORY_DIR")
        os.environ["ENGINE_MEMORY_DIR"] = self._memtmp.name

    def tearDown(self):
        if self._prev_mem is None:
            os.environ.pop("ENGINE_MEMORY_DIR", None)
        else:
            os.environ["ENGINE_MEMORY_DIR"] = self._prev_mem
        self._memtmp.cleanup()

    def _module_dir(self, d, fname, body):
        md = os.path.join(d, ".engine", "modules", "m")
        os.makedirs(os.path.join(md, "migrations"))
        with open(os.path.join(md, "migrations", fname), "w", encoding="utf-8") as fh:
            fh.write(body)
        return lambda mid: os.path.join(d, ".engine", "modules", mid)

    def test_config_migration_runs_directly(self):
        with tempfile.TemporaryDirectory() as d:
            marker = os.path.join(d, "out.txt")
            mdir = self._module_dir(d, "c.py",
                                    "def migrate(context):\n"
                                    f"    with open({marker!r}, 'w') as fh:\n"
                                    "        fh.write(context['kind'])\n")
            sel = [{"module_id": "m", "version": "0.1.0", "run": "migrations/c.py", "kind": "config"}]
            res = module_manager.run_migrations(sel, {"m": "0.0.0"}, "v1", module_dir=mdir)
            self.assertEqual(res["ran"], ["m -> 0.1.0 (config)"])
            with open(marker) as fh:
                self.assertEqual(fh.read(), "config")

    def test_data_migration_refused_without_a_backup_seam_and_never_runs(self):
        # Force the no-backup-available condition so the test is deterministic regardless of the developer's
        # ambient state: with backup=None, _resolve_backup_seam falls back to the live memory seam, which is
        # available iff memory.migration_backup_available() (a configured vault pointer). On a dev machine with a
        # real vault configured that is True, the seam resolves, and the data migration would RUN — so pin it
        # False here (the same isolation TestBackupSeamResolution uses) to exercise the refusal path this asserts.
        import memory
        orig = memory.migration_backup_available
        memory.migration_backup_available = lambda: False
        try:
            with tempfile.TemporaryDirectory() as d:
                marker = os.path.join(d, "out.txt")
                mdir = self._module_dir(d, "dd.py",
                                        "def migrate(context):\n"
                                        f"    with open({marker!r}, 'w') as fh:\n"
                                        "        fh.write('RAN')\n")
                sel = [{"module_id": "m", "version": "0.2.0", "run": "migrations/dd.py", "kind": "data"}]
                res = module_manager.run_migrations(sel, {"m": "0.0.0"}, "v1", module_dir=mdir, backup=None)
                self.assertEqual(res["ran"], [])
                self.assertEqual(len(res["refused"]), 1)
                self.assertIn("no data backup", res["refused"][0])
                self.assertFalse(os.path.exists(marker))          # the migration body never ran
        finally:
            memory.migration_backup_available = orig

    def test_data_migration_runs_after_backup_and_is_stamped(self):
        with tempfile.TemporaryDirectory() as d:
            marker = os.path.join(d, "out.txt")
            mdir = self._module_dir(d, "dd.py",
                                    "def migrate(context):\n"
                                    "    handle = context['backup']('store', context['engine_version'])\n"
                                    "    assert handle\n"
                                    f"    with open({marker!r}, 'w') as fh:\n"
                                    "        fh.write(context['engine_version'])\n")
            calls = []
            seam = lambda store, ver, migration_id=None, **kw: (
                calls.append((store, ver, migration_id, kw.get("reversibility_floor"))) or {"ok": True})
            sel = [{"module_id": "m", "version": "0.2.0", "run": "migrations/dd.py", "kind": "data"}]
            res = module_manager.run_migrations(sel, {"m": "0.0.0"}, "v2", module_dir=mdir, backup=seam)
            self.assertEqual(res["ran"], ["m -> 0.2.0 (data)"])
            # the backup was taken (before the body ran), with the migration id bound in for collision-free naming, and
            # the lone data migration is the upgrade's reversibility floor (#303)
            self.assertEqual(calls, [("store", "v2", "m@0.2.0", True)])
            with open(marker) as fh:
                self.assertEqual(fh.read(), "v2")              # the migration stamped the engine version

    def test_run_migrations_flags_an_unlockable_snapshot_for_the_operator(self):
        # The snapshot handle carries whether the retained pre-update copy could be locked; run_migrations reduces
        # it to a single flag so the upgrade can tell the operator to keep an unlockable copy. Only a handle that
        # plainly reports it could NOT be locked (hardened is False) sets the flag.
        with tempfile.TemporaryDirectory() as d:
            mdir = self._module_dir(d, "dd.py",
                                    "def migrate(context):\n"
                                    "    assert context['backup']('store', context['engine_version'])\n")
            sel = [{"module_id": "m", "version": "0.2.0", "run": "migrations/dd.py", "kind": "data"}]
            unprot = module_manager.run_migrations(sel, {"m": "0.0.0"}, "v2", module_dir=mdir,
                                                   backup=lambda *a, **k: {"backed-up": True, "hardened": False})
            self.assertTrue(unprot["backup_unprotected"])
            prot = module_manager.run_migrations(sel, {"m": "0.0.0"}, "v2", module_dir=mdir,
                                                 backup=lambda *a, **k: {"backed-up": True, "hardened": True})
            self.assertFalse(prot["backup_unprotected"])
            empty = module_manager.run_migrations([], {}, "v2", module_dir=mdir)
            self.assertFalse(empty["backup_unprotected"])       # no data migration -> never flagged

    def test_only_the_first_data_migration_of_the_upgrade_is_the_reversibility_floor(self):
        # #303: one run_migrations call == one upgrade. reversibility_floor is True for the FIRST data migration only;
        # config migrations take no backup, and later data migrations of the same upgrade are NOT the floor.
        with tempfile.TemporaryDirectory() as d:
            md = os.path.join(d, ".engine", "modules", "m", "migrations")
            os.makedirs(md)
            body = ("def migrate(context):\n"
                    "    if context['kind'] == 'data':\n"
                    "        assert context['backup']('store', context['engine_version'])\n")
            for fn in ("c.py", "d0.py", "d1.py"):
                with open(os.path.join(md, fn), "w", encoding="utf-8") as fh:
                    fh.write(body)
            mdir = lambda mid: os.path.join(d, ".engine", "modules", mid)
            calls = []
            seam = lambda store, ver, migration_id=None, **kw: (
                calls.append((migration_id, kw.get("reversibility_floor"))) or {"ok": True})
            sel = [{"module_id": "m", "version": "0.1.0", "run": "migrations/c.py", "kind": "config"},
                   {"module_id": "m", "version": "0.2.0", "run": "migrations/d0.py", "kind": "data"},
                   {"module_id": "m", "version": "0.3.0", "run": "migrations/d1.py", "kind": "data"}]
            res = module_manager.run_migrations(sel, {"m": "0.0.0"}, "v2", module_dir=mdir, backup=seam)
            self.assertEqual(len(res["ran"]), 3)
            self.assertEqual(calls, [("m@0.2.0", True), ("m@0.3.0", False)])   # floor = first data migration only

    def test_a_data_migration_runs_inside_an_in_flight_window_that_clears_after(self):
        # (#396): the marker is present DURING the migration (the body reads it) and lowered AFTER.
        from memory import capture, ledger
        with tempfile.TemporaryDirectory() as d:
            seen = os.path.join(d, "seen.txt")
            mdir = self._module_dir(d, "dd.py",
                                    "def migrate(context):\n"
                                    "    context['backup']('store', context['engine_version'])\n"
                                    "    from memory import capture, ledger\n"
                                    "    inflight = capture.migration_in_flight(ledger.ledger_dir())\n"
                                    f"    open({seen!r}, 'w').write('1' if inflight else '0')\n")
            seam = lambda store, ver, **kw: {"ok": True}
            sel = [{"module_id": "m", "version": "0.2.0", "run": "migrations/dd.py", "kind": "data"}]
            res = module_manager.run_migrations(sel, {"m": "0.0.0"}, "v2", module_dir=mdir, backup=seam)
            self.assertEqual(res["ran"], ["m -> 0.2.0 (data)"])
            with open(seen) as fh:
                self.assertEqual(fh.read(), "1")               # the window was open while the body ran
            self.assertFalse(capture.migration_in_flight(ledger.ledger_dir()))   # lowered after

    def test_a_data_migration_is_refused_when_the_window_cannot_open(self):
        # Fail CLOSED: a held single-writer lock => the marker can't be raised => the migration is REFUSED, never
        # run marker-less (the exact interleave this prevents). The body must not run.
        from memory import capture, ledger
        with tempfile.TemporaryDirectory() as d:
            ran = os.path.join(d, "ran.txt")
            mdir = self._module_dir(d, "dd.py",
                                    "def migrate(context):\n"
                                    "    context['backup']('store', context['engine_version'])\n"
                                    f"    open({ran!r}, 'w').write('RAN')\n")
            data_dir = ledger.ledger_dir()
            os.makedirs(data_dir, exist_ok=True)
            lock_fd = capture._acquire_lock(os.path.join(data_dir, capture.LOCK_FILENAME))
            self.addCleanup(capture._release_lock, lock_fd)
            seam = lambda store, ver, **kw: {"ok": True}
            sel = [{"module_id": "m", "version": "0.2.0", "run": "migrations/dd.py", "kind": "data"}]
            res = module_manager.run_migrations(sel, {"m": "0.0.0"}, "v2", module_dir=mdir, backup=seam)
            self.assertEqual(res["ran"], [])
            self.assertEqual(len(res["refused"]), 1)
            self.assertIn("couldn't start safely", res["refused"][0])
            self.assertFalse(os.path.exists(ran))              # the body never ran

    def test_data_migration_whose_backup_fails_at_runtime_refuses_cleanly_without_mutating(self):
        # A seam resolved LIVE but that returns a falsy handle at call time (a vault reachable at pre-flight,
        # gone at the snapshot): the migration's backup-first assert fires; run_migrations must DEGRADE LOUD
        # (a refusal, not a raw traceback) and the body must not have mutated.
        with tempfile.TemporaryDirectory() as d:
            marker = os.path.join(d, "out.txt")
            mdir = self._module_dir(d, "dd.py",
                                    "def migrate(context):\n"
                                    "    handle = context['backup']('store', context['engine_version'])\n"
                                    "    assert handle, 'backup-first: a data migration must snapshot before mutating'\n"
                                    f"    open({marker!r}, 'w').write('RAN')\n")
            sel = [{"module_id": "m", "version": "0.2.0", "run": "migrations/dd.py", "kind": "data"}]
            failing = lambda store, ver, **kw: None            # the backup fails at the moment of the snapshot
            res = module_manager.run_migrations(sel, {"m": "0.0.0"}, "v2", module_dir=mdir, backup=failing)
            self.assertEqual(res["ran"], [])                   # not counted as run
            self.assertEqual(len(res["refused"]), 1)
            self.assertIn("backup could not be completed", res["refused"][0])
            self.assertIn("Ask me to", res["refused"][0])      # carries a recovery action
            self.assertFalse(os.path.exists(marker))           # the body bailed before mutating


class TestVersionStamp(unittest.TestCase):
    """The post-revert version-stamp check: pure detection + the read-only promote_finding surfacing."""

    def test_mismatch_detected_when_running_code_is_older_than_the_data(self):
        f = module_manager.stamp_mismatch_finding("ledger", "0.2.0", "0.1.0", "engine restore ledger")
        self.assertIsNotNone(f)
        self.assertEqual(f["severity"], "hard")
        self.assertIn("restore", f["message"].lower())

    def test_no_mismatch_when_code_is_at_or_ahead_of_the_data(self):
        self.assertIsNone(module_manager.stamp_mismatch_finding("ledger", "0.2.0", "0.2.0", "cmd"))
        self.assertIsNone(module_manager.stamp_mismatch_finding("ledger", "0.2.0", "0.3.0", "cmd"))

    def test_the_finding_message_is_plain_peer_voice_and_carries_no_raw_ref(self):
        # the promoted Issue body is operator-facing (boot.open_findings renders it): plain peer voice, never a
        # tag/ref/version-machinery (the operator meets a plain handle, not the mechanism).
        f = module_manager.stamp_mismatch_finding(
            "recall-ledger", "2.0.0", "1.0.0", "ask me to restore the copy saved before the last update")
        msg = f["message"]
        for banned in ("refs/", "engine-snapshot/", "@", "stored data"):
            self.assertNotIn(banned, msg)
        self.assertIn("saved memory", msg.lower())             # peer voice

    def test_surface_promotes_exactly_one_finding_via_a_faked_github(self):
        import telemetry
        gh = telemetry.GitHubIssues("you/proj", "tok", transport=telemetry._FakeGitHub().transport)
        num = module_manager.surface_stamp_mismatch(
            "ledger", "0.2.0", "0.1.0", "engine restore ledger",
            now="2026-01-01T00:00:00Z", github=gh)
        self.assertTrue(num)                                   # an Issue number was opened
        # no mismatch -> nothing surfaced
        self.assertIsNone(module_manager.surface_stamp_mismatch(
            "ledger", "0.2.0", "0.2.0", "cmd", now="2026-01-01T00:00:00Z", github=gh))


class TestEngineReleaseNormalization(unittest.TestCase):
    """The stored engine_release stays BARE even when the resolved release ref is v-prefixed, so the manifest
    never carries the engine as `v0.1.0` while the packages read `0.1.0` (the tag-grammar round-trip fix)."""

    def test_a_v_prefixed_tag_is_stored_bare_and_consistent_with_the_packages(self):
        with tempfile.TemporaryDirectory() as d:
            live = os.path.join(d, "live")
            os.makedirs(live)
            with module_manager._redirect_root(live):
                module_manager._build_upgrade_fixture(live)
                module_manager._bump_engine_manifest({"base": "0.2.0"}, "v0.2.0")   # v-prefixed ref in
                engine = module_manager.module_coherence.load_engine_manifest()
        self.assertEqual((engine or {}).get("engine_release"), "0.2.0")             # stored bare, not "v0.2.0"
        self.assertEqual((engine or {}).get("packages", {}).get("base"), "0.2.0")   # matches the package form

    def test_a_bare_ref_is_stored_unchanged(self):
        with tempfile.TemporaryDirectory() as d:
            live = os.path.join(d, "live")
            os.makedirs(live)
            with module_manager._redirect_root(live):
                module_manager._build_upgrade_fixture(live)
                module_manager._bump_engine_manifest({"base": "0.2.0"}, "0.2.0")    # already bare -> no double-strip
                engine = module_manager.module_coherence.load_engine_manifest()
        self.assertEqual((engine or {}).get("engine_release"), "0.2.0")


class TestMigrationsSchema(unittest.TestCase):
    """The tightened module.v1.json `migrations` shape: a well-formed entry passes, a malformed one fails
    the same schema the hard/CI module-manifest check enforces."""

    def _validator(self):
        from jsonschema import Draft202012Validator
        schema = module_manager.validate.load_json(
            os.path.join(module_manager.validate.ROOT, ".engine", "schemas", "module.v1.json"))
        return Draft202012Validator(schema)

    def test_wellformed_migrations_entry_validates(self):
        man = {"id": "a", "version": "1.0.0", "status": "optional", "provides": {},
               "migrations": {"1.0.0": {"description": "reshape", "run": "migrations/x.py", "kind": "data"}}}
        self.assertEqual(list(self._validator().iter_errors(man)), [])

    def test_migrations_entry_missing_kind_is_rejected(self):
        man = {"id": "a", "version": "1.0.0", "status": "optional", "provides": {},
               "migrations": {"1.0.0": {"description": "reshape", "run": "migrations/x.py"}}}
        self.assertTrue(list(self._validator().iter_errors(man)))    # `kind` is required

    def test_migrations_entry_with_bad_kind_is_rejected(self):
        man = {"id": "a", "version": "1.0.0", "status": "optional", "provides": {},
               "migrations": {"1.0.0": {"description": "x", "run": "migrations/x.py", "kind": "sideways"}}}
        self.assertTrue(list(self._validator().iter_errors(man)))    # kind must be data|config

    def test_present_manifests_carry_no_migrations_field_so_stay_valid(self):
        # the tightening is zero-breakage today: neither shipped manifest declares migrations
        v = self._validator()
        for rel, m in module_manager.module_coherence.discover_manifests():
            self.assertEqual(list(v.iter_errors(m)), [], f"{rel} must validate against module.v1.json")


class TestSelectRetiredCapabilities(unittest.TestCase):
    """PURE retired-capability selection + ordering — announcement-only, no run/kind, nothing executes."""

    def test_only_in_range_are_selected(self):
        m = _man("a", retired_capabilities={
            "0.1.0": {"description": "gone at 0.1"},
            "0.2.0": {"description": "gone at 0.2"},
            "0.3.0": {"description": "gone at 0.3"}})
        sel = module_manager.select_retired_capabilities({"a": "0.1.0"}, {"a": "0.2.0"}, [m])
        self.assertEqual(sel, [{"module_id": "a", "version": "0.2.0", "description": "gone at 0.2"}])

    def test_fresh_adopter_at_target_sees_nothing(self):
        # from == target: a fresh adopter who never had the capability gets no announcement
        m = _man("a", retired_capabilities={"0.2.0": {"description": "x"}})
        self.assertEqual(module_manager.select_retired_capabilities({"a": "0.2.0"}, {"a": "0.2.0"}, [m]), [])

    def test_versions_order_numerically_not_lexically(self):
        # the "0.10.0" < "0.9.0" string-sort bug guard: 0.9.0 must precede 0.10.0
        m = _man("a", retired_capabilities={
            "0.10.0": {"description": "ten"}, "0.9.0": {"description": "nine"}})
        sel = module_manager.select_retired_capabilities({"a": "0.0.0"}, {"a": "0.10.0"}, [m])
        self.assertEqual([s["version"] for s in sel], ["0.9.0", "0.10.0"])

    def test_modules_emit_in_dependency_order(self):
        base = _man("base", retired_capabilities={"1.0.0": {"description": "b"}})
        ext = _man("ext", depends={"base": ""}, retired_capabilities={"1.0.0": {"description": "e"}})
        # input order puts ext BEFORE base; topological order must still emit base first
        sel = module_manager.select_retired_capabilities({"base": "0.0.0", "ext": "0.0.0"},
                                                         {"base": "1.0.0", "ext": "1.0.0"}, [ext, base])
        self.assertEqual([s["module_id"] for s in sel], ["base", "ext"])

    def test_a_long_jump_catches_up_every_notice_in_range(self):
        m = _man("a", retired_capabilities={
            "0.1.0": {"description": "one"}, "0.2.0": {"description": "two"}, "0.3.0": {"description": "three"}})
        sel = module_manager.select_retired_capabilities({"a": "0.0.0"}, {"a": "0.3.0"}, [m])
        self.assertEqual([s["version"] for s in sel], ["0.1.0", "0.2.0", "0.3.0"])

    def test_absent_block_selects_nothing(self):
        self.assertEqual(
            module_manager.select_retired_capabilities({"a": "0.0.0"}, {"a": "1.0.0"}, [_man("a")]), [])

    def test_a_two_part_boundary_key_is_shown_not_skipped(self):
        # #689: same boundary edge as the migration selector — a two-part target ('0.4') must still show a
        # three-part retirement notice keyed at the boundary ('0.4.0'); the notice exists to reach the very
        # lagging upgrader, so it must never vanish at the boundary. Fails on main, passes after the fix.
        m = _man("a", retired_capabilities={"0.4.0": {"description": "gone at 0.4"}})
        sel = module_manager.select_retired_capabilities({"a": "0.3.0"}, {"a": "0.4"}, [m])
        self.assertEqual(sel, [{"module_id": "a", "version": "0.4.0", "description": "gone at 0.4"}])


class TestRetiredCapabilitiesRender(unittest.TestCase):
    """The notice reaches BOTH operator surfaces (unlike a migration description, which reaches only the
    preview), and stray Markdown can't reshape the durable line."""

    def test_pr_body_shows_the_retirement_section(self):
        result = {"retired_capabilities": [
            {"module_id": "core", "version": "0.5.0",
             "description": "The engine no longer offers to bring a set-aside note back into search."}]}
        body = module_manager.render_upgrade_pr_body({"core": "0.4.0"}, {"core": "0.5.0"}, result)
        self.assertIn("Capabilities this update removed", body)
        self.assertIn("bring a set-aside note back into search", body)

    def test_pr_body_has_no_section_when_none_retired(self):
        body = module_manager.render_upgrade_pr_body({"core": "0.4.0"}, {"core": "0.5.0"},
                                                     {"retired_capabilities": []})
        self.assertNotIn("Capabilities this update removed", body)

    def test_preview_prints_the_retirement_line_and_is_not_a_bare_bump(self):
        p = {"status": "update-available", "current": "0.4.0", "target_ref": "0.5.0",
             "files": {}, "wires": {}, "migrations": [],
             "retired_capabilities": [{"module_id": "core", "version": "0.5.0",
                                       "description": "the one-shot cache reset is gone; use cleanup instead"}]}
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            module_manager._render_upgrade_preview(p)
        out = buf.getvalue()
        self.assertIn("Removes a capability: the one-shot cache reset is gone; use cleanup instead", out)
        self.assertNotIn("version bump only", out)   # a retirement-only preview is a real change, not "nothing"

    def test_terminal_text_preserves_content_and_only_collapses_whitespace(self):
        # the decisive fidelity fix: the terminal form must NEVER strip a leading marker — that silently
        # corrupted real notices ('>50% mode' -> '50% mode', '--force' -> 'force'). Only whitespace collapses.
        self.assertEqual(module_manager._retired_capability_text(">50% memory mode is gone"),
                         ">50% memory mode is gone")
        self.assertEqual(module_manager._retired_capability_text("--force is gone; use --yes instead"),
                         "--force is gone; use --yes instead")
        self.assertEqual(module_manager._retired_capability_text("a\n\n  b   c"), "a b c")
        self.assertEqual(module_manager._retired_capability_text("   "), "a capability was removed")

    def test_pr_body_line_escapes_markdown_but_deletes_no_character(self):
        line = module_manager._retired_capability_line("use [x](y), `z`, <img>, *b*, and >50%")
        # every alphanumeric token the author wrote survives — escaped, never deleted
        for token in ("x", "y", "z", "img", "b", "50"):
            self.assertIn(token, line)
        # and the reshaping constructs are neutralized, so none renders as a link / code / html / emphasis
        self.assertNotIn("[x](y)", line)          # the raw link syntax is broken by the escape
        self.assertIn(r"\[x\](y)", line)
        self.assertIn(r"\`z\`", line)
        self.assertIn(r"\<img\>", line)
        self.assertIn(r"\*b\*", line)
        self.assertTrue(line.startswith("- "))

    def test_pr_body_flags_the_capability_loss_in_risk_and_scope(self):
        result = {"retired_capabilities": [
            {"module_id": "core", "version": "0.5.0", "description": "the old thing is gone; do Y instead"}]}
        body = module_manager.render_upgrade_pr_body({"core": "0.4.0"}, {"core": "0.5.0"}, result)
        self.assertIn("A capability you could use before is gone", body)   # the Risk "what to weigh" bullet
        self.assertIn("capabilities it retires", body)                     # the Scope one-line summary
        self.assertIn("and no longer can (1):", body)                      # the header carries an item count

    def test_pr_body_omits_the_capability_risk_line_when_none_retired(self):
        body = module_manager.render_upgrade_pr_body({"core": "0.4.0"}, {"core": "0.5.0"},
                                                     {"retired_capabilities": []})
        self.assertNotIn("A capability you could use before is gone", body)


class TestRetiredCapabilitiesSchema(unittest.TestCase):
    """module.v1.json `retired_capabilities`: a well-formed entry passes, a malformed one fails the same schema
    the hard/CI module-manifest check enforces; and it is OPTIONAL, so present manifests stay valid."""

    def _validator(self):
        from jsonschema import Draft202012Validator
        schema = module_manager.validate.load_json(
            os.path.join(module_manager.validate.ROOT, ".engine", "schemas", "module.v1.json"))
        return Draft202012Validator(schema)

    def test_wellformed_entry_validates(self):
        man = {"id": "a", "version": "1.0.0", "status": "optional", "provides": {},
               "retired_capabilities": {"1.0.0": {"description": "the old thing is gone; do X instead"}}}
        self.assertEqual(list(self._validator().iter_errors(man)), [])

    def test_entry_missing_description_is_rejected(self):
        man = {"id": "a", "version": "1.0.0", "status": "optional", "provides": {},
               "retired_capabilities": {"1.0.0": {}}}
        self.assertTrue(list(self._validator().iter_errors(man)))          # `description` is required

    def test_announcement_only_extra_field_is_rejected(self):
        man = {"id": "a", "version": "1.0.0", "status": "optional", "provides": {},
               "retired_capabilities": {"1.0.0": {"description": "x", "run": "migrations/x.py"}}}
        self.assertTrue(list(self._validator().iter_errors(man)))          # no run/kind — announcement only

    def test_empty_description_is_rejected(self):
        man = {"id": "a", "version": "1.0.0", "status": "optional", "provides": {},
               "retired_capabilities": {"1.0.0": {"description": ""}}}
        self.assertTrue(list(self._validator().iter_errors(man)))          # minLength 1


class TestUpgradeEndToEnd(unittest.TestCase):
    """The live upgrade glue — faked fetch, overlay off the present set, wiring deltas, the migration runner
    (config + backup-first data), the engine-manifest bump with identity preserved, coherence, and the
    faked PR open — plus the degrade and no-backup paths — exercised by the shipped upgrade demo."""

    def test_upgrade_demo_passes(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(module_manager.upgrade_demo())


class TestUpgradeSafety(unittest.TestCase):
    """Upgrade's defense-in-depth: degrade on an unreachable release, the data-migration pre-flight refusal
    (before any overlay), and the overlay containment guard — each changes nothing."""

    def test_unreachable_release_degrades_to_the_current_version(self):
        with tempfile.TemporaryDirectory() as d:
            live = os.path.join(d, "live")
            os.makedirs(live)
            with module_manager._redirect_root(live):
                module_manager._build_upgrade_fixture(live)
                saved = module_manager._fetch_release_tree
                module_manager._fetch_release_tree = lambda *a, **k: (_ for _ in ()).throw(
                    RuntimeError("HTTP Error 404: Not Found"))
                try:
                    res = module_manager.upgrade(ref="v9.9.9")
                finally:
                    module_manager._fetch_release_tree = saved
                engine = module_manager.module_coherence.load_engine_manifest()
            self.assertTrue(res["refused"])
            self.assertIn("Couldn't reach", res["reason"])
            self.assertEqual((engine or {}).get("packages", {}).get("base"), "0.0.0")   # unchanged

    def test_upgrade_resolves_and_fetches_from_the_recorded_home_never_origin(self):
        # BOTH the latest-ref resolution and the release fetch target the engine's recorded HOME (#367).
        seen = {}
        with tempfile.TemporaryDirectory() as d:
            live = os.path.join(d, "live")
            os.makedirs(live)
            with module_manager._redirect_root(live):
                module_manager._build_upgrade_fixture(live)             # records home "acme/engine-home"
                sr, sf = module_manager._resolve_release_ref, module_manager._fetch_release_tree
                module_manager._resolve_release_ref = lambda ref, repo=None, token=None: (
                    seen.__setitem__("resolve_repo", repo) or "v0.2.0")

                def _spy(ref, dest, repo=None, token=None):
                    seen["fetch_repo"] = repo
                    raise RuntimeError("stop after capturing the source")
                module_manager._fetch_release_tree = _spy
                try:
                    module_manager.upgrade()                            # ref=None -> resolve latest FROM THE HOME
                finally:
                    module_manager._resolve_release_ref, module_manager._fetch_release_tree = sr, sf
        self.assertEqual(seen.get("resolve_repo"), "acme/engine-home")
        self.assertEqual(seen.get("fetch_repo"), "acme/engine-home")

    def test_upgrade_with_no_recorded_home_refuses_with_a_remedy_never_origin(self):
        called = {"n": 0}
        with tempfile.TemporaryDirectory() as d:
            live = os.path.join(d, "live")
            os.makedirs(live)
            with module_manager._redirect_root(live):
                module_manager._build_upgrade_fixture(live)
                p = os.path.join(live, ".engine", "engine.json")        # strip the home -> a pre-field engine
                m = module_manager.validate.load_json(p)
                m.pop("home_repository", None)
                module_manager._write_json(p, m)
                sf = module_manager._fetch_release_tree
                module_manager._fetch_release_tree = lambda *a, **k: called.__setitem__("n", called["n"] + 1)
                try:
                    res = module_manager.upgrade()
                finally:
                    module_manager._fetch_release_tree = sf
                engine = module_manager.module_coherence.load_engine_manifest()
        self.assertTrue(res["refused"])
        self.assertIn("no update home recorded", res["reason"])
        self.assertEqual(called["n"], 0)                                # never fell through to a fetch (never origin)
        self.assertEqual((engine or {}).get("packages", {}).get("base"), "0.0.0")   # nothing changed

    def test_upgrade_release_missing_at_the_home_refuses_naming_it_distinct_from_transport(self):
        import urllib.error
        with tempfile.TemporaryDirectory() as d:
            live = os.path.join(d, "live")
            os.makedirs(live)
            with module_manager._redirect_root(live):
                module_manager._build_upgrade_fixture(live)
                sf = module_manager._fetch_release_tree
                module_manager._fetch_release_tree = lambda *a, **k: (_ for _ in ()).throw(
                    urllib.error.HTTPError("u", 404, "Not Found", {}, None))   # a REAL 404 at the home
                try:
                    missing = module_manager.upgrade(ref="v9.9.9")
                finally:
                    module_manager._fetch_release_tree = sf
        self.assertTrue(missing["refused"])
        self.assertIn("acme/engine-home", missing["reason"])            # names the home (not just "the release")
        self.assertNotIn("network", missing["reason"].lower())          # distinct from the transport-degrade wording

    def test_no_published_release_at_the_home_refuses_naming_it_not_transport(self):
        # A reachable home with NO published release (releases API 200, null tag -> _NoPublishedRelease) is a
        # missing-release: refused naming the home, not mis-degraded as a network failure (#367 tech review).
        with tempfile.TemporaryDirectory() as d:
            live = os.path.join(d, "live")
            os.makedirs(live)
            with module_manager._redirect_root(live):
                module_manager._build_upgrade_fixture(live)
                sr = module_manager._resolve_release_ref
                module_manager._resolve_release_ref = lambda *a, **k: (_ for _ in ()).throw(
                    module_manager._NoPublishedRelease("no published release"))
                try:
                    res = module_manager.upgrade()   # ref=None -> resolve latest -> no published release
                finally:
                    module_manager._resolve_release_ref = sr
        self.assertTrue(res["refused"])
        self.assertIn("acme/engine-home", res["reason"])               # names the home
        self.assertNotIn("network", res["reason"].lower())             # not the transport-degrade wording

    def test_upgrade_preserves_the_recorded_home_across_the_version_bump(self):
        # engine.json is preserved-not-overlaid, so a successful upgrade keeps the home while
        # the module versions DO bump.
        opened = []
        with tempfile.TemporaryDirectory() as d:
            live = os.path.join(d, "live")
            os.makedirs(live)
            release = module_manager._build_upgrade_release(os.path.join(d, "release"))
            with module_manager._redirect_root(live):
                module_manager._build_upgrade_fixture(live)
                module_manager.upgrade(ref="v0.2.0", release_tree=release,
                                       opener=lambda **k: opened.append(k) or {"number": 1},
                                       backup=lambda *a, **k: {"ok": 1})
                engine = module_manager.module_coherence.load_engine_manifest()
        self.assertEqual((engine or {}).get("home_repository"), "acme/engine-home")   # preserved
        self.assertEqual((engine or {}).get("packages", {}).get("base"), "0.2.0")     # but versions bumped

    def test_upgrade_reasserts_the_foundation_gitignore_fence_and_keeps_operator_lines(self):
        # #409: the foundation fence is release-evolvable — an upgrade re-applies it (like the CODEOWNERS
        # re-render / CLAUDE.md floor merge), so a repo provisioned before/without it converges, and an
        # operator's own ignore lines are preserved (block-scoped apply, never a wholesale overlay).
        with tempfile.TemporaryDirectory() as d:
            live = os.path.join(d, "live")
            os.makedirs(live)
            release = module_manager._build_upgrade_release(os.path.join(d, "release"))
            with module_manager._redirect_root(live):
                module_manager._build_upgrade_fixture(live)
                with open(os.path.join(live, ".gitignore"), "w", encoding="utf-8") as fh:
                    fh.write("# mine\nnode_modules/\n")               # operator content, no engine fence yet
                res = module_manager.upgrade(ref="v0.2.0", release_tree=release,
                                             opener=lambda **k: {"number": 1}, backup=lambda *a, **k: {"ok": 1})
                after = module_manager.validate.read(os.path.join(live, ".gitignore"))
            self.assertEqual(res["foundation_ignores"]["status"], "written")
            self.assertIn("BEGIN engine-managed block: foundation-ignores", after)
            self.assertIn(".engine/.venv/", after)
            self.assertIn("node_modules/", after, "the operator's own ignore lines are preserved on upgrade")

    def test_data_migration_without_backup_refuses_the_whole_upgrade_before_overlay(self):
        with tempfile.TemporaryDirectory() as d:
            live = os.path.join(d, "live")
            os.makedirs(live)
            release = module_manager._build_upgrade_release(os.path.join(d, "release"))
            with module_manager._redirect_root(live):
                module_manager._build_upgrade_fixture(live)
                res = module_manager.upgrade(ref="v0.2.0", release_tree=release,
                                             opener=lambda **k: {"number": 1}, backup=None)
                engine = module_manager.module_coherence.load_engine_manifest()
                tool = module_manager.validate.read(os.path.join(live, ".engine/tools/base_tool.py"))
            self.assertTrue(res["refused"])
            self.assertIn("data", res["reason"])
            self.assertEqual((engine or {}).get("packages", {}).get("base"), "0.0.0")   # NOT bumped
            self.assertIn("v0", tool)        # base tool NOT overlaid: the pre-flight refuses before any write

    def test_data_migration_whose_backup_fails_at_runtime_is_declined_not_crashed(self):
        # A backup available at pre-flight but that fails at the snapshot must NOT crash upgrade() with a raw
        # traceback: the migration refuses (degrade loud), upgrade declines to open the change for review, and
        # nothing is merged. (opener must never fire.)
        opened = []
        with tempfile.TemporaryDirectory() as d:
            live = os.path.join(d, "live")
            os.makedirs(live)
            release = module_manager._build_upgrade_release(os.path.join(d, "release"))
            with module_manager._redirect_root(live):
                module_manager._build_upgrade_fixture(live)
                res = module_manager.upgrade(
                    ref="v0.2.0", release_tree=release,
                    opener=lambda **k: opened.append(k) or {"number": 1},
                    backup=lambda *a, **k: None)               # resolved live, but the snapshot fails at run time
        self.assertFalse(res.get("refused"))                   # not a pre-flight refusal — it got past the overlay
        self.assertIn("backup did not succeed", res["reason"])
        self.assertIn("NOT opened for review", res["reason"])
        self.assertEqual(opened, [])                           # the change was never opened for review

    def test_a_successful_data_migration_upgrade_discloses_the_saved_copy_once(self):
        # A successful data-migration upgrade tells the operator a pre-update copy was saved —
        # ONCE per upgrade, plainly, as reassurance for the later restore offer.
        opened = []
        with tempfile.TemporaryDirectory() as d:
            live = os.path.join(d, "live")
            os.makedirs(live)
            release = module_manager._build_upgrade_release(os.path.join(d, "release"))
            with module_manager._redirect_root(live):
                module_manager._build_upgrade_fixture(live)
                res = module_manager.upgrade(
                    ref="v0.2.0", release_tree=release,
                    opener=lambda **k: opened.append(k) or {"number": 1},
                    backup=lambda *a, **k: {"ok": 1})          # the pre-update snapshot succeeds
        self.assertTrue(opened)                                # the upgrade actually opened for review
        disclosures = [n for n in res.get("notes", []) if "saved a copy of it from right before this update" in n]
        self.assertEqual(len(disclosures), 1)                  # exactly one disclosure per upgrade
        self.assertIn("nothing for you to do now", disclosures[0])
        # the seam here reports nothing about locking, so no keep-it heads-up is added
        self.assertNotIn("couldn't confirm", disclosures[0])

    def test_an_unlockable_saved_copy_adds_a_keep_it_heads_up(self):
        # When the retained copy could not be locked against hand-deletion (hardened is False), the reversibility
        # disclosure carries a plain heads-up to keep it — so the operator does not delete their own undo.
        opened = []
        with tempfile.TemporaryDirectory() as d:
            live = os.path.join(d, "live")
            os.makedirs(live)
            release = module_manager._build_upgrade_release(os.path.join(d, "release"))
            with module_manager._redirect_root(live):
                module_manager._build_upgrade_fixture(live)
                res = module_manager.upgrade(
                    ref="v0.2.0", release_tree=release,
                    opener=lambda **k: opened.append(k) or {"number": 1},
                    backup=lambda *a, **k: {"backed-up": True, "hardened": False})   # can't be locked on this plan
        disclosures = [n for n in res.get("notes", []) if "saved a copy of it from right before this update" in n]
        self.assertEqual(len(disclosures), 1)
        self.assertIn("couldn't confirm", disclosures[0])       # states what the engine knows, not a guess about why
        self.assertIn("deleted by hand", disclosures[0])
        self.assertIn("keep it in place", disclosures[0])

    def test_a_lockable_saved_copy_has_no_keep_it_heads_up(self):
        opened = []
        with tempfile.TemporaryDirectory() as d:
            live = os.path.join(d, "live")
            os.makedirs(live)
            release = module_manager._build_upgrade_release(os.path.join(d, "release"))
            with module_manager._redirect_root(live):
                module_manager._build_upgrade_fixture(live)
                res = module_manager.upgrade(
                    ref="v0.2.0", release_tree=release,
                    opener=lambda **k: opened.append(k) or {"number": 1},
                    backup=lambda *a, **k: {"backed-up": True, "hardened": True})    # locked -> nothing to warn
        disclosures = [n for n in res.get("notes", []) if "saved a copy of it from right before this update" in n]
        self.assertEqual(len(disclosures), 1)
        self.assertNotIn("couldn't confirm", disclosures[0])

    def test_escaping_provides_in_a_release_is_refused_before_any_write(self):
        with tempfile.TemporaryDirectory() as d:
            live = os.path.join(d, "live")
            os.makedirs(live)
            release = os.path.join(d, "release")
            os.makedirs(os.path.join(release, ".engine", "modules", "base"))
            module_manager._write_json(
                os.path.join(release, ".engine", "modules", "base", "manifest.json"),
                {"id": "base", "version": "0.2.0", "status": "required",
                 "provides": {"tool": ["../sneak.py"]}, "depends": {}, "migrations": {}})
            with open(os.path.join(d, "sneak.py"), "w", encoding="utf-8") as fh:
                fh.write("# outside ROOT\n")
            with module_manager._redirect_root(live):
                module_manager._build_upgrade_fixture(live)
                res = module_manager.upgrade(ref="v0.2.0", release_tree=release,
                                             opener=lambda **k: {"number": 1}, backup=lambda *a, **k: {"ok": 1})
            self.assertTrue(res["refused"])
            self.assertIn("outside the engine", res["reason"])

    def test_upgrade_refreshes_codeowners_with_new_engine_files_keeping_operator_rules(self):
        # The design's upgrade re-render (engine.json `handle`): a release
        # whose `base` ADDS an engine file must land in the CODEOWNERS wall so the new file still routes to
        # the operator — and an operator's OWN rule must survive (fence-scoped).
        with tempfile.TemporaryDirectory() as d:
            live = os.path.join(d, "live")
            os.makedirs(live)
            release = os.path.join(d, "release")
            os.makedirs(os.path.join(release, ".engine", "modules", "base"))
            os.makedirs(os.path.join(release, ".engine", "tools"))
            module_manager._write_json(
                os.path.join(release, ".engine", "modules", "base", "manifest.json"),
                {"id": "base", "version": "0.2.0", "status": "required",
                 "provides": {"tool": [".engine/tools/base_tool.py", ".engine/tools/base_helper.py"]},
                 "depends": {}, "migrations": {}})
            for rel, txt in ((".engine/tools/base_tool.py", "# base v2\n"),
                             (".engine/tools/base_helper.py", "# a NEW engine file shipped in v2\n")):
                with open(os.path.join(release, rel), "w") as fh:
                    fh.write(txt)
            with module_manager._redirect_root(live):
                module_manager._build_upgrade_fixture(live)
                eng = module_coherence.load_engine_manifest()
                eng["handle"] = "@me"            # the preserved-identity owner first-run would have recorded
                module_manager._write_json(os.path.join(live, ".engine", "engine.json"), eng)
                co_path = os.path.join(live, ".github", "CODEOWNERS")
                os.makedirs(os.path.dirname(co_path), exist_ok=True)
                # seed an operator's OWN rule + the OLD engine block (pre-upgrade path set, no base_helper)
                with open(co_path, "w") as fh:
                    fh.write(wiring.render_codeowners("# mine\n/src/ @me\n",
                                                      module_coherence.codeowners_path_set(), "@me"))
                self.assertNotIn("base_helper.py", module_manager.validate.read(co_path))   # not engine-owned yet
                res = module_manager.upgrade(ref="v0.2.0", release_tree=release,
                                             opener=lambda **k: {"number": 1})
                after = module_manager.validate.read(co_path)
            self.assertFalse(res["refused"])
            self.assertEqual(res["codeowners"], "written")
            self.assertIn("/.engine/tools/base_helper.py @me", after)   # the new file now routes for review
            self.assertIn("/src/ @me", after)                           # the operator's own rule survived

    def test_upgrade_without_a_handle_degrades_codeowners_leaving_it_unchanged(self):
        # No operator handle on record (the construction repo / a pre-handle manifest) -> the re-render
        # DEGRADES: nothing written, no crash, the operator's CODEOWNERS left exactly as it was.
        with tempfile.TemporaryDirectory() as d:
            live = os.path.join(d, "live")
            os.makedirs(live)
            release = module_manager._build_upgrade_release(os.path.join(d, "release"))
            with module_manager._redirect_root(live):
                module_manager._build_upgrade_fixture(live)              # engine.json carries NO handle
                co_path = os.path.join(live, ".github", "CODEOWNERS")
                os.makedirs(os.path.dirname(co_path), exist_ok=True)
                with open(co_path, "w") as fh:
                    fh.write("# operator only\n/src/ @me\n")
                before = module_manager.validate.read(co_path)
                res = module_manager.upgrade(ref="v0.2.0", release_tree=release,
                                             opener=lambda **k: {"number": 1}, backup=lambda *a, **k: {"ok": 1})
                after = module_manager.validate.read(co_path)
            self.assertFalse(res["refused"])
            self.assertEqual(res["codeowners"], "degraded")
            self.assertEqual(after, before)                             # untouched on degrade


class TestMergeClaudeFloor(unittest.TestCase):
    """`_merge_claude_floor` keyed-merges the engine floor from a release's CLAUDE.deployed.md into the local
    CLAUDE.md — replacing only the `floor` block, preserving operator content, never appending a duplicate or
    crashing, and never letting the release's construction CLAUDE.md overlay the floor (#234 6a)."""
    FENCE = module_manager._FLOOR_FENCE
    STYLE = wiring.MD_FENCE

    def _release(self, d, floor_text="# New floor\n\nProject status v2.\n"):
        # Since #323 the release's root CLAUDE.md IS the fenced adopter floor — the source _merge_claude_floor
        # reads (its `floor` fence body). floor_text=None models a pre-promotion release with no floor fence
        # (its root file absent here → skipped).
        rel = os.path.join(d, "release")
        os.makedirs(rel, exist_ok=True)
        if floor_text is not None:
            lines = floor_text.split("\n")
            if lines and lines[-1] == "":
                lines = lines[:-1]
            with open(os.path.join(rel, "CLAUDE.md"), "w", encoding="utf-8") as fh:
                fh.write(wiring.fence_apply("", self.FENCE, lines, style=self.STYLE))
        return rel

    def _write_local(self, live, text):
        with open(os.path.join(live, "CLAUDE.md"), "w", encoding="utf-8") as fh:
            fh.write(text)

    def test_replaces_only_the_block_and_preserves_operator_content(self):
        with tempfile.TemporaryDirectory() as d:
            live = os.path.join(d, "live"); os.makedirs(live)
            rel = self._release(d)
            top, bottom = "# My product\n\nintro\n\n", "\n## More\n\ntail\n"
            with module_manager._redirect_root(live):
                self._write_local(
                    live, top + wiring.fence_apply("", self.FENCE, ["old"], style=self.STYLE) + bottom)
                out = module_manager._merge_claude_floor(rel)
                after = module_manager.validate.read(os.path.join(live, "CLAUDE.md"))
        self.assertEqual(out, "merged")
        self.assertIn(top, after)
        self.assertIn(bottom, after)
        self.assertIn("Project status v2.", after)
        self.assertNotIn("old", after)
        # Only the release fence's BODY was merged, never its markers — so the local file has exactly one
        # well-formed floor fence, not a doubled/nested one.
        self.assertEqual(after.count("BEGIN engine-managed block: floor"), 1)

    def test_no_local_fence_is_skipped_not_appended(self):
        # A pre-6a raw-floor (or any fence-less) CLAUDE.md is LEFT UNTOUCHED — never a duplicate floor.
        with tempfile.TemporaryDirectory() as d:
            live = os.path.join(d, "live"); os.makedirs(live)
            rel = self._release(d)
            raw = "# Your project runs on an Engine\n\nProject status (raw, unfenced).\n"
            with module_manager._redirect_root(live):
                self._write_local(live, raw)
                out = module_manager._merge_claude_floor(rel)
                after = module_manager.validate.read(os.path.join(live, "CLAUDE.md"))
        self.assertEqual(out, "skipped-no-section")
        self.assertEqual(after, raw)                           # untouched, no append
        self.assertNotIn("Project status v2.", after)

    def test_release_without_a_floor_source_is_skipped(self):
        with tempfile.TemporaryDirectory() as d:
            live = os.path.join(d, "live"); os.makedirs(live)
            rel = self._release(d, floor_text=None)            # no CLAUDE.deployed.md in the release
            fenced = wiring.fence_apply("", self.FENCE, ["keep"], style=self.STYLE)
            with module_manager._redirect_root(live):
                self._write_local(live, fenced)
                out = module_manager._merge_claude_floor(rel)
                after = module_manager.validate.read(os.path.join(live, "CLAUDE.md"))
        self.assertEqual(out, "skipped")
        self.assertEqual(after, fenced)                        # untouched

    def test_malformed_local_fence_degrades_without_crashing(self):
        with tempfile.TemporaryDirectory() as d:
            live = os.path.join(d, "live"); os.makedirs(live)
            rel = self._release(d)
            dup = (wiring.fence_apply("", self.FENCE, ["a"], style=self.STYLE)
                   + wiring.fence_apply("", self.FENCE, ["b"], style=self.STYLE))   # two blocks → malformed
            with module_manager._redirect_root(live):
                self._write_local(live, dup)
                out = module_manager._merge_claude_floor(rel)
                after = module_manager.validate.read(os.path.join(live, "CLAUDE.md"))
        self.assertEqual(out, "degraded")
        self.assertEqual(after, dup)                           # left unchanged, no crash


class TestRemoveReversesClaudeFloor(unittest.TestCase):
    """Clean engine removal block-reverses the root CLAUDE.md `floor` fence: a brownfield operator's own
    content is KEPT (only the engine block is removed), not clobbered wholesale — the data-loss guard the
    `outside`-set carve-out + the reversal provide together (#234 6a). The all-engine-delete case is covered
    by the shipped removal demo."""

    def _fakes(self):
        import bootstrap
        def opener(branch, title, body):
            return {"number": 0, "html_url": "(fixture)"}
        def transport(method, path, body=None):
            if method == "GET" and path.endswith("/rulesets"):
                return (200, [{"id": 1, "name": bootstrap.ENGINE_RULESET_NAME}], {})
            return (200 if method == "PUT" else 204 if method == "DELETE" else 200, None, {})
        return opener, transport

    def test_brownfield_claude_keeps_operator_content_on_remove(self):
        opener, transport = self._fakes()
        top, bottom = "# My product\n\nintro\n\n", "\n## More\n\ntail\n"
        with tempfile.TemporaryDirectory() as d:
            with module_manager._redirect_root(d):
                module_manager._build_remove_fixture(d)
                block = wiring.fence_apply("", module_manager._FLOOR_FENCE, ["# engine floor"],
                                           style=wiring.MD_FENCE)
                with open(os.path.join(d, "CLAUDE.md"), "w", encoding="utf-8") as fh:
                    fh.write(top + block + bottom)
                module_manager.remove_engine(opener=opener, transport=transport, choice="keep",
                                             announce=lambda m: None)
                after = module_manager.validate.read(os.path.join(d, "CLAUDE.md"))
        self.assertIn(top, after)                              # operator content kept
        self.assertIn(bottom, after)
        self.assertNotIn("# engine floor", after)             # the engine block body is gone
        self.assertNotIn("BEGIN engine-managed block: floor", after)


class TestRemoveReversesFoundationIgnores(unittest.TestCase):
    """#409: clean engine removal block-reverses the root `.gitignore` foundation fence — the operator's
    own ignore lines are KEPT (only the engine `foundation-ignores` block is removed), never wholesale-deleted
    (`.gitignore` is excluded from remove_engine's delete set + block-reversed, like CODEOWNERS/CLAUDE.md)."""

    def _fakes(self):
        import bootstrap
        def opener(branch, title, body):
            return {"number": 0, "html_url": "(fixture)"}
        def transport(method, path, body=None):
            if method == "GET" and path.endswith("/rulesets"):
                return (200, [{"id": 1, "name": bootstrap.ENGINE_RULESET_NAME}], {})
            return (200 if method == "PUT" else 204 if method == "DELETE" else 200, None, {})
        return opener, transport

    def test_remove_keeps_operator_ignore_lines_and_drops_only_the_engine_block(self):
        opener, transport = self._fakes()
        with tempfile.TemporaryDirectory() as d:
            with module_manager._redirect_root(d):
                module_manager._build_remove_fixture(d)
                # an operator's own ignore line + the engine foundation fence in one .gitignore
                with open(os.path.join(d, ".gitignore"), "w", encoding="utf-8") as fh:
                    fh.write("# mine\nnode_modules/\n")
                wiring.apply_foundation_ignores(os.path.join(d, ".gitignore"))
                module_manager.remove_engine(opener=opener, transport=transport, choice="keep",
                                             announce=lambda m: None)
                after = module_manager.validate.read(os.path.join(d, ".gitignore"))
        self.assertIn("node_modules/", after)                      # operator content kept
        self.assertNotIn("foundation-ignores", after)              # the engine block is gone
        self.assertNotIn(".engine/.venv/", after)
        self.assertNotIn("BEGIN engine-managed block: foundation-ignores", after)


class TestUpgradeSurfacesClaudeFloor(unittest.TestCase):
    """The CLAUDE.md merge outcome must reach the operator's consent surface — the upgrade PR body AND the
    console render — exactly as the CODEOWNERS outcome does. A degraded/skipped floor merge is an engine edit
    (or a silent non-update) of a file the operator co-owns and must never be invisible at the merge gate."""

    def _body(self, cf):
        return module_manager.render_upgrade_pr_body({"base": "0.1.0"}, {"base": "0.2.0"}, {"claude_floor": cf})

    def test_pr_body_names_the_merged_outcome(self):
        self.assertIn("working guide", self._body("merged").lower())

    def test_pr_body_names_the_not_updated_outcomes(self):
        self.assertIn("looked damaged", self._body("degraded"))
        self.assertIn("no engine marked block", self._body("skipped-no-section"))

    def test_render_prints_the_degraded_outcome(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            module_manager._render_upgrade({"from": {"base": "0.1.0"}, "to": {"base": "0.2.0"},
                                            "claude_floor": "degraded"})
        self.assertIn("working guide", buf.getvalue().lower())

    def test_pr_body_surfaces_the_foundation_ignores_reassertion(self):
        # #409: the .gitignore fence re-assert gets an operator-facing line on
        # upgrade, like its CODEOWNERS / CLAUDE.md siblings — not just a raw git diff.
        body = module_manager.render_upgrade_pr_body({"base": "0.1.0"}, {"base": "0.2.0"},
                                               {"foundation_ignores": {"status": "written"}})
        self.assertIn("ignore list", body.lower())
        # an unchanged ("already") re-assert stays silent — nothing changed to disclose
        quiet = module_manager.render_upgrade_pr_body({"base": "0.1.0"}, {"base": "0.2.0"},
                                                {"foundation_ignores": {"status": "already"}})
        self.assertNotIn("ignore list", quiet.lower())

    def test_agents_floor_outcome_is_surfaced_like_the_claude_floor(self):
        # Parity (#599 Slice 1): the tail computes agents_floor beside claude_floor, but the old body disclosed
        # only claude_floor. A degraded/skipped AGENTS.md merge is an engine edit (or refusal) of a co-owned
        # file and must reach the consent surface exactly as its CLAUDE.md sibling does.
        merged = module_manager.render_upgrade_pr_body({"base": "0.1.0"}, {"base": "0.2.0"},
                                                       {"agents_floor": "merged"})
        self.assertIn("codex guide", merged.lower())
        degraded = module_manager.render_upgrade_pr_body({"base": "0.1.0"}, {"base": "0.2.0"},
                                                         {"agents_floor": "degraded"})
        self.assertIn("looked damaged", degraded)
        skipped = module_manager.render_upgrade_pr_body({"base": "0.1.0"}, {"base": "0.2.0"},
                                                        {"agents_floor": "skipped-no-section"})
        self.assertIn("no engine marked block", skipped)


class TestUpgradePrBodyIsTemplateConforming(unittest.TestCase):
    """#599 Slice 1: the engine's own update pull request must clear the SAME body-completeness gate every
    engine pull request clears — the free-form body it replaced did not (an issue #599 finding). These run the
    REAL `pr-body-completeness` check against the rendered body, so a test cannot pass on a body the merge gate
    would reject."""

    def _rule(self):
        return module_manager.validate.load_json(
            os.path.join(module_manager.validate.CHECK_DIR, "pr-body-completeness.json"))

    def test_rendered_update_body_clears_the_completeness_gate(self):
        body = module_manager.render_upgrade_pr_body(
            {"base": "0.1.0"}, {"base": "0.2.0"},
            {"codeowners": "written", "claude_floor": "merged", "agents_floor": "merged",
             "foundation_ignores": {"status": "written"},
             "migrations": {"ran": ["base@0.2.0: moved the cache location"]}})
        passed, findings = module_manager.validate.kind_presence(self._rule(), {"pr_body": body})
        self.assertTrue(passed, f"update PR body failed the completeness gate: {findings}")
        self.assertEqual(findings, [])

    def test_rendered_body_clears_the_gate_even_when_minimal(self):
        # A clean update with no shared-file changes carries almost nothing in the tail; the body must still be
        # a complete, conforming consent surface, never a half-filled template.
        body = module_manager.render_upgrade_pr_body({"base": "0.1.0"}, {"base": "0.2.0"}, {})
        passed, findings = module_manager.validate.kind_presence(self._rule(), {"pr_body": body})
        self.assertTrue(passed, f"minimal update PR body failed the completeness gate: {findings}")

    def test_validation_section_claims_only_the_consistency_check_not_ci(self):
        # Consent honesty: this body is authored before the update PR opens, so the PR's CI has not run yet —
        # it must NOT claim the CI/full suite passed, only the consistency check that runs before the update is opened.
        body = module_manager.render_upgrade_pr_body({"base": "0.1.0"}, {"base": "0.2.0"}, {}).lower()
        self.assertIn("consistency check", body)
        self.assertNotIn("ci suite", body)
        self.assertNotIn("full ci", body)

    def test_data_migration_surfaces_the_recovery_copy_fact_on_the_durable_surface(self):
        # A data migration mutates the operator's saved memory — its reversibility fact (a recovery copy was
        # saved first) must live on the DURABLE PR body, not only the transient CLI note; and the keep-it
        # heads-up must ride along when the recovery copy could not be confirmed locked.
        body = module_manager.render_upgrade_pr_body(
            {"base": "0.1.0"}, {"base": "0.2.0"},
            {"migrations": {"ran": ["base -> 0.2.0 (data)"], "backup_unprotected": False}})
        self.assertIn("recovery copy", body.lower())
        self.assertIn("your saved memory", body.lower())      # the (data) tag is glossed, not raw
        self.assertNotIn("could not confirm", body.lower())   # locked copy -> no keep-it heads-up
        unprot = module_manager.render_upgrade_pr_body(
            {"base": "0.1.0"}, {"base": "0.2.0"},
            {"migrations": {"ran": ["base -> 0.2.0 (data)"], "backup_unprotected": True}})
        self.assertIn("could not confirm", unprot.lower())    # unlocked copy -> the keep-it heads-up rides along

    def test_config_only_migration_does_not_claim_a_saved_memory_change(self):
        # A config migration touches a committed settings file, not saved memory — it must NOT trigger the
        # saved-memory recovery-copy reassurance (which would misstate what happened).
        body = module_manager.render_upgrade_pr_body(
            {"base": "0.1.0"}, {"base": "0.2.0"},
            {"migrations": {"ran": ["base -> 0.2.0 (config)"]}}).lower()
        self.assertIn("engine settings", body)                # the (config) tag is glossed
        self.assertNotIn("recovery copy", body)               # no saved-memory reassurance for a config change

    def test_reconcile_facts_are_disclosed_and_still_clear_the_gate(self):
        # #599 Slice 2a / R4: the reconcile facts — fixtures delivered, AGENTS.md created, and files removed
        # (bucketed) — must ride the durable body so the destructive delete leg is visible at the merge, and
        # the body must still clear the completeness gate.
        body = module_manager.render_upgrade_pr_body(
            {"base": "0.1.0"}, {"base": "0.2.0"},
            {"codeowners": "written", "agents_floor": "created",
             "fixtures_delivered": [".engine/_fixtures/probe/bad_input.md"],
             "orphans_removed": {"engine": [".claude/agents/base-helper.md"],
                                 "suspect": [".engine/tools/operator_note.py"],
                                 "left_in_place": []},
             "migrations": {"ran": []}})
        passed, findings = module_manager.validate.kind_presence(self._rule(), {"pr_body": body})
        self.assertTrue(passed, f"reconcile body failed the completeness gate: {findings}")
        low = body.lower()
        self.assertIn("created", low)                                    # AGENTS.md created disclosed
        self.assertIn(".engine/_fixtures/probe/bad_input.md", body)      # fixtures delivered named
        self.assertIn(".claude/agents/base-helper.md", body)            # the engine rename orphan named
        self.assertIn(".engine/tools/operator_note.py", body)          # the operator-suspect removal named
        self.assertIn("added yourself", low)                            # the suspect-bucket caveat is present
        self.assertNotIn("full ci", low)                               # Validation stays honest (structural, not CI)

    def test_removed_files_are_bucketed_so_a_suspect_operator_file_is_distinguished(self):
        # risk-S1: the removal disclosure separates "engine files the release dropped" from "files under an
        # engine folder the release does not ship" — the operator-file-suspect bucket the merge gate can catch.
        body = module_manager.render_upgrade_pr_body(
            {"base": "0.1.0"}, {"base": "0.2.0"},
            {"orphans_removed": {"engine": [".claude/agents/old.md"], "suspect": [".engine/tools/mine.py"],
                                 "left_in_place": []}})
        # the two buckets are under different, distinguishable headings (the suspect one names the caveat)
        engine_idx = body.find(".claude/agents/old.md")
        suspect_idx = body.find(".engine/tools/mine.py")
        caveat_idx = body.lower().find("added yourself")
        self.assertGreater(engine_idx, 0)
        self.assertGreater(suspect_idx, 0)
        self.assertGreater(caveat_idx, 0)
        self.assertLess(caveat_idx, suspect_idx, "the suspect caveat must introduce the suspect list")


class TestLifecycleRendersCarryCoherenceWarrant(unittest.TestCase):
    """#400 F5: the add/remove/upgrade renders must carry the structural-not-fitness coherence warrant, so an
    operator never misreads a bare "consistent" line as "the module works." The warrant is single-homed in
    module_coherence.COHERENCE_WARRANT (reused, not re-typed) and, matching the standalone CLI's _print_report,
    prints on EVERY non-refused report — including the upgrade path that opens a review PR (the dominant case,
    which prints neither the hard-findings nor the staged-consistent line)."""

    _WARRANT_TELL = "not a fitness check"  # a distinctive phrase from COHERENCE_WARRANT

    def _render(self, fn, result):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            fn(result)
        return buf.getvalue()

    def test_the_tell_is_actually_in_the_single_homed_warrant(self):
        # guards against the warrant text drifting out from under the phrase these tests assert on
        self.assertIn(self._WARRANT_TELL, module_coherence.COHERENCE_WARRANT)

    def test_remove_render_carries_the_warrant(self):
        out = self._render(module_manager._render_remove, {"module_id": "demo"})
        self.assertIn("consistent", out)
        self.assertIn(self._WARRANT_TELL, out)

    def test_add_render_carries_the_warrant(self):
        out = self._render(module_manager._render_add, {"module_id": "demo", "version": "1.0.0"})
        self.assertIn(self._WARRANT_TELL, out)

    def test_upgrade_staged_path_carries_the_warrant(self):
        out = self._render(module_manager._render_upgrade,
                           {"from": {"base": "0.1.0"}, "to": {"base": "0.2.0"}})
        self.assertIn("staged and consistent", out)
        self.assertIn(self._WARRANT_TELL, out)

    def test_upgrade_PR_path_carries_the_warrant(self):
        # the dominant upgrade case opens a PR and prints neither branch — the warrant must still appear
        out = self._render(module_manager._render_upgrade,
                           {"from": {"base": "0.1.0"}, "to": {"base": "0.2.0"}, "pr": {"number": 42}})
        self.assertIn("#42", out)
        self.assertIn(self._WARRANT_TELL, out)

    def test_refused_render_does_not_carry_the_warrant(self):
        # a refused op checked nothing — no "consistent" claim, so no warrant to qualify
        out = self._render(module_manager._render_remove,
                           {"module_id": "demo", "refused": True, "reason": "does not exist"})
        self.assertNotIn(self._WARRANT_TELL, out)


class TestFrozenCheckNames(unittest.TestCase):
    """The engine CI check names are a FROZEN contract across versions — an upgrade/migration may never
    rename them (a renamed required check 'waits forever' and deadlocks every pull request). Changing this is a guardrail-weakening
    change, caught here and at the guard."""

    def test_required_check_names_are_frozen(self):
        import protection_guard
        self.assertEqual(protection_guard.REQUIRED_CHECKS, ["engine-ci", "engine-guard"])

    def test_the_overlay_replaces_the_engine_owned_workflow_files(self):
        # the overlay replaces the engine-owned CI workflows wholesale (a new version ships its own), so the
        # frozen names live in those files; the invariant is the release must keep the names unchanged
        self.assertIn(".github/workflows/engine-ci.yml", module_manager.FOUNDATION_CODE)
        self.assertIn(".github/workflows/engine-guard.yml", module_manager.FOUNDATION_CODE)


class TestFoundationInfra(unittest.TestCase):
    """FOUNDATION_INFRA is the single source; NAMED_INFRA + FOUNDATION_CODE derive from it (core 25c PR-3)."""

    def test_named_infra_is_the_engine_subset_a_pure_refactor(self):
        self.assertEqual(module_coherence.NAMED_INFRA,
                         {p for p in module_coherence.FOUNDATION_INFRA if p.startswith(".engine/")})
        # identical to the historical literal — no membership change to the ownership-walk carve-out
        self.assertEqual(module_coherence.NAMED_INFRA,
                         {".engine/engine.json", ".engine/pyproject.toml", ".engine/uv.lock"})

    def test_foundation_code_is_foundation_infra_minus_manifest_codeowners_claude_and_gitignore(self):
        expected = tuple(p for p in module_coherence.FOUNDATION_INFRA
                         if p not in (module_coherence.ENGINE_MANIFEST_REL, ".github/CODEOWNERS",
                                      "CLAUDE.md", "AGENTS.md", ".gitignore"))
        self.assertEqual(module_manager.FOUNDATION_CODE, expected)

    def test_foundation_code_also_excludes_the_agents_floor(self):
        # AGENTS.md is CLAUDE.md's exact sibling: keyed-merged on upgrade, so it must never be in the
        # overlay-replace set (an overlay would clobber an adopter's own content around the fence).
        self.assertNotIn("AGENTS.md", module_manager.FOUNDATION_CODE)
        self.assertIn("AGENTS.md", module_manager.module_coherence.FOUNDATION_INFRA)
        # the issue templates are now in the overlay set; the manifest, CODEOWNERS, root CLAUDE.md, and root
        # .gitignore are excluded — CLAUDE.md/.gitignore carry a keyed engine fence re-asserted locally
        # (_merge_claude_floor / apply_foundation_ignores), not fetched-and-replaced wholesale (#234 /
        # #409), so a release's file never overlays an adopter's own content
        self.assertIn(".github/ISSUE_TEMPLATE/*.md", module_manager.FOUNDATION_CODE)
        self.assertNotIn(".engine/engine.json", module_manager.FOUNDATION_CODE)
        self.assertNotIn(".github/CODEOWNERS", module_manager.FOUNDATION_CODE)
        self.assertNotIn("CLAUDE.md", module_manager.FOUNDATION_CODE)
        self.assertNotIn(".gitignore", module_manager.FOUNDATION_CODE)

    def test_engine_owned_paths_unions_provides_and_foundation_concretely(self):
        with tempfile.TemporaryDirectory() as d:
            with module_manager._redirect_root(d):
                module_manager._build_fixture(d)             # base + optx, provides two tool files
                os.makedirs(os.path.join(d, ".github"))
                with open(os.path.join(d, "CLAUDE.md"), "w") as fh:
                    fh.write("# floor\n")
                paths = module_coherence.engine_owned_paths(module_coherence.discover_manifests())
                self.assertIn(".engine/tools/base_tool.py", paths)        # a provides-claimed file
                self.assertIn(".engine/engine.json", paths)               # a foundation member
                self.assertIn("CLAUDE.md", paths)                         # a non-.engine foundation member
                # no glob literal leaks in — paths are concrete
                self.assertFalse(any("*" in p for p in paths))

    def test_codeowners_path_set_adds_self_only_in_the_codeowners_helper(self):
        # codeowners_path_set = engine_owned_paths + the CODEOWNERS self-add; the self-add lives ONLY here,
        # never in engine_owned_paths (whose other consumers must not carry CODEOWNERS' self-ownership).
        with tempfile.TemporaryDirectory() as d:
            with module_manager._redirect_root(d):
                module_manager._build_fixture(d)
                base_paths = module_coherence.engine_owned_paths(module_coherence.discover_manifests())
                co_paths = module_coherence.codeowners_path_set()
                self.assertNotIn(".github/CODEOWNERS", base_paths)        # NOT in the bare engine set
                self.assertIn(".github/CODEOWNERS", co_paths)             # self-owned in the CODEOWNERS set
                self.assertIn(".engine/tools/base_tool.py", co_paths)     # still unions the provides set
                self.assertEqual(co_paths, sorted(set(base_paths) | {".github/CODEOWNERS"}))


class TestIssueTemplateOverlay(unittest.TestCase):
    """tech plan-gate SERIOUS: the overlay must COPY the issue templates, not merely list a glob string
    (the foundation loop globs the release tree; a literal isfile on '*.md' would silently drop them)."""

    def test_overlay_copies_issue_templates_from_a_release(self):
        with tempfile.TemporaryDirectory() as d:
            release, live = os.path.join(d, "release"), os.path.join(d, "live")
            os.makedirs(os.path.join(release, ".engine", "modules", "base"))
            os.makedirs(os.path.join(release, ".github", "ISSUE_TEMPLATE"))
            module_manager._write_json(
                os.path.join(release, ".engine", "modules", "base", "manifest.json"),
                {"id": "base", "version": "0.0.0", "status": "required", "provides": {}, "depends": {}})
            for name in ("bug.md", "feature.md"):
                with open(os.path.join(release, ".github", "ISSUE_TEMPLATE", name), "w") as fh:
                    fh.write("template\n")
            os.makedirs(live)
            with module_manager._redirect_root(live):
                copied, _ = module_manager._overlay_engine_code(release, ["base"])
            self.assertIn(".github/ISSUE_TEMPLATE/bug.md", copied)
            self.assertIn(".github/ISSUE_TEMPLATE/feature.md", copied)
            self.assertTrue(os.path.isfile(os.path.join(live, ".github", "ISSUE_TEMPLATE", "bug.md")))


class TestOverlayExclude(unittest.TestCase):
    """#234 6b: the brownfield arrival passes `exclude` so an engine-exclusive path the operator chose to keep
    (a class-1 'leave-as-is') is NOT overwritten by the incoming engine file — the engine coexists around it."""

    def _release(self, release):
        os.makedirs(os.path.join(release, ".engine", "modules", "base"))
        os.makedirs(os.path.join(release, ".engine", "tools"))
        module_manager._write_json(
            os.path.join(release, ".engine", "modules", "base", "manifest.json"),
            {"id": "base", "version": "0.0.0", "status": "required",
             "provides": {"tool": [".engine/tools/keep.py", ".engine/tools/other.py"]}, "depends": {}})
        for name in ("keep.py", "other.py"):
            with open(os.path.join(release, ".engine", "tools", name), "w") as fh:
                fh.write("# engine version\n")

    def test_excluded_path_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as d:
            release, live = os.path.join(d, "release"), os.path.join(d, "live")
            self._release(release)
            os.makedirs(os.path.join(live, ".engine", "tools"))
            with open(os.path.join(live, ".engine", "tools", "keep.py"), "w") as fh:
                fh.write("# the product's own file\n")    # an operator file at an engine path (class-1)
            with module_manager._redirect_root(live):
                copied, _ = module_manager._overlay_engine_code(
                    release, ["base"], exclude={".engine/tools/keep.py"})
            self.assertNotIn(".engine/tools/keep.py", copied)            # kept, not overwritten
            self.assertIn(".engine/tools/other.py", copied)             # the rest still overlaid
            with open(os.path.join(live, ".engine", "tools", "keep.py")) as fh:
                self.assertEqual(fh.read(), "# the product's own file\n")


class TestRemoveEngine(unittest.TestCase):
    """Clean whole-engine removal (core 25c PR-3): de-bootstrap first, reverse all wires, delete every engine
    file (including the .github/ ones, unlike per-module remove), open a reviewed pull request. All four
    boundaries injected; the REAL reversal / delete-set / de-bootstrap-decision logic runs."""

    def _fakes(self, ruleset_present=True):
        import bootstrap
        prs, methods = [], []

        def opener(branch, title, body):
            prs.append((branch, title, body))
            return {"number": 5, "html_url": "http://x/5"}

        def transport(method, path, body=None):
            methods.append(method)
            if method == "GET" and path.endswith("/rulesets"):
                return (200, ([{"id": 3, "name": bootstrap.ENGINE_RULESET_NAME}]
                              if ruleset_present else []), {})
            if method == "PUT":
                return (200, {"id": 3}, {})
            if method == "DELETE":
                return (204, None, {})
            return (200, None, {})
        return opener, transport, prs, methods

    def _fixture_with_github(self, d):
        module_manager._build_fixture(d)                  # base + optx (+ optx wires applied)
        os.makedirs(os.path.join(d, ".github", "workflows"))
        with open(os.path.join(d, ".github", "workflows", "engine-ci.yml"), "w") as fh:
            fh.write("ci\n")
        with open(os.path.join(d, "CLAUDE.md"), "w", encoding="utf-8") as fh:
            # An all-engine greenfield CLAUDE.md is the floor wrapped in the engine fence (6a); removal
            # block-reverses it → whitespace-only → the file is deleted (nothing operator-owned to keep).
            fh.write(wiring.fence_apply("", module_manager._FLOOR_FENCE, ["# floor"], style=wiring.MD_FENCE))

    def test_reverses_wires_deletes_all_engine_files_and_opens_a_pr(self):
        opener, transport, prs, _ = self._fakes(True)
        with tempfile.TemporaryDirectory() as d:
            with module_manager._redirect_root(d):
                self._fixture_with_github(d)
                r = module_manager.remove_engine(opener=opener, transport=transport,
                                                 choice="keep", announce=lambda m: None)
                engine_gone = not os.path.isdir(os.path.join(d, ".engine"))
                ci_gone = not os.path.isfile(os.path.join(d, ".github", "workflows", "engine-ci.yml"))
                claude_gone = not os.path.isfile(os.path.join(d, "CLAUDE.md"))
        self.assertEqual(r["de_bootstrap"]["status"], "kept")
        self.assertTrue(r["reversed"], "optx's wires should be reversed")
        self.assertTrue(engine_gone and ci_gone and claude_gone)
        self.assertEqual(len(prs), 1)
        self.assertIsNotNone(r["pr"])

    def test_github_member_is_in_the_delete_set_unlike_per_module_remove(self):
        # whole-engine removal deletes the .github/ foundation files + root CLAUDE.md too — these are
        # foundation infra, NOT any module's `provides`, so per-module remove() (which deletes only the
        # sole-owned files a module provides, at any path since #409) never touches them
        opener, transport, _, _ = self._fakes(True)
        with tempfile.TemporaryDirectory() as d:
            with module_manager._redirect_root(d):
                self._fixture_with_github(d)
                r = module_manager.remove_engine(opener=opener, transport=transport,
                                                 choice="keep", announce=lambda m: None)
        self.assertIn(".github/workflows/engine-ci.yml", r["deleted"])
        self.assertIn("CLAUDE.md", r["deleted"])
        self.assertIn(".engine/", r["deleted"])

    def test_codeowners_engine_block_removed_operator_rules_kept(self):
        opener, transport, _, _ = self._fakes(True)
        with tempfile.TemporaryDirectory() as d:
            with module_manager._redirect_root(d):
                self._fixture_with_github(d)
                co = wiring.render_codeowners("# mine\n/src/ @me\n", [".engine/engine.json"], "@me")
                with open(os.path.join(d, ".github", "CODEOWNERS"), "w") as fh:
                    fh.write(co)
                module_manager.remove_engine(opener=opener, transport=transport,
                                             choice="keep", announce=lambda m: None)
                with open(os.path.join(d, ".github", "CODEOWNERS")) as fh:
                    text = fh.read()
        self.assertIn("/src/ @me", text)             # operator rule kept
        self.assertNotIn("engine.json", text)        # engine block removed

    def test_drop_choice_deletes_the_safety_rule(self):
        opener, transport, _, methods = self._fakes(True)
        with tempfile.TemporaryDirectory() as d:
            with module_manager._redirect_root(d):
                self._fixture_with_github(d)
                r = module_manager.remove_engine(opener=opener, transport=transport,
                                                 choice="drop", announce=lambda m: None)
        self.assertEqual(r["de_bootstrap"]["status"], "dropped")
        self.assertIn("DELETE", methods)

    def test_frozen_check_names_untouched_by_removal(self):
        import protection_guard
        before = list(protection_guard.REQUIRED_CHECKS)
        opener, transport, _, _ = self._fakes(True)
        with tempfile.TemporaryDirectory() as d:
            with module_manager._redirect_root(d):
                self._fixture_with_github(d)
                module_manager.remove_engine(opener=opener, transport=transport,
                                             choice="keep", announce=lambda m: None)
        self.assertEqual(protection_guard.REQUIRED_CHECKS, before)

    def test_remove_engine_demo_passes(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(module_manager.remove_engine_demo())


class TestBackupSeamResolution(unittest.TestCase):
    """_resolve_backup_seam: now that memory ships the seam, "a backup is available" means the mechanism is
    installed AND a vault is configured — so memory-installed-but-no-vault resolves to None (refuse the data
    migration cleanly) rather than handing back a seam that fails mid-snapshot. An injected backup always wins."""

    def test_no_vault_configured_resolves_to_no_seam(self):
        import memory
        orig = memory.migration_backup_available
        memory.migration_backup_available = lambda: False
        try:
            self.assertIsNone(module_manager._resolve_backup_seam(None))
        finally:
            memory.migration_backup_available = orig

    def test_vault_configured_resolves_to_the_live_seam(self):
        import memory
        orig = memory.migration_backup_available
        memory.migration_backup_available = lambda: True
        try:
            self.assertIs(module_manager._resolve_backup_seam(None), memory.snapshot_for_migration)
        finally:
            memory.migration_backup_available = orig

    def test_an_injected_backup_wins_over_resolution(self):
        sentinel = lambda store, ver: {"ok": True}
        self.assertIs(module_manager._resolve_backup_seam(sentinel), sentinel)


class TestUpgradeTailAndSafeCli(unittest.TestCase):
    """The #594 fix (the version-sensitive tail runs as freshly-overlaid code) + the safe upgrade CLI
    (preview-by-default, --confirm to apply, no footgun)."""

    def _incoherent_fixture(self, live):
        """A coherent upgrade fixture made INCOHERENT by declaring a gitignore wire that is never applied —
        the shape a half-applied earlier update leaves (engine.json bumped, the tree inconsistent)."""
        module_manager._build_upgrade_fixture(live)
        man_path = os.path.join(live, ".engine", "modules", "base", "manifest.json")
        man = json.load(open(man_path, encoding="utf-8"))
        man["wires"] = list(man.get("wires") or []) + [
            {"type": "gitignore", "key": "never-applied", "lines": [".engine/base/.nope/"]}]
        with open(man_path, "w", encoding="utf-8") as fh:
            json.dump(man, fh)

    def test_preview_flags_a_half_applied_tree_instead_of_up_to_date(self):
        # The false-clear the plan-gate caught: engine.json is bumped before coherence, so a version-only
        # check would read "up to date" on a half-applied tree. The preview must run coherence and say so.
        with tempfile.TemporaryDirectory() as d:
            live = os.path.join(d, "live")
            os.makedirs(live)
            with module_manager._redirect_root(live):
                self._incoherent_fixture(live)
                preview = module_manager.upgrade_preview()
        self.assertFalse(preview["coherent"])
        self.assertEqual(preview["status"], "inconsistent")
        self.assertIn("--confirm", preview["reason"])
        self.assertNotIn("up to date", module_manager._display_ver(preview["current"]) or "")

    def test_preview_reports_available_or_up_to_date_when_coherent(self):
        # Slice 2: an available update now PREVIEWS impact (a real fetch), so the fetch is stubbed to a local
        # release; up-to-date still short-circuits BEFORE any fetch (no download when there's nothing newer).
        with tempfile.TemporaryDirectory() as d:
            live = os.path.join(d, "live")
            os.makedirs(live)
            release = module_manager._build_upgrade_release(os.path.join(d, "release"))
            with module_manager._redirect_root(live):
                module_manager._build_upgrade_fixture(live)   # coherent, engine_release 0.0.0
                orig_r, orig_f = module_manager._resolve_release_ref, module_manager._fetch_release_tree
                fetched = []
                try:
                    module_manager._resolve_release_ref = lambda ref, repo=None, token=None: "v0.2.0"
                    module_manager._fetch_release_tree = (
                        lambda ref, dest, repo=None, token=None: fetched.append(ref) or release)
                    available = module_manager.upgrade_preview()
                    module_manager._resolve_release_ref = lambda ref, repo=None, token=None: "0.0.0"
                    current = module_manager.upgrade_preview()
                finally:
                    module_manager._resolve_release_ref = orig_r
                    module_manager._fetch_release_tree = orig_f
        self.assertEqual(available["status"], "update-available")
        self.assertEqual(available["available"], "v0.2.0")
        self.assertEqual(available["target_versions"]["base"], "0.2.0")   # it read the release's real manifest
        self.assertEqual(current["status"], "up-to-date")
        self.assertEqual(fetched, ["v0.2.0"])   # fetched once for the available update, never for up-to-date

    def _run_main(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = module_manager.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_upgrade_help_prints_usage_and_never_applies(self):
        # --help must print usage AND never reach the apply path (upgrade() must not be called).
        orig = module_manager.upgrade
        called = []
        try:
            module_manager.upgrade = lambda *a, **k: called.append(True)
            code, out, _ = self._run_main(["upgrade", "--help"])
        finally:
            module_manager.upgrade = orig
        self.assertEqual(code, 0)
        self.assertIn("PREVIEWS only", out)
        self.assertEqual(called, [])   # apply path never entered

    def test_upgrade_rejects_an_unknown_flag(self):
        code, _, err = self._run_main(["upgrade", "--yolo"])
        self.assertEqual(code, 2)
        self.assertIn("unknown option", err)

    def test_preview_of_a_named_version_previews_that_versions_impact(self):
        # Slice 2 lifts slice 1's "a named ref can't be previewed" limit: a named ref now previews THAT
        # version's real impact (fetching it directly), never latching onto the latest release.
        with tempfile.TemporaryDirectory() as d:
            live = os.path.join(d, "live")
            os.makedirs(live)
            release = module_manager._build_upgrade_release(os.path.join(d, "release"))
            with module_manager._redirect_root(live):
                module_manager._build_upgrade_fixture(live)
                orig_f = module_manager._fetch_release_tree
                fetched = []
                try:
                    module_manager._fetch_release_tree = (
                        lambda ref, dest, repo=None, token=None: fetched.append(ref) or release)
                    preview = module_manager.upgrade_preview("v0.2.0")
                finally:
                    module_manager._fetch_release_tree = orig_f
        self.assertEqual(preview["status"], "update-available")
        self.assertEqual(preview["named_ref"], "v0.2.0")
        self.assertEqual(preview["target_ref"], "v0.2.0")
        self.assertEqual(fetched, ["v0.2.0"])   # fetched the NAMED version, not the latest
        self.assertTrue(any(m["kind"] == "data" for m in preview["migrations"]))   # its real impact was read

    def _capture_child_env(self, practice):
        """Run _spawn_upgrade_tail with subprocess.run stubbed, returning the env the child would get."""
        import subprocess
        captured = {}

        def _stub(argv, **kw):
            captured.update(kw.get("env") or {})
            return type("P", (), {"returncode": 1, "stderr": ""})()   # non-zero -> clean failure branch
        orig = subprocess.run
        try:
            subprocess.run = _stub
            module_manager._spawn_upgrade_tail({
                "release_tree": "/tmp", "target_ref": "v1", "from_versions": {}, "target_versions": {},
                "present_ids": [], "old_by_id": {}, "handle": None, "practice": practice,
                "marker": module_manager._UPGRADE_TAIL_MARKER})
        finally:
            subprocess.run = orig
        return captured

    def test_child_env_scopes_the_github_token_by_practice(self):
        # Security boundary: the freshly-downloaded child holds the token ONLY on the real (PR-opening) path,
        # never on a practice run — and is always marked as an internal child.
        orig_token = os.environ.get("GITHUB_TOKEN")
        os.environ["GITHUB_TOKEN"] = "sekret"
        try:
            practice_env = self._capture_child_env(True)
            real_env = self._capture_child_env(False)
        finally:
            if orig_token is None:
                os.environ.pop("GITHUB_TOKEN", None)
            else:
                os.environ["GITHUB_TOKEN"] = orig_token
        self.assertEqual(practice_env.get("ENGINE_UPGRADE_CHILD"), "1")
        self.assertNotIn("GITHUB_TOKEN", practice_env)          # practice: token withheld
        self.assertEqual(real_env.get("GITHUB_TOKEN"), "sekret")  # real path: token passed deliberately

    def test_a_child_failure_maps_to_a_clean_recoverable_result_and_renders_it(self):
        # A child that dies (non-zero exit / no result file) must yield a clean "run again with --confirm"
        # result, and _render_upgrade must SURFACE that reason — never claim "staged and consistent".
        import subprocess
        orig_run = subprocess.run
        try:
            subprocess.run = lambda a, **kw: type("P", (), {"returncode": 1, "stderr": "boom"})()
            tail = module_manager._spawn_upgrade_tail({
                "release_tree": "/tmp", "target_ref": "v1", "from_versions": {}, "target_versions": {},
                "present_ids": [], "old_by_id": {}, "handle": None, "practice": False,
                "marker": module_manager._UPGRADE_TAIL_MARKER})
        finally:
            subprocess.run = orig_run
        self.assertTrue(tail["applied"])
        self.assertIn("--confirm", tail["reason"])
        self.assertIsNone(tail.get("pr"))
        # the renderer must print the recovery reason, not the false "staged and consistent"
        result = {"refused": False, "from": {"core": "0.2.0"}, "to": {"core": "0.3.0"}, "copied": [],
                  "notes": [], "findings": [], "pr": None}
        module_manager._merge_tail(result, tail)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            module_manager._render_upgrade(result)
        rendered = out.getvalue()
        self.assertIn("--confirm", rendered)
        self.assertNotIn("staged and consistent", rendered)

    def test_upgrade_tail_command_refuses_without_the_child_env_marker(self):
        # The internal verb must not be drivable by a stray operator command / injected instruction.
        os.environ.pop("ENGINE_UPGRADE_CHILD", None)
        code, _, err = self._run_main(["__upgrade_tail__", "/no/such/state.json"])
        self.assertEqual(code, 2)
        self.assertIn("internal step", err)

    def test_upgrade_tail_command_refuses_a_state_without_the_internal_marker(self):
        with tempfile.TemporaryDirectory() as d:
            state_path = os.path.join(d, "state.json")
            with open(state_path, "w", encoding="utf-8") as fh:
                json.dump({"release_tree": d, "present_ids": [], "from_versions": {},
                           "target_versions": {}, "target_ref": "v1", "old_by_id": {},
                           "practice": True, "result_path": os.path.join(d, "r.json")}, fh)  # NO marker
            os.environ["ENGINE_UPGRADE_CHILD"] = "1"
            try:
                code, _, err = self._run_main(["__upgrade_tail__", state_path])
            finally:
                os.environ.pop("ENGINE_UPGRADE_CHILD", None)
        self.assertEqual(code, 2)
        self.assertIn("marker", err)

    def test_the_594_falsification_demo_passes(self):
        # Covers the subprocess tail end-to-end (a release's new wire seam applies via a fresh child, the
        # in-process path reproduces the bug) and is the regression test that lets the demo retire at
        # first run. Heavier than a unit test (copytree + subprocess), run once.
        import demo_594_upgrade_tail_reexec as demo
        import quiet_call
        code = quiet_call.run(demo.main)   # captures the demo's walkthrough (keeps the suite summary clean)
        self.assertEqual(code, 0)


class TestUpgradeReconcile(unittest.TestCase):
    """The #599 release-authoritative reconcile: an update drives a deployed tree to provision(release) —
    delivering the file category the copy-only overlay missed, removing what the release dropped/renamed and
    any first-run file the overlay resurrected, creating a never-created foundation floor — then refuses
    cleanly if the rebuilt tree is inconsistent. The in-process path runs the fixture-safe coherence gate; the
    full structural gate is exercised on a clone by the #594 demo and proven across versions at cut time
    (Slice 3)."""

    @staticmethod
    def _augment(live, release):
        """Shape the shared fixtures into a realistic provisioned-deployed upgrade: the deployed tree carries
        an old-named engine agent (a rename orphan) and an operator's own agent (a literal namespace — never a
        delete candidate), plus an operator file under an engine GLOB namespace (surfaced AND removed); the
        release renames the agent, and ships a first-run tool it provides by glob (the overlay resurrects it,
        the reconcile removes it) that its retire manifest lists."""
        import json

        def _load(p):
            with open(p, encoding="utf-8") as fh:
                return json.load(fh)
        base_live = os.path.join(live, ".engine", "modules", "base", "manifest.json")
        man = _load(base_live)
        man["provides"] = {"tool": [".engine/tools/*.py"], "agent": [".claude/agents/base-helper.md"]}
        with open(base_live, "w", encoding="utf-8") as fh:
            json.dump(man, fh)
        os.makedirs(os.path.join(live, ".claude", "agents"), exist_ok=True)
        for rel, txt in ((".claude/agents/base-helper.md", "# engine agent (old name)\n"),
                         (".claude/agents/my-custom.md", "# an operator's own agent\n"),
                         (".engine/tools/operator_note.py", "# an operator's own script under an engine glob\n")):
            with open(os.path.join(live, rel), "w", encoding="utf-8") as fh:
                fh.write(txt)
        base_rel = os.path.join(release, ".engine", "modules", "base", "manifest.json")
        rman = _load(base_rel)
        rman["provides"] = {"tool": [".engine/tools/*.py"], "agent": [".claude/agents/engine-helper.md"],
                            "migration": [".engine/modules/base/migrations/*.py"], "state": [".engine/state/*.json"]}
        with open(base_rel, "w", encoding="utf-8") as fh:
            json.dump(rman, fh)
        os.makedirs(os.path.join(release, ".claude", "agents"), exist_ok=True)
        with open(os.path.join(release, ".claude", "agents", "engine-helper.md"), "w", encoding="utf-8") as fh:
            fh.write("# engine agent (new name)\n")
        with open(os.path.join(release, ".engine", "tools", "setup_only.py"), "w", encoding="utf-8") as fh:
            fh.write("# a first-run-only tool the deployed repo must not carry\n")
        fra = os.path.join(release, ".engine", "provisioning", "first-run-assets.json")
        data = _load(fra)
        data["files"] = [".engine/tools/setup_only.py"]
        with open(fra, "w", encoding="utf-8") as fh:
            json.dump(data, fh)

    def test_reconcile_to_provision_release(self):
        opened = []
        with tempfile.TemporaryDirectory() as d:
            live = os.path.join(d, "live")
            os.makedirs(live)
            release = module_manager._build_upgrade_release(os.path.join(d, "release"))
            with module_manager._redirect_root(live):
                module_manager._build_upgrade_fixture(live)
                self._augment(live, release)
                res = module_manager.upgrade(ref="v0.2.0", release_tree=release,
                                             opener=lambda **k: opened.append(k) or {"number": 7},
                                             backup=lambda *a, **k: {"ok": 1})

                def _exists(rel):
                    return os.path.exists(os.path.join(live, rel))
                # DELIVER — the fixture category the copy-only overlay missed is now on disk (#599 class 3)
                self.assertTrue(_exists(".engine/_fixtures/probe/bad_input.md"), "fixtures were not delivered")
                # CREATE-IF-ABSENT — AGENTS.md was never on the deployed tree; the reconcile creates it (class 2)
                self.assertTrue(_exists("AGENTS.md"), "AGENTS.md was not created")
                self.assertEqual(res["agents_floor"], "created")
                # DELETE — the renamed engine agent's old path is gone; the new name is delivered (class 1)
                self.assertFalse(_exists(".claude/agents/base-helper.md"), "the rename orphan was not removed")
                self.assertTrue(_exists(".claude/agents/engine-helper.md"), "the renamed agent was not delivered")
                # NO RESURRECTION — the first-run tool the overlay re-copied is removed again (the latent bug)
                self.assertFalse(_exists(".engine/tools/setup_only.py"), "a first-run file was resurrected")
                # OPERATOR SURVIVES — a literal-namespace operator agent is never a delete candidate
                self.assertTrue(_exists(".claude/agents/my-custom.md"), "an operator's own agent was removed")
                # SUSPECT SURFACING — an operator file under an engine GLOB namespace is removed AND surfaced
                self.assertFalse(_exists(".engine/tools/operator_note.py"))
        self.assertIn(".claude/agents/base-helper.md", res["orphans_removed"]["engine"])
        self.assertIn(".engine/tools/operator_note.py", res["orphans_removed"]["suspect"])
        # GATE PASSED -> a review pull request was opened
        self.assertTrue(res.get("pr"), f"the reconcile did not open a pull request: {res.get('reason')}")
        self.assertEqual(len(opened), 1)

    def test_refuses_cleanly_on_a_hard_consistency_finding_without_opening(self):
        # An .engine file no module claims makes check_coherence hard-flag; the gate must refuse in plain
        # language (no raw check id), leave the working copy staged (half-state), and open nothing.
        with tempfile.TemporaryDirectory() as d:
            live = os.path.join(d, "live")
            os.makedirs(live)
            release = module_manager._build_upgrade_release(os.path.join(d, "release"))
            with module_manager._redirect_root(live):
                module_manager._build_upgrade_fixture(live)
                with open(os.path.join(live, ".engine", "tools", "unclaimed_orphan.py"), "w") as fh:
                    fh.write("# a file no module's provides claims\n")
                res = module_manager.upgrade(ref="v0.2.0", release_tree=release,
                                             opener=lambda **k: {"number": 9}, backup=lambda *a, **k: {"ok": 1})
        self.assertIsNone(res.get("pr"), "a pull request was opened despite a hard consistency finding")
        self.assertTrue(res.get("applied"), "the working copy should be mutated (the half-state law)")
        self.assertIn("consistency check", res["reason"])
        self.assertNotIn("engine/check/", res["reason"], "the refusal must name no raw check id")

    def test_a_bad_release_retire_manifest_refuses_cleanly_never_resurrecting(self):
        # risk-S3: an unreadable release first-run-assets.json must REFUSE, never fall through to the
        # un-projected template shape (which would deliver + protect the first-run set).
        with tempfile.TemporaryDirectory() as d:
            live = os.path.join(d, "live")
            os.makedirs(live)
            release = module_manager._build_upgrade_release(os.path.join(d, "release"))
            with open(os.path.join(release, ".engine", "provisioning", "first-run-assets.json"), "w") as fh:
                fh.write("{ not valid json")
            with module_manager._redirect_root(live):
                module_manager._build_upgrade_fixture(live)
                res = module_manager.upgrade(ref="v0.2.0", release_tree=release,
                                             opener=lambda **k: {"number": 9}, backup=lambda *a, **k: {"ok": 1})
        self.assertIsNone(res.get("pr"))
        self.assertIn("setup-file list", res["reason"])
        self.assertIn("undo", res["reason"].lower())   # the refusal names a recourse (usability review)

    def test_structural_gate_binds_to_live_ci_hard_rules(self):
        # T-S2/DH-2: the gate's rule filter only NARROWS, so a renamed or dropped check id would silently shrink
        # the gate while it still reports "passed". Bind the set to the live corpus: every id must be a live CI
        # hard rule, and the filter must select exactly them — a drift fails loudly HERE, not silently at the gate.
        rules = module_manager.validate.load_rules()
        by_id = {r.get("id"): r for r in rules}
        for cid in module_manager._STRUCTURAL_GATE_CHECK_IDS:
            self.assertIn(cid, by_id, f"gate check id {cid} is not a live rule (renamed/removed?)")
            self.assertEqual(by_id[cid].get("tier"), "hard", f"{cid} is no longer a hard check")
            self.assertIn("CI", by_id[cid].get("suites") or [], f"{cid} left the CI suite")
        selected = {r.get("id") for r in rules if r.get("id") in module_manager._STRUCTURAL_GATE_CHECK_IDS}
        self.assertEqual(selected, set(module_manager._STRUCTURAL_GATE_CHECK_IDS),
                         "the gate filter must select exactly its declared ids against the live roster")

    def test_a_root_resolving_retire_entry_refuses_never_deleting_the_tree(self):
        # security review: a retire manifest whose directory entry resolves to the repo root (or is empty/absolute)
        # must REFUSE cleanly, never rmtree the whole tree.
        import json
        with tempfile.TemporaryDirectory() as d:
            live = os.path.join(d, "live")
            os.makedirs(live)
            release = module_manager._build_upgrade_release(os.path.join(d, "release"))
            fra = os.path.join(release, ".engine", "provisioning", "first-run-assets.json")
            with open(fra, encoding="utf-8") as fh:
                data = json.load(fh)
            data["directories"] = [""]        # resolves to the repo root
            with open(fra, "w", encoding="utf-8") as fh:
                json.dump(data, fh)
            with module_manager._redirect_root(live):
                module_manager._build_upgrade_fixture(live)
                res = module_manager.upgrade(ref="v0.2.0", release_tree=release,
                                             opener=lambda **k: {"number": 9}, backup=lambda *a, **k: {"ok": 1})
                survived = os.path.isfile(os.path.join(live, ".engine", "engine.json"))
        self.assertIsNone(res.get("pr"))
        self.assertTrue(survived, "the repo must still exist — the dangerous retire entry was refused")
        self.assertIn("unusable path", res["reason"])

    def test_an_untracked_operator_file_under_an_engine_glob_is_left_not_deleted(self):
        # security review: a git-ignored (untracked) operator file under an engine glob namespace is NOT
        # recoverable by the undo, so the reconcile LEAVES it and surfaces it, rather than deleting it.
        import subprocess
        scratch = os.path.join(".engine", "tools", "untracked_scratch.py")
        with tempfile.TemporaryDirectory() as d:
            live = os.path.join(d, "live")
            os.makedirs(live)
            release = module_manager._build_upgrade_release(os.path.join(d, "release"))
            with module_manager._redirect_root(live):
                module_manager._build_upgrade_fixture(live)
                self._augment(live, release)
                subprocess.run(["git", "init", "-q"], cwd=live, check=True)
                subprocess.run(["git", "add", "-A"], cwd=live, check=True)
                subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "x"],
                               cwd=live, check=True)
                with open(os.path.join(live, scratch), "w", encoding="utf-8") as fh:
                    fh.write("# an operator's own scratch tool, never committed\n")
                res = module_manager.upgrade(ref="v0.2.0", release_tree=release,
                                             opener=lambda **k: {"number": 7}, backup=lambda *a, **k: {"ok": 1})
                left = os.path.exists(os.path.join(live, scratch))
        self.assertTrue(left, "an untracked operator file under an engine glob must be left in place")
        self.assertTrue(any("untracked_scratch.py" in s for s in res["orphans_removed"]["left_in_place"]),
                        "the preserved untracked file must be surfaced to the operator")

    def test_deliver_synced_delivers_the_fixture_category_for_arrival(self):
        # SC-3: arrival delivers via the shared primitive with project_retire=False; prove it carries the
        # fixture category the copy-only overlay missed (the arrival gap #599 also closes).
        with tempfile.TemporaryDirectory() as d:
            live = os.path.join(d, "live")
            os.makedirs(live)
            release = module_manager._build_upgrade_release(os.path.join(d, "release"))
            candidates = {"base": module_manager.validate.load_json(
                os.path.join(release, ".engine", "modules", "base", "manifest.json"))}
            with module_manager._redirect_root(live):
                module_manager._build_upgrade_fixture(live)
                delivered = module_manager._deliver_synced(release, candidates, project_retire=False)
        self.assertIn(".engine/_fixtures/probe/bad_input.md", delivered)

    def test_the_599_falsification_demo_passes(self):
        # Covers the reconcile end-to-end on a real clone — the delete leg removes a renamed-away engine agent
        # (so the tree stays provider-parity clean); disabling it leaves the orphan a hard CI check catches.
        # This surviving reference is also what lets the demo travel (census-completeness) rather than retire.
        import demo_599_upgrade_reconcile as demo
        import quiet_call
        self.assertEqual(quiet_call.run(demo.main), 0)   # captures the demo walkthrough; keeps the summary clean

    def test_archive_tree_extracts_a_ref_offline_returning_dest_itself(self):
        # The offline sibling of _fetch_release_tree: `git archive` has NO owner-repo-sha wrapper, so it
        # returns `dest` itself (arch-N2). Uses HEAD, so it runs in a shallow CI checkout too.
        with tempfile.TemporaryDirectory() as d:
            dest = os.path.join(d, "tree")
            out = module_manager._archive_tree("HEAD", dest)
            self.assertEqual(out, dest)
            self.assertTrue(os.path.isdir(os.path.join(dest, ".engine")),
                            "the archived tree root should contain .engine/ directly (no wrapper dir)")


class TestReconcileDeliverySuperset(unittest.TestCase):
    """#599 Slice 3 drift guard: the reconcile deliver set must never drop BELOW what the engine considers
    owned. Holds by construction today (`engine_synced_map` globs the same `provides` as `engine_owned_paths`);
    pinned so a future `provides` refactor (e.g. key-filtering groups) can't silently drop a delivered index
    like self-map.md / suites.json without reddening. A SUPERSET guard — `delivered` legitimately carries more
    (fixtures, module manifests). This is the honest, narrow residual the earlier release-cut ownership check
    was reduced to: it proves the reconcile covers the owned surface, NOT that every classification is correct."""

    def test_deliver_set_covers_every_owned_engine_file(self):
        manifests = module_coherence.discover_manifests()
        by_id = {m.get("id"): m for _rel, m in manifests}
        owned_engine = {p for p in module_coherence.engine_owned_paths(manifests) if p.startswith(".engine/")}
        delivered = module_manager.engine_synced_paths(validate.ROOT, by_id, project_retire=False)
        missing = sorted(owned_engine - delivered)
        self.assertEqual(missing, [], f"reconcile deliver set dropped owned engine files: {missing}")


class TestUpgradePreviewImpact(unittest.TestCase):
    """The read-only `plan_upgrade` impact preview (slice 2 of #594) — computed offline via an injected
    release tree, so no test touches the network."""

    def test_offline_impact_names_files_settings_and_data_changes(self):
        with tempfile.TemporaryDirectory() as d:
            live = os.path.join(d, "live")
            os.makedirs(live)
            release = module_manager._build_upgrade_release(os.path.join(d, "release"))
            with module_manager._redirect_root(live):
                module_manager._build_upgrade_fixture(live)
                plan = module_manager.plan_upgrade("v0.2.0", release_tree=release, target_ref="v0.2.0")
        self.assertFalse(plan["refused"])
        self.assertEqual(plan["status"], "update-available")
        # SETTINGS: base swaps its 'oldcache' gitignore for 'newcache' -> one turned off, one turned on
        self.assertIn("oldcache", [w.get("key") for _m, w in plan["wires"]["removed"]])
        self.assertIn("newcache", [w.get("key") for _m, w in plan["wires"]["added"]])
        self.assertEqual(plan["wires"]["updated"], [])
        # STORED-DATA / CONFIG: both migrations surface with their real kinds
        self.assertEqual(sorted(m["kind"] for m in plan["migrations"]), ["config", "data"])
        # RETIRED CAPABILITY: the fixture's 0.2.0 retirement surfaces on the preview path too (not just apply)
        self.assertEqual([r["version"] for r in plan["retired_capabilities"]], ["0.2.0"])
        self.assertIn("one-shot cache reset", plan["retired_capabilities"][0]["description"])
        # a data migration + no vault configured in the fixture -> backup NOT ready (reported, never refused)
        self.assertFalse(plan["backed_up"])
        # FILES: base_tool.py is replaced; the release's new migration files are added
        self.assertIn(".engine/tools/base_tool.py", plan["files"]["replaced"])
        self.assertTrue(any("migrations/" in r for r in plan["files"]["added"]))
        self.assertEqual(plan["target_versions"]["base"], "0.2.0")

    def test_a_same_identity_content_change_is_updated_not_removed_and_added(self):
        # R1 (arch plan-gate): an mcp server is keyed on NAME, so a definition change is an in-place re-apply
        # the preview must call 'updated' — never surface it as both a removal and an addition.
        old = {"base": {"wires": [{"type": "mcp", "name": "engine-x", "definition": {"a": 1}}]}}
        new = {"base": {"wires": [{"type": "mcp", "name": "engine-x", "definition": {"a": 2}}]}}
        delta = module_manager._wiring_delta(old, new)
        self.assertEqual([w["name"] for _m, w in delta["updated"]], ["engine-x"])
        self.assertEqual(delta["added"], [])
        self.assertEqual(delta["removed"], [])

    def test_preview_removed_set_is_exactly_what_the_apply_reverses(self):
        # R1 single-home: the removed set the preview reports IS the set _apply_wiring_deltas would reverse.
        # A same-identity survivor (engine-keep) is not removed; an identity that vanishes (gone) is.
        old = {"base": {"wires": [{"type": "gitignore", "key": "gone", "lines": ["x/"]},
                                  {"type": "mcp", "name": "engine-keep", "definition": {}}]}}
        new = {"base": {"wires": [{"type": "mcp", "name": "engine-keep", "definition": {}},
                                  {"type": "gitignore", "key": "fresh", "lines": ["y/"]}]}}
        self.assertEqual([w.get("key") for _m, w in module_manager._wiring_delta(old, new)["removed"]],
                         ["gone"])

    def test_an_identity_less_removed_wire_is_never_reported_removed(self):
        # R1: a permission wire has no engine identity, so the apply never reverses it -> the preview must
        # not promise a removal that won't happen.
        old = {"base": {"wires": [{"type": "permission", "value": "Bash(*)"}]}}
        new = {"base": {"wires": []}}
        self.assertEqual(module_manager._wiring_delta(old, new)["removed"], [])

    def test_a_tampered_release_escaping_path_refuses_the_preview(self):
        # R4 (risk plan-gate): a release whose provides climb OUT of the engine must REFUSE the preview and
        # enumerate nothing — the same containment wall the apply uses, now on the read-only path.
        with tempfile.TemporaryDirectory() as d:
            live = os.path.join(d, "live")
            os.makedirs(live)
            release = os.path.join(d, "release")
            base = os.path.join(release, ".engine", "modules", "base")
            os.makedirs(base)
            module_manager._write_json(os.path.join(base, "manifest.json"),
                                       {"id": "base", "version": "0.2.0", "status": "required",
                                        "provides": {"tool": ["../sneak.py"]}, "depends": {}})  # climbs out
            with open(os.path.join(d, "sneak.py"), "w", encoding="utf-8") as fh:
                fh.write("# a file the escaping glob would match, OUTSIDE the engine\n")
            with module_manager._redirect_root(live):
                module_manager._build_upgrade_fixture(live)
                plan = module_manager.plan_upgrade("v0.2.0", release_tree=release, target_ref="v0.2.0")
        self.assertTrue(plan["refused"])
        self.assertEqual(plan["status"], "unsafe-release")
        self.assertEqual(plan["files"], {"replaced": [], "added": []})   # nothing host-path was enumerated

    def test_render_speaks_plain_language_with_no_seam_jargon(self):
        # R5 (product plan-gate): the operator-facing render describes settings plainly, never 'wire'/'seam'/
        # 'matcher', and matches the operation doc's "turn settings on/off" framing.
        plan = {"status": "update-available", "current": "0.3.0", "target_ref": "v0.3.1",
                "available": "v0.3.1", "named_ref": None,
                "files": {"replaced": [".engine/tools/x.py"], "added": [".engine/tools/y.py"]},
                "wires": {"added": [("base", {"type": "mcp", "name": "engine-graph"})],
                          "removed": [("base", {"type": "gitignore", "key": "old",
                                                "lines": [".engine/old/"]})],
                          "updated": []},
                "migrations": [{"module_id": "base", "kind": "data", "description": "Reshape stored data."}],
                "backed_up": False}
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            module_manager._render_upgrade_preview(plan)
        text = out.getvalue()
        self.assertIn("Turns on: a connected engine tool (engine-graph)", text)
        self.assertIn("Turns off: an internal engine housekeeping rule", text)   # NOT "data folder" (misread)
        self.assertIn("Changes stored data", text)
        self.assertIn("needs a backup", text)
        self.assertIn("/engine-upgrade", text)
        for jargon in ("wire", "seam", "matcher"):
            self.assertNotIn(jargon, text.lower())

    def test_a_malformed_non_dict_wire_never_crashes_the_preview(self):
        # A tampered release with a non-dict wire entry must not crash the operator's check (the render sits
        # outside upgrade_preview's own guard). _wiring_delta skips it; _describe_wire degrades gracefully.
        self.assertEqual(module_manager._describe_wire("not-a-dict"), "an engine setting")
        delta = module_manager._wiring_delta({"base": {"wires": []}},
                                             {"base": {"wires": ["junk", 7, {"type": "mcp", "name": "ok"}]}})
        self.assertEqual([w["name"] for _m, w in delta["added"]], ["ok"])   # only the well-formed wire

    def test_bare_upgrade_previews_and_never_applies(self):
        # The CLI dispatch: bare `upgrade` renders the preview and NEVER reaches the apply path.
        canned = {"status": "update-available", "current": "0.3.0", "target_ref": "v0.3.1",
                  "available": "v0.3.1", "named_ref": None, "files": {"replaced": [], "added": []},
                  "wires": {"added": [], "removed": [], "updated": []}, "migrations": [], "backed_up": None}
        orig_prev, orig_up = module_manager.upgrade_preview, module_manager.upgrade
        applied = []
        try:
            module_manager.upgrade_preview = lambda ref=None: canned
            module_manager.upgrade = lambda *a, **k: applied.append(True) or {}
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = module_manager.main(["upgrade"])
        finally:
            module_manager.upgrade_preview, module_manager.upgrade = orig_prev, orig_up
        self.assertEqual(code, 0)
        self.assertEqual(applied, [])                       # apply path never entered
        self.assertIn("nothing changed", out.getvalue())


import subprocess   # noqa: E402 — the rollback tests exercise real git in a throwaway repo


def _git(root, *args):
    return subprocess.run(["git", "-C", root, *args], capture_output=True, text=True, check=False)


class TestUpgradeFloorPreflight(unittest.TestCase):
    """#599 Slice 4: the clean-upgrade floor refuses a below-floor engine cleanly (pre-overlay) and fails OPEN
    on absent/dev/unparseable inputs so it never blocks a legitimate update. The refusal names both versions and
    routes to the undo — it promises no unsupported recovery."""

    def _release_tree(self, d, floor):
        tree = os.path.join(d, "rel")
        eng = {"engine_release": "9.9.9", "packages": {"base": "9.9.9"}, "identity": "solo"}
        if floor is not None:
            eng["min_upgradeable_from"] = floor
        module_manager._write_json(os.path.join(tree, ".engine", "engine.json"), eng)
        module_manager._write_json(os.path.join(tree, ".engine", "modules", "base", "manifest.json"),
                                   {"id": "base", "version": "9.9.9", "status": "required",
                                    "provides": {}, "depends": {}})
        return tree

    def test_below_floor_returns_a_refusal_naming_both_versions_and_routing_to_undo(self):
        with tempfile.TemporaryDirectory() as d:
            reason = module_manager._below_floor_refusal("0.2.0", self._release_tree(d, "0.3.2"))
            self.assertIsNotNone(reason)
            self.assertIn("0.2.0", reason)
            self.assertIn("0.3.2", reason)
            self.assertIn("engine is unchanged", reason.lower())
            self.assertIn("undo", reason.lower())                      # routes to the real, built recovery
            self.assertNotIn("re-instantiate", reason.lower())         # never the unsupported recovery
            self.assertNotIn("re-create", reason.lower())

    def test_at_or_above_floor_proceeds(self):
        with tempfile.TemporaryDirectory() as d:
            tree = self._release_tree(d, "0.3.2")
            self.assertIsNone(module_manager._below_floor_refusal("0.3.2", tree))   # equal -> proceed
            self.assertIsNone(module_manager._below_floor_refusal("0.4.0", tree))   # above -> proceed

    def test_absent_floor_or_absent_manifest_proceeds(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(module_manager._below_floor_refusal("0.1.0", self._release_tree(d, None)))  # no floor
            self.assertIsNone(module_manager._below_floor_refusal("0.1.0", d))      # no .engine/engine.json at all

    def test_dev_absent_or_unparseable_deployed_version_proceeds(self):
        # validate._ver_tuple coerces junk to a low tuple; an explicit guard must stop that reading as below-floor.
        with tempfile.TemporaryDirectory() as d:
            tree = self._release_tree(d, "0.3.2")
            for dep in ("0.0.0-dev", "0.0.0", "", None, "garbage", "not.a.version"):
                self.assertIsNone(module_manager._below_floor_refusal(dep, tree), dep)

    def test_non_string_version_values_do_not_crash(self):
        # A JSON-valid but MISTYPED floor (or deployed version) — reachable via a hand-corrupted local manifest or
        # a mis-cut remote release — must fail OPEN, never raise into a caller whose try has no except.
        with tempfile.TemporaryDirectory() as d:
            for floor in (5, ["0.3.2"], {"v": 1}, None):
                self.assertIsNone(module_manager._below_floor_refusal("0.2.0", self._release_tree(d, floor)))
            tree = self._release_tree(d, "0.3.2")
            for dep in (5, 3.1, ["0.2.0"], {"v": 1}):
                self.assertIsNone(module_manager._below_floor_refusal(dep, tree), dep)

    def _deployed_root(self, d, version):
        root = os.path.join(d, f"dep-{version}")
        module_manager._write_json(os.path.join(root, ".engine", "engine.json"),
                                   {"engine_release": version, "packages": {"base": version}, "identity": "solo",
                                    "home_repository": "acme/engine-home"})
        module_manager._write_json(os.path.join(root, ".engine", "modules", "base", "manifest.json"),
                                   {"id": "base", "version": version, "status": "required",
                                    "provides": {}, "depends": {}})
        return root

    def test_upgrade_and_preview_refuse_below_floor_before_any_overlay(self):
        # STANDING integration coverage (plan item 4): the preflight must be WIRED into the mutating upgrade() and
        # the read-only preview, refusing pre-overlay — not just correct as an isolated helper. A refactor that
        # moved the check after the overlay, or dropped it from plan_upgrade, must fail here.
        with tempfile.TemporaryDirectory() as d:
            release = self._release_tree(d, "0.3.2")
            deployed = self._deployed_root(d, "0.2.0")
            with module_manager._redirect_root(deployed):
                prev = module_manager.plan_upgrade(release_tree=release, target_ref="9.9.9", available="9.9.9")
                self.assertTrue(prev.get("refused"))
                self.assertEqual(prev.get("status"), "below-floor")
                up = module_manager.upgrade(release_tree=release, opener=lambda *a, **k: None, backup=None)
                self.assertTrue(up.get("refused"))
                self.assertNotEqual(up.get("applied"), True)          # refused BEFORE any overlay/write
                self.assertIn("0.3.2", up.get("reason") or "")
                # an at/above-floor deployed engine is NOT refused by the floor
                above = self._deployed_root(d, "0.3.2")
            with module_manager._redirect_root(above):
                ok = module_manager.plan_upgrade(release_tree=release, target_ref="9.9.9", available="9.9.9")
                self.assertNotEqual(ok.get("status"), "below-floor")


def _init_repo(root):
    """A throwaway git repo with the fixture engine committed as the pre-update baseline (branch `main`)."""
    _git(root, "init", "-b", "main")
    _git(root, "-c", "user.email=t@t", "-c", "user.name=t", "add", "-A")
    _git(root, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", "baseline")


def _stage_a_stalled_update(root):
    """Reproduce the #594-shape staged/stalled upgrade ON DISK, uncommitted: a release that ADDS a new
    provides file + a new wire to the present `base` module (not just an edited file) — the overlay overwrote
    base's manifest to declare an extra tool + a new gitignore wire, wrote that new tool, changed the existing
    tool, and bumped engine.json. Nothing is committed, so this is exactly a stall's working tree."""
    eng = os.path.join(root, ".engine")
    module_manager._write_json(
        os.path.join(eng, "modules", "base", "manifest.json"),
        {"id": "base", "version": "0.2.0", "status": "required",
         "provides": {"tool": [".engine/tools/base_tool.py", ".engine/tools/base_extra.py"]},
         "depends": {}, "migrations": {},
         "wires": [{"type": "gitignore", "key": "oldcache", "lines": [".engine/base/.oldcache/"]},
                   {"type": "gitignore", "key": "newcache", "lines": [".engine/base/.newcache/"]}]})
    with open(os.path.join(eng, "tools", "base_tool.py"), "w") as fh:
        fh.write("# base v2\n")                                   # an existing overlay-code file, changed
    with open(os.path.join(eng, "tools", "base_extra.py"), "w") as fh:
        fh.write("# new tool the release added\n")                # a NEW provides file (the #594 shape)
    module_manager._write_json(
        os.path.join(eng, "engine.json"),
        {"engine_release": "0.2.0", "packages": {"base": "0.2.0"}, "identity": "solo",
         "home_repository": "acme/engine-home"})                  # the version bump


class TestRollback(unittest.TestCase):
    """The `rollback` undo: the staged-update signal, the foreign-work guard, the recovery-point-then-discard
    sequence on a real temp git repo (the #594 new-file shape), the restore-then-detect ordering + consent/
    no-override on the memory leg, and the confirm-gated CLI."""

    def _staged_repo(self, d):
        live = os.path.join(d, "live")
        os.makedirs(live)
        with module_manager._redirect_root(live):   # _build_upgrade_fixture applies a wire via the redirected paths
            module_manager._build_upgrade_fixture(live)
        _init_repo(live)                        # commit the pre-update baseline
        _stage_a_stalled_update(live)           # then dirty the tree with a staged update
        return live

    def test_staged_update_is_detected_by_dirty_overlay_code_not_coherence(self):
        with tempfile.TemporaryDirectory() as d:
            live = self._staged_repo(d)
            with module_manager._redirect_root(live):
                self.assertTrue(module_manager._staged_upgrade_dirty())
                self.assertEqual(module_manager._diagnose_undo()["state"], "staged")

    def test_a_coherence_green_but_uncommitted_overlay_edit_is_still_detected_as_staged(self):
        # RC-S3: the signal must NOT key on coherence — a content-only edit to an overlay-code file leaves
        # coherence green (it checks ownership + wiring, not file bytes) yet is a staged/uncommitted state.
        with tempfile.TemporaryDirectory() as d:
            live = os.path.join(d, "live")
            os.makedirs(live)
            with module_manager._redirect_root(live):
                module_manager._build_upgrade_fixture(live)
            _init_repo(live)                                    # commit a coherent baseline
            with open(os.path.join(live, ".engine", "tools", "base_tool.py"), "w") as fh:
                fh.write("# base v0 — a byte changed, nothing else\n")   # content-only, uncommitted
            with module_manager._redirect_root(live):
                self.assertFalse([f for f in module_coherence.check_coherence()
                                  if f.get("severity") == "hard"])       # coherence is GREEN
                self.assertTrue(module_manager._staged_upgrade_dirty())  # yet the staged signal still fires

    def test_discard_saves_a_recovery_point_then_restores_the_tree_to_before_the_update(self):
        with tempfile.TemporaryDirectory() as d:
            live = self._staged_repo(d)
            calls = {"resync": 0}
            with module_manager._redirect_root(live):
                res = module_manager.rollback(confirm=True, resync=lambda: calls.__setitem__("resync", 1) or True,
                                              transport=None)
            self.assertTrue(res.get("undone"))
            self.assertEqual(calls["resync"], 1)                 # the runtime rebuild seam was called
            self.assertTrue((res.get("recovery_point") or "").startswith("engine-rescue/"))
            # the tree is clean again, the added file is gone, and the changed file/engine.json are back
            self.assertEqual(_git(live, "status", "--porcelain").stdout.strip(), "")
            self.assertFalse(os.path.exists(os.path.join(live, ".engine", "tools", "base_extra.py")))
            with open(os.path.join(live, ".engine", "tools", "base_tool.py")) as fh:
                self.assertEqual(fh.read(), "# base v0\n")
            eng = module_manager.validate.load_json(os.path.join(live, ".engine", "engine.json"))
            self.assertEqual(eng["engine_release"], "0.0.0")     # engine.json reverted -> data undo can now fire
            # the recovery point still holds the discarded update (nothing lost)
            self.assertIn("engine-rescue/", _git(live, "branch").stdout)

    def test_discard_refuses_when_the_operator_has_their_own_unsaved_work(self):
        with tempfile.TemporaryDirectory() as d:
            live = self._staged_repo(d)
            with open(os.path.join(live, "my_notes.txt"), "w") as fh:   # foreign, outside the footprint
                fh.write("my own work\n")
            with module_manager._redirect_root(live):
                res = module_manager.rollback(confirm=True, resync=lambda: True, transport=None)
            self.assertTrue(res.get("refused"))
            self.assertIn("my_notes.txt", res.get("your_changes") or [])
            # nothing was undone or rescued — the staged update and the operator's file are both still there
            self.assertTrue(os.path.exists(os.path.join(live, ".engine", "tools", "base_extra.py")))
            self.assertTrue(os.path.exists(os.path.join(live, "my_notes.txt")))
            self.assertNotIn("engine-rescue/", _git(live, "branch").stdout)

    def test_footprint_covers_the_floor_files_and_wiring_targets_the_guard_would_else_miss(self):
        # RC-B1: engine_owned_paths omits these; the guard would false-refuse a real stall without them.
        with tempfile.TemporaryDirectory() as d:
            live = self._staged_repo(d)
            with module_manager._redirect_root(live):
                fp = module_manager._upgrade_footprint()
            for must in ("CLAUDE.md", "AGENTS.md", ".github/CODEOWNERS", module_coherence.ENGINE_MANIFEST_REL):
                self.assertIn(must, fp)
            for target in module_coherence.WIRING_TARGETS.values():
                self.assertIn(target, fp)

    def test_footprint_covers_the_reconcile_delivered_fixtures(self):
        # #599 Defect A: the Slice-2a reconcile delivers the `.engine/_fixtures/**` namespace (untracked adds),
        # but the pre-Slice-2a footprint knew only the overlay copy-map — so a delivered fixture read as the
        # operator's OWN work and `rollback` refused. Sourcing the footprint from the deliver set closes that.
        # Falsifiable: the old `overlay_replace_paths()` footprint carried no fixtures, so this would fail on it.
        fp = module_manager._upgrade_footprint()          # against the real construction tree (has fixtures)
        self.assertTrue(any(p.startswith(".engine/_fixtures/") for p in fp),
                        "the reconcile deliver set (.engine/_fixtures/**) must be inside the rollback footprint")

    def test_undo_does_not_false_refuse_a_reconcile_renamed_away_tracked_file(self):
        # #599 / arch-B1: the reconcile os.removes a renamed-away OLD path (no longer in the post-overlay
        # manifest); a rename that also rewrote the file shows as a staged 'D', not an 'R'. A staged tracked-file
        # deletion is losslessly reversible (the discard's branch checkout restores it, the recovery point commits
        # it first), so the undo must NOT read it as the operator's own uncommitted work and refuse.
        # Falsifiable: without excluding tracked deletions, the renamed-away path lands in `foreign` -> refusal.
        with tempfile.TemporaryDirectory() as d:
            live = os.path.join(d, "live")
            os.makedirs(live)
            with module_manager._redirect_root(live):
                module_manager._build_upgrade_fixture(live)
            renamed_away = ".engine/tools/base_renamed_away.py"        # an engine file the release renames away
            with open(os.path.join(live, renamed_away), "w") as fh:
                fh.write("# old name, tracked in the baseline; the release renames it away\n")
            _init_repo(live)                                          # commit the baseline WITH the old file
            _stage_a_stalled_update(live)                             # the release's manifest no longer names it
            # RENAME + REWRITE: the release renames it to a NEW, dissimilar successor it DOES provide. Because the
            # content differs, git does not collapse the pair into an 'R' (which would land on the in-footprint new
            # path and pass anyway) — it shows a bare 'D' (old) + 'A' (new): the exact shape the delete-side fix
            # must handle. The new path is provided, so it is in the footprint; the old path is not.
            successor = ".engine/tools/base_renamed_new.py"
            man_path = os.path.join(live, ".engine", "modules", "base", "manifest.json")
            man = validate.load_json(man_path)
            man["provides"]["tool"].append(successor)
            module_manager._write_json(man_path, man)
            with open(os.path.join(live, successor), "w") as fh:
                fh.write("# the rewritten successor: entirely different body, so git sees D+A, not a rename R\n")
            os.remove(os.path.join(live, renamed_away))               # the reconcile's delete leg (the old path)
            _git(live, "-c", "user.email=t@t", "-c", "user.name=t", "add", "-A")
            with module_manager._redirect_root(live):
                self.assertIn(renamed_away, module_manager._git_deleted_paths(live))       # the delete is seen
                self.assertNotIn(renamed_away, module_manager._upgrade_footprint())        # and it's renamed away
                res = module_manager.rollback(confirm=True, resync=lambda: True, transport=None)
        self.assertTrue(res.get("undone"), res)                       # the undo proceeds, not a false refusal
        self.assertNotEqual(res.get("refused"), True, res)

    def test_memory_leg_restores_with_consent_and_never_override_after_the_code_is_back(self):
        # The staged discard's step (f): once engine.json is reverted, put the pre-update memory back — with
        # the operator's confirm standing in for consent, and NEVER override (the resurrection guard stays on).
        from memory import restore_vault as _rv
        import memory as _memory
        captured = {}
        saved_detect, saved_restore = _rv.detect_migration_revert, _memory.restore_pre_migration

        def _detect_only_after_revert(**k):
            # The stub is ORDERING-SENSITIVE: it hands back a tag only once engine.json is back at the old
            # version — so if the code consulted it BEFORE reverting the tree, no restore would fire and this
            # test would fail. This pins step (f) running after step (d).
            eng = module_manager.validate.load_json(
                os.path.join(module_manager.validate.ROOT, ".engine", "engine.json")) or {}
            return {"tag": "engine-snapshot/ns/base@0.2.0"} if eng.get("engine_release") == "0.0.0" else None
        _rv.detect_migration_revert = _detect_only_after_revert
        _memory.restore_pre_migration = lambda **k: captured.update(k) or {"ok": True, "message": "put back"}
        try:
            with tempfile.TemporaryDirectory() as d:
                live = self._staged_repo(d)
                with module_manager._redirect_root(live):
                    res = module_manager.rollback(confirm=True, resync=lambda: True, transport="FAKE")
        finally:
            _rv.detect_migration_revert, _memory.restore_pre_migration = saved_detect, saved_restore
        self.assertTrue(res.get("undone"))
        self.assertEqual(captured.get("consent"), "y")
        self.assertEqual(captured.get("tag"), "engine-snapshot/ns/base@0.2.0")
        self.assertEqual(captured.get("transport"), "FAKE")
        self.assertNotEqual(captured.get("override"), True)      # resurrection guard preserved
        self.assertTrue(res.get("restored"))

    def test_memory_ahead_state_restores_the_pre_update_copy_without_a_discard(self):
        from memory import restore_vault as _rv
        import memory as _memory
        captured = {}
        saved_detect, saved_restore = _rv.detect_migration_revert, _memory.restore_pre_migration
        # a clean tree (no staged update) but the store is ahead of the code -> state 2
        _rv.detect_migration_revert = lambda **k: {"tag": "engine-snapshot/ns/base@0.2.0"}
        _memory.restore_pre_migration = lambda **k: captured.update(k) or {"ok": True, "message": "put back"}
        try:
            with tempfile.TemporaryDirectory() as d:
                live = os.path.join(d, "live")
                os.makedirs(live)
                with module_manager._redirect_root(live):
                    module_manager._build_upgrade_fixture(live)
                _init_repo(live)                                # clean tree, nothing staged
                with module_manager._redirect_root(live):
                    self.assertEqual(module_manager._diagnose_undo()["state"], "memory-ahead")
                    res = module_manager.rollback(confirm=True, resync=lambda: True, transport="FAKE")
        finally:
            _rv.detect_migration_revert, _memory.restore_pre_migration = saved_detect, saved_restore
        self.assertTrue(res.get("restored"))
        self.assertEqual(captured.get("consent"), "y")
        self.assertNotEqual(captured.get("override"), True)

    def test_bare_rollback_previews_and_changes_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            live = self._staged_repo(d)
            with module_manager._redirect_root(live):
                diag = module_manager.rollback(confirm=False)     # no confirm -> read-only diagnosis
            self.assertEqual(diag["state"], "staged")
            self.assertTrue(os.path.exists(os.path.join(live, ".engine", "tools", "base_extra.py")))  # untouched
            self.assertNotIn("engine-rescue/", _git(live, "branch").stdout)

    def test_the_rollback_falsification_demo_passes(self):
        # Runs the maintainer-runnable demo end-to-end (the real undo reverts a staged update; removing the
        # switch-back leaves it un-undone). Importing it here is what makes it TRAVEL with the engine.
        import demo_594_rollback_discard as demo
        import quiet_call
        self.assertEqual(quiet_call.run(demo.main), 0)

    def test_cli_rollback_gate(self):
        with tempfile.TemporaryDirectory() as d:
            live = self._staged_repo(d)
            with module_manager._redirect_root(live):
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    self.assertEqual(module_manager.main(["rollback"]), 0)          # bare -> preview, exit 0
                self.assertIn("undo", out.getvalue())
                err = io.StringIO()
                with contextlib.redirect_stderr(err):
                    self.assertEqual(module_manager.main(["rollback", "--bogus"]), 2)  # unknown flag -> exit 2
                self.assertIn("CONFIG ERROR", err.getvalue())
                self.assertEqual(module_manager.main(["rollback", "--help"]), 0)    # help -> usage, exit 0
                # still nothing changed by any of the read-only calls
                self.assertNotIn("engine-rescue/", _git(live, "branch").stdout)


class TestOpenUpgradePrDiagnostics(unittest.TestCase):
    """#672: the shared PR opener surfaces a DIAGNOSABLE failure — GitHub's real reason (the nested
    errors[].message, not just the generic top-level 'Validation Failed'), the resolved repo/base/head, and a
    concrete manual recovery path — and never the auth token; on success it returns the parsed pull request. This
    real network path has no other coverage: every arrival/upgrade test injects a fake `opener`."""

    def _run(self, urlopen_impl):
        # Drive the REAL _open_upgrade_pr with only its two boundaries faked — git (subprocess.run) and the
        # network (urllib.request.urlopen) — with scoped patches that auto-revert. repo=/token= are passed so
        # boot is never consulted.
        from unittest import mock
        with mock.patch("subprocess.run", return_value=None), \
             mock.patch("urllib.request.urlopen", side_effect=urlopen_impl):
            return module_manager._open_upgrade_pr(branch="engine-arrival", title="Feature: add the engine",
                                                   body="body", repo="acme/widget", token="secret-token-xyz")

    def test_422_raises_a_diagnosable_error_without_leaking_the_token(self):
        import urllib.error
        body = json.dumps({"message": "Validation Failed",
                           "errors": [{"resource": "PullRequest",
                                       "message": "A pull request already exists for acme:engine-arrival."}]}).encode()

        def raise_422(req, timeout=None):
            raise urllib.error.HTTPError("https://api.github.com/repos/acme/widget/pulls", 422,
                                         "Unprocessable Entity", {}, io.BytesIO(body))
        with self.assertRaises(RuntimeError) as ctx:
            self._run(raise_422)
        msg = str(ctx.exception)
        self.assertIn("A pull request already exists", msg)   # GitHub's NESTED reason, not just "Validation Failed"
        self.assertIn("422", msg)
        self.assertIn("acme/widget", msg)                     # the resolved repo
        self.assertIn("--head engine-arrival", msg)           # the resolved head
        self.assertIn("--base", msg)                          # the resolved base
        self.assertIn("gh pr create", msg)                    # a concrete manual recovery path
        self.assertIn("was pushed", msg)                      # the POST-failure case: the branch IS pushed
        self.assertNotIn("secret-token-xyz", msg)             # the auth token NEVER surfaces
        self.assertNotIn("Bearer", msg)
        self.assertNotIn("..", msg)                           # no double-period artifact after GitHub's reason

    def test_unreachable_github_raises_a_diagnosable_error(self):
        import urllib.error

        def unreachable(req, timeout=None):
            raise urllib.error.URLError("name resolution failed")
        with self.assertRaises(RuntimeError) as ctx:
            self._run(unreachable)
        msg = str(ctx.exception)
        self.assertIn("could not be reached", msg)
        self.assertIn("was pushed", msg)                      # transport failure is post-push, like the 422 case
        self.assertIn("--head engine-arrival", msg)
        self.assertNotIn("secret-token-xyz", msg)

    def test_success_returns_the_parsed_pull_request(self):
        from unittest import mock
        resp = mock.MagicMock()
        resp.read.return_value = json.dumps({"number": 7,
                                             "html_url": "https://github.com/acme/widget/pull/7"}).encode()
        resp.__enter__.return_value = resp                    # the code uses `with urlopen(...) as resp:`
        resp.__exit__.return_value = False

        def ok(req, timeout=None):
            return resp
        out = self._run(ok)
        self.assertEqual(out["number"], 7)

    def test_a_git_step_failure_says_the_branch_is_NOT_pushed(self):
        # The failure-before-the-POST case (e.g. `checkout -b` colliding with a leftover branch): the recovery is
        # the OPPOSITE of the POST case — the branch is not pushed, so re-running after clearing the collision is
        # the fix, NOT opening a PR from a branch that isn't there. git's own stderr is surfaced.
        import subprocess
        from unittest import mock

        def boom(args, **kw):
            raise subprocess.CalledProcessError(128, args,
                                                stderr=b"fatal: a branch named 'engine-arrival' already exists\n")
        with mock.patch("subprocess.run", side_effect=boom), \
             mock.patch("urllib.request.urlopen", side_effect=AssertionError("POST must not be reached")):
            with self.assertRaises(RuntimeError) as ctx:
                module_manager._open_upgrade_pr(branch="engine-arrival", title="t", body="b",
                                                repo="acme/widget", token="secret-token-xyz")
        msg = str(ctx.exception)
        self.assertIn("was not created", msg)                 # the CREATE step failed → no branch yet
        self.assertIn("already exists", msg)                  # surfaces git's real stderr
        self.assertIn("git branch -D engine-arrival", msg)    # delete is safe ONLY for the checkout collision
        self.assertNotIn("was pushed but", msg)               # never the POST-case "branch was pushed" claim
        self.assertNotIn("secret-token-xyz", msg)

    def test_a_push_failure_does_NOT_tell_the_operator_to_delete_the_branch(self):
        # The far more likely git failure (auth/network/branch-protection at `git push`): checkout/add/commit
        # already succeeded, so the branch holds the arrival's committed work. The recovery must NOT say
        # `git branch -D` (that would discard the work) — it must say the branch was created, keep it, and finish
        # by hand.
        import subprocess
        from unittest import mock

        def fail_on_push(args, **kw):
            if "push" in args:
                raise subprocess.CalledProcessError(1, args, stderr=b"fatal: Authentication failed\n")
            return None                                        # checkout/add/commit succeed
        with mock.patch("subprocess.run", side_effect=fail_on_push), \
             mock.patch("urllib.request.urlopen", side_effect=AssertionError("POST must not be reached")):
            with self.assertRaises(RuntimeError) as ctx:
                module_manager._open_upgrade_pr(branch="engine-arrival", title="t", body="b",
                                                repo="acme/widget", token="secret-token-xyz")
        msg = str(ctx.exception)
        self.assertIn("git push", msg)                        # names the failed step
        self.assertIn("Authentication failed", msg)           # surfaces git's real stderr
        self.assertIn("was created", msg)                     # tells the operator the branch exists
        self.assertIn("do not", msg.lower())                  # and NOT to delete it
        self.assertNotIn("git branch -D", msg)                # the destructive advice is absent for a push failure
        self.assertIn("gh pr create", msg)                    # the finish-by-hand recovery
        self.assertNotIn("secret-token-xyz", msg)

    def test_github_error_detail_ignores_a_non_list_errors_field(self):
        # The helper must NEVER raise (it explains an HTTP failure); a malformed `errors` that is not a list must
        # not turn a TypeError loose in place of the diagnostic.
        import urllib.error
        for shape in (5, True, "oops", {"message": "x"}):
            body = json.dumps({"message": "Validation Failed", "errors": shape}).encode()
            exc = urllib.error.HTTPError("u", 422, "x", {}, io.BytesIO(body))
            detail = module_manager._github_error_detail(exc)   # must not raise
            self.assertIn("Validation Failed", detail)

    def test_github_error_detail_on_empty_and_non_json_bodies(self):
        import urllib.error
        # empty-but-readable body -> nothing to add
        self.assertEqual(
            module_manager._github_error_detail(urllib.error.HTTPError("u", 500, "x", {}, io.BytesIO(b""))), "")
        # non-JSON body -> a bounded slice of the raw text, still useful
        detail = module_manager._github_error_detail(
            urllib.error.HTTPError("u", 502, "x", {}, io.BytesIO(b"<html>Bad Gateway</html>")))
        self.assertIn("Bad Gateway", detail)
        self.assertLessEqual(len(detail), 300)
        # valid JSON but not a dict -> nothing usable, no raise
        self.assertEqual(
            module_manager._github_error_detail(
                urllib.error.HTTPError("u", 422, "x", {}, io.BytesIO(b"[1, 2]"))), "")

    def test_github_error_detail_joins_top_level_and_nested_messages(self):
        import urllib.error
        body = json.dumps({"message": "Validation Failed",
                           "errors": [{"message": "No commits between main and engine-arrival"}]}).encode()
        exc = urllib.error.HTTPError("u", 422, "x", {}, io.BytesIO(body))
        detail = module_manager._github_error_detail(exc)
        self.assertIn("Validation Failed", detail)
        self.assertIn("No commits between main and engine-arrival", detail)

    def test_github_error_detail_is_empty_and_safe_on_unreadable_body(self):
        # A diagnostic helper must never raise; an unreadable/empty body yields "" so the HTTP status stands alone.
        import urllib.error
        exc = urllib.error.HTTPError("u", 500, "Server Error", {}, None)   # fp=None -> a read would raise
        self.assertEqual(module_manager._github_error_detail(exc), "")


if __name__ == "__main__":
    unittest.main()
