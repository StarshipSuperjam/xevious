#!/usr/bin/env python3
"""Behavioral FALSIFICATION for issue #594, slice 3 — undoing a staged/stalled engine update puts the working
tree back to before the update, losslessly.

This is the maintainer-runnable evidence for a feature that, by design, has NO pull request to review: a
stalled update was never committed, so `rollback --confirm` changes the working copy directly. The safety
floor is a recovery point saved first; this demo shows the undo genuinely reverts the tree, and that the
branch-switch-back is the load-bearing step (neutralize it and the update is NOT undone).

FAIL-THEN-PASS on the same staged fixture; the only difference between the arms is whether the discard's
switch back to the pre-update branch runs:
  * POSITIVE (the real undo): `rollback(confirm=True)` saves a recovery point, then switches back to the
    pre-update branch — the added file is gone, the changed file is back, the tree is clean, and the recovery
    point holds the discarded update.
  * NEGATIVE CONTROL (the step removed): neutralize only the switch-back — the recovery point is still made,
    but the tree is NOT reverted (the update's new file is still present), and the result is `partial`, never
    `undone`. This proves the switch-back is what reverts; remove it and the undo stops working.

It exercises the REAL `rollback` surface against a throwaway COPY of a fixture engine in a real temp git repo,
with only the runtime rebuild seamed to a no-op (nothing here needs `uv sync`) and no network (no data
migration in the fixture, so the memory leg is a clean no-op). It TRAVELS with the engine: its companion test
(`test_module_manager.TestRollback.test_the_rollback_falsification_demo_passes`) runs it, so it keeps guarding
this shipped operator capability in every generated repo.
"""
from __future__ import annotations
import os
import subprocess
import tempfile

import module_manager as mm


def _git(root, *args):
    subprocess.run(["git", "-C", root, *args], capture_output=True, text=True, check=False)


def _stage(root):
    """Build a coherent baseline engine, commit it, then dirty the tree with a #594-shape staged update: a
    release that ADDS a new tool file + a new wire to the present module, changes the existing tool, and bumps
    the version — all uncommitted, exactly a stall's working tree."""
    with mm._redirect_root(root):
        mm._build_upgrade_fixture(root)
    _git(root, "init", "-b", "main")
    _git(root, "-c", "user.email=t@t", "-c", "user.name=t", "add", "-A")
    _git(root, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", "baseline")
    eng = os.path.join(root, ".engine")
    mm._write_json(os.path.join(eng, "modules", "base", "manifest.json"),
                   {"id": "base", "version": "0.2.0", "status": "required",
                    "provides": {"tool": [".engine/tools/base_tool.py", ".engine/tools/base_extra.py"]},
                    "depends": {}, "migrations": {},
                    "wires": [{"type": "gitignore", "key": "oldcache", "lines": [".engine/base/.oldcache/"]},
                              {"type": "gitignore", "key": "newcache", "lines": [".engine/base/.newcache/"]}]})
    with open(os.path.join(eng, "tools", "base_tool.py"), "w") as fh:
        fh.write("# base v2\n")
    with open(os.path.join(eng, "tools", "base_extra.py"), "w") as fh:
        fh.write("# a new tool the update added\n")
    mm._write_json(os.path.join(eng, "engine.json"),
                   {"engine_release": "0.2.0", "packages": {"base": "0.2.0"}, "identity": "solo",
                    "home_repository": "acme/engine-home"})


def _dirty(root) -> bool:
    out = subprocess.run(["git", "-C", root, "status", "--porcelain"], capture_output=True, text=True)
    return bool(out.stdout.strip())


def main() -> int:
    failures = []
    print("=" * 78)
    print("DEMO #594 (slice 3) — undoing a staged/stalled update puts the tree back, losslessly.")
    print("Same staged fixture, two arms; the only difference is whether the switch-back to the pre-update")
    print("branch runs (the load-bearing revert step).")
    print("=" * 78)

    # ---- POSITIVE: the real undo reverts the tree and the added file is gone -----------------------------
    with tempfile.TemporaryDirectory() as d:
        live = os.path.join(d, "live")
        os.makedirs(live)
        _stage(live)
        extra = os.path.join(live, ".engine", "tools", "base_extra.py")
        before_dirty = _dirty(live)
        with mm._redirect_root(live):
            res = mm.rollback(confirm=True, resync=lambda: True, transport=None)
        undone = bool(res.get("undone")) and not _dirty(live) and not os.path.exists(extra)
        rp = res.get("recovery_point") or ""
        recovered = rp.startswith("engine-rescue/")
        print(f"\n[POSITIVE — real undo] tree was dirty: {before_dirty}; after: undone={res.get('undone')}, "
              f"tree clean={not _dirty(live)}, added file gone={not os.path.exists(extra)}, "
              f"recovery point={rp!r}")
        if not (before_dirty and undone and recovered):
            failures.append("POSITIVE: the real undo did not cleanly revert the staged update")

    # ---- NEGATIVE CONTROL: neutralize ONLY the switch-back -> the update is NOT undone -------------------
    with tempfile.TemporaryDirectory() as d:
        live = os.path.join(d, "live")
        os.makedirs(live)
        _stage(live)
        extra = os.path.join(live, ".engine", "tools", "base_extra.py")
        real_git = mm._git

        def _no_switchback(root, *args, **kw):
            if args[:1] == ("checkout",) and len(args) == 2:   # the discard's switch back to the pre-update branch
                return None
            return real_git(root, *args, **kw)
        mm._git = _no_switchback
        try:
            with mm._redirect_root(live):
                res = mm.rollback(confirm=True, resync=lambda: True, transport=None)
        finally:
            mm._git = real_git
        still_there = os.path.exists(extra)
        print(f"\n[NEGATIVE — switch-back removed] undone={res.get('undone')}, partial={res.get('partial')}, "
              f"the update's new file still present={still_there}")
        if res.get("undone") or not res.get("partial") or not still_there:
            failures.append("NEGATIVE: without the switch-back the update was still (wrongly) reported undone")

    print("\n" + ("-" * 78))
    if failures:
        for f in failures:
            print(f"FAIL: {f}")
        print("DEMO FAILED")
        return 1
    print("DEMO PASSED — the undo reverts a staged update, and the switch-back is load-bearing.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
