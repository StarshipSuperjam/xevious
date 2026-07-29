#!/usr/bin/env python3
"""Block-registry coherence guard — the custom/script entry for engine/check/block-coherence.

Runs as a `custom/script` check in the CI suite. It assembles the block-eligible invariant registry
(`module_coherence.block_eligible_registrations()` — modes' explore write-gate + engine-Issue reroute,
close's findings-disposition gate) and runs the pure block-registry leg
(`validate.block_budget_findings`) over it. That one leg owns TWO cross-field rules (the multi-rule
agent_coherence_findings shape), so a single first-class check validates the WHOLE invariant, never
half of it:

  1. BLOCK BUDGET — only PreToolUse and Stop may hard-block.
  2. MODE DIMENSION — every block declares the stances it is active in, as DATA, not code-only
     (eADR-0022): a non-empty `modes` list drawn from the valid stance
     vocabulary. This is the property U05b makes first-class — before it, the mode a block was active
     in lived only in modes.handler's control flow; now a missing/malformed declaration reds engine-ci.

Honest limit: the leg covers every DECLARED block. A PreToolUse/Stop deny that fires in code but is
never registered escapes it (the registry is consumer-assembled by hand — owes → 25's
registry-discovery pattern). This check proves the declared registry is well-formed, not that every
possible deny is registered — the message says so.

Reads local declarations only — no network, no token — so it runs unchanged in the head-checkout
engine-ci context. Emits finding.v1 JSON on stdout and returns 0 on a successful evaluation: an empty
array when the registry is coherent, one finding per problem (each carrying the plain-language fix).
An internal crash returns non-zero, which the custom/script kind turns into a hard fail-closed finding.
`demo` prints an operator-runnable fail-then-pass narration of the guard.
"""
from __future__ import annotations
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate  # noqa: E402
import modes  # noqa: E402
import module_coherence  # noqa: E402

_MESSAGE = ("Fix the block declaration so only PreToolUse/Stop hard-block and every block names the "
            "stances it is active in — see the owning system's BLOCK_INVARIANT (modes.py / close.py). "
            "This check covers every DECLARED block; a deny that fires in code but is never registered "
            "in block_eligible_registrations() is outside it (owes → 25's registry-discovery pattern).")


def registrations() -> list:
    """The block-eligible registry to validate. ENGINE_BLOCK_FIXTURE (unset in production) lets the
    negative-fixture meta-check point the read at a seeded registry JSON — a list of block dicts — so
    the coherence gate is witnessed biting a real bad input (#286) without a malformed invariant having
    to live in the real modes/close declarations. Unset → the live assembled registry."""
    fixture = validate.env_override_path("ENGINE_BLOCK_FIXTURE")
    if fixture:
        with open(fixture, encoding="utf-8") as fh:
            return json.load(fh)
    return module_coherence.block_eligible_registrations()


def emit(findings: list) -> int:
    """Write the finding.v1 array to stdout and return 0 — a successful evaluation, whatever it found."""
    print(json.dumps(findings))
    return 0


def _demo() -> int:
    """An operator-runnable fail-then-pass demonstration over the REAL registry. Nothing on disk
    changes — the "broken" variant is built in memory. It shows the engine's block declarations are
    well-formed (each on an allowed event, each naming its stances) and that the guard catches a block
    that drops its mode declaration or lands on an event that may not hard-block."""
    tier = "hard"
    present = module_coherence.block_eligible_registrations()
    print("Your engine's hard-block declarations, and the safety check that keeps each one on an "
          "allowed event and naming the stances it is active in:\n")
    for b in present:
        print(f"  {str(b.get('name')):26} event={b.get('event'):11} modes={b.get('modes')}")

    clean = validate.block_budget_findings(present, tier, _MESSAGE, stances=modes.STANCES)
    if clean:
        print("\nThe safety check found a problem with the block registry as declared (see engine-ci).")
    else:
        print("\nThe safety check: all clear — every declared block hard-blocks only on an allowed "
              "event and names the stances it is active in.")

    broken = [{k: v for k, v in present[0].items() if k != "modes"}] if present else [
        {"event": "PreToolUse", "name": "example", "owner": "modes"}]
    name = broken[0].get("name")
    found = validate.block_budget_findings(broken, tier, _MESSAGE, stances=modes.STANCES)
    print(f"\nNow suppose someone removed the mode declaration from '{name}' (shown here in memory "
          f"only — your files are untouched):")
    if found:
        print(f"  -> the safety check turns RED: '{name}' no longer names the stances it is active in, "
              f"so the mode dimension would be code-only again. The build is blocked until it is declared.")
    print("\nThat is the safety net: a block can't quietly drop its mode declaration or move to an "
          "event that may not hard-block — the check catches it before it could be merged.")
    if not found:
        print("\nDEMO UNEXPECTED: the guard did not flag the removed mode declaration.", file=sys.stderr)
        return 1
    return 0


def main(argv: list) -> int:
    if argv and argv[0] == "demo":
        return _demo()
    tier = os.environ.get("ENGINE_RULE_TIER", "hard")
    return emit(validate.block_budget_findings(registrations(), tier, _MESSAGE, stances=modes.STANCES))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
