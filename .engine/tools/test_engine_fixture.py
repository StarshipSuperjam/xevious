#!/usr/bin/env python3
"""Regression tests for `engine_fixture.clone_engine` — the tracked-only demo fixture clone (#850).

Proves the contract directly, in a throwaway git repo, so it never writes junk into the real `.engine/`
tree: an UNTRACKED file (the `.DS_Store` that caused #850) must never enter the fixture; the tracked engine
surface must land intact (content fidelity, so a hollowed-out fixture cannot pass a positive arm); the scope
must not widen to files outside the copy roots; a locally-modified tracked file must carry its working-tree
content; and a git-unavailable or empty surface must FAIL LOUD rather than degrade to a raw copy.
"""
from __future__ import annotations
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import engine_fixture  # noqa: E402


def _git(repo: str, *args: str) -> None:
    subprocess.run(["git", "-C", repo, *args], check=True, capture_output=True, text=True)


def _write(root: str, rel: str, content: str) -> None:
    path = os.path.join(root, rel.replace("/", os.sep))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)


def _init_repo(path: str) -> str:
    os.makedirs(path, exist_ok=True)
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "t@t.test")
    _git(path, "config", "user.name", "test")
    return path


# The tracked engine surface a fixture must carry — one file under each copy root plus a tracked root file.
_TRACKED_SURFACE = (
    ".engine/modules/core/manifest.json",
    ".engine/tools/thing.py",
    ".claude/agents/engine-a.md",
    ".mcp.json",
    "CLAUDE.md",
)


class TrackedOnlyCloneTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = _init_repo(os.path.join(self._tmp.name, "repo"))
        for rel in _TRACKED_SURFACE:
            _write(self.repo, rel, "{}\n" if rel.endswith(".json") else "seed\n")
        _write(self.repo, "README.md", "# outside the copy roots\n")  # tracked, but not in scope
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-qm", "seed")

    def _dest(self, name: str = "dest") -> str:
        return os.path.join(self._tmp.name, name)

    def test_untracked_junk_is_excluded(self) -> None:
        """The #850 bug: an untracked `.DS_Store` (or editor swap file) must never enter the fixture."""
        open(os.path.join(self.repo, ".engine", ".DS_Store"), "w").close()
        open(os.path.join(self.repo, ".engine", "tools", "scratch.py.swp"), "w").close()
        dest = engine_fixture.clone_engine(self.repo, self._dest())
        self.assertFalse(os.path.exists(os.path.join(dest, ".engine", ".DS_Store")))
        self.assertFalse(os.path.exists(os.path.join(dest, ".engine", "tools", "scratch.py.swp")))

    def test_tracked_surface_is_present(self) -> None:
        """Fidelity: the whole tracked surface — including the root files — lands, with content, not just paths."""
        dest = engine_fixture.clone_engine(self.repo, self._dest())
        for rel in _TRACKED_SURFACE:
            self.assertTrue(os.path.isfile(os.path.join(dest, rel.replace("/", os.sep))), rel)
        # Content lands, not merely the path (a hollowed-out fixture must not pass a positive arm).
        with open(os.path.join(dest, ".engine", "modules", "core", "manifest.json"), encoding="utf-8") as fh:
            self.assertIn("{}", fh.read())

    def test_scope_excludes_files_outside_copy_roots(self) -> None:
        """A tracked file outside the copy roots (README.md) must NOT be cloned — no widening to whole repo."""
        dest = engine_fixture.clone_engine(self.repo, self._dest())
        self.assertFalse(os.path.exists(os.path.join(dest, "README.md")))

    def test_working_tree_modification_is_carried(self) -> None:
        """A tracked file modified but not committed carries its WORKING-TREE content, as copy-from-disk did."""
        _write(self.repo, ".engine/tools/thing.py", "x = 999  # local edit\n")
        dest = engine_fixture.clone_engine(self.repo, self._dest())
        with open(os.path.join(dest, ".engine", "tools", "thing.py"), encoding="utf-8") as fh:
            self.assertIn("999", fh.read())

    def test_git_unavailable_raises(self) -> None:
        """A source that is not a git work tree must fail loud — never fall back to a raw copy (would re-open #850)."""
        non_repo = os.path.join(self._tmp.name, "plain")
        _write(non_repo, ".engine/x.py", "x = 1\n")
        with self.assertRaises(engine_fixture.FixtureCloneError):
            engine_fixture.clone_engine(non_repo, self._dest("d1"))

    def test_empty_surface_raises(self) -> None:
        """A real git repo with no tracked engine surface must fail loud, not pass with an empty fixture."""
        empty = _init_repo(os.path.join(self._tmp.name, "empty"))
        _write(empty, "unrelated.txt", "hi\n")
        _git(empty, "add", "-A")
        _git(empty, "commit", "-qm", "seed")
        with self.assertRaises(engine_fixture.FixtureCloneError):
            engine_fixture.clone_engine(empty, self._dest("d2"))


if __name__ == "__main__":
    unittest.main()
