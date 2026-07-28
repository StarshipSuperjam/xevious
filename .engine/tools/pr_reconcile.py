#!/usr/bin/env python3
"""Reconcile a pull request stranded on the engine's derived-committed index files (#136).

When two pieces of work are in flight at once they can both rewrite the engine's two internal index files —
the knowledge graph (`.engine/knowledge/graph.json`) and the self-map (`.engine/self-map.md`) — and a sibling
pull request merging first leaves THIS pull request in a GitHub `CONFLICTING` state a non-engineer cannot
clear. Those two files are *derived-committed*: their content is a pure function of the source tree, so
a conflict on them is **spurious** — resolved by regenerating from the reconciled tree, never a hand-merge,
never a side-pick, and **never handed to the operator**.

This mirrors `checkout_health`'s detect → assess → execute shape, **lossless-or-it-does-not-run**:
  - `detect_conflict(gh)` — READ-ONLY, boot-relayed: is the current branch's open PR in a GitHub conflicting
    merge state? Returns an offer dict on a confirmed conflict, else None. GitHub computes `mergeable`
    asynchronously, so an *unknown* state degrades QUIETLY to None (caught next boot) — never a false
    "all clear". The authoritative file-level classifier is `assess`, not GitHub's async field.
  - `assess()` — READ-ONLY classification. A working-tree-free `git merge-tree` against the freshly-fetched
    default branch decides whether the conflict is confined to the two derived-committed members (`fixable`, lossless) or
    touches authored files (`needs-manual` — a real conflict for human decision, never auto-resolved). It
    refuses on a tree that carries no engine files (an external-contribution / fork-main branch is never
    regenerated onto).
  - `reconcile(apply=True)` — the executor: an **append-only merge** of the default branch (no history
    rewrite, NO force-push), regenerate the two members from the reconciled tree, re-verify, then a plain
    push. ANY surprise → `git reset --hard` to the captured pre-state and a plain-language refusal.

boot OFFERS the fix; the assistant runs `reconcile(apply=True)` on the operator's consent (the
`checkout_health.unstrand` model; `boot-session-start.md`). The operator is offered the fix, never handed the
conflict.

CLI:  python tools/pr_reconcile.py             # classify THIS branch's PR (offer line or "no conflict")
      python tools/pr_reconcile.py reconcile   # dry-run: what the fix WOULD do (no mutation)
      python tools/pr_reconcile.py reconcile --apply   # reconcile THIS PR (only if fixable)
      python tools/pr_reconcile.py demo        # a lossless-recovery + safe-refuse walkthrough on throwaway repos
"""
from __future__ import annotations

import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate          # noqa: E402

# The two derived-committed members, repo-relative (the exact paths git reports in a conflict). Membership
# is by property (a fully source-deterministic committed file); v1 has exactly these two.
MEMBERS = (".engine/knowledge/graph.json", ".engine/self-map.md")

# An inline identity so a merge/commit never fails for lack of a configured git user on the operator's machine.
_IDENT = ["-c", "user.email=engine@local", "-c", "user.name=engine"]


# ---- the git boundary (best-effort; a mutation reports success, never raises) ----------------

def _run(args: list, root: str, timeout: int = 30) -> str | None:
    """Run a local git command under `root` and return stripped stdout, or None on any non-zero / failure.
    Never raises — every read is best-effort."""
    try:
        out = subprocess.run(["git", "-C", root, *args], capture_output=True, text=True,
                             timeout=timeout, check=False)
        return out.stdout.strip() if out.returncode == 0 else None
    except Exception:  # noqa: BLE001 — a missing binary / timeout / OS error degrades to "unavailable"
        return None


def _ok(args: list, root: str, timeout: int = 120) -> bool:
    """Run a git MUTATION under `root` and report success (return code 0). Never raises."""
    try:
        return subprocess.run(["git", "-C", root, *args], capture_output=True, text=True,
                              timeout=timeout, check=False).returncode == 0
    except Exception:  # noqa: BLE001
        return False


