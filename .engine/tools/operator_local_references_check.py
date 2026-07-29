#!/usr/bin/env python3
"""Local-reference declaration check — the fail-CLOSED shape gate for a deployment's own reference vocabulary.

A deployment declares the references that mean something only inside it — a decision-log id, a spec section,
a ticket prefix — by committing `.engine/operator-local-references.json`. The contribution paths read that
declaration and scan outbound work against it (`local_references.py`), so work does not carry a reference
that names nothing a reader of the other repository can reach.

Because the operator relies on that scan, two failures must never reach the base branch silently:

  - a MALFORMED or DEGENERATE declaration (not an object, an unrecognised entry, a non-list value, a
    non-string or blank member, or a single-character member that would match nearly every line). These are
    HARD findings: the declaration cannot merge until fixed. Failing closed here means the runtime reader
    never faces a malformed list, and the matches-everything footgun is caught at the door.
  - a declaration that is present but UNPARSEABLE. The reader treats that as "no vocabulary", so the scan
    silently checks nothing — the state most easily mistaken for clean.

ABSENT is the normal steady state (this repository, and every deployment before its first declaration), so
with no file this check surfaces NOTHING — mirroring the guarded-paths and saved-settings gates.

WHAT THIS CHECK DELIBERATELY DOES NOT DO. It does not read the repository to judge whether a declared entry
is too broad. A `custom/script` that crashes or overruns its two-minute budget becomes a HARD finding
whatever its own tier, so a whole-repository content scan here would red a deployment's required CI over the
size or encoding of that deployment's own tree — the very defect class this gate was built alongside fixing.
Breadth surfaces where it is cheap and harmless instead: on the bounded outbound diff, at scan time, where a
declaration that matches an implausible number of places says so in its own finding.

It is a `custom/script` rule: it prints the finding.v1 array on stdout and returns 0; the run fails only on
a HARD finding. `ENGINE_LOCAL_REFERENCES_PATH` (unset in production) lets the negative-fixture meta-check
feed a seeded declaration so this gate is witnessed biting a real bad input.
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import local_references  # noqa: E402  (the declared-key vocabulary — one source of truth)
import validate  # noqa: E402  (finding constructor + ROOT)

_FILE = local_references.DECLARATION_REL
_KEYS = local_references.DECLARED_KEYS

_ABSENT = object()     # no file on disk -> surface nothing (the normal steady state)
_MALFORMED = object()  # present but not parseable JSON -> a hard finding

_KEY_LIST = "“" + "”, “".join(_KEYS) + "”"

_NOT_JSON = ("Your list of local references (" + _FILE + ") is not valid JSON, so the engine cannot read it. "
             "Nothing is being checked against it — which is not the same as your work being clean. Ask the engine "
             "to put the file right, and it will.")
_NOT_OBJECT = ("Your list of local references (" + _FILE + ") must be a single set of entries (a JSON object "
               "with " + _KEY_LIST + " lists), but it is something else, so none of it is being applied. Ask "
               "the engine to put the shape right, and it will.")
_UNKNOWN_KEY = ("Your list of local references (" + _FILE + ") has an entry the engine does not recognise "
                "(“{key}”). The only entries allowed are " + _KEY_LIST + "; anything else is ignored — and a "
                "value hidden under an unrecognised entry would quietly check nothing while looking as "
                "though it did. Ask the engine to remove “{key}”, and it will.")
_NOT_LIST = ("In your list of local references (" + _FILE + "), “{field}” must be a list, but it is something "
             "else, so it is being ignored. Ask the engine to make it a list of plain words, and it will.")
_BAD_ENTRY = ("In your list of local references (" + _FILE + "), one entry under “{field}” is not a plain word "
              "or phrase ({val}) — every entry must be a plain, non-empty word or phrase. Ask the engine to fix or remove it.")
_TOO_SHORT = ("In your list of local references (" + _FILE + "), the entry “{val}” under “{field}” is a "
              "single character, so it would match nearly every line of every change — every contribution "
              "would be flagged, forever. Use the whole reference — for example “ACME-” rather than “A”.")

_ADVICE = {
    "id_prefixes": "an id prefix such as “ACME-”, matched when digits follow it",
    "phrases": "a literal phrase, matched on its own word boundaries",
    "section_refs": "a document name, matched only when a section marker follows it (“Law 5”, “§4”)",
}


def load_declaration(path: str):
    """Read the committed (or seeded) declaration. Returns the parsed object, `_ABSENT` (no file -> surface
    nothing), or `_MALFORMED` (present but not parseable JSON -> a hard finding).

    Kept separate from `local_references.load_vocabulary`, which degrades an unreadable file to an empty
    vocabulary. That is the right behaviour for a runtime reader standing behind this gate and the wrong
    behaviour for the gate itself: the two fail in opposite directions on the same file, so they stay in
    separate files rather than one reader serving both."""
    if not os.path.exists(path):
        return _ABSENT
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:  # noqa: BLE001 — a present-but-unreadable declaration is a hard shape failure
        return _MALFORMED


def findings(tier: str, decl) -> list:
    """The finding.v1 list for a declaration object (or the `_ABSENT`/`_MALFORMED` sentinels). All findings
    here are the rule's tier (hard in CI): every one of them is a declaration that cannot do its job."""
    if decl is _ABSENT:
        return []                                            # no declaration -> nothing to say
    if decl is _MALFORMED:
        return [validate.finding(tier, _NOT_JSON)]
    if not isinstance(decl, dict):
        return [validate.finding(tier, _NOT_OBJECT)]
    out = []
    # Forbid unknown top-level keys. Not tidiness: an unrecognised key would hold entries the reader never
    # compiles, so the operator would believe a reference was covered while nothing matched it — a silent
    # gap in a check they rely on, which is worse than an empty declaration they can see is empty.
    for key in decl:
        if key not in _KEYS:
            out.append(validate.finding(tier, _UNKNOWN_KEY.format(key=key)))
    for field in _KEYS:
        entries = decl.get(field, [])
        if not isinstance(entries, list):
            out.append(validate.finding(tier, _NOT_LIST.format(field=field)))
            continue
        for e in entries:
            if not isinstance(e, str) or not e.strip():
                out.append(validate.finding(tier, _BAD_ENTRY.format(field=field, val=repr(e))))
            elif len(e.strip()) < 2:
                out.append(validate.finding(tier, _TOO_SHORT.format(val=e.strip(), field=field)))
    return out


