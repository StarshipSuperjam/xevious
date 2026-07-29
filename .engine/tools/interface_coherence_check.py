#!/usr/bin/env python3
"""Interface-resolution coherence guard — the custom/script entry for engine/check/interface-coherence.

Runs as a `custom/script` check in the CI suite: it discovers the engine's interface declarations
(`.engine/interfaces/*.json`), the answerable server handles wired in `.mcp.json`, and the present
non-default implementations, then runs the pure interface-resolution leg
(`validate.interface_resolution_findings`) over them. That leg owns three rules — more than one tool
installed to answer one interface (only one may be active), a present tool missing an operation the
interface declares (it can't reliably stand in), and the built-in that answers an interface not being
wired at all (a setup gap). This is the sibling of the agent-coherence and skill-coherence guards: the
same "surfaces, never silently picks" posture applied to the swappable capability seams.

HONEST LIMIT: implementations are discovered by the presence of a conforming file, and no engine
mechanism yet drops a second one — so in a real repo today nothing non-default is present to detect,
and the two hard rules (only-one-active, missing-operation) are exercised only by this check's negative
fixture. They begin firing on real inputs the day an install mechanism can add an implementation. The
one rule that IS live today is the built-in-not-wired note: it reads the real `.mcp.json`, so removing a
built-in server surfaces the setup gap for real.

Reads local committed files only — no network, no token — so it runs unchanged in the head-checkout
engine-ci context. `.mcp.json` is read TOLERANTLY: it is an operator/product-owned file, so an absent,
unreadable, or oddly-shaped one is treated as "no handles present", never a crash — because with nothing
non-default present, a consumer crash would be the only way this check could go red, and it must never
red on product content the engine does not own. Emits finding.v1 JSON on stdout and returns 0 on a
successful evaluation: an empty array when the set is coherent, one finding per problem (each carrying
the plain-language fix). An internal crash returns non-zero, which the custom/script kind turns into a
hard fail-closed finding. `demo` prints an operator-runnable fail-then-pass narration of the guard.
"""
from __future__ import annotations
import glob
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate  # noqa: E402

_INTERFACE_GLOB = ".engine/interfaces/*.json"
_MCP_PATH = ".mcp.json"
_MESSAGE = ("Each engine capability that can be swapped must have exactly one tool answering it — the engine "
            "never silently picks between two, which is why this is caught at merge.")


def engine_interfaces(root: str | None = None) -> list:
    """Parse the present interface declarations. A file that is not readable JSON is skipped rather than
    crashing the scan — a malformed declaration is caught by its own schema check, not this coherence leg."""
    base = root or validate.ROOT
    decls = []
    for path in sorted(glob.glob(os.path.join(base, _INTERFACE_GLOB))):
        try:
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            decls.append(data)
    return decls


def present_handles(root: str | None = None) -> set:
    """The set of answerable server handles wired in `.mcp.json` (the keys under `mcpServers`). Read
    tolerantly: `.mcp.json` is operator/product-owned, so an absent, unreadable, or wrong-shaped file
    yields an empty set, never a raise — the check must not go red on product content the engine does
    not own. When a fallback's handle is missing from this set, its interface's built-in is not wired."""
    path = os.path.join(root or validate.ROOT, _MCP_PATH)
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return set()
    servers = data.get("mcpServers") if isinstance(data, dict) else None
    return set(servers.keys()) if isinstance(servers, dict) else set()


def present_impls(fixture_dir: str | None = None) -> dict:
    """The present NON-DEFAULT implementations, keyed by interface id. In production this is empty: no
    engine mechanism drops a second conforming implementation yet, so there is nothing non-default to
    detect. The negative-fixture seam (ENGINE_INTERFACE_FIXTURE_DIR, unset in production) injects a
    seeded present set so the only-one-active rule is witnessed biting a real bad input, without a phantom
    implementation shipping into any adopter."""
    if fixture_dir:
        try:
            with open(os.path.join(fixture_dir, "present_impls.json"), encoding="utf-8") as handle:
                data = json.load(handle)
            if isinstance(data, dict):
                return data
        except (OSError, ValueError):
            return {}
    return {}


def emit(findings: list) -> int:
    """Write the finding.v1 array to stdout and return 0 — a successful evaluation, whatever it found."""
    print(json.dumps(findings))
    return 0


def _demo() -> int:
    """An operator-runnable fail-then-pass demonstration over the REAL guard and the REAL declarations.
    Nothing on disk changes — the "two tools installed" break is built in memory. It shows the engine's
    swappable capabilities each have exactly one answer today, and that the guard catches it if a second
    tool for the same capability were ever installed."""
    tier = "hard"
    decls = engine_interfaces()
    handles = present_handles()
    print("Your engine's swappable capabilities, and the safety check that makes sure exactly one tool "
          "answers each:\n")
    if not decls:
        print("  (no swappable capabilities are declared yet)")
        return 0
    for decl in decls:
        name = decl.get("title") or decl.get("id")
        fallback = (decl.get("fallback") or {}).get("handle")
        wired = "built-in wired and answering" if fallback in handles else "built-in NOT wired"
        print(f"  {str(name):28} {wired}")

    clean = validate.interface_resolution_findings(decls, present_impls(), handles, tier, _MESSAGE)
    if clean:
        print("\nThe safety check found a problem with the capabilities as installed (see engine-ci).")
    else:
        print("\nThe safety check: all clear — exactly one tool answers each capability, and every "
              "built-in is wired.")

    target = next((d for d in decls if d.get("id")), None)
    if target is None:
        return 0
    name = target.get("title") or target.get("id")
    broken_present = {target.get("id"): [
        {"handle": "extra-tool-a", "operations": [op.get("name") for op in (target.get("operations") or [])]},
        {"handle": "extra-tool-b", "operations": [op.get("name") for op in (target.get("operations") or [])]},
    ]}
    print(f"\nNow suppose a second tool were installed to answer '{name}' (shown here in memory only — your "
          f"files are untouched):")
    found = validate.interface_resolution_findings(decls, broken_present, handles, tier, _MESSAGE)
    if found:
        print(f"  -> the safety check turns RED: two tools would answer '{name}', and the engine must never "
              f"silently pick between them. The build is blocked until one is removed.")
    print("\nThat is the safety net: a swappable capability can never end up with two active answers and "
          "the engine quietly choosing one — the check catches it before it could be merged.")
    if not found:
        print("\nDEMO UNEXPECTED: the guard did not flag the two installed tools.", file=sys.stderr)
        return 1
    return 0


def main(argv: list) -> int:
    if argv and argv[0] == "demo":
        return _demo()
    tier = os.environ.get("ENGINE_RULE_TIER", "hard")
    # ENGINE_INTERFACE_FIXTURE_DIR (unset in production) lets the negative-fixture meta-check point the
    # present-implementation set at a seeded fixture, so the only-one-active rule is witnessed biting a real
    # bad input without a phantom implementation being discovered as real.
    fixture_dir = validate.env_override_path("ENGINE_INTERFACE_FIXTURE_DIR")
    findings = validate.interface_resolution_findings(
        engine_interfaces(), present_impls(fixture_dir), present_handles(), tier, _MESSAGE)
    return emit(findings)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
