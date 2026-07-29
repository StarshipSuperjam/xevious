#!/usr/bin/env python3
"""Local-reference containment — a deployment's own vocabulary, and the outbound work checked against it.

WHAT THIS IS FOR. Every repository has references that mean something only inside it: a decision-log id, a
spec section, a ticket prefix. They read as ordinary shorthand at home. Sent to another repository they name
a record that repository cannot reach — a reader meets a bare identifier and has nowhere to go. A surface
that travels must name the CAPABILITY it means, never a reference that resolves only where it was written.

A deployment declares its own vocabulary in `.engine/operator-local-references.json`. This module reads that
declaration and scans outbound work against it. The declaration ships ABSENT: a repository that has not
declared one is the normal steady state, and the scan then has nothing to match.

THE THREE DECLARED SHAPES, all PLAIN STRINGS. The operator never writes a regular expression — every string
is escaped before it becomes one, so a declaration cannot express a pathological pattern:

  - `id_prefixes`  — a prefix followed by digits. `"ACME-"` matches `ACME-156`, not `AACME-156` and not `D-`.
  - `phrases`      — a literal run of text, matched only on its own word boundaries.
  - `section_refs` — a document name followed by a SECTION MARKER (`§4`, `Law 5`, `Section 2`). This exists
                     because the bare name over-fires: a name like `acme-topology` appears both in a
                     citation (`acme-topology Law 5` — the defect) and in prose naming the rule it
                     stands for (`the acme-topology wall` — the FIX). Matching only the cited form
                     leaves the prose alone. Declaring such a name under `phrases` instead would flag the
                     very wording that resolves the defect, which trains an operator to click past findings
                     — worse than no check at all.

WHY THE READER LIVES HERE AND THE SHAPE GATE DOES NOT. `operator_local_references_check.py` is the merge
gate over the declaration's SHAPE, and it must fail CLOSED: a malformed declaration is a hard finding. This
module is the runtime READER, and it degrades: an absent declaration is silent. Those fail-directions are
opposite, so they stay in separate files — the same split the guarded-paths declaration already uses. Being
a separate file also keeps this one OUT of the guarded set: only a check rule's `params.script` is guarded
by presence, so tuning how a phrase matches never costs a deliberate guardrail acknowledgment.

WHAT A GREEN SCAN DOES AND DOES NOT MEAN. It means no DECLARED reference was found in what was read. It is
not a claim that the work carries nothing foreign — an undeclared vocabulary is unmatched by construction.
Callers must narrate the FIVE read states apart, and only one of them licenses "I checked and found none":

  - DECLARED   — entries were compiled and the change was scanned against them. A real claim.
  - EMPTY      — a declaration exists and lists nothing at all. A deliberate "this project has no shorthand",
                 so it is an answer rather than a gap; nothing was scanned, so nothing may be claimed.
  - UNUSABLE   — entries WERE listed and none survived (an unrecognised key, a too-short or non-text entry).
                 Distinct from EMPTY because the operator did state their shorthand and it was discarded;
                 telling them they said they have none would misreport their own instruction.
  - ABSENT     — no declaration at all. The engine offers to set one up.
  - UNREADABLE — a declaration exists and could not be parsed. The worst state to report as clean, because
                 the operator believes a check is protecting them.

Only DECLARED was scanned against. Reporting any of the other four as "clean" would be a false claim.

CLI (operator-runnable):
  uv run --directory .engine -- python tools/local_references.py demo
  uv run --directory .engine -- python tools/local_references.py scan --ref <ref> [--checkout <path>]
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate  # noqa: E402  (the finding constructor + ROOT)

DECLARATION_REL = ".engine/operator-local-references.json"
DECLARED_KEYS = ("id_prefixes", "phrases", "section_refs")

# Read states, told apart so a caller can narrate them honestly rather than collapsing them into "clean".
ABSENT = "absent"          # no declaration — the starting state; the engine offers to set one up
EMPTY = "empty"            # a declaration that deliberately lists nothing — "this project has no shorthand"
DECLARED = "declared"      # a declaration with entries; a scan against it is a real claim
UNUSABLE = "unusable"      # entries were listed but none survived — NEVER report this as "you have none"
UNREADABLE = "unreadable"  # a declaration is present but could not be parsed — NEVER report this as clean

# A reference sits on its own boundary: not butted against a letter, digit or underscore. A HYPHEN is
# deliberately NOT in this class — a reference is routinely hyphen-joined to surrounding words, as in the
# file name `docs/ACME-156-migration.md`, and excluding hyphens here would miss exactly those. `ACME-156` still
# does not match inside `AACME-156` (a letter on the left) or inside `ACME-1567` (the digits run on).
_EDGE = r"[0-9A-Za-z_]"
_LEFT, _RIGHT = rf"(?<!{_EDGE})", rf"(?!{_EDGE})"
# The section markers a cited document name is followed by. Deliberately closed: widening this is what turns
# a narrow citation match back into a bare-name match.
_MARKER = r"(?:§\s*\d+|(?:Law|Section|Part|Chapter|Rule)\s+\d+)"

# A scan enumerates at most this many hits by name; the rest are counted. A declaration broad enough to
# exceed it is reporting its own breadth — the failure mode a too-wide vocabulary produces in practice.
ENUMERATED_HITS = 12


def _compile(kind: str, token: str):
    """One declared string -> one compiled pattern. `re.escape` is load-bearing: the declaration is operator
    text, never a pattern, so no declared string can be a regular expression."""
    lit = re.escape(token.strip())
    if kind == "id_prefixes":
        body = lit + r"\d+"
    elif kind == "section_refs":
        body = lit + r"[\s,;:]*" + _MARKER
    else:
        body = lit
    return re.compile(_LEFT + body + _RIGHT, re.IGNORECASE)


def compile_vocabulary(decl) -> list:
    """The declaration object -> `[(kind, token, pattern)]`. Defensive: only non-empty strings survive, and a
    single-character entry is dropped (it would match nearly everything). Belt-and-braces behind the hard CI
    shape gate, which blocks such a declaration from reaching the base branch in the first place."""
    out = []
    if not isinstance(decl, dict):
        return out
    for kind in DECLARED_KEYS:
        for token in decl.get(kind) or []:
            if isinstance(token, str) and len(token.strip()) >= 2:
                out.append((kind, token.strip(), _compile(kind, token)))
    return out


def load_vocabulary(path: str | None = None) -> tuple:
    """Read the deployment's declaration. Returns `(compiled, state)` — one of the five states in the module
    docstring, of which only DECLARED lets a caller say "I checked and found none".

    Every state but DECLARED yields an empty vocabulary, and they are told apart precisely so that a caller
    cannot narrate an unscanned contribution as clean. ABSENT is the steady state before a first declaration;
    EMPTY is a deliberate "this project has no shorthand"; UNUSABLE means entries were listed and discarded;
    UNREADABLE is the worst to misreport, because the operator believes a check is protecting them."""
    path = path if path is not None else os.path.join(validate.ROOT, DECLARATION_REL)
    if not os.path.exists(path):
        return [], ABSENT
    try:
        with open(path, encoding="utf-8") as fh:
            decl = json.load(fh)
    except Exception:  # noqa: BLE001 — present but unparseable; the hard shape gate is the teeth, this is the flag
        return [], UNREADABLE
    if not isinstance(decl, dict):
        return [], UNREADABLE
    compiled = compile_vocabulary(decl)
    # Counted across EVERY key, not just the recognised ones: an entry typed under `ticket_ids` was still
    # something the operator told the engine, and it was thrown away.
    listed = sum(len(v) for v in decl.values() if isinstance(v, list))
    if not compiled and listed:
        return [], UNUSABLE
    if not compiled:
        # A declaration that lists nothing is NOT a declaration that was checked against. Collapsing it into
        # DECLARED would let a caller say "I checked and found none" when no pattern was compiled and the
        # change was never read — the exact false claim of cleanliness this module exists to prevent, and the
        # likeliest shape to hit it, since an empty skeleton is the natural first thing an operator writes.
        # It is a legitimate END state too: an operator who has none can say so once and stop being asked.
        return [], EMPTY
    return compiled, DECLARED


# ---- git transport (read-only; its own, deliberately NOT the path-list helper) -------------------

# The diff is asked for in a FIXED format, because the operator's own git configuration can otherwise change
# it out from under the parser — and every such change makes the scan report "clean" over something it never
# read. `--no-ext-diff` is the sharpest: a globally configured external differ (difftastic's own setup tells
# people to set one) emits output with no added-line markers at all, which would parse as an empty diff that
# was successfully inspected. `--text` closes the same hole for a file the project marks un-diffable in its
# committed `.gitattributes` — an ordinary setting on lock files and generated assets, which otherwise
# carries that file's references past the scan exactly as a rename would. The prefix and quoting flags pin
# the file-header shape the parser keys on.
_FORMAT = ("-U0", "--no-color", "--no-renames", "--no-ext-diff", "--no-textconv", "--text",
           "--src-prefix=a/", "--dst-prefix=b/")
_CONFIG = ("-c", "core.quotepath=false")   # non-ASCII paths render literally, not as \303\251 escapes


def _git(args: list, checkout: str | None = None, timeout: int = 60) -> bytes | None:
    """Run a read-only git command in `checkout` and return RAW stdout bytes, or None on any failure.

    Deliberately separate from the contribution package's path-list helper, which decodes strictly and
    strips. Neither is safe here: diff hunks are raw file bytes, so one file that is not valid UTF-8 would
    raise inside the decode and collapse the whole read to a failure, and stripping would drop the leading
    space that distinguishes a context line from an added one. Bytes are decoded by the caller with
    replacement, so an undecodable file costs a garbled character rather than an unread diff."""
    try:
        out = subprocess.run(["git", *_CONFIG, *args], capture_output=True, timeout=timeout,
                             check=False, cwd=checkout)
        return out.stdout if out.returncode == 0 else None
    except Exception:  # noqa: BLE001 — missing binary / OS error / timeout all degrade to "unavailable"
        return None


def added_lines(ref: str, checkout: str | None = None, *, run=_git) -> tuple:
    """The lines this branch ADDS against `ref`, as `[(path, line_number, text)]`, plus whether the diff was
    actually inspected: `(lines, inspected)`.

    Only ADDED lines are scanned. Scanning whole files would flag content the target repository already
    carries, which the contribution did not write. `--no-renames` is load-bearing: git renders a rename as a
    header with no `+` lines at all, so a file MOVED into the contribution would carry its references past
    the scan entirely; forcing the full add costs size and buys correctness, the same trade the outbound
    path-list read already makes by refusing to cap itself.

    `inspected` is False when git could not be read. A caller must never narrate cleanliness on an
    uninspected diff — a failed read is an unknown diff, not a clean one."""
    raw = run(["diff", *_FORMAT, f"{ref}...HEAD"], checkout)
    if raw is None:
        return [], False
    text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
    return _parse_added(text), True


def _parse_added(text: str) -> list:
    """Parse a unified diff STRUCTURALLY: each hunk header states how many lines of the new file follow, and
    exactly that many are consumed as hunk content.

    Sniffing each line's leading characters instead would let a line's own CONTENT be read as diff syntax.
    Git prefixes every added line with `+`, so an added line that itself begins `++ b/x` renders as `+++ b/x`
    — indistinguishable from a file header by shape. Reading it as one silently reattributes every following
    line to a filename lifted from the change's own text, which then drops out of the scan and travels into
    the published finding. Counting from the header makes that unreachable: inside a hunk, content is content.
    """
    out, path = [], None
    # split("\n"), NOT splitlines(): git counts a line as ending at a newline and nothing else, while
    # splitlines() also breaks on form feed, vertical tab, a lone carriage return and four Unicode
    # separators. Any of those inside an added line would make this parser see one more "line" than the hunk
    # header promised, desync the count, and silently discard the rest of that file — a form feed is the
    # conventional page separator in Python and C source, so this is ordinary content, not an attack.
    lines = iter(text.split("\n"))
    for line in lines:
        if line.startswith("diff --git "):
            path = None                                  # a new file's headers follow; forget the last one
        elif line.startswith("+++ "):
            header = line[4:]
            path = None if header == "/dev/null" else (header[2:] if header[:2] == "b/" else header)
        elif line.startswith("@@"):
            m = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", line)
            if not m:
                continue
            lineno = int(m.group(1))
            remaining = int(m.group(2)) if m.group(2) is not None else 1
            while remaining > 0:
                body = next(lines, None)
                if body is None:
                    break
                if body.startswith("+"):
                    out.append((path, lineno, body[1:]))
                    lineno += 1
                    remaining -= 1
                elif body.startswith(" "):
                    lineno += 1                          # context line: occupies a line, adds nothing
                    remaining -= 1
                elif body.startswith("-") or body.startswith("\\"):
                    continue                             # a removed line / "no newline" marker costs no count
                else:
                    break                                # not hunk content — the hunk ended early
    return out


def changed_paths(ref: str, checkout: str | None = None, *, run=_git) -> tuple:
    """The paths this branch changes against `ref`, and whether the read succeeded. A path NAME can carry a
    reference just as a line can (`docs/ACME-156-migration.md`)."""
    raw = run(["diff", "--name-only", "--no-renames", f"{ref}...HEAD"], checkout)
    if raw is None:
        return [], False
    text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
    return [p for p in text.splitlines() if p.strip()], True


# ---- the scan -----------------------------------------------------------------------------------

def scan(vocabulary: list, *, lines=None, paths=None, blobs=None) -> list:
    """Every declared reference found in the outbound material. Returns `[{where, line, kind, token, text}]`.

    Three material kinds, because a contribution carries its references in three places: the lines it adds,
    the paths it changes, and the prose it sends alongside them (`blobs` — the pull-request title and body,
    which travel to the other repository just as the diff does).

    The declaration file itself is skipped, as a line and as a path. Its own content contains the declared
    strings by definition, so scanning it would report the operator's vocabulary back to them as a leak on
    every change to it. `blobs` are labelled prose, not paths, so the skip does not apply there."""
    hits = []
    for kind, token, pattern in vocabulary:
        for path, lineno, text in (lines or []):
            # Compared EXACTLY, not by file name: matching any path merely ending in that name would make
            # `vendor/x-operator-local-references.json` or `docs/operator-local-references.json` scan-free
            # zones, and the only path the reader ever loads is this one.
            if path == DECLARATION_REL:
                continue
            m = pattern.search(text)
            if m:
                hits.append({"where": path or "(unknown file)", "line": lineno,
                             "kind": kind, "token": _cap(m.group(0)), "declared": token})
        for path in (paths or []):
            if path == DECLARATION_REL:
                continue
            m = pattern.search(path)
            if m:
                hits.append({"where": path, "line": None, "kind": kind,
                             "token": _cap(m.group(0)), "declared": token})
        for label, text in (blobs or {}).items():
            for n, line in enumerate((text or "").splitlines(), start=1):
                m = pattern.search(line)
                if m:
                    hits.append({"where": label, "line": n, "kind": kind,
                                 "token": _cap(m.group(0)), "declared": token})
    return hits


def ordered(hits: list) -> list:
    """Hits sorted by where they sit, not by which declared entry found them.

    `scan` loops patterns on the outside, so its raw order groups every hit of the first declared entry
    before any hit of the second. Truncating THAT order would hide whole entries behind "and N more" — the
    operator fixes what they were shown, re-prepares, and meets a fresh pause naming things they had no way
    to know about. Ordering by location spreads the shown set across the entries that actually fired."""
    return sorted(hits, key=lambda h: (h.get("where") or "", h.get("line") or 0, h.get("token") or ""))


TOKEN_CAP = 40   # a matched token is PUBLISHED into an engine-opened issue; bound what can land there


def _cap(token: str) -> str:
    """Bound a matched token before it is reported. An id prefix is followed by `\\d+` with no limit, so a
    declared prefix sitting beside a long run of digits produces an arbitrarily long string — and that string
    is embedded verbatim in an engine-opened GitHub Issue."""
    return token if len(token) <= TOKEN_CAP else token[:TOKEN_CAP] + "…"


def _summarize(hits: list) -> str:
    """The hits in plain words: the matched token and where it sits, never the surrounding line. The line
    could contain anything the change touched, and this string is published — it becomes the first sentence
    of an engine-opened issue title and is embedded in its body."""
    shown = ordered(hits)[:ENUMERATED_HITS]
    parts = [f"“{h['token']}” in {h['where']}" + (f" line {h['line']}" if h["line"] else "") for h in shown]
    rest = len(hits) - len(shown)
    if rest > 0:
        parts.append(f"and {rest} more")
    return "; ".join(parts)


def too_broad_note(hits: list) -> str:
    """The plain sentence for a scan that matched implausibly many places — shared, so the OPERATOR reads the
    same diagnosis the recorded finding carries. Breadth is surfaced here rather than at the merge gate,
    which cannot walk a deployment's whole tree to judge it; showing the operator a wall of hits without
    naming the likely cause would leave them fixing symptoms of their own declaration."""
    if len(hits) <= ENUMERATED_HITS:
        return ""
    return ("  Your list matched this many places, which usually means one of its entries is too broad to be "
            "useful — a check that fires on everything is one people learn to click past. Worth narrowing "
            "before you act on this.")


def findings(tier: str, hits: list) -> list:
    """The finding list for a scan. SOFT by construction: this is a decision for the operator to make about
    their own contribution, never a refusal. `tier` is accepted for symmetry with the check surface and
    deliberately not honoured for the scan legs."""
    del tier
    if not hits:
        return []
    breadth = too_broad_note(hits)
    return [validate.finding(
        "soft",
        "This contribution carries references that mean something only in your own project, so they would "
        "name nothing a reader of the other project can reach: " + _summarize(hits) + "." + breadth,
        {"file": ordered(hits)[0]["where"], "line": ordered(hits)[0]["line"]})]


# ---- CLI ----------------------------------------------------------------------------------------

def _scan_cli(argv: list) -> int:
    """Scan a checkout's outbound branch against THIS tree's declaration.

    The two are deliberately different trees. On the owned-product path the engine builds a product that
    lives in a SEPARATE checkout, so the vocabulary to check against is the ENGINE's — the repository whose
    references would dangle — while the diff to scan is the product's. Reading the declaration from the
    checkout being scanned would read the target's own (absent) declaration and report clean forever."""
    ref, checkout = None, None
    for i, a in enumerate(argv):
        if a == "--ref" and i + 1 < len(argv):
            ref = argv[i + 1]
        elif a == "--checkout" and i + 1 < len(argv):
            checkout = os.path.expanduser(argv[i + 1])
    if not ref:
        print("Tell me what to compare against: --ref <branch-or-ref> [--checkout <path>]", file=sys.stderr)
        return 2
    vocabulary, state = load_vocabulary()
    if state == ABSENT:
        print("This project has not declared a local reference vocabulary, so there was nothing to check "
              f"against. To declare one, write {DECLARATION_REL} with any of: "
              + ", ".join(DECLARED_KEYS) + ".")
        return 0
    if state == UNUSABLE:
        print("Your list of local references has entries the engine could not use — an entry it does not "
              "recognise, or one too short to be a reference — so none of them were applied and nothing was "
              "checked. That is not the same as this contribution being clean.", file=sys.stderr)
        return 1
    if state == EMPTY:
        # Handled distinctly for the same reason the prepared narration does: this path is the one the
        # owned-product runbook MANDATES, and that path has no merge gate behind it — so a confident green
        # here is the one nothing else would catch.
        print("Your list of local references lists nothing, so there was nothing of that kind to look for. "
              "That is a fine answer if this project has no shorthand of its own — but it is not the same as "
              "this contribution having been checked.")
        return 0
    if state == UNREADABLE:
        print(f"Your local reference vocabulary ({DECLARATION_REL}) could not be read, so this contribution "
              "was NOT checked. That is not the same as clean — fix the file, then run this again.",
              file=sys.stderr)
        return 1
    lines, seen_lines = added_lines(ref, checkout)
    paths, seen_paths = changed_paths(ref, checkout)
    if not (seen_lines and seen_paths):
        print(f"I could not read what this branch changes against “{ref}”, so nothing was checked. That is "
              "not the same as clean.", file=sys.stderr)
        return 1
    hits = scan(vocabulary, lines=lines, paths=paths)
    if not hits:
        print(f"Checked against your declared vocabulary — nothing in this branch would name a record the "
              f"other project cannot reach. ({len(lines)} added lines, {len(paths)} changed paths.)")
        return 0
    for f in findings("soft", hits):
        print(f["message"])
    return 1