def emit(fs: list) -> int:
    """Write the finding.v1 array to stdout (the custom/script machine channel) and return 0 — a successful
    evaluation, whatever it found. Human-readable prose lives inside each finding's `message`."""
    print(json.dumps(fs))
    return 0


def _demo() -> int:
    """Show the gate over a planted declaration that has two mistakes — nothing on disk is touched. It
    plants a single-character entry (would match nearly every line) and an entry the engine does not
    recognise (would check nothing while looking as though it did). Self-check: exactly two hard findings."""
    planted = {"id_prefixes": ["A"], "ticket_numbers": ["ACME-"]}
    fs = findings("hard", planted)
    print("What the merge gate would say about this list of local references:\n")
    for f in fs:
        print(f"  - [{f.get('severity')}] {f.get('message')}")
    print("\nThe entries the engine does recognise:")
    for k in _KEYS:
        print(f"  - {k}: {_ADVICE[k]}")
    hard = [f for f in fs if f.get("severity") == "hard"]
    if len(hard) != 2:
        print(f"\nDEMO UNEXPECTED: expected two hard findings (the single character and the unrecognised "
              f"entry), got {len(hard)}.", file=sys.stderr)
        return 1
    print("\nBoth block the merge: an entry that would match everything, and entries hidden under a name "
          "the engine does not know — the two ways this list stops being worth having. (A list that is "
          "deliberately EMPTY is fine: it records that this project has no shorthand of its own.)")
    return 0


def main(argv: list) -> int:
    if argv and argv[0] == "demo":
        return _demo()
    tier = os.environ.get("ENGINE_RULE_TIER", "hard")
    seeded = validate.env_override_path("ENGINE_LOCAL_REFERENCES_PATH")
    path = seeded if seeded else os.path.join(validate.ROOT, _FILE)
    return emit(findings(tier, load_declaration(path)))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
