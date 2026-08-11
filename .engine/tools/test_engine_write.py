"""Tests for engine_write — the engine-owned write boundary, homed once (#862/#923).

Run: uv run --directory .engine --frozen -- python tools/selftest.py
"""
from __future__ import annotations
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import engine_write  # noqa: E402


class TestWriteThroughSymlinkReason(unittest.TestCase):
    """The one predicate: refuse a symlinked leaf, a symlinked ancestor escaping the base, a dangling
    link, and a plain path-escape; allow a regular in-tree file. The #862 helper, relocated verbatim."""

    def test_a_regular_in_tree_file_is_allowed(self):
        with tempfile.TemporaryDirectory() as d:
            root = os.path.join(d, "repo")
            os.makedirs(os.path.join(root, ".engine"))
            target = os.path.join(root, ".engine", "engine.json")
            with open(target, "w", encoding="utf-8") as fh:
                fh.write("{}\n")
            self.assertIsNone(engine_write.write_through_symlink_reason(target, root))

    def test_an_absent_regular_path_is_allowed(self):
        # a first write (the file does not exist yet) must not be refused — realpath of a non-existent
        # in-tree path still resolves under the base
        with tempfile.TemporaryDirectory() as d:
            root = os.path.join(d, "repo")
            os.makedirs(os.path.join(root, ".engine"))
            target = os.path.join(root, ".engine", "engine.json")
            self.assertIsNone(engine_write.write_through_symlink_reason(target, root))

    def test_a_symlinked_leaf_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            root = os.path.join(d, "repo")
            os.makedirs(os.path.join(root, ".engine"))
            outside = os.path.join(d, "outside.json")
            with open(outside, "w", encoding="utf-8") as fh:
                fh.write("{}\n")
            target = os.path.join(root, ".engine", "engine.json")
            os.symlink(outside, target)
            self.assertIsNotNone(engine_write.write_through_symlink_reason(target, root))

    def test_a_dangling_symlink_is_refused(self):
        # os.path.exists/isfile FOLLOW a link and say "absent" for a dangling one — the predicate must
        # still refuse it (islink sees the link itself), the #862 ordering lesson
        with tempfile.TemporaryDirectory() as d:
            root = os.path.join(d, "repo")
            os.makedirs(os.path.join(root, ".engine"))
            target = os.path.join(root, ".engine", "engine.json")
            os.symlink(os.path.join(d, "never-created.json"), target)
            self.assertIsNotNone(engine_write.write_through_symlink_reason(target, root))

    def test_a_symlinked_ancestor_escaping_the_base_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            root = os.path.join(d, "repo")
            os.makedirs(os.path.join(root, ".engine"))
            outside_dir = os.path.join(d, "outside-dir")
            os.makedirs(outside_dir)
            os.symlink(outside_dir, os.path.join(root, ".engine", "linkdir"))
            through = os.path.join(root, ".engine", "linkdir", "engine.json")
            self.assertIsNotNone(engine_write.write_through_symlink_reason(through, root))

    def test_a_plain_path_escape_is_refused_without_any_symlink(self):
        with tempfile.TemporaryDirectory() as d:
            root = os.path.join(d, "repo")
            os.makedirs(root)
            escape = os.path.join(root, "..", "outside.json")
            self.assertIsNotNone(engine_write.write_through_symlink_reason(escape, root))

    def test_the_base_itself_is_allowed(self):
        # resolved == root is the documented allow (the exact-root case)
        with tempfile.TemporaryDirectory() as d:
            root = os.path.join(d, "repo")
            os.makedirs(root)
            self.assertIsNone(engine_write.write_through_symlink_reason(root, root))

    def test_a_parent_derived_base_reduces_to_the_leaf_rule(self):
        # the caller-supplied-path doctrine: base = the target's own parent allows an out-of-tree
        # fixture (a temp copy) while still refusing a symlinked leaf there
        with tempfile.TemporaryDirectory() as d:
            target = os.path.join(d, "fixture.json")
            base = os.path.dirname(os.path.abspath(target))
            self.assertIsNone(engine_write.write_through_symlink_reason(target, base))
            os.symlink(os.path.join(d, "elsewhere.json"), target)
            self.assertIsNotNone(engine_write.write_through_symlink_reason(target, base))


class TestWriteJson(unittest.TestCase):
    """The guarded writer: check → raise → makedirs → write, in that order."""

    def test_a_clean_write_lands_with_the_manifest_shape(self):
        with tempfile.TemporaryDirectory() as d:
            root = os.path.join(d, "repo")
            os.makedirs(root)
            target = os.path.join(root, ".engine", "engine.json")
            engine_write.write_json(target, {"a": 1}, base=root)
            with open(target, encoding="utf-8") as fh:
                text = fh.read()
            self.assertEqual(json.loads(text), {"a": 1})
            self.assertTrue(text.endswith("\n"), "trailing newline — the manifest's on-disk shape")
            self.assertIn('  "a": 1', text, "2-space indent — the manifest's on-disk shape")

    def test_a_symlinked_destination_refuses_and_writes_nothing_through(self):
        with tempfile.TemporaryDirectory() as d:
            root = os.path.join(d, "repo")
            os.makedirs(os.path.join(root, ".engine"))
            outside = os.path.join(d, "outside.json")
            os.symlink(outside, os.path.join(root, ".engine", "engine.json"))
            with self.assertRaises(engine_write.EngineWriteRefused):
                engine_write.write_json(os.path.join(root, ".engine", "engine.json"), {"a": 1}, base=root)
            self.assertFalse(os.path.exists(outside), "nothing was written through the link, out of the tree")

    def test_a_refused_write_creates_no_directories(self):
        # the check runs BEFORE makedirs: a refused write must not create directories through a
        # symlinked ancestor either (the ordering the docstring pins)
        with tempfile.TemporaryDirectory() as d:
            root = os.path.join(d, "repo")
            os.makedirs(os.path.join(root, ".engine"))
            outside_dir = os.path.join(d, "outside-dir")
            os.makedirs(outside_dir)
            os.symlink(outside_dir, os.path.join(root, ".engine", "linkdir"))
            through = os.path.join(root, ".engine", "linkdir", "deeper", "engine.json")
            with self.assertRaises(engine_write.EngineWriteRefused):
                engine_write.write_json(through, {"a": 1}, base=root)
            self.assertEqual(os.listdir(outside_dir), [],
                             "no directory was created through the symlinked ancestor")


if __name__ == "__main__":
    unittest.main()
