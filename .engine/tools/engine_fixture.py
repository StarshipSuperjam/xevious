#!/usr/bin/env python3
"""Shared fixture builder for the upgrade / reconcile / release-gate falsification demos.

`clone_engine(real_root, dest)` copies this repo's real engine surface into a throwaway `dest` so a demo
can boot a genuine engine and let the structural/coherence gates pass — isolating the behaviour under
test, not a broken fixture. It clones **git-tracked content only**, which is the whole point: an UNTRACKED
file (a macOS `.DS_Store` dropped by Finder, an editor swap file, a scratch dir) is never copied into the
fixture, so it can never be hard-flagged as a module-ownership orphan and fail the self-test suite (StarshipSuperjam/engine-template#850).

Why tracked-only rather than a name denylist: the real ownership gate already scopes itself to git-tracked
files — `module_coherence.engine_file_inventory()` intersects the file walk with `git ls-files` precisely so
an untracked file is not read as a committed-ownership concern (StarshipSuperjam/engine-template#281). A demo fixture is a temp dir with no
`.git`, so inside it that intersection fails soft to a raw walk; cloning tracked-only is exactly what keeps
that fallback's answer identical to the real gate's — a genuine committed orphan is still copied and still
caught, only the untracked noise disappears.

This replaces four byte-identical private `_clone_engine` helpers (in demo_594/599/663/664) with one home,
so the fix cannot drift back into three stale copies. It is intentionally NOT named `demo_*` — it is a shared
library tool, not construction evidence, so it must travel into every generated repo and never retire.
"""
from __future__ import annotations
import os
import shutil
import subprocess

# The engine surface a real coherent engine needs on disk for a child to boot and for the coherence/ownership
# gates to pass: the whole `.engine`, the shared-file wiring targets (`.claude`, `.codex`, `.mcp.json`), and
# the floor sources. The clone is git-tracked-only, so caches / `.venv` / worktrees / `.git` are excluded for
# free (they are untracked or ignored) — no denylist needed. The four loose root files sit under no directory
# root, so they are named explicitly; `.mcp.json` in particular is a wiring target the coherence gate reads.
COPY_DIRS = (".engine", ".claude", ".codex", ".agents", ".github")
COPY_FILES = (".mcp.json", ".gitignore", "CLAUDE.md", "AGENTS.md")


class FixtureCloneError(RuntimeError):
    """The tracked-only clone could not be built faithfully. Raised — never silently degraded to a raw copy,
    which would copy untracked files back into the fixture and re-open StarshipSuperjam/engine-template#850 while passing green."""


def _tracked_under(real_root: str, roots) -> list | None:
    """`git -C real_root ls-files -z -- <roots>` → the NUL-split, forward-slash, real_root-relative tracked
    relpaths, or None on any non-zero exit / missing binary / timeout. Never raises. Fixed argv, no shell —
    no injection surface; `-z` is verbatim NUL-terminated, so a filename with spaces/quotes is safe. Scoped
    to the PASSED real_root (not a module ROOT global), so a caller cloning a tree other than validate.ROOT
    still enumerates the tree it named. Mirrors module_coherence._git_lines."""
    try:
        out = subprocess.run(["git", "-C", real_root, "ls-files", "-z", "--", *roots],
                             capture_output=True, text=True, timeout=30, check=False)
    except Exception:  # noqa: BLE001 — missing binary / timeout / OS error all degrade to "unavailable"
        return None
    if out.returncode != 0:
        return None
    return [p for p in out.stdout.split("\0") if p]


def clone_engine(real_root: str, dest: str) -> str:
    """Copy `real_root`'s engine surface into `dest`, git-tracked content only, and return `dest`.

    The copy scope is exactly the tracked files under `COPY_DIRS` plus the tracked `COPY_FILES` — the same
    selective surface the demos have always cloned, now filtered to tracked content instead of a raw
    `shutil.copytree`. Each file is copied from the WORKING TREE (so a local modification to a tracked file
    still travels into the fixture, matching the old behaviour); a tracked file deleted from the work tree
    has no source and is skipped (the fixture reflects the work tree, as the old copy-from-disk did).

    FAILS LOUD, never silently degrades. `real_root` must be a git working tree — every context that runs
    these demos is one (local, `selftest.py`, the CI `actions/checkout`, the release-cut git-init'd
    projection, a deployed repo). If git is unavailable (so tracked-vs-untracked cannot be told apart) or the
    enumeration is empty (a populated surface that produced nothing), it raises `FixtureCloneError` rather
    than falling back to a raw copy — a silent fallback would re-open StarshipSuperjam/engine-template#850 and, worse, pass green.
    """
    tracked = _tracked_under(real_root, (*COPY_DIRS, *COPY_FILES))
    if tracked is None:
        raise FixtureCloneError(
            f"cannot build the demo fixture: `git ls-files` failed in {real_root!r} (git unavailable or not "
            f"a work tree). The tracked-only clone needs a real git checkout; refusing rather than copying "
            f"untracked content back in, which would re-open #850.")
    if not tracked:
        raise FixtureCloneError(
            f"cannot build the demo fixture: no git-tracked files under {list(COPY_DIRS)} + "
            f"{list(COPY_FILES)} in {real_root!r}. Expected a populated engine surface; refusing an empty / "
            f"degraded fixture rather than passing vacuously.")
    os.makedirs(dest, exist_ok=True)
    for rel in tracked:
        native = rel.replace("/", os.sep)
        src = os.path.join(real_root, native)
        dst = os.path.join(dest, native)
        if not os.path.lexists(src):  # tracked but deleted from the work tree — nothing on disk to copy
            continue
        os.makedirs(os.path.dirname(dst) or dest, exist_ok=True)
        if os.path.islink(src):
            # Recreate the link rather than dereference it (the old copytree passed symlinks=True). No tracked
            # symlink exists under these roots today; this keeps the helper honest if one is ever added.
            if os.path.lexists(dst):
                os.remove(dst)
            os.symlink(os.readlink(src), dst)
        elif os.path.isdir(src):
            # A tracked path whose work-tree entry is a directory is a git submodule (gitlink). The engine
            # surface ships none; refuse loudly rather than let copy2 raise a bare IsADirectoryError that would
            # escape the FixtureCloneError contract. (Forward-looking, like the symlink branch — none today.)
            raise FixtureCloneError(
                f"cannot build the demo fixture: tracked path {rel!r} in {real_root!r} is a git submodule "
                f"(gitlink); the engine surface has none and the fixture clone does not support one.")
        else:
            shutil.copy2(src, dst)
    return dest
