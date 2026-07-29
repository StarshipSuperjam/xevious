#!/usr/bin/env python3
"""Deferred-work marker form — the custom/script entry for engine/check/engine-todo-form (eADR-0035).

A marker that records nothing is worse than no marker: it occupies the sanctioned form, so it reads as a
recorded deferral to every later reader and to `list`, while saying nothing about what is unbuilt. This check
goes red on exactly that case — a marker whose description, joined across its continuation lines, is empty.

The hard tier is held to that one unambiguous case ON PURPOSE. The recognition rule travels into repositories
full of text the engine did not author, so a rule that reddens committed source there must be narrow enough
that its verdict is never arguable. A parenthetical the grammar does not define is reserved for later
extension and reported soft, so widening the grammar later cannot redden a repository that already adopted an
unrecognised form.

Scope is the git-tracked tree. In a deployed repository the files an engine update overwrites are left out,
because a local fix to one is wiped on the next update; the deployed test fails toward SCANNING, so a checkout
whose origin cannot be read is reported in full rather than silently under-reported.

Emits finding.v1 JSON on stdout, exit 0 on a successful evaluation; a crash exits non-zero.
"""
from __future__ import annotations
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate  # noqa: E402
import engine_todo  # noqa: E402


def findings(tier: str, root: str = None) -> list:
    base = root or validate.ROOT
    # A seeded fixture root is scanned whole: it stands in for the repository, so an ownership skip
    # computed against the real tree has no meaning there.
    skip = set() if root else engine_todo.engine_owned_skip(base)
    out = []
    # A seeded tree is walked; a repository is read from its git index. Falling back to an empty list for a
    # seeded tree would let the negative-fixture meta-check pass while this check did nothing.
    try:
        found = engine_todo.markers(root=base, skip=skip, walk=bool(root))
    except engine_todo.Unreadable as exc:
        # Never a silent green: a scan that could not enumerate the tree reports that it could not look,
        # rather than the clean result an empty file list would otherwise produce.
        return [validate.finding(tier,
                "The deferred-work markers could not be checked because the list of files this repository "
                "tracks could not be read, so nothing was scanned — this is not a clean result. It usually "
                "means the check ran outside a git working copy, or without git available. Re-run it inside "
                f"the repository. ({exc})")]
    for marker in found:
        where = validate.loc(os.path.join(base, marker.path), marker.line)
        if not marker.description:
            out.append(validate.finding(tier,
                       f"'{marker.path}' line {marker.line} carries a deferred-work marker with no "
                       f"description, so it records that something is unbuilt without saying what — a later "
                       f"reader, and the command that lists outstanding work, both learn nothing from it. "
                       f"Write what is not built and what the code does instead on the same line (or on the "
                       f"lines that continue it), or remove the marker if nothing is actually owed.",
                       where))
        elif marker.ref is not None and not engine_todo._ISSUE_REF.match(marker.ref):
            out.append(validate.finding("soft",
                       f"'{marker.path}' line {marker.line} carries a marker whose parenthesised reference "
                       f"is not an issue number. That form is reserved for a later extension of the grammar, "
                       f"so nothing reads it today: either cite an issue number or drop the parentheses.",
                       where))
    return out


def main(argv: list) -> int:
    tier = os.environ.get("ENGINE_RULE_TIER", "hard")
    # ENGINE_TODO_FIXTURE_ROOT (unset in production) points the scan at a seeded tree so the negative-fixture
    # meta-check witnesses this guard biting a real bad input.
    fixture = validate.env_override_path("ENGINE_TODO_FIXTURE_ROOT")
    print(json.dumps(findings(tier, root=fixture)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
