#!/usr/bin/env python3
"""hooks_path_health — the standing broken-`core.hooksPath` detector + repair (issues StarshipSuperjam/engine-template#707, StarshipSuperjam/engine-template#708; part of #690).

Catches when git's `core.hooksPath` is SET to a directory that NO LONGER EXISTS, so a git hook the operator
relies on (the engine-mechanic `pre-push` ADR-containment guardrail is the motivating case) is silently
disabled — git runs no hooks and says nothing. A folder move that leaves `hooksPath` pointing at the old,
now-gone location is exactly how StarshipSuperjam/engine-template#690 happened: `git worktree repair` fixes worktree PATHS but never touches
`hooksPath`, and the value lives only in LOCAL git config (never a tracked file), so no commit can fix it. The
detect / surface / consent split mirrors the sibling health detectors (`license_health`, `checkout_health`):
provisioning/boot detects, boot surfaces the plain-language line, the operator consents to the repair.

OFFLINE + READ-ONLY at the core. `detect_broken_hooks_path()` reads `core.hooksPath` from the CURRENT
worktree (`git -C <top> ...`) at each git config scope it can reason about, resolves the value the way git
itself does, and fires ONLY when a set value resolves to a directory that does not exist. The verdict depends
solely on git-config values and directory existence (`os.path.isdir`) — it reads NO hook file contents, no
network, no clock — so it is DETERMINISTIC and CONTENT-FREE (StarshipSuperjam/engine-template#708). It emits NO operator prose (the leaf law
keeps git verbs off the operator surface); boot renders the plain-language offer.

RESOLUTION (faithful to how git locates a client-side hook):
  - Broken-ness is detected with a plain `git config --type=path --get core.hooksPath` (the EFFECTIVE value,
    works on any git) so the check can never go quiet on a scope-flag that an old git lacks — this detector is
    deliberately un-silenceable (kept out of the retire allowlist), so its detection must be too.
  - `--type=path` expands a leading `~`; a RELATIVE value is left relative and resolved against the worktree
    TOP (`git rev-parse --show-toplevel`), because git runs a non-bare-repo hook with the worktree root as its
    working directory — verified against git's actual behaviour, not its docs.
  - Scope ROUTING (which scope the repair may touch) is read with the scope flags `--worktree` / `--local`
    (git labels the shared `.git/config` value `local`; there is no "shared" scope token). The per-worktree
    override is only meaningful when `extensions.worktreeConfig` is on — off, `--worktree` silently collapses
    to `--local`, so the worktree read is gated on that extension being on.

REPAIR — conservative-complete (operator-confirmed), removal-only, lossless-or-it-does-not-run:
  - Unset the CURRENT worktree's OWN broken override (`--worktree`) — always safe, affects only this worktree.
  - Unset the SHARED (`--local`) value ONLY when it is ABSOLUTE-and-missing — an absolute path is broken
    identically in EVERY worktree, so removing it is universally peer-safe AND is what makes NEW worktrees stop
    inheriting the stale value.
  - NEVER sweep peer worktrees' own overrides (that is cross-session interference — each peer self-heals on its
    OWN next boot); NEVER auto-touch a RELATIVE shared value (it could resolve to a real dir in a peer worktree)
    or a `global`/`system` value the removal-only repair cannot address — those route to `needs-manual`, which
    boot surfaces with a safe operator-guided path, never a dead-end (StarshipSuperjam/engine-template#708 "safe repair path, no silent bypass").
  - The `isdir` guard is RE-CHECKED per scope immediately before each `--unset` (not cached from detection), so
    a directory that reappears in the window is never unset — the one path by which this repair could disable a
    WORKING hook is closed. Unsetting reverts git to its built-in default (`.git/hooks`, always present), so the
    worst case is removing a pointer that already pointed at nothing.
  - Its ONLY git mutation is `config --unset`. It NEVER runs reset / clean / checkout / any force flag / push /
    stash drop (the tests source-scan for those tokens, as `checkout_health`'s repairs do).

Removal-only is a deliberate scope choice: this repair can only ever move `hooksPath` toward "unset" (git's
default). It cannot RE-POINT a guardrail whose hook directory legitimately moved to a new path — correct for
engine-template, which ships no git hooks and whose correct value IS unset; a fork that ships hooks and points
`hooksPath` at them would want to re-point, not unset, which is out of scope here.

CLI:  python tools/hooks_path_health.py              # classify THIS worktree's core.hooksPath (signal or healthy)
      python tools/hooks_path_health.py repair        # dry-run: what the repair WOULD unset (no mutation)
      python tools/hooks_path_health.py repair --apply # unset THIS worktree's stale value (only if broken)
      python tools/hooks_path_health.py demo          # detection + repair walkthroughs on throwaway fixtures
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import sys

_HOOKS_PATH_KEY = "core.hooksPath"


def _run(cmd: list, cwd: str | None = None, timeout: int = 15) -> str | None:
    """Run a local git command and return raw stdout, or None on any non-zero / failure. Never raises — every
    read is best-effort, and the SessionStart pack must never stall on a hung git, so every call is bounded
    (the `checkout_health` / `license_health` convention). Stdout is returned UNSTRIPPED so an empty-value read
    (`""`) stays distinguishable from an unset read (None)."""
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False, cwd=cwd)
        return out.stdout if out.returncode == 0 else None
    except Exception:  # noqa: BLE001 — a missing binary / timeout / OS error all degrade to "unavailable"
        return None


def _status(cmd: list, cwd: str | None = None, timeout: int = 15) -> int | None:
    """Run a local git MUTATION and return its exit code (None on spawn failure). Used for `config --unset`,
    where git returns 5 for an already-absent key — a lost race under concurrent worktrees, NOT a failure."""
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False, cwd=cwd)
        return out.returncode
    except Exception:  # noqa: BLE001
        return None


def _blank(value: str | None) -> bool:
    """A value that is unset (None) or the empty string — neither points at a location, so neither is broken."""
    return value is None or value == ""


def _toplevel(cwd: str | None = None) -> str | None:
    """The current worktree's top-level directory (git runs a client-side hook with this as its cwd, so a
    relative hooksPath resolves against it). None for a bare repo / non-repo / git unavailable."""
    out = _run(["git", "-C", cwd or ".", "rev-parse", "--show-toplevel"], cwd=cwd)
    return out.strip() if out else None


def _worktree_config_on(top: str) -> bool:
    """Whether `extensions.worktreeConfig` is on — only then is a `--worktree` override a real, separate scope.
    Off, git collapses `--worktree` onto `--local`, so we must not read/route a worktree scope that isn't one."""
    out = _run(["git", "-C", top, "config", "--type=bool", "--get", "extensions.worktreeConfig"])
    return bool(out) and out.strip() == "true"


