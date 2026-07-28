"""Tests for the first-run reference-closure gate (issue #150).

This test is PERMANENT (it must survive first-run retirement), so it imports only `validate` and the check
module — never the retired `instantiator`/`test_instantiator` (that is the very defect the check exists to
prevent; importing them here would make this test the next dangler). All scenarios run against throwaway
fixture trees with a FAKE removed-asset set, plus one assertion that the real committed repo is closed.

Run: uv run --directory .engine --frozen -- python -m unittest discover -s tools -p 'test_*.py' -b
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import first_run_reference_closure_check as frc  # noqa: E402
import demo_first_run_reference_closure as demo  # noqa: E402
import quiet_call  # noqa: E402


def _build(root, *, files, survivors, create_removed_py=True, directories=None):
    """A throwaway tree: a manifest naming a FAKE removed set, the removed `.py` modules present on disk (unless
    create_removed_py is False, modelling the post-setup tree), and the given surviving `.engine/tools` files."""
    prov = os.path.join(root, ".engine", "provisioning")
    tools = os.path.join(root, ".engine", "tools")
    os.makedirs(prov, exist_ok=True)
    os.makedirs(tools, exist_ok=True)
    with open(os.path.join(prov, "first-run-assets.json"), "w", encoding="utf-8") as fh:
        json.dump({"files": files, "directories": directories or []}, fh)
    if create_removed_py:
        for rel in files:
            if rel.endswith(".py"):
                p = os.path.join(root, rel)
                os.makedirs(os.path.dirname(p), exist_ok=True)
                open(p, "w", encoding="utf-8").close()
    for name, content in survivors.items():
        p = os.path.join(tools, name)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(content)


class TestRealRepositoryIsClosed(unittest.TestCase):
    def test_no_surviving_file_references_a_removed_first_run_asset(self):
        # The load-bearing assertion: after this PR the committed repo is reference-closed.
        self.assertEqual(frc.check(), [])

    def test_main_emits_a_json_array_and_exits_zero(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = frc.main()
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(buf.getvalue()), [])


class TestClosureScan(unittest.TestCase):
    def test_a_surviving_import_of_a_removed_module_is_one_hard_finding(self):
        with tempfile.TemporaryDirectory() as d:
            _build(d, files=[".engine/tools/removed_mod.py"],
                   survivors={"survivor.py": "import removed_mod\n"})
            findings = frc.check(d)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["severity"], "hard")
        self.assertEqual(findings[0]["location"]["file"], ".engine/tools/survivor.py")
        self.assertIn("survivor.py", findings[0]["message"])

    def test_from_import_and_importlib_and_dunder_import_are_all_caught(self):
        for src in ("from removed_mod import thing\n",
                    "import importlib\nimportlib.import_module('removed_mod')\n",
                    "__import__('removed_mod')\n"):
            with tempfile.TemporaryDirectory() as d:
                _build(d, files=[".engine/tools/removed_mod.py"], survivors={"s.py": src})
                self.assertEqual(len(frc.check(d)), 1, f"not caught: {src!r}")

    def test_a_literal_read_of_a_removed_path_is_caught(self):
        with tempfile.TemporaryDirectory() as d:
            _build(d, files=[".engine/tools/removed_mod.py", ".engine/templates/gone.md"],
                   survivors={"reader.py": 'open(".engine/templates/gone.md")\n'})
            findings = frc.check(d)
        self.assertEqual(len(findings), 1)
        self.assertIn("reads or runs", findings[0]["message"])

    def test_a_regenerated_retired_asset_is_not_flagged_but_a_gone_for_good_one_is(self):
        # #404 carve-out: a removed path the engine rewrites at runtime (named in
        # _REGENERATED_RETIRED_ASSETS — the audit digest) is not permanently gone, so a surviving literal
        # reference to it is not a dangling reference and the path leg skips it. But the carve-out is a NARROW
        # named set, NOT "anything provided": a removed path NOT in that set (e.g. a construction-only asset
        # that is gone for good) is still caught. This pins both directions so the carve-out can't silently
        # widen to gone-for-good paths.
        with tempfile.TemporaryDirectory() as d:
            _build(d, files=[".engine/tools/removed_mod.py", ".engine/audits/regen.md",
                             ".engine/operations/gone-for-good.md"],
                   survivors={"reader.py": ('open(".engine/audits/regen.md")\n'
                                            'open(".engine/operations/gone-for-good.md")\n')})
            with mock.patch.object(frc, "_REGENERATED_RETIRED_ASSETS", frozenset({".engine/audits/regen.md"})):
                findings = frc.check(d)
            # the regenerated asset is exempt; the gone-for-good one is still one hard finding
            self.assertEqual(len(findings), 1, "only the gone-for-good removed path should be flagged")
            self.assertIn("gone-for-good.md", findings[0]["message"])
            self.assertNotIn("regen.md", findings[0]["message"])

    def test_the_carve_out_is_path_leg_only_a_regenerated_import_still_fails_closed(self):
        # The carve-out never covers the import leg: a genuine `import` of a removed module dangles at import
        # time regardless of the allowlist, so it must still be one hard finding.
        with tempfile.TemporaryDirectory() as d:
            _build(d, files=[".engine/tools/removed_mod.py"], survivors={"s.py": "import removed_mod\n"})
            with mock.patch.object(frc, "_REGENERATED_RETIRED_ASSETS", frozenset({".engine/tools/removed_mod.py"})):
                self.assertEqual(len(frc.check(d)), 1, "a regenerated-but-imported removed module must still fail closed")

    def test_the_real_allowlist_holds_only_the_regenerated_digest(self):
        # Guards the allowlist itself: the audit digest is the sole regenerated-retired asset today. This exact
        # equality proves no other retired path (e.g. `.engine/operations/first-run.md`, provided by core but
        # gone for good) is exempt — without naming one as a bare string literal, which would itself trip this
        # very closure check (an exact-path constant in a surviving file).
        self.assertEqual(frc._REGENERATED_RETIRED_ASSETS, frozenset({".engine/audits/audit-digest.md"}))

    def test_a_prose_mention_is_not_flagged(self):
        # module_catalog.py mentions `instantiator.py` in a docstring; an exact-path-only match must not flag it.
        with tempfile.TemporaryDirectory() as d:
            _build(d, files=[".engine/tools/removed_mod.py"],
                   survivors={"prose.py": '"""See removed_mod and .engine/tools/removed_mod for context."""\n'})
            self.assertEqual(frc.check(d), [])

    def test_a_removed_file_is_excluded_from_the_survivor_scan(self):
        # A removed file may reference another removed asset (they go together); it must not be scanned.
        with tempfile.TemporaryDirectory() as d:
            _build(d, files=[".engine/tools/removed_mod.py", ".engine/tools/removed_test.py"],
                   survivors={})
            # plant the would-be dangler AS a removed file (not a survivor)
            with open(os.path.join(d, ".engine/tools/removed_test.py"), "w", encoding="utf-8") as fh:
                fh.write("import removed_mod\n")
            self.assertEqual(frc.check(d), [])

    def test_recurses_into_tool_subpackages(self):
        with tempfile.TemporaryDirectory() as d:
            _build(d, files=[".engine/tools/removed_mod.py"], survivors={})
            sub = os.path.join(d, ".engine", "tools", "memorysub")
            os.makedirs(sub)
            with open(os.path.join(sub, "test_deep.py"), "w", encoding="utf-8") as fh:
                fh.write("import removed_mod\n")
            findings = frc.check(d)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["location"]["file"], ".engine/tools/memorysub/test_deep.py")

    def test_noop_when_the_machinery_is_already_removed(self):
        with tempfile.TemporaryDirectory() as d:
            _build(d, files=[".engine/tools/removed_mod.py"],
                   survivors={"survivor.py": "import removed_mod\n"}, create_removed_py=False)
            self.assertEqual(frc.check(d), [])  # post-setup tree: machinery gone, nothing to close over

    def test_fails_closed_when_the_manifest_is_missing(self):
        # The removed-files list is PERMANENT (it never self-retires), so a missing one is a fault, never a
        # legitimate state — the check must fail closed, not silently pass (a guard can never silently pass).
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, ".engine", "tools"))
            findings = frc.check(d)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["severity"], "hard")
        self.assertEqual(findings[0]["location"]["file"],
                         os.path.join(".engine", "provisioning", "first-run-assets.json"))

    def test_fails_closed_when_the_manifest_is_malformed(self):
        with tempfile.TemporaryDirectory() as d:
            prov = os.path.join(d, ".engine", "provisioning")
            os.makedirs(prov)
            os.makedirs(os.path.join(d, ".engine", "tools"))
            with open(os.path.join(prov, "first-run-assets.json"), "w", encoding="utf-8") as fh:
                fh.write("{ this is not valid")           # unparseable → cannot compute the removed set
            findings = frc.check(d)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["severity"], "hard")

    def test_fails_closed_when_the_manifest_is_structurally_malformed(self):
        # Present and valid JSON but the wrong shape — a JSON array, the `files` key absent, or `files` not a list.
        # The check can't compute the removed set, so it fails closed with the plain-language finding, never a raw
        # traceback and never a green pass (which would lean entirely on the sibling schema check).
        for bad in ("[]", '{"directories": []}', '{"files": 5}', '{"files": "nope"}', '{"files": null}'):
            with self.subTest(bad=bad), tempfile.TemporaryDirectory() as d:
                prov = os.path.join(d, ".engine", "provisioning")
                os.makedirs(prov)
                os.makedirs(os.path.join(d, ".engine", "tools"))
                with open(os.path.join(prov, "first-run-assets.json"), "w", encoding="utf-8") as fh:
                    fh.write(bad)
                findings = frc.check(d)
                self.assertEqual(len(findings), 1, bad)
                self.assertEqual(findings[0]["severity"], "hard", bad)

    def test_empty_files_list_is_a_valid_nothing_to_remove_set_not_a_fault(self):
        # A present, well-formed manifest with an empty `files` list is a legitimate 'nothing to remove' state,
        # not a fault — it must stay green (only a missing/unreadable/mis-shaped manifest fails closed).
        with tempfile.TemporaryDirectory() as d:
            prov = os.path.join(d, ".engine", "provisioning")
            os.makedirs(prov)
            os.makedirs(os.path.join(d, ".engine", "tools"))
            with open(os.path.join(prov, "first-run-assets.json"), "w", encoding="utf-8") as fh:
                json.dump({"files": [], "directories": []}, fh)
            self.assertEqual(frc.check(d), [])


class TestFindingIsPlainLanguage(unittest.TestCase):
    def test_the_message_states_the_consequence_without_engineer_shorthand(self):
        with tempfile.TemporaryDirectory() as d:
            _build(d, files=[".engine/tools/removed_mod.py"],
                   survivors={"survivor.py": "import removed_mod\n"})
            msg = frc.check(d)[0]["message"].lower()
        self.assertIn("first", msg)  # names the adopter's first check / new project
        for shorthand in ("reference-closure", "retire-set", "import graph", "collection-time",
                          "closed under", "static"):
            self.assertNotIn(shorthand, msg, f"operator shorthand leaked: {shorthand!r}")

    def test_the_manifest_fault_message_states_the_consequence_without_engineer_shorthand(self):
        # The missing/damaged-list finding has a DIFFERENT remedy (restore the file in this PR), so it carries its
        # own plain-language copy — held to the same no-shorthand bar as the dangling-reference message above.
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, ".engine", "tools"))
            msg = frc.check(d)[0]["message"].lower()
        self.assertIn("first", msg)  # names the new project's first run
        for shorthand in ("manifest", "reference-closure", "retire-set", "fail-closed", "fail closed",
                          "unreadable", "closed under", "import graph"):
            self.assertNotIn(shorthand, msg, f"operator shorthand leaked: {shorthand!r}")


class TestDemoRunsGreen(unittest.TestCase):
    def test_demo_passes(self):
        # Route through quiet_call.run so the demo's walkthrough is captured at the call site — a direct
        # demo.main() here would flood the run without `-b` (the papercut quiet_call exists to end).
        self.assertEqual(quiet_call.run(demo.main), 0)


if __name__ == "__main__":
    unittest.main()
