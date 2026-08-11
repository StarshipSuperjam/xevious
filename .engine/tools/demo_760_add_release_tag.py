#!/usr/bin/env python3
"""Behavioral FALSIFICATION for issue #760 — `module add` (and `upgrade`) must resolve the engine's recorded
release to the home's REAL published tag before fetching, so a home that tags releases `vX.Y.Z` is fetched
correctly instead of 404ing on the bare version.

The bug: the engine records its release BARE (`_bump_engine_manifest` strips a leading `v`), so the manifest
holds a VERSION (`0.4.1`), not a fetchable TAG. `add` fed that bare version straight to the release fetch,
which requests `…/tarball/0.4.1` and 404s on a home whose real tag is `v0.4.1` — blocking the sanctioned add /
re-add of every optional module. `upgrade` never tripped because it already resolved through
`_resolve_release_ref`; the fix routes `add` through the same resolver and teaches that resolver to turn a bare
version into the home's published tag.

The resolver's network probe (`_release_tag_published` — a direct `releases/tags/{tag}` lookup) is a named
inductive gap that never runs in the construction repo, so this demo INJECTS a fake home and exercises the REAL
resolution logic (`_resolve_release_ref` → `_resolve_bare_version_tag` → the candidate order) against it — the
same call `add`/`upgrade` make. Read the cells by eye:

  [1] a `v`-tagging home (the common convention, and what this home uses): the recorded bare `0.4.1` resolves to
      the real tag `v0.4.1`. (This is the case that 404'd before the fix.)
  [2] a bare-tagging home: `0.4.1` resolves to `0.4.1` — format-agnostic, not a `v`-only special case.
  [3] a real tag / a sha / `latest` is left UNTOUCHED and never triggers a probe — only a bare version is
      resolved, so the tag-pin supply-chain control is unchanged.
  [4] a home that publishes NO release for the version: refused as MISSING (loud, names the home) — classified
      by `_release_is_missing`, never mis-degraded as a transport failure.
  [5] NEGATIVE CONTROL (the pre-#760 behavior): with the `v`-candidate dropped, the bare version does not
      resolve on a `v`-tagging home — reproducing the exact 404 this fix removes. (Restore the candidate list
      and this cell flips back to resolving.)

Run it directly: `uv run --directory .engine -- python tools/demo_760_add_release_tag.py`.
"""
from __future__ import annotations
import contextlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import module_manager as mm  # noqa: E402


@contextlib.contextmanager
def _home_publishing(*tags: str):
    """Inject a fake home whose published release tags are exactly `tags`, so the REAL resolver runs offline —
    `_release_tag_published` is the single network boundary, replaced here and restored afterward."""
    published = set(tags)
    saved = mm._release_tag_published
    mm._release_tag_published = lambda tag, repo=None, token=None: tag in published
    try:
        yield
    finally:
        mm._release_tag_published = saved


def _v_tagging_home_resolves_the_bare_version() -> bool:
    with _home_publishing("v0.4.1", "v0.4.0"):
        got = mm._resolve_release_ref("0.4.1", repo="acme/engine-home")
    ok = got == "v0.4.1"
    print(f"   recorded bare 0.4.1 on a v-tagging home -> resolved tag: {got!r}   (candidates tried: "
          f"{mm._release_ref_candidates('0.4.1')})")
    return ok


def _bare_tagging_home_resolves_bare() -> bool:
    with _home_publishing("0.4.1"):
        got = mm._resolve_release_ref("0.4.1", repo="acme/engine-home")
    ok = got == "0.4.1"
    print(f"   recorded bare 0.4.1 on a bare-tagging home -> resolved tag: {got!r}")
    return ok


def _real_ref_passes_through_without_a_probe() -> bool:
    # A non-bare ref must never touch the network: make any probe an error, then confirm it is not called.
    saved = mm._release_tag_published
    mm._release_tag_published = lambda *a, **k: (_ for _ in ()).throw(AssertionError("probed a pinned ref"))
    try:
        tag = mm._resolve_release_ref("v0.4.1", repo="acme/engine-home")          # a real tag
        sha = mm._resolve_release_ref("abc1234def5678", repo="acme/engine-home")  # a sha
    finally:
        mm._release_tag_published = saved
    ok = tag == "v0.4.1" and sha == "abc1234def5678"
    print(f"   pinned tag v0.4.1 -> {tag!r}; sha abc1234def5678 -> {sha!r}   (no probe; both unchanged)")
    print(f"   _is_bare_version: 0.4.1={mm._is_bare_version('0.4.1')}  v0.4.1={mm._is_bare_version('v0.4.1')}  "
          f"main={mm._is_bare_version('main')}")
    return ok


def _no_release_is_a_named_missing_not_a_transport_degrade() -> bool:
    raised = None
    with _home_publishing():  # the home publishes nothing for this version
        try:
            mm._resolve_release_ref("0.4.1", repo="acme/engine-home")
        except Exception as exc:  # noqa: BLE001 — the demo classifies it below
            raised = exc
    classified_missing = raised is not None and mm._release_is_missing(raised)
    print(f"   no matching release -> raised {type(raised).__name__ if raised else None}; "
          f"_release_is_missing (refuse loudly, not degrade)? {classified_missing}")
    return classified_missing


def _negative_control_pre_fix_bare_version_404s_on_a_v_home() -> bool:
    # The pre-#760 behavior: fetch the bare recorded version verbatim. Simulated by dropping the v-candidate,
    # so on a v-tagging home the bare version resolves to nothing -> the fetch would 404 (the exact bug).
    saved = mm._release_ref_candidates
    mm._release_ref_candidates = lambda version: [version]   # bare only — no v-normalization
    reproduced = False
    try:
        with _home_publishing("v0.4.1"):
            try:
                mm._resolve_release_ref("0.4.1", repo="acme/engine-home")
            except Exception as exc:  # noqa: BLE001
                reproduced = mm._release_is_missing(exc)
    finally:
        mm._release_ref_candidates = saved
    print(f"   with the v-candidate dropped, bare 0.4.1 fails to resolve on a v-home (reproduces #760): "
          f"{reproduced}")
    return reproduced


def main() -> int:
    print("=" * 78)
    print("module add/upgrade resolve a bare recorded version to the home's real published tag (#760)")
    print("=" * 78)
    checks = [
        ("\n[1] v-tagging home: bare 0.4.1 -> v0.4.1 (the case that 404'd before the fix)",
         _v_tagging_home_resolves_the_bare_version),
        ("\n[2] bare-tagging home: bare 0.4.1 -> 0.4.1 (format-agnostic)",
         _bare_tagging_home_resolves_bare),
        ("\n[3] a real tag / sha passes through untouched, with no probe (tag-pin unchanged)",
         _real_ref_passes_through_without_a_probe),
        ("\n[4] no published release for the version -> a named MISSING refusal, not a transport degrade",
         _no_release_is_a_named_missing_not_a_transport_degrade),
        ("\n[5] NEGATIVE CONTROL: drop the v-candidate and bare 0.4.1 404s on a v-home (reproduces #760)",
         _negative_control_pre_fix_bare_version_404s_on_a_v_home),
    ]
    failures = []
    for title, fn in checks:
        print(title)
        print("-" * 78)
        if not fn():
            failures.append(title.strip())

    print("\n" + "=" * 78)
    if failures:
        print("DEMO #760 FAILED:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("DEMO #760 PASSED: a bare recorded version resolves to the home's real published tag before the "
          "fetch, so `module add` works on a v-tagging home; a pinned ref is left untouched, a genuinely "
          "missing release is refused loudly, and dropping the v-candidate reproduces the original 404.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