def _scoped(top: str, scope: str) -> str | None:
    """The `core.hooksPath` value at a specific config scope ("local" | "worktree"), git-resolved as a path
    (`--type=path` expands `~`). None when unset at that scope; "" for a set-but-empty value. Parsed by
    stripping only the trailing newline (a full `.strip()` would corrupt a value with leading/trailing space)."""
    out = _run(["git", "-C", top, "config", f"--{scope}", "--type=path", "--get", _HOOKS_PATH_KEY])
    return None if out is None else out.rstrip("\n")


def _effective(top: str) -> str | None:
    """The EFFECTIVE `core.hooksPath` git would use (highest-priority scope), via a plain `--get` that works on
    any git version — the robust broken-ness probe, so a missing scope flag never silences detection."""
    out = _run(["git", "-C", top, "config", "--type=path", "--get", _HOOKS_PATH_KEY])
    return None if out is None else out.rstrip("\n")


def _resolve(value: str, top: str) -> str:
    """The absolute directory a hooksPath value points at. `~` is already expanded by `--type=path`; a relative
    value resolves against the worktree top (how git runs the hook)."""
    path = value if os.path.isabs(value) else os.path.join(top, value)
    return os.path.normpath(os.path.abspath(path))


def _missing(value: str, top: str) -> bool:
    """Whether a (non-blank) hooksPath value resolves to a directory that does not exist. `isdir` fails LOUD
    (an unreadable/absent dir returns False and so fires) — the detector never falls toward all-clear."""
    return not _blank(value) and not os.path.isdir(_resolve(value, top))