def _current_branch(root: str) -> str | None:
    """The current branch name, or None on a detached HEAD (which `rev-parse --abbrev-ref` reports as 'HEAD')."""
    b = _run(["rev-parse", "--abbrev-ref", "HEAD"], root)
    return b if b and b != "HEAD" else None


def _default_branch(root: str) -> str:
    """The repo's default branch name, resolved from origin/HEAD, else the PROTECTED_BRANCH env / 'main'."""
    head = _run(["symbolic-ref", "--short", "-q", "refs/remotes/origin/HEAD"], root)
    if head:
        return head.split("origin/", 1)[1] if head.startswith("origin/") else head
    return os.environ.get("PROTECTED_BRANCH", "main")


def _dirty(root: str) -> bool:
    return bool((_run(["status", "--porcelain"], root) or "").strip())


def _unmerged(root: str) -> list[str]:
    """The paths left in a conflicted (unmerged) state by an in-progress merge."""
    out = _run(["diff", "--name-only", "--diff-filter=U"], root) or ""
    return [line for line in out.splitlines() if line.strip()]


def _merge_tree(base: str, root: str, head: str = "HEAD") -> tuple[str, list[str]] | None:
    """`git merge-tree --write-tree --name-only <base> <head>` — compute the merge WITHOUT touching the working
    tree. Returns ("clean", []) on a clean merge (exit 0), ("conflict", [paths]) on conflicts (exit 1), or None
    when merge-tree is unavailable / errored / unparseable (the caller treats None as 'cannot classify safely
    → needs-manual', never 'fixable'). Conflicted paths are the first stdout section after the tree-OID line,
    keyed off the exit code (NOT a substring grep of the trailing message block)."""
    try:
        p = subprocess.run(["git", "-C", root, "merge-tree", "--write-tree", "--name-only", base, head],
                          capture_output=True, text=True, timeout=120, check=False)
    except Exception:  # noqa: BLE001
        return None
    if p.returncode == 0:
        return ("clean", [])
    if p.returncode != 1:
        return None                       # 128 = old git / bad args / not a repo → cannot classify
    lines = p.stdout.splitlines()
    if not lines:
        return None
    paths: list[str] = []
    for line in lines[1:]:                # line 0 is the written tree's OID
        if line == "":                    # the name-only section ends at the first blank line
            break
        paths.append(line)
    return ("conflict", paths) if paths else None   # exit 1 but no parseable paths → cannot classify safely


def _members_present(root: str) -> bool:
    """The derived-committed members exist in the tree — the external-contribution / fork-main guard (a product/upstream
    contribution branch carries no engine files and is NEVER regenerated onto; locked build-orchestration)."""
    return all(os.path.isfile(os.path.join(root, m)) for m in MEMBERS)


# ---- detect (READ-ONLY, boot-relayed) --------------------------------------------------------

def detect_conflict(gh, *, root: str | None = None) -> dict | None:
    """Is the current branch's open PR in a GitHub conflicting merge state? READ-ONLY. Returns
    {"pr": <n>, "title": <str>} on a confirmed conflict; None on clean / no-PR / no-GitHub / an UNKNOWN
    (async-uncomputed) merge state. A `mergeable == null` / `mergeable_state == "unknown"` NEVER reads as a
    confident "all clear" — it degrades quietly to None and is caught at the next boot (the authoritative
    file-level classifier is `assess`). `gh` is a `telemetry.GitHubIssues` (it carries its own transport)."""
    if gh is None:
        return None
    root = root or validate.ROOT
    branch = _current_branch(root)
    if not branch:
        return None
    try:
        owner = gh.repo.split("/")[0]
        status, pulls = gh._transport(
            "GET", f"/repos/{gh.repo}/pulls?state=open&head={owner}:{branch}&per_page=10", None)
        if status >= 400 or not isinstance(pulls, list) or not pulls:
            return None
        number = pulls[0].get("number")
        if not number:
            return None
        status, pr = gh._transport("GET", f"/repos/{gh.repo}/pulls/{number}", None)
        if status >= 400 or not isinstance(pr, dict):
            return None
        mergeable = pr.get("mergeable")
        mstate = (pr.get("mergeable_state") or "").lower()
        if mergeable is False or mstate == "dirty":
            return {"pr": number, "title": pr.get("title") or ""}
        # mergeable is True → cleanly mergeable; None / "unknown" → GitHub hasn't computed it yet. Either way we
        # surface nothing this boot — an uncomputed state is caught at the next boot (boot never blocks polling),
        # and the authoritative file-level classifier is assess(). Never a false "all clear".
        return None
    except Exception:  # noqa: BLE001 — any read failure degrades this one signal quietly
        return None


