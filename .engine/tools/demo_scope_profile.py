#!/usr/bin/env python3
"""Operator-runnable demo: the change-profile classifies real files by their catalogued kind, buckets an
uncatalogued file as "other", and sums the size correctly — and it is REPORT-ONLY: it never blocks a merge.

Run: uv run --directory .engine -- python tools/demo_scope_profile.py

This exercises the REAL profile logic against the REAL wiring map (`.engine/knowledge/graph.json`), not a
re-implementation: it reads the actual surface catalogue, feeds a known set of changed files through the
very `profile()` / `surface_kind()` the tool ships, and checks the classification and arithmetic by eye.
Read the three blocks:
  [1] real catalogued files are labelled by their real kind, and an uncatalogued file falls to "other",
  [2] the size totals (+added / -deleted, file count) sum correctly,
  [3] the profile is report-only — the one check it rides on (the Behaviors nudge) is SOFT, so nothing
      here can ever block your merge.
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scope_profile  # noqa: E402

CHECK_JSON = os.path.join(scope_profile.ROOT, ".engine", "check", "pr-behaviors-declared.json")
# A path no surface catalogue would ever carry — must classify as "other".
_UNCATALOGUED = ".engine/tools/__demo_not_a_real_surface__.py"


def _sample_catalogued(kmap: dict, n: int = 3) -> list:
    """Pick up to n REAL catalogued paths of distinct kinds, deterministically (sorted)."""
    by_kind: dict = {}
    for path in sorted(kmap):
        by_kind.setdefault(kmap[path], path)
    picks = []
    for kind in sorted(by_kind):
        picks.append((by_kind[kind], kind))
        if len(picks) == n:
            break
    return picks


def _classification_holds() -> bool:
    """Real catalogued files get their real kind; an uncatalogued file becomes 'other'."""
    kmap = scope_profile.kind_map()
    samples = _sample_catalogued(kmap)
    rows = [(1, 0, path) for path, _kind in samples] + [(1, 0, _UNCATALOGUED)]
    prof = scope_profile.profile(rows, kmap)

    ok = True
    for path, kind in samples:
        got = scope_profile.surface_kind(path, kmap)
        print(f"   {path}\n      -> classified as '{got}' (catalogue says '{kind}')")
        ok = ok and got == kind
    other = scope_profile.surface_kind(_UNCATALOGUED, kmap)
    print(f"   {_UNCATALOGUED}\n      -> classified as '{other}' (expected 'other')")
    ok = ok and other == "other"

    # the aggregate kinds tally must match what we fed in
    expected: dict = {}
    for _p, kind in samples:
        expected[kind] = expected.get(kind, 0) + 1
    expected["other"] = expected.get("other", 0) + 1
    print(f"   aggregate kinds: {prof['kinds']}  (expected {expected})")
    return ok and prof["kinds"] == expected


def _arithmetic_holds() -> bool:
    """File count and +added / -deleted totals sum exactly."""
    rows = [(10, 2, "a"), (5, 0, "b"), (0, 8, "c")]
    prof = scope_profile.profile(rows, {})
    print(f"   fed 3 files (+15 / -10) -> profile reports "
          f"{prof['files']} files, +{prof['added']} / -{prof['deleted']}")
    return prof == {"files": 3, "added": 15, "deleted": 10,
                    "kinds": {"other": 3}, "areas": {"a": 1, "b": 1, "c": 1}}


def _never_blocks_holds() -> bool:
    """The falsifiable guarantee: the check the profile rides on is SOFT — it can only nudge."""
    with open(CHECK_JSON, encoding="utf-8") as fh:
        rule = json.load(fh)
    tier = rule.get("tier")
    print(f"   the Behaviors check ({rule.get('id')}) tier: '{tier}' "
          f"({'can only warn' if tier == 'soft' else 'WOULD BLOCK'})")
    return tier == "soft"


def main() -> int:
    print("=" * 78)
    print("The change-profile: classifies real files, sums the size, and never blocks a merge")
    print("=" * 78)

    print("\n[1] Real catalogued files by kind; an uncatalogued file falls to 'other'.")
    print("-" * 78)
    classified = _classification_holds()

    print("\n[2] The size totals sum correctly.")
    print("-" * 78)
    arithmetic = _arithmetic_holds()

    print("\n[3] The profile is report-only — the Behaviors check it rides on is SOFT.")
    print("-" * 78)
    advisory = _never_blocks_holds()

    ok = classified and arithmetic and advisory
    print()
    if not ok:
        print("DEMO UNEXPECTED — these did not hold:", file=sys.stderr)
        for name, held in [("classification", classified), ("arithmetic", arithmetic),
                           ("report-only", advisory)]:
            if not held:
                print(f"  - {name}", file=sys.stderr)
        return 1
    print("All held: real files are classified by their catalogued kind, an uncatalogued file falls to "
          "'other',\nthe size sums correctly, and the profile can only ever describe — never block.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
