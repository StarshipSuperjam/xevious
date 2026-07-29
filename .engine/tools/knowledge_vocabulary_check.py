#!/usr/bin/env python3
"""Knowledge-graph entity-type vocabulary guard — the custom/script entry for engine/check/knowledge-vocabulary.

The knowledge graph's entity types are DERIVED from the surface catalog: an entity's type is the catalogued
surface a file lives under, plus the literal 'module' for an installed module. But two files hard-code that
vocabulary as a closed list — the graph's format schema (.engine/schemas/knowledge.v1.json: the entity-`type`
enum and the entityId `pattern` alternation) and the knowledge-retrieval interface
(.engine/interfaces/knowledge-retrieval.json: the `find`-op `type` enum and the entity-id `pattern`s its ops
accept). The one fingerprint gate over the graph (knowledge-coverage) re-derives entity types from the catalog
and so is blind to a hard-coded list that drifts from it — exactly how a stray `state` type sat in both files
unseen (issue #131).

This check closes that blind spot. It reads the surface catalog as the source of truth, then, for EVERY file
that encodes the vocabulary, extracts each declared type set (every type `enum` and every entity-id `pattern`
alternation) and asserts it equals the catalogued surface names plus 'module'. A new surface added to the
catalog but not to these files — or a stray type left in a file but never catalogued — turns engine-ci red
with a plain-language message, so this class of format drift cannot return unseen.

Reads local committed files only — no network, no token — so it runs unchanged in the head-checkout engine-ci
context. Emits finding.v1 JSON on stdout and returns 0 on a successful evaluation: an empty array when every
site matches the catalog, one finding per drifting site (each naming the file, the site, and the difference).
An internal crash returns non-zero, which the custom/script kind turns into a hard fail-closed finding.
`demo` prints an operator-runnable fail-then-pass narration of the guard.
"""
from __future__ import annotations
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate  # noqa: E402

# The files that hard-code the entity-type vocabulary (exhaustively verified as the only two; a tool never
# hard-codes the list — the generator derives types from the catalog). Each is scanned for ALL vocabulary
# sites, so a new enum/pattern added inside either file is covered automatically.
_VOCAB_FILES = (
    ("the knowledge-graph schema (.engine/schemas/knowledge.v1.json)",
     os.path.join(validate.SCHEMAS_DIR, "knowledge.v1.json")),
    ("the knowledge-retrieval interface (.engine/interfaces/knowledge-retrieval.json)",
     os.path.join(validate.ENGINE_DIR, "interfaces", "knowledge-retrieval.json")),
)

_MESSAGE = ("The knowledge graph's entity-type vocabulary must equal the catalogued surface names plus "
            "'module' — the graph derives an entity's type from the surface catalog, so a file that hard-codes "
            "the type list must match it. To fix: add the new surface to .engine/schemas/surface-catalog.json, "
            "or remove the stray type from the file.")

# An entity-id pattern looks like '^(contract|policy|...|module):[A-Za-z0-9._-]+$'. Capture the alternation.
# Tokens are surface names: lowercase letters and hyphens (the catalog's own name grammar — e.g.
# codex-skill), joined by '|'.
_ALT_RE = re.compile(r"^\^\(([a-z][a-z|-]*[a-z])\):")


def expected_vocabulary(catalog: dict) -> set:
    """The source-of-truth type set: every catalogued surface name plus the literal 'module'."""
    return set((catalog or {}).get("surfaces", {})) | {"module"}


def _alternation_types(pattern: str):
    """The type set an entity-id `pattern` accepts, or None if the string is not an entity-id alternation.
    The 'module' + 'contract' anchors keep an unrelated pattern from being read as the vocabulary."""
    m = _ALT_RE.match(pattern)
    if not m:
        return None
    parts = m.group(1).split("|")
    if "module" in parts and "contract" in parts:
        return set(parts)
    return None


