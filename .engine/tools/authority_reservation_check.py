#!/usr/bin/env python3
"""Authority-tier reservation guard (issue #401) — the custom/script entry for
engine/check/ontology-authority-reservation.

Runs as a `custom/script` check in the CI suite: it reads the live surface catalog and every installed
module's manifest, and runs the pure reservation scan (`validate.authority_reservation_findings`) over them.
The scan enforces the ontology's locked reservation law — the top two authority ranks (`decisions`,
`standing-rules`) belong to the self-referential core alone (the `contract` and `policy` surfaces) — from two
angles: an added surface climbing to a reserved rank, or `contract`/`policy` knocked off its rank (the catalog
leg), and a non-core module declaring an `ontology-entry` in the reserved space at its source (the manifest
leg, the owner-based half the write-time seam guard in wiring.catalog_add cannot see).

This is the merge-gate half of the reservation; the wiring.catalog_add seam guard is the write-time half.
Reads local committed files only — no network, no token — so it runs unchanged in the head-checkout engine-ci
context. Emits finding.v1 JSON on stdout and returns 0 on a successful evaluation (an empty array when the
reservation holds, one finding per violation, each carrying the plain-language fix). An internal crash returns
non-zero, which the custom/script kind turns into a hard fail-closed finding. `demo` prints an
operator-runnable fail-then-pass narration of the guard.
"""
from __future__ import annotations
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate  # noqa: E402
import module_coherence  # noqa: E402  (the single present-set reader — reused so the manifest roster the
#                                        guard scans cannot diverge from the one module coherence itself walks)

_MESSAGE = ("To fix, give the surface an ordinary authority rank in .engine/schemas/surface-catalog.json (and "
            "remove any non-core module's attempt to define the core rulebook or standing rules), or restore "
            "the core surface's own rank.")


def _catalog() -> dict:
    """The live surface catalog. ENGINE_CATALOG_FIXTURE (unset in production) is the negative-fixture
    meta-check's seam: it points the read at a seeded bad catalog so the guard is witnessed biting a real bad
    input, without that fixture ever being the committed catalog."""
    return validate.load_json(validate.env_override_path("ENGINE_CATALOG_FIXTURE", validate.CATALOG_PATH))


def _manifests() -> list:
    """Every installed module's manifest as a dict — reusing module coherence's own discoverer so the roster
    the reservation scans is exactly the one coherence walks (no divergence, no silent no-op)."""
    return [m for _path, m in module_coherence.discover_manifests()]


def emit(findings: list) -> int:
    """Write the finding.v1 array to stdout and return 0 — a successful evaluation, whatever it found."""
    print(json.dumps(findings))
    return 0


def _demo() -> int:
    """An operator-runnable fail-then-pass demonstration over the REAL guard and the REAL surface catalog.
    Nothing on disk changes — the "broken" variant is built in memory. It shows that only the engine's own
    core holds its two highest authority ranks, and that the guard catches anything added that tries to
    outrank it."""
    tier = "hard"
    catalog = validate.load_json(validate.CATALOG_PATH)
    manifests = _manifests()
    surfaces = catalog.get("surfaces") or {}
    ladder = {"decisions": "the engine's HIGHEST rank (its core rulebook)",
              "standing-rules": "the engine's second-highest rank (its core standing rules)"}
    print("Your engine ranks its surfaces by authority — which one wins when two disagree:\n")
    for name, rec in surfaces.items():
        auth = rec.get("authority") if isinstance(rec, dict) else None
        print(f"  {str(name):11} {ladder.get(auth, 'an ordinary rank')}")

    clean = validate.authority_reservation_findings(catalog, manifests, tier, _MESSAGE)
    if clean:
        print("\nThe reservation guard found a problem in the catalog as committed (see engine-ci).", file=sys.stderr)
        return 1
    print("\nThe reservation guard: all clear — only the engine's own core holds its two highest ranks.")

    bad = {"surfaces": {**surfaces, "usurper": {"authority": "decisions"}}}
    found = validate.authority_reservation_findings(bad, manifests, tier, _MESSAGE)
    print("\nNow suppose someone added a surface 'usurper' claiming the engine's highest rank (shown here in "
          "memory only — your files are untouched):")
    if not found:
        print("\nDEMO UNEXPECTED: the guard did not flag the added top-rank surface.", file=sys.stderr)
        return 1
    print("  -> the guard turns RED: nothing added to the engine may outrank its own core rulebook, so the "
          "build is blocked until it is put back.")
    print(f"     it says: {found[0]['message']}")
    return 0


def main(argv: list) -> int:
    if argv and argv[0] == "demo":
        return _demo()
    tier = os.environ.get("ENGINE_RULE_TIER", "hard")
    return emit(validate.authority_reservation_findings(_catalog(), _manifests(), tier, _MESSAGE))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