def _demo() -> int:
    """Show the scan over a planted vocabulary and a planted outbound change — nothing on disk is touched.

    The planted material is the real shape this exists for: a citation that must be caught, a piece of
    capability prose naming the same document that must NOT be caught, and a path name carrying an id."""
    decl = {"id_prefixes": ["ACME-"], "section_refs": ["acme-topology"], "phrases": ["Acme Handbook"]}
    vocabulary = compile_vocabulary(decl)
    lines = [
        ("src/app.py", 12, "# kept out of git (acme-topology Law 5; ACME-156) — see the log"),
        ("src/app.py", 40, "# the acme-topology rule: your checkout stays a viewing surface"),
        ("README.md", 3, "Follow the Acme Handbook when contributing."),
        ("src/ok.py", 7, "# a plain comment naming what the code does"),
    ]
    paths = ["docs/ACME-156-migration.md", "src/app.py"]
    hits = scan(vocabulary, lines=lines, paths=paths)
    print("A contribution about to go to another project, checked against this project's own vocabulary.\n")
    for h in hits:
        print(f"  caught  “{h['token']}” in {h['where']}" + (f" line {h['line']}" if h["line"] else ""))
    print("\n  left alone  line 40 — “the acme-topology rule” names the capability rather than citing")
    print("              a section, which is exactly the wording that FIXES this defect.")
    print("  left alone  line 7 — no declared reference.\n")
    for f in findings("soft", hits):
        print(f"  [{f['severity']}] {f['message']}")
    tokens = sorted(h["token"] for h in hits)
    ok = tokens == ["ACME-156", "ACME-156", "Acme Handbook", "acme-topology Law 5"] and all(
        f["severity"] == "soft" for f in findings("soft", hits))
    if not ok:
        print(f"\nDEMO UNEXPECTED: expected both ACME-156 citations (one in a line, one in a path name), the "
              f"section citation and the phrase, all soft; got {tokens}.", file=sys.stderr)
        return 1
    print("\nThe citation and the id are caught; the prose naming the same rule is not. Nothing blocks — "
          "this is a decision the operator makes about their own contribution.")
    return 0


def main(argv: list) -> int:
    if argv and argv[0] == "demo":
        return _demo()
    if argv and argv[0] == "scan":
        return _scan_cli(argv[1:])
    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
