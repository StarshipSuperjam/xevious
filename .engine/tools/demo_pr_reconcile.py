#!/usr/bin/env python3
"""Demo — why a stuck pull request is never your problem, and how the engine knows when to stop (#136).

What this checks, in plain words: when two pieces of work are in flight at once they can both rewrite the
engine's two internal index files — the knowledge graph and the self-map — and a clash can leave a pull request
"stuck" (it can't be merged until the clash is cleared). This shows, on THROWAWAY COPIES of your project, two
things you need to trust:

  (1) When the clash is only on those two index files, the one-step reconcile recovers it LOSSLESSLY — it
      rebuilds the files from the merged work, so BOTH pieces of work are still there, and you are never handed
      the conflict.
  (2) When the clash is on real, authored content (not just the index files), the reconcile KNOWS TO STOP — it
      changes nothing, leaves the branch byte-for-byte as it was, and routes the decision back to you. The
      scary part (it rewrites git history and pushes) is the part that must refuse correctly, so you watch it
      refuse here too.

It runs the REAL reconcile (the same code an engine session runs), against real throwaway git repositories with
a real remote. Nothing real is touched — it all happens in a temporary directory and prints what it finds.

Run: uv run --directory .engine -- python tools/demo_pr_reconcile.py
"""
from __future__ import annotations
import json
import os
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate          # noqa: E402
import pr_reconcile      # noqa: E402

# --- the knob: rename these and re-run; watch each piece of work survive (or the refuse stay safe) -----------
WIDGET_A = "reconcile_demo_widget_a"      # "branch A" adds this tool — one piece of work
WIDGET_B = "reconcile_demo_widget_b"      # "the work that merged first" adds this tool — the other piece
SHARED   = "reconcile_demo_shared"        # both sides edit THIS one — a real authored clash the reconcile refuses


def _git(root: str, *args: str):
    return subprocess.run(["git", "-C", root, *args], capture_output=True, text=True, check=False)


def _commit(root: str, message: str) -> None:
    _git(root, "add", "-A")
    _git(root, "-c", "user.email=e@x", "-c", "user.name=n", "commit", "-q", "-m", message)


def _add_tool(work: str, name: str, body: str) -> None:
    with open(os.path.join(work, ".engine", "tools", f"{name}.py"), "w", encoding="utf-8") as fh:
        fh.write(f'"""{body}"""\n')


def _regen(work: str) -> None:
    """Rebuild the two index files from the work's own sources — the same call an integrate session makes."""
    if not pr_reconcile._regen_members(work):
        raise RuntimeError("regeneration failed while setting up the demo fixture")


def _entity_ids(work: str) -> set:
    with open(os.path.join(work, ".engine", "knowledge", "graph.json"), encoding="utf-8") as fh:
        return {e["id"] for e in json.load(fh)["entities"]}


def _origin_and_work(holder: str) -> tuple[str, str]:
    """A bare 'origin' remote + a working clone of your real tree, with `main` committed and pushed. The work
    tree is a throwaway COPY (its tools resolve their own root, so they read the copy, never your project)."""
    origin = os.path.join(holder, "origin.git")
    work = os.path.join(holder, "work")
    subprocess.run(["git", "init", "-q", "--bare", origin], check=False)
    shutil.copytree(validate.ROOT, work, symlinks=True,
                    ignore=shutil.ignore_patterns(".git", ".venv", "__pycache__", "*.pyc"))
    _git(work, "init", "-q", "-b", "main")
    _git(work, "remote", "add", "origin", origin)
    _commit(work, "seed (a copy of your real project)")
    _git(work, "push", "-q", "origin", "main")
    _git(work, "remote", "set-head", "origin", "main")
    return origin, work