def iter_vocabulary_sites(node):
    """Yield (kind, type-set) for every vocabulary site reachable in a parsed JSON doc: a type `enum` (a list
    of strings carrying both 'module' and 'contract' — the surface-type vocabulary signature) or an entity-id
    `pattern` alternation. Recurses through the whole structure so a site at any depth is found."""
    if isinstance(node, dict):
        for key, val in node.items():
            if key == "enum" and isinstance(val, list) and all(isinstance(x, str) for x in val) \
                    and "module" in val and "contract" in val:
                yield ("type enum", set(val))
            elif key == "pattern" and isinstance(val, str):
                types = _alternation_types(val)
                if types is not None:
                    yield ("entity-id pattern", types)
            yield from iter_vocabulary_sites(val)
    elif isinstance(node, list):
        for item in node:
            yield from iter_vocabulary_sites(item)


def collect_sites(files=_VOCAB_FILES) -> list:
    """Every (file-label, kind, type-set) across the vocabulary-bearing files."""
    sites = []
    for label, path in files:
        for kind, types in iter_vocabulary_sites(validate.load_json(path)):
            sites.append((label, kind, types))
    return sites


def vocabulary_findings(expected: set, sites: list, tier: str, message: str = _MESSAGE) -> list:
    """One finding per site whose declared type set differs from the expected vocabulary, naming the file,
    the site kind, and the exact difference. Pure over its inputs (fixture-testable)."""
    exp = set(expected)
    findings = []
    for label, kind, types in sites:
        got = set(types)
        if got == exp:
            continue
        parts = []
        extra = sorted(got - exp)
        missing = sorted(exp - got)
        if extra:
            parts.append("type(s) the catalog does not define: " + ", ".join(repr(x) for x in extra))
        if missing:
            parts.append("catalogued type(s) it omits: " + ", ".join(repr(x) for x in missing))
        findings.append(validate.finding(
            tier, f"{message} Drift in {label} ({kind}): " + "; ".join(parts) + "."))
    return findings


def emit(findings: list) -> int:
    """Write the finding.v1 array to stdout and return 0 — a successful evaluation, whatever it found."""
    print(json.dumps(findings))
    return 0


def _demo() -> int:
    """An operator-runnable fail-then-pass demonstration over the REAL catalog and the REAL vocabulary files.
    Nothing on disk changes — the "broken" variant is built in memory. It shows the types match the catalog
    today and that the guard would catch the retired 'state' type if it ever re-entered a file."""
    tier = "hard"
    catalog = validate.load_json(validate.CATALOG_PATH)
    expected = expected_vocabulary(catalog)
    print("The engine's knowledge-graph entity types must match the surface catalog exactly.\n")
    print(f"  Catalogued surfaces + 'module' ({len(expected)}): {', '.join(sorted(expected))}\n")
    real = collect_sites()
    clean = vocabulary_findings(expected, real, tier)
    print(f"  Files that encode this vocabulary: {len(_VOCAB_FILES)}; vocabulary sites checked: {len(real)}.")
    if clean:
        print("  -> the check is RED on the files as they stand (see engine-ci):")
        for f in clean:
            print(f"       {f['message']}")
    else:
        print("  -> the check is GREEN: every site matches the catalog.")

    broken = real + [("a hypothetical edit that re-introduced the old 'state' type", "type enum",
                      expected | {"state"})]
    found = vocabulary_findings(expected, broken, tier)
    print("\nNow suppose someone re-introduced the retired 'state' type (shown here in memory only — your "
          "files are untouched):")
    if found:
        print(f"  -> the check turns RED: {found[-1]['message']}")
    print("\nThat is the backstop: a stray entity type (like the 'state' bug this fixed) can't quietly "
          "re-enter the graph's vocabulary — the build is blocked until it matches the catalog again.")
    if not found:
        print("\nDEMO UNEXPECTED: the guard did not catch the re-introduced retired 'state' type.",
              file=sys.stderr)
        return 1
    return 0


def main(argv: list) -> int:
    if argv and argv[0] == "demo":
        return _demo()
    tier = os.environ.get("ENGINE_RULE_TIER", "hard")
    # ENGINE_CATALOG_PATH (unset in production) lets the negative-fixture meta-check point the
    # expected-vocabulary side at a seeded catalog that drifts from the real vocabulary files, so the
    # gate is witnessed biting a real bad input (#286). The vocabulary sites still read the real files.
    catalog = validate.load_json(validate.env_override_path("ENGINE_CATALOG_PATH") or validate.CATALOG_PATH)
    return emit(vocabulary_findings(expected_vocabulary(catalog), collect_sites(), tier))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
