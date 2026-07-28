#!/usr/bin/env python3
"""Conduct shape check — the body-to-frontmatter correspondence for a codes-of-conduct
layer file. Each code declared in the frontmatter `codes` list must have exactly one matching
`## <title>` section in the body, and every `## ` section must map to a declared code; a `disables` list
must not appear on the engine defaults layer (it is operator-layer-only).

The other prose surfaces use the closed `shape` kind, which compares the body's headings to a FIXED list
of section names. Conduct cannot: its section titles are the codes' own titles (one per code, data-driven),
so the required set is not fixed. This is therefore a `custom/script` rule (the policy-override-stale
pattern): it reads each `.engine/conduct/*.md`, compares its `codes` frontmatter to its `## ` headings, and
prints a finding.v1 array on stdout, returning 0. With no conduct files it surfaces nothing.
"""
from __future__ import annotations
import glob
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate  # noqa: E402  (frontmatter + section reader + finding constructor)

_DIR = os.path.join(validate.ENGINE_DIR, "conduct")


def _titles(fm: dict) -> list:
    """The declared code titles, in order — skipping any malformed entry (the frontmatter rule reports
    those; this rule only checks the body correspondence of well-formed entries)."""
    return [str(c.get("title", "")).strip()
            for c in (fm.get("codes") or []) if isinstance(c, dict) and c.get("title")]


def findings(tier: str, paths=None) -> list:
    """One finding per body/frontmatter mismatch across the conduct layer files (or the supplied paths,
    injectable for tests/demo)."""
    out = []
    files = paths if paths is not None else sorted(glob.glob(os.path.join(_DIR, "*.md")))
    for path in files:
        rel = os.path.relpath(path, validate.ROOT)
        fm = validate.frontmatter(path) or {}
        titles = _titles(fm)
        title_set = set(titles)
        headings = [h.strip() for h in validate.section_order(validate.read(path))]
        heading_set = set(headings)
        for t in titles:
            if t not in heading_set:
                out.append(validate.finding(tier, f"In {rel}, the code of conduct titled “{t}” is "
                    f"listed in the settings block but has no matching “## {t}” section below it. "
                    "Add the section (the plain-language rule), or remove the entry."))
        for h in headings:
            if h not in title_set:
                out.append(validate.finding(tier, f"In {rel}, the section “## {h}” has no matching "
                    "entry in the settings block at the top. Add an entry for it (id, title, status), or "
                    "remove the section."))
        if os.path.basename(path) == "defaults.md" and fm.get("disables"):
            out.append(validate.finding(tier, f"In {rel}, a “disables” list appears on the "
                "engine's default codes of conduct, but disabling a default is for your own override file "
                "only. Remove it here; to drop a default, list its id under “disables” in "
                "operator.md."))
    return out


def emit(fs: list) -> int:
    """Write the finding.v1 array to stdout (the custom/script machine channel) and return 0."""
    print(json.dumps(fs))
    return 0


def _demo() -> int:
    """Show the check over a planted in-memory pair — nothing on disk is touched. It plants one file with a
    code that is missing its section and an orphan section, and prints what the operator would see."""
    import tempfile
    print("CONDUCT SHAPE CHECK DEMO — body sections must line up with the codes listed at the top.\n")
    good = ("---\ncodes:\n  - id: conduct-plain-language\n    title: Speak in plain language\n    "
            "status: active\n---\n\n## Speak in plain language\n\nI explain things in plain language.\n")
    bad = ("---\ncodes:\n  - id: conduct-plain-language\n    title: Speak in plain language\n    "
           "status: active\n  - id: conduct-missing\n    title: A code with no section\n    status: active\n"
           "---\n\n## Speak in plain language\n\nI explain things in plain language.\n\n"
           "## An orphan section\n\nThis section has no entry at the top.\n")
    with tempfile.TemporaryDirectory() as tmp:
        gp = os.path.join(tmp, "good.md")
        bp = os.path.join(tmp, "bad.md")
        with open(gp, "w") as fh:
            fh.write(good)
        with open(bp, "w") as fh:
            fh.write(bad)
        clean = findings("hard", paths=[gp])
        print(f"A well-formed file — findings: {len(clean)} (expected 0).")
        print("\nA file with a missing section and an orphan section — what the operator would see:\n")
        flagged = findings("hard", paths=[bp])
        for f in flagged:
            print(f"  - {f.get('message')}")
    ok = not clean and bool(flagged)
    if not ok:
        print("\nDEMO UNEXPECTED: the shape check did not behave as expected — a well-formed file should "
              "produce no findings and a file with a missing/orphan section should be flagged.", file=sys.stderr)
        return 1
    return 0


def main(argv: list) -> int:
    if argv and argv[0] == "demo":
        return _demo()
    tier = os.environ.get("ENGINE_RULE_TIER", "hard")
    # ENGINE_CONDUCT_DIR (unset in production) lets the negative-fixture meta-check point the shape
    # gate at a seeded conduct dir, so it is witnessed biting a real bad input (#286).
    conduct_dir = validate.env_override_path("ENGINE_CONDUCT_DIR")
    paths = sorted(glob.glob(os.path.join(conduct_dir, "*.md"))) if conduct_dir else None
    return emit(findings(tier, paths))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
