#!/usr/bin/env python3
"""Instance guarded-paths declaration check (#532) — the fail-CLOSED shape gate for a deployment's own
extra-guarded-paths list, plus a plain-language typo catcher.

A deployment can extend the guardrail-weakening guard's watched set with its OWN product-side paths — a
containment scanner and the like the engine cannot discover by presence — by committing
`.engine/operator-guarded-paths.json` (`{"guarded_paths": [...], "guarded_prefixes": [...]}`). The guard
(`weakening_guard.is_guardrail`) UNIONS that list with its built-in floor, read from the trusted base. Because
that list is a protection a non-engineer relies on, two failures must never reach the base branch silently:

  - a MALFORMED or DEGENERATE declaration (not an object, a non-string entry, or a prefix like ``""``/``.``/``/``
    that ``startswith`` would make match EVERY file — the "guards everything, so every merge needs an ack
    forever" footgun). These are HARD findings: the declaration cannot merge until fixed. Fail CLOSED here means
    the base-read guard never faces a malformed list, and the degenerate-prefix footgun is caught at the door.
  - a well-formed entry that names a path NOT PRESENT in the tree — a typo that silently protects nothing (the
    guard would just never match it). This is a SOFT finding ("guarded path X does not exist — it protects
    nothing"): it does not block, but it surfaces the mistake at the merge so the operator can fix it, closing the
    otherwise-invisible "I declared it but it never guarded anything" trap.

ABSENT is the normal steady state (the construction repo, and every deployment before its first declaration), so
with no file this check surfaces NOTHING — mirroring `policy_override_check` over `operator-overrides.json`. It is
a `custom/script` rule: it prints the finding.v1 array on stdout and returns 0; the run fails only on a HARD
finding (the custom/script kind's tiering). `ENGINE_GUARDED_PATHS_PATH` (unset in production) lets the
negative-fixture meta-check feed a seeded declaration so this gate is witnessed biting a real bad input (#286)."""
from __future__ import annotations
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate  # noqa: E402  (finding constructor + ROOT)

_FILE = ".engine/operator-guarded-paths.json"
_ABSENT = object()    # no file on disk -> surface nothing (the normal steady state)
_MALFORMED = object()  # a file that is not parseable JSON -> a hard finding

_NOT_JSON = ("Your list of extra protected files (" + _FILE + ") is not valid JSON, so the engine cannot read it "
             "and no path in it is being protected. Fix the file so it is valid JSON before merging.")
_NOT_OBJECT = ("Your list of extra protected files (" + _FILE + ") must be a single set of entries (a JSON object "
               "with “guarded_paths” and “guarded_prefixes” lists), but it is something else, so "
               "none of it is being applied. Fix the shape before merging.")
_UNKNOWN_KEY = ("Your list of extra protected files (" + _FILE + ") has an entry the engine does not recognise "
                "(“{key}”). The only entries allowed are “guarded_paths” and “guarded_prefixes”; anything else is "
                "ignored — and a value hidden under an unrecognised entry could quietly drop a protection without "
                "the safety check noticing. Remove “{key}” before merging.")
_NOT_LIST = ("In your list of extra protected files (" + _FILE + "), “{field}” must be a list, but it is "
             "something else, so it is being ignored. Make it a list of paths before merging.")
_BAD_PATH = ("In your list of extra protected files (" + _FILE + "), one entry under “guarded_paths” is not a "
             "path ({val}) — every entry must be a non-empty path relative to the repository root. Fix or remove "
             "it before merging.")
_ABS_PATH = ("In your list of extra protected files (" + _FILE + "), the entry “{val}” starts with “/” "
             "— paths here are relative to the repository root, so an absolute path never matches anything. Drop the "
             "leading “/” before merging.")
_BAD_PREFIX = ("In your list of extra protected files (" + _FILE + "), one entry under “guarded_prefixes” is not "
               "a folder prefix ({val}) — every entry must be a non-empty path relative to the repository root. Fix "
               "or remove it before merging.")
_DEGEN_PREFIX = ("In your list of extra protected files (" + _FILE + "), the folder prefix “{val}” would match "
                 "EVERY file in the project — so every change would need a deliberate acknowledgment, forever. A "
                 "prefix must name a real folder and end with “/” (for example “scanners/”). Fix "
                 "it before merging.")
_MISSING_PATH = ("In your list of extra protected files (" + _FILE + "), “{val}” does not exist in the project, "
                 "so it protects nothing — a change to that path would not be flagged. Fix the path (a typo?) or "
                 "remove the entry.")
_MISSING_PREFIX = ("In your list of extra protected files (" + _FILE + "), the folder prefix “{val}” matches no "
                   "folder in the project, so it protects nothing — check it names a real folder (a typo?) or remove "
                   "the entry.")

_DEGENERATE = {".", "/", "./"}


def _exists(rel: str, root: str) -> bool:
    """True iff `rel` (repo-relative) points at a real file or directory under `root`. Used only for the SOFT
    existence note — resolved under ROOT, never the process CWD, so it reads the real tree."""
    return os.path.exists(os.path.join(root, rel))