def _fingerprint(parts: list) -> str:
    """A stable identity for the ledger collapse: the same broken configuration fingerprints the same
    session-to-session (so an unchanged alarm collapses to a terse reminder), and changes when the broken value
    changes (so a NEW breakage re-surfaces full). Content-derived; never shown to the operator."""
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:16]


def detect_broken_hooks_path(cwd: str | None = None) -> dict | None:
    """OFFLINE, READ-ONLY, DETERMINISTIC, CONTENT-FREE. Returns a prose-free dict when `core.hooksPath` is SET
    and its resolved directory does not exist; else None — healthy (unset, or the resolved dir exists),
    unresolvable (bare/non-repo), or git unavailable. All non-fire paths are fail-soft quiet (never a crash
    into boot's SessionStart), but a SET-and-missing value is reported, never softened.

    Fires when ANY of these is broken, so latent breakage that would infect FUTURE worktrees is caught, not
    only the value active in this one:
      - the current worktree's own `--worktree` override,
      - the shared `--local` value (even when a valid worktree override currently masks it),
      - a `global`/`system` value (surfaced as needs-manual — the removal-only repair can't address it).

    The returned dict carries the structured facts boot needs to render and the repair needs to act:
      plan_kind        "fixable" (an auto `--unset` applies) | "manual" (surface-only, operator-guided);
      worktree_broken  the current worktree's own override is set-and-missing (auto-fixable, peer-safe);
      local_absolute   the shared value is set-and-missing AND absolute (auto-fixable, peer-safe universally);
      local_relative   the shared value is set-and-missing AND relative (manual — may be valid in a peer);
      external_broken  the effective value comes from global/system scope and is missing (manual);
      top / effective_scope / fingerprint (the ledger-collapse identity)."""
    top = _toplevel(cwd)
    if top is None:
        return None
    wt_on = _worktree_config_on(top)
    wt_val = _scoped(top, "worktree") if wt_on else None
    local_val = _scoped(top, "local")
    eff_val = _effective(top)
    if _blank(wt_val) and _blank(local_val) and _blank(eff_val):
        return None  # nothing set anywhere -> git default .git/hooks -> healthy

    wt_broken = (not _blank(wt_val)) and _missing(wt_val, top)
    local_broken = (not _blank(local_val)) and _missing(local_val, top)
    local_absolute = local_broken and os.path.isabs(local_val)  # type: ignore[arg-type]
    local_relative = local_broken and not os.path.isabs(local_val)  # type: ignore[arg-type]

    # Effective scope + broken-ness — for the global/system case a scoped read never reaches.
    if not _blank(wt_val):
        eff_scope, eff = "worktree", wt_val
    elif not _blank(local_val):
        eff_scope, eff = "local", local_val
    elif not _blank(eff_val):
        eff_scope, eff = "external", eff_val  # global or system config
    else:
        eff_scope, eff = None, None
    external_broken = eff_scope == "external" and eff is not None and _missing(eff, top)

    if not (wt_broken or local_broken or external_broken):
        return None  # something is set, but everything set resolves to a real directory -> healthy

    fixable = wt_broken or local_absolute
    return {
        "top": top,
        "plan_kind": "fixable" if fixable else "manual",
        "worktree_broken": wt_broken,
        "local_broken": local_broken,
        "local_absolute": local_absolute,
        "local_relative": local_relative,
        "external_broken": external_broken,
        "effective_scope": eff_scope,
        "fingerprint": _fingerprint([
            f"wt:{wt_val if wt_broken else ''}",
            f"local:{local_val if local_broken else ''}",
            f"ext:{eff if external_broken else ''}",
        ]),
    }