# ---- assess (READ-ONLY classification; the authoritative file-level check) --------------------

def assess(*, root: str | None = None, default: str | None = None, fetch: bool = True) -> dict:
    """Classify the current branch's mergeability against the default branch, OFFLINE of GitHub's async field.
    status ∈ healthy | fixable | needs-manual. `fixable` iff a non-empty conflict set is confined to the two
    derived-committed members (lossless regenerate-to-resolve); any authored conflict, an unclassifiable merge, or a tree
    carrying no engine members → `needs-manual` (never `fixable`)."""
    root = root or validate.ROOT
    default = default or _default_branch(root)
    if not _members_present(root):
        return {"status": "needs-manual", "reason": "no-engine-members", "base": None, "conflicted": []}
    if fetch and not _ok(["fetch", "origin", default], root):
        return {"status": "needs-manual", "reason": "fetch-failed", "base": None, "conflicted": []}
    base = _run(["rev-parse", f"origin/{default}"], root) or _run(["rev-parse", default], root)
    if not base:
        return {"status": "needs-manual", "reason": "no-base", "base": None, "conflicted": []}
    mt = _merge_tree(base, root)
    if mt is None:
        return {"status": "needs-manual", "reason": "cannot-classify", "base": base, "conflicted": []}
    kind, paths = mt
    if kind == "clean":
        return {"status": "healthy", "base": base, "conflicted": []}
    authored = [p for p in paths if p not in set(MEMBERS)]
    if authored:
        return {"status": "needs-manual", "reason": "authored-conflict", "base": base, "conflicted": paths}
    return {"status": "fixable", "base": base, "conflicted": paths}    # ⊆ members, non-empty → lossless


# ---- reconcile (the executor; lossless-or-refuse; NO force-push) ------------------------------

def _regen_members(root: str) -> bool:
    """Regenerate the two members FROM the reconciled tree by running the tree's OWN generators (so a throwaway
    copy regenerates itself, exactly as an integrate session does — the demo-fidelity rule). Both must succeed."""
    for tool in ("knowledge_gen.py", "self_map.py"):
        try:
            p = subprocess.run([sys.executable, os.path.join(root, ".engine", "tools", tool), "generate"],
                             capture_output=True, text=True, timeout=300, check=False, cwd=root)
        except Exception:  # noqa: BLE001
            return False
        if p.returncode != 0:
            return False
    return True