def _scenario_recover() -> bool:
    """Two pieces of work, each adding its own tool; one merges first; the other is reconciled — both survive."""
    print("(1) Lossless recovery — two pieces of work clash on the index files, and BOTH are kept.\n")
    with tempfile.TemporaryDirectory(prefix="pr-reconcile-recover-") as holder:
        _origin, work = _origin_and_work(holder)
        a_id, b_id = f"tool:{WIDGET_A}", f"tool:{WIDGET_B}"

        # "branch A" — your stuck pull request: adds WIDGET_A, rebuilds the index, pushes.
        _git(work, "checkout", "-q", "-b", "feature")
        _add_tool(work, WIDGET_A, "Widget A — work from your pull request")
        _regen(work)
        _commit(work, f"add {WIDGET_A}")
        _git(work, "push", "-q", "origin", "feature")

        # "the work that merged first" — a sibling adds WIDGET_B on main and lands it, clashing on the index.
        _git(work, "checkout", "-q", "main")
        _add_tool(work, WIDGET_B, "Widget B — the work that landed first")
        _regen(work)
        _commit(work, f"add {WIDGET_B}")
        _git(work, "push", "-q", "origin", "main")

        # back on the stuck pull request: run the REAL reconcile.
        _git(work, "checkout", "-q", "feature")
        before = _entity_ids(work)
        result = pr_reconcile.reconcile(apply=True, root=work, default="main")
        after = _entity_ids(work)

        kept_a, kept_b = a_id in after, b_id in after
        ok = result.get("status") == "reconciled" and kept_a and kept_b
        print(f"      Before reconcile, your branch's index had:  A={a_id in before}  B={b_id in before}")
        print(f"      The reconcile returned:                      {result.get('status')!r}")
        print(f"      After reconcile, your branch's index has:    A={kept_a}  B={kept_b}")
        if ok:
            print("      -> both pieces of work survived; the pull request is reconciled and pushed. "
                  "No work lost.\n")
        else:
            print("      -> MISMATCH — a contribution was dropped or the reconcile did not complete. "
                  "Investigate; this is a real signal, not a pass.\n")
        return ok


def _scenario_refuse() -> bool:
    """Both sides change the SAME authored file — a real conflict the reconcile must refuse, untouched."""
    print("(2) Safe refusal — the clash is on real authored content, so the reconcile stops and changes "
          "nothing.\n")
    with tempfile.TemporaryDirectory(prefix="pr-reconcile-refuse-") as holder:
        _origin, work = _origin_and_work(holder)

        # a shared authored tool exists on main first.
        _add_tool(work, SHARED, "Shared tool — original")
        _regen(work)
        _commit(work, f"add {SHARED}")
        _git(work, "push", "-q", "origin", "main")

        # the pull request edits the shared tool one way...
        _git(work, "checkout", "-q", "-b", "feature")
        _add_tool(work, SHARED, "Shared tool — the pull request's version")
        _regen(work)
        _commit(work, f"edit {SHARED} (pull request)")
        _git(work, "push", "-q", "origin", "feature")

        # ...and the work that lands first edits the SAME shared tool the other way — a genuine authored clash.
        _git(work, "checkout", "-q", "main")
        _add_tool(work, SHARED, "Shared tool — the version that landed first")
        _regen(work)
        _commit(work, f"edit {SHARED} (landed first)")
        _git(work, "push", "-q", "origin", "main")

        _git(work, "checkout", "-q", "feature")
        before_head = _git(work, "rev-parse", "HEAD").stdout.strip()
        before_tree = _git(work, "rev-parse", "HEAD^{tree}").stdout.strip()
        result = pr_reconcile.reconcile(apply=True, root=work, default="main")
        after_head = _git(work, "rev-parse", "HEAD").stdout.strip()
        after_tree = _git(work, "rev-parse", "HEAD^{tree}").stdout.strip()
        clean_tree = not _git(work, "status", "--porcelain").stdout.strip()

        untouched = before_head == after_head and before_tree == after_tree and clean_tree
        refused = result.get("status") == "needs-manual"
        ok = refused and untouched
        print(f"      The reconcile returned:        {result.get('status')!r} "
              f"(reason: {result.get('reason')!r})")
        print(f"      The branch is byte-identical:  {untouched}  (same commit, same tree, clean working copy)")
        if ok:
            print("      -> it correctly refused a real authored conflict and left everything exactly as it "
                  "was. Your work is safe; the decision is yours.\n")
        else:
            print("      -> UNEXPECTED — it should have refused and left the branch untouched. Investigate.\n")
        return ok


def main(_argv=None) -> int:
    print("What this checks: a pull request stuck on the engine's two index files recovers losslessly, and a")
    print("pull request stuck on real authored content is refused safely. (#136)\n")
    recovered = _scenario_recover()
    refused = _scenario_refuse()
    ok = recovered and refused
    if ok:
        print("In plain words: when the clash was only on the engine's index files, rebuilding from the merged")
        print("work recovered every contribution — no work lost, and you were never handed a conflict. When the")
        print("clash was on real content, the reconcile knew to stop and left your branch exactly as it was.\n")
        print("Vary it yourself: rename WIDGET_A / WIDGET_B / SHARED at the top of this file and re-run — watch")
        print("each piece of work survive the recovery, and the refusal stay safe. Your real project is never "
              "touched.")
    else:
        print("This run did NOT confirm both behaviours. That is a real signal worth investigating, not a "
              "pass. Your real project was not touched.")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
