"""Tests for engine/check/manifest-write-funnel — the mechanical funnel floor (#923).

Run: uv run --directory .engine --frozen -- python tools/selftest.py
"""
from __future__ import annotations
import os
import sys
import tempfile
import textwrap
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import manifest_write_funnel_check as check  # noqa: E402


def _scan(body: str) -> list:
    """Write `body` as a tool under a throwaway root and run the check over that root."""
    with tempfile.TemporaryDirectory() as root:
        tools = os.path.join(root, ".engine", "tools")
        os.makedirs(tools)
        with open(os.path.join(tools, "probe.py"), "w", encoding="utf-8") as fh:
            fh.write("import os\nimport json\nimport tempfile\nimport validate\n" + textwrap.dedent(body))
        return check.check(root)


class TestBitesBypasses(unittest.TestCase):
    """Every shape that writes the deployed manifest outside the funnel must be flagged."""

    def test_the_historical_bug_shape_is_flagged(self):
        # the exact pre-#923 shape: the generic writer + the path helper, no funnel
        f = _scan("def _bump(engine):\n    _write_json(_engine_manifest_path(), engine)\n")
        self.assertEqual(len(f), 1)
        self.assertIn("does not route through the guarded write funnel", f[0]["message"])

    def test_plain_open_write_through_the_helper_is_flagged(self):
        f = _scan('def _evil():\n    with open(_engine_manifest_path(), "w") as fh:\n        fh.write("{}")\n')
        self.assertEqual(len(f), 1)

    def test_os_replace_onto_the_bare_literal_against_root_is_flagged(self):
        f = _scan('def _evil(tmp):\n    os.replace(tmp, os.path.join(validate.ROOT, ".engine", "engine.json"))\n')
        self.assertEqual(len(f), 1)

    def test_the_engine_json_path_helper_variant_is_flagged(self):
        f = _scan('def _evil():\n    with open(_engine_json_path(), "w") as fh:\n        fh.write("{}")\n')
        self.assertEqual(len(f), 1)

    def test_an_aliased_local_destination_is_flagged(self):
        f = _scan('def _evil():\n    p = _engine_manifest_path()\n    with open(p, "w") as fh:\n        fh.write("{}")\n')
        self.assertEqual(len(f), 1)

    def test_shutil_copyfile_onto_the_helper_is_flagged(self):
        # a "stage to temp, copy over" pattern — no more exotic than os.replace, must be caught
        f = _scan("def _evil(src):\n    shutil.copyfile(src, _engine_manifest_path())\n")
        self.assertEqual(len(f), 1)

    def test_os_rename_and_shutil_move_onto_the_helper_are_flagged(self):
        self.assertEqual(len(_scan("def _a(t):\n    os.rename(t, _engine_manifest_path())\n")), 1)
        self.assertEqual(len(_scan("def _b(s):\n    shutil.move(s, _engine_manifest_path())\n")), 1)

    def test_pathlib_write_text_onto_the_helper_is_flagged(self):
        f = _scan("from pathlib import Path\ndef _evil():\n    Path(_engine_manifest_path()).write_text('{}')\n")
        self.assertEqual(len(f), 1)

    def test_a_coincidental_variable_name_does_not_suppress_the_finding(self):
        # a marker is recognised by EXACT identifier, not substring — a variable that merely CONTAINS
        # "engine_write" must NOT be mistaken for the guard (the review's blocking finding)
        f = _scan('def bump(engine):\n    engine_write_log = "x"\n    _write_json(_engine_manifest_path(), engine)\n')
        self.assertEqual(len(f), 1, "a coincidental name suppressed the finding — the marker match is too loose")


class TestPassesLegitimate(unittest.TestCase):
    """Guarded writers, reads, generic writers, and fixtures must all pass clean."""

    def test_a_guarded_write_json_passes(self):
        f = _scan("def _bump(engine):\n    engine_write.write_json(_engine_manifest_path(), engine, base=validate.ROOT)\n")
        self.assertEqual(f, [])

    def test_a_preflighted_replace_passes(self):
        f = _scan('def apply(tmp):\n    if engine_write.write_through_symlink_reason(_engine_manifest_path(), validate.ROOT):\n        return\n    os.replace(tmp, _engine_manifest_path())\n')
        self.assertEqual(f, [])

    def test_the_instantiator_alias_marker_passes(self):
        # instantiator uses the aliased private name — the substring marker must recognise it
        f = _scan('def _marker():\n    if _write_through_symlink_reason(_engine_manifest_path(), validate.ROOT):\n        return\n    _write_json(_engine_manifest_path(), {})\n')
        self.assertEqual(f, [])

    def test_a_read_of_the_manifest_passes(self):
        f = _scan('def _load():\n    with open(_engine_manifest_path(), encoding="utf-8") as fh:\n        return fh.read()\n')
        self.assertEqual(f, [])

    def test_reading_the_manifest_then_writing_an_unrelated_file_passes(self):
        # per-write-call correlation: the write's destination is another file, not the manifest — the
        # function must NOT be flagged just because it also READS the manifest (the review's false positive)
        f = _scan('def sync(other):\n    with open(_engine_manifest_path()) as fh:\n        d = fh.read()\n    with open(other, "w") as fh:\n        fh.write(d)\n')
        self.assertEqual(f, [])

    def test_a_generic_writer_with_no_manifest_reference_passes(self):
        f = _scan('def _write_json(path, data):\n    with open(path, "w") as fh:\n        fh.write("{}")\n')
        self.assertEqual(f, [])

    def test_a_fixture_write_under_a_redirected_temp_root_passes(self):
        f = _scan('def demo():\n    with tempfile.TemporaryDirectory() as d:\n        validate.ROOT = d\n        with open(os.path.join(validate.ROOT, ".engine", "engine.json"), "w") as fh:\n            fh.write("{}")\n')
        self.assertEqual(f, [])


class TestRealTreeAndFixture(unittest.TestCase):
    def test_the_real_engine_tree_is_clean(self):
        # the whole point: every real manifest writer already routes through the funnel
        self.assertEqual(check.check(), [], "a real manifest writer bypasses the funnel")

    def test_the_committed_negative_fixture_bites(self):
        # the fixture the hard-check-bite meta-check runs — prove it flags via the ENGINE_ROOT seam
        import validate
        fixture = os.path.join(validate.ROOT, ".engine", "_fixtures", "manifest-write-funnel")
        f = check.check(fixture)
        self.assertTrue(any("does not route through the guarded write funnel" in x["message"] for x in f),
                        "the negative fixture no longer bites the check")


if __name__ == "__main__":
    unittest.main()