def reconcile(*, apply: bool = False, root: str | None = None, default: str | None = None) -> dict:
    """Reconcile the current branch's PR against the default branch, regenerating the two derived-committed members from the
    reconciled tree. Dry-run (apply=False) returns the assessment without mutating. apply=True executes an
    APPEND-ONLY merge (no history rewrite, NO force-push), resolves a member-only conflict by regeneration,
    re-verifies, and pushes. On ANY surprise it `git reset --hard`es to the captured pre-state and REFUSES —
    it never loses work, never side-picks, never hand-merges, and never claims a success it didn't earn."""
    root = root or validate.ROOT
    default = default or _default_branch(root)
    a = assess(root=root, default=default)
    if a["status"] != "fixable" or not apply:
        return {**a, "applied": False}

    branch = _current_branch(root)
    if not branch:
        return {"status": "needs-manual", "reason": "detached-head", "applied": False}
    if _dirty(root):
        return {"status": "needs-manual", "reason": "dirty-tree", "applied": False}
    pre = _run(["rev-parse", "HEAD"], root)
    if not pre:
        return {"status": "needs-manual", "reason": "no-head", "applied": False}
    base = a["base"]

    def _restore() -> None:
        _ok(["merge", "--abort"], root)            # convenience while a merge is in progress
        _ok(["reset", "--hard", pre], root)        # the UNIVERSAL restore — valid after an auto-completed merge

    def _refuse(reason: str) -> dict:
        _restore()
        return {"status": "needs-manual", "reason": reason, "applied": False}

    merged_clean = _ok([*_IDENT, "merge", "--no-ff", "--no-edit", base], root)
    if not merged_clean:
        conflicted = set(_unmerged(root))
        if not conflicted or (conflicted - set(MEMBERS)):     # an authored / unexpected conflict appeared
            return _refuse("unexpected-conflict")
        if not _regen_members(root):
            return _refuse("regen-failed")
        if not _ok(["add", *MEMBERS], root) or not _ok([*_IDENT, "commit", "--no-edit"], root):
            return _refuse("commit-failed")
    else:
        # The merge auto-completed (the members textually auto-merged). Regenerate anyway so the committed
        # members are the canonical regeneration of the merged sources, then record any change.
        if not _regen_members(root):
            return _refuse("regen-failed")
        if _dirty(root):
            if not _ok(["add", *MEMBERS], root) or not _ok(
                    [*_IDENT, "commit", "-m", "Regenerate engine index files from the reconciled tree"], root):
                return _refuse("commit-failed")

    # Re-verify locally BEFORE pushing: the branch must now merge into the default branch with no conflict
    # (the load-bearing reconcile-before-merge guarantee — the server-side merge button cannot run a local fix).
    if _merge_tree(base, root) != ("clean", []):
        return _refuse("verify-failed")
    # Plain push (NON-force). A non-fast-forward rejection means someone advanced the branch → refuse.
    if not _ok(["push", "origin", branch], root):
        return _refuse("push-rejected")
    return {"status": "reconciled", "branch": branch, "base": base, "applied": True}


# ---- operator-facing CLI copy (plain words; no git verbs reach the operator surface) ----------

def _plain_reconcile(apply: bool) -> int:
    r = reconcile(apply=apply)
    status, reason = r["status"], r.get("reason")
    if status == "healthy":
        print("No pull request is stuck — nothing to reconcile.")
    elif status == "reconciled":
        print("Done — I reconciled your pull request against the latest main and pushed the result. Both "
              "pieces of work are still there; nothing was lost. If another change lands before you merge, "
              "I'll offer to do this again.")
    elif status == "needs-manual" and reason == "authored-conflict":
        print("I stopped and left everything exactly as it was — nothing changed, no work lost. This one I "
              "can't safely fix on my own: the two pieces of work changed the same actual content (not just "
              "the engine's index files), and choosing between them is a real decision. Tell me which "
              "direction you want, or ask me to walk you through the two versions in plain English — I'll do "
              "the rest once you've chosen.")
    elif status == "fixable" and not apply:
        print("This pull request is stuck on the engine's internal index files. I can fix it safely and keep "
              "both pieces of work (I reconcile it against the latest main and rebuild those files). Re-run "
              "with --apply to do it.")
    else:
        print("I stopped and left your work untouched — I couldn't safely finish this here. Nothing changed. "
              "Try again in a moment, or ask me what happened and I'll explain it in plain words.")
    return 0


# ---- the operator-runnable demo (throwaway git repos; the REAL reconcile) ---------------------

def _demo() -> int:
    import demo_pr_reconcile  # the walkthrough lives in its own demo_* file (the demo convention)
    return demo_pr_reconcile.main([])


def main(argv: list) -> int:
    if argv and argv[0] == "demo":
        return _demo()
    if argv and argv[0] == "reconcile":
        return _plain_reconcile(apply="--apply" in argv)
    # Default: classify THIS branch (build a GitHub reader the way boot does, lazily to avoid an import cycle).
    import boot
    repo, token = boot.repo_slug(), boot.gh_token()
    import telemetry
    gh = telemetry.GitHubIssues(repo, token) if repo and token else None
    hit = detect_conflict(gh)
    print(hit if hit else "no conflicting pull request detected for the current branch")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