def assess(cwd: str | None = None) -> dict:
    """OFFLINE, no mutation: the repair PLAN. status is "healthy" (nothing broken), "fixable" (one or more
    peer-safe `--unset` steps apply), or "needs-manual" (broken but nothing the removal-only repair may safely
    touch — a relative shared value or a global/system value). `plan` is the ordered subset of
    {"unset-worktree", "unset-local"} that applies."""
    d = detect_broken_hooks_path(cwd)
    if d is None:
        return {"status": "healthy", "plan": [], "detail": None}
    plan: list = []
    if d["worktree_broken"]:
        plan.append("unset-worktree")
    if d["local_absolute"]:
        plan.append("unset-local")
    status = "fixable" if plan else "needs-manual"
    return {"status": status, "plan": plan, "top": d["top"], "detail": d}


def repair(cwd: str | None = None, apply: bool = False) -> dict:
    """Removal-only, lossless-or-it-does-not-run. Dry-run by default (returns the plan, mutates nothing);
    `apply=True` unsets. Each scope's `isdir` guard is RE-CHECKED immediately before its `--unset` (never
    cached from `assess`), so a value that became valid in the window is left untouched. An already-absent key
    (git exit 5, a lost race under concurrent worktrees) counts as done, not failed."""
    a = assess(cwd)
    if a["status"] != "fixable" or not apply:
        return {**a, "applied": False, "did": [], "skipped": []}
    top = a["top"]
    did: list = []
    skipped: list = []
    for step in a["plan"]:
        scope = "worktree" if step == "unset-worktree" else "local"
        val = _scoped(top, scope)  # re-read at apply time
        # Re-verify THIS scope's plan gate against the LIVE value immediately before unsetting (TOCTOU): the value
        # must still resolve to a missing dir, and — for the shared value — still be ABSOLUTE (a value that flipped
        # to relative in the window could resolve to a real dir in a peer worktree, so it is never auto-unset).
        if not _missing(val, top) or (step == "unset-local" and not os.path.isabs(val)):
            skipped.append(step)
            continue
        rc = _status(["git", "-C", top, "config", f"--{scope}", "--unset", _HOOKS_PATH_KEY])
        if rc in (0, 5):  # 5 = key already absent -> the desired end-state is reached
            did.append(step)
        else:
            skipped.append(step)
    # Success is whole-config clean, NOT merely "nothing auto-fixable left": a residual needs-manual value
    # (a shared-relative or global/system broken value the removal-only repair won't touch) means a hook is STILL
    # disabled, so report needs-manual (routing the operator to the guided path) — never a false "fixed".
    if detect_broken_hooks_path(cwd) is None:
        status = "fixed"
    else:
        status = "needs-manual" if assess(cwd)["status"] == "needs-manual" else "partial"
    return {"status": status, "applied": True, "did": did, "skipped": skipped, "top": top}


# ---- in-tool demo: a self-checking falsification (issues StarshipSuperjam/engine-template#707, StarshipSuperjam/engine-template#708) --------------------------

def _git(root: str, *args: str) -> None:
    subprocess.run(["git", "-C", root, *args], capture_output=True, text=True, check=False)


def _repo(tmp: str, name: str, *, worktree_config: bool = True) -> str:
    """A throwaway committed git checkout, optionally with `extensions.worktreeConfig` on (the target repo's
    state, and what makes `--worktree` a real scope)."""
    root = os.path.join(tmp, name)
    os.makedirs(root, exist_ok=True)
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    if worktree_config:
        _git(root, "config", "extensions.worktreeConfig", "true")
    _git(root, "commit", "-qm", "seed", "--allow-empty")
    return root