def _prefix_matches_a_folder(prefix: str, root: str) -> bool:
    """True iff a real folder under `root` corresponds to `prefix` (e.g. ``scanners/`` -> a ``scanners`` dir).
    Best-effort, filesystem-only (no tree walk): a directory at the prefix, stripped of its trailing slash."""
    return os.path.isdir(os.path.join(root, prefix.rstrip("/")))


def load_declaration(path: str):
    """Read the committed (or seeded) declaration. Returns the parsed object, `_ABSENT` (no file -> surface
    nothing), or `_MALFORMED` (present but not parseable JSON -> a hard finding)."""
    if not os.path.exists(path):
        return _ABSENT
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:  # noqa: BLE001 — a present-but-unreadable declaration is a hard shape failure
        return _MALFORMED


def findings(tier: str, decl, root: str | None = None) -> list:
    """The finding.v1 list for a declaration object (or the `_ABSENT`/`_MALFORMED` sentinels). HARD findings for a
    malformed/degenerate shape (fail closed — cannot merge); SOFT findings for a well-formed entry that names a
    path not present in the tree (a typo that protects nothing). `decl` is injectable for tests/demo; `root`
    defaults to the real repository root."""
    root = root if root is not None else validate.ROOT
    if decl is _ABSENT:
        return []                                           # no declaration -> nothing to say
    if decl is _MALFORMED:
        return [validate.finding(tier, _NOT_JSON)]
    if not isinstance(decl, dict):
        return [validate.finding(tier, _NOT_OBJECT)]
    out = []
    # Forbid unknown top-level keys. This is load-bearing for the weakening guard's shrink detector, not mere
    # tidiness: if an unrecognised key were allowed, a pull request could remove a path from `guarded_paths`
    # while re-adding the same string under that inert key — the diff would look unchanged and the removal would
    # slip past the shrink detector, silently un-guarding the path. With only the two honored keys, every place a
    # removed value can be re-added means it is genuinely still guarded.
    for key in decl:
        if key not in ("guarded_paths", "guarded_prefixes"):
            out.append(validate.finding(tier, _UNKNOWN_KEY.format(key=key)))
    paths = decl.get("guarded_paths", [])
    prefixes = decl.get("guarded_prefixes", [])
    if not isinstance(paths, list):
        out.append(validate.finding(tier, _NOT_LIST.format(field="guarded_paths")))
        paths = []
    if not isinstance(prefixes, list):
        out.append(validate.finding(tier, _NOT_LIST.format(field="guarded_prefixes")))
        prefixes = []
    for p in paths:
        if not isinstance(p, str) or not p.strip():
            out.append(validate.finding(tier, _BAD_PATH.format(val=repr(p))))
        elif p.startswith("/"):
            out.append(validate.finding(tier, _ABS_PATH.format(val=p)))
        elif not _exists(p, root):
            out.append(validate.finding("soft", _MISSING_PATH.format(val=p)))
    for p in prefixes:
        if not isinstance(p, str) or not p.strip():
            out.append(validate.finding(tier, _BAD_PREFIX.format(val=repr(p))))
        elif p.strip() in _DEGENERATE or not p.endswith("/"):
            out.append(validate.finding(tier, _DEGEN_PREFIX.format(val=p)))
        elif not _prefix_matches_a_folder(p, root):
            out.append(validate.finding("soft", _MISSING_PREFIX.format(val=p)))
    return out


def emit(fs: list) -> int:
    """Write the finding.v1 array to stdout (the custom/script machine channel) and return 0 — a successful
    evaluation, whatever it found. Human-readable prose lives inside each finding's `message`."""
    print(json.dumps(fs))
    return 0


def _demo() -> int:
    """Show the check over a planted declaration that has two mistakes — nothing on disk is touched. It plants a
    degenerate ``.`` prefix (would guard every file) and a made-up path (protects nothing), and prints what the
    operator would see at the merge gate. Self-checks: exactly one hard finding (the degenerate prefix) and one
    soft finding (the missing path)."""
    planted = {"guarded_paths": ["scanners/does_not_exist.py"], "guarded_prefixes": ["."]}
    fs = findings("hard", planted)
    print("What the merge gate would say about this extra-protected-files list:\n")
    for f in fs:
        print(f"  - [{f.get('severity')}] {f.get('message')}")
    hard = [f for f in fs if f.get("severity") == "hard"]
    soft = [f for f in fs if f.get("severity") == "soft"]
    ok = len(hard) == 1 and len(soft) == 1
    if not ok:
        print(f"\nDEMO UNEXPECTED: expected one hard (degenerate prefix) + one soft (missing path), "
              f"got {len(hard)} hard / {len(soft)} soft.", file=sys.stderr)
        return 1
    print("\nThe degenerate prefix blocks the merge (hard); the missing path is a non-blocking warning (soft).")
    return 0


def main(argv: list) -> int:
    if argv and argv[0] == "demo":
        return _demo()
    tier = os.environ.get("ENGINE_RULE_TIER", "hard")
    # ENGINE_GUARDED_PATHS_PATH (unset in production) lets the negative-fixture meta-check feed a seeded
    # declaration so this shape gate is witnessed biting a real bad input (#286).
    seeded = validate.env_override_path("ENGINE_GUARDED_PATHS_PATH")
    path = seeded if seeded else os.path.join(validate.ROOT, _FILE)
    return emit(findings(tier, load_declaration(path)))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