def _demo() -> int:
    import tempfile
    print("What this proves: the detector fires ONLY when core.hooksPath is SET to a directory that does not")
    print("exist, preserves an unset value and a value pointing at a real directory, and the repair unsets a")
    print("stale value while REFUSING to touch a valid one.\n")
    with tempfile.TemporaryDirectory() as tmp:
        unset = _repo(tmp, "unset")

        broken = _repo(tmp, "broken")
        _git(broken, "config", "core.hooksPath", os.path.join(tmp, "gone-hooks"))  # absolute, missing

        valid = _repo(tmp, "valid")
        good = os.path.join(valid, "myhooks")
        os.makedirs(good, exist_ok=True)
        _git(valid, "config", "core.hooksPath", good)

        d_unset = detect_broken_hooks_path(cwd=unset)
        d_broken = detect_broken_hooks_path(cwd=broken)
        d_valid = detect_broken_hooks_path(cwd=valid)

        print(f"1) core.hooksPath unset                              -> healthy (None): {d_unset is None}")
        print(f"2) core.hooksPath = /a/missing/dir                   -> FIRES:          {d_broken is not None}")
        print(f"3) core.hooksPath = an existing dir                  -> healthy (None): {d_valid is None}")

        # Repair: dry-run mutates nothing, --apply unsets the stale value, and re-detection goes clean.
        dry = repair(cwd=broken, apply=False)
        still_set = _effective(_toplevel(broken))
        done = repair(cwd=broken, apply=True)
        after = detect_broken_hooks_path(cwd=broken)
        print(f"4) repair dry-run leaves the value in place          -> unchanged:      {not _blank(still_set)}")
        print(f"5) repair --apply unsets it, re-detect is clean      -> fixed:          "
              f"{done['status'] == 'fixed' and after is None}")

        # Refuses a valid value: repair on the healthy 'valid' repo does nothing and the value survives.
        r_valid = repair(cwd=valid, apply=True)
        valid_survives = not _blank(_effective(_toplevel(valid)))
        print(f"6) repair never unsets a VALID hooks path            -> preserved:      "
              f"{r_valid['status'] == 'healthy' and valid_survives}")

        print("\n7) The plain-language line the operator sees (an offer, ranked below the safety alarms):\n")
        import boot  # lazy: boot is fully loaded by demo time, and a top-level import would cycle
        signals = boot.gather_signals()
        signals["hooks_path"] = {**d_broken, "collapsed": False}
        print(boot.render_dashboard(signals))
        rendered = boot.render_dashboard(signals)

        ok = (d_unset is None and d_broken is not None and d_valid is None
              and dry["applied"] is False and not _blank(still_set)
              and done["status"] == "fixed" and after is None
              and r_valid["status"] == "healthy" and valid_survives
              and "your project's hooks" in rendered)  # boot renders the actual hooks_path offer line
        if not ok:
            print("\nDEMO UNEXPECTED: detection, repair, or the boot offer line did not behave as expected.",
                  file=sys.stderr)
            return 1
    return 0


def main(argv: list) -> int:
    if argv and argv[0] == "demo":
        return _demo()
    if argv and argv[0] == "repair":
        r = repair(apply="--apply" in argv)
        status, applied = r["status"], r.get("applied", False)
        if status == "healthy":
            print("core.hooksPath resolves to a real directory (or is unset) — nothing to repair.")
        elif status == "needs-manual":
            pre = f"Cleared {', '.join(r['did'])}; " if applied and r.get("did") else ""
            print(f"{pre}a shared or global core.hooksPath still points at a missing directory that the automatic "
                  "repair won't change — it may be in use by another worktree; investigate it with the operator.")
        elif not applied:
            print(f"Would clear the stale core.hooksPath: {', '.join(r['plan'])} (dry-run; add --apply to apply).")
        elif status == "fixed":
            print(f"Cleared the stale core.hooksPath ({', '.join(r['did']) or 'it had already changed'}).")
        else:  # partial
            print(f"Cleared {', '.join(r['did']) or 'nothing'}; some steps couldn't complete — re-run to retry.")
        return 0
    d = detect_broken_hooks_path()
    print("healthy — core.hooksPath resolves (or is unset)" if d is None
          else f"core.hooksPath points at a missing directory ({d['plan_kind']}).")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
