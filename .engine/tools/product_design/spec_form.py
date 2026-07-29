#!/usr/bin/env python3
"""Product-spec form inspector — the read-only `custom/script` entry for engine/check/product-spec-form
(the product-design module's spec-form validation over a committed `docs/spec/` tree).

What it does: when a project carries a product specification under `docs/spec/`, it checks that the spec is
well-FORMED — not whether the design is right. A spec is a master index (`docs/spec/index.md`, the coherence
ledger that lists every capability, its stage, and a link to its document) plus one document per capability.
The checks are lifecycle-scaled by each document's stage:

- a not-yet-described slot (a placeholder for a capability not yet written) needs only a recognized stage
  marker and a place in the index — nothing more;
- an in-progress or settled document additionally needs its `## Summary`, `## Behavior`, and
  `## Acceptance criteria` sections, and a well-formed acceptance-criteria table (each row: what must be
  true | how it is verified | who can check it — you or the engine).

Across the tree it checks coherence: every document is listed in the index, every index entry points to a
document that exists, and the stage the index shows matches the stage the document declares.

Disclosed-no-op: when a project has no `docs/spec/` tree yet, it says so plainly (one soft note, never a
silent pass) — that is the normal state for a project that hasn't written its spec, and the check starts
working on its own once `docs/spec/` is added.

Honest floor — the engine/product wall and the read-only firewall: it inspects the product's own
`docs/spec/` tree ONLY, never the engine's `.engine/` tooling; it reads file contents to check structure but
never writes; and it checks FORM, not correctness — semantic quality and freshness stay unmonitored by
design. The lock decision on a settled spec is the operator's, gated separately.

Operator-communication law: the engine-internal lifecycle ladder (the stub/draft/locked markers a
document carries) NEVER surfaces to the operator as a raw token — every finding renders the stage in plain
language ("not yet described / in progress / settled"). The raw marker lives only in the document frontmatter
the engine attaches and in this script's own logic.

Tiers / blocking: a real structural problem in an in-progress or settled spec is reported at the rule's tier
(`hard`, so it blocks the merge — a malformed committed spec is mechanically decidable structural hygiene).
The disclosed-no-op is always `soft`, so a project with no spec is never blocked. Read-only throughout.

Shared spec-grammar home: besides being the form-check entry, this module is the de-facto shared library for the
product_design package — `lock_integrity.py` and `coverage.py` import its readers (`_frontmatter_status`, the
pipe-table and link parsers) and its path constants (`_SPEC_DIR`, `_INDEX_REL`, `_BUILD_PLAN_REL`). Keeping the
spec grammar in one place is deliberate (no duplication across the package's checks).

Contract: invoked by the validator with NO arguments, it prints a finding.v1 JSON array to stdout and exits
0. A separate `demo` subcommand runs a falsifiable self-check.
"""
from __future__ import annotations
import json
import os
import re
import sys

# Make the sibling `.engine/tools/` modules importable whether imported as `product_design.spec_form`
# or run directly as the wired check script (the migration_discipline / dependency_discipline idiom).
_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PARENT not in sys.path:
    sys.path.insert(0, _PARENT)

import validate  # noqa: E402 — ROOT (test-redirectable) + the finding.v1 helper

# The committed product spec tree, relative to the repository root, and the master-index filename.
_SPEC_DIR = os.path.join("docs", "spec")
_INDEX_NAME = "index.md"
_INDEX_REL = os.path.join(_SPEC_DIR, _INDEX_NAME)
# The committed build-plan doc (the build order) — a separate artifact under docs/spec/, NOT a capability and
# NOT subject to the lock (no lock gravity). It is excluded from the capability walk by exact path, exactly as
# the index is, and is the input the acceptance-criteria coverage check (coverage.py) reads. lock_integrity
# needs no exclusion: the build-plan carries no `locked` status, so its content filter already skips it.
_BUILD_PLAN_NAME = "build-plan.md"
_BUILD_PLAN_REL = os.path.join(_SPEC_DIR, _BUILD_PLAN_NAME)

# The lifecycle ladder is engine-internal; operator-facing prose renders it plainly, NEVER the raw token
# (the operator-communication law). These plain renders are the only stage words a finding shows.
_VALID_STATUS = ("stub", "draft", "locked")
_PLAIN_STATUS = {"stub": "not yet described", "draft": "in progress", "locked": "settled"}
_PLAIN_STAGES = "not yet described, in progress, or settled"
# Documents past the placeholder stage carry the full structure; a not-yet-described slot does not.
_DRAFTED = ("draft", "locked")

# Sections a drafted (in-progress / settled) capability document must carry, as level-2 headings.
_REQUIRED_SECTIONS = ("Summary", "Behavior", "Acceptance criteria")
_CRITERIA_SECTION = "Acceptance criteria"
# The acceptance-criteria table columns, and the recognized values for the who-checks-it column.
_CRITERIA_COLUMNS = ("criterion", "how verified", "who checks it")
_DISCHARGE_VALUES = ("operator", "engine")
# The master-index coherence-ledger columns.
_LEDGER_COLUMNS = ("capability", "status", "doc")

_READONLY_TAIL = ("This check only reads your files — it never changes them — and it checks that the parts "
                  "are present and well-formed, not whether the design is right.")


# --------------------------------------------------------------------------------------------------
# Reading the tree (read-only; no contents are written, ever)
# --------------------------------------------------------------------------------------------------

def _spec_root(root: str) -> str:
    return os.path.join(root, _SPEC_DIR)


def _capability_doc_rels(root: str) -> list:
    """Repo-root-relative paths of every `*.md` under `docs/spec/` EXCEPT the two non-capability files — the
    master index and the build-plan doc (both excluded by exact top-level path, so a nested doc that happens to
    share either name stays an ordinary capability), sorted. Symlinks are not followed (the safe default);
    dot-directories are skipped."""
    spec_root = _spec_root(root)
    out = []
    for dirpath, dirnames, filenames in os.walk(spec_root):  # followlinks=False (default)
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for name in filenames:
            if not name.endswith(".md"):
                continue
            rel = os.path.relpath(os.path.join(dirpath, name), root)
            if rel in (_INDEX_REL, _BUILD_PLAN_REL):
                continue
            out.append(rel)
    return sorted(out)


def _read(root: str, rel: str) -> str:
    # utf-8-sig transparently strips a leading byte-order mark (BOM) — common from Windows editors on the
    # operator's own files — so a well-formed spec is never mis-read as malformed.
    with open(os.path.join(root, rel), "r", encoding="utf-8-sig", errors="replace") as fh:
        return fh.read()


def _frontmatter_status(text: str) -> "str | None":
    """The lowercased value of the first `status:` key inside the leading `---` frontmatter block, or None
    when there is no frontmatter or no status key. Tolerant of malformed content — it never raises."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    for line in lines[1:]:
        if line.strip() == "---":
            break
        m = re.match(r"\s*status\s*:\s*(.+?)\s*$", line)
        if m:
            value = re.sub(r"\s+#.*$", "", m.group(1).strip())  # drop a trailing inline YAML comment
            return value.strip().strip("'\"").lower()
    return None


def _h2_headings(text: str) -> set:
    """The set of level-2 heading texts (`## Heading`), lowercased and stripped."""
    return {m.group(1).strip().lower()
            for m in re.finditer(r"(?m)^##[ \t]+(.+?)[ \t]*#*$", text)}


def _section_body(text: str, heading: str) -> "str | None":
    """The text between a `## <heading>` line and the next `## ` (or end of file), or None if absent."""
    lines = text.splitlines()
    want = heading.strip().lower()
    start = None
    for i, line in enumerate(lines):
        m = re.match(r"^##[ \t]+(.+?)[ \t]*#*$", line)
        if m and m.group(1).strip().lower() == want:
            start = i + 1
            break
    if start is None:
        return None
    end = len(lines)
    for j in range(start, len(lines)):
        if re.match(r"^##[ \t]+", lines[j]):
            end = j
            break
    return "\n".join(lines[start:end])


# --------------------------------------------------------------------------------------------------
# Markdown pipe-table parsing (presence/shape only — cell text is never interpreted as design content)
# --------------------------------------------------------------------------------------------------

def _is_table_row(line: str) -> bool:
    s = line.strip()
    return s.startswith("|") and s.count("|") >= 2


def _is_separator_row(line: str) -> bool:
    s = line.strip()
    return bool(s) and bool(re.fullmatch(r"\|[\s:\-|]+\|?", s)) and "-" in s


def _cells(line: str) -> list:
    parts = [c.strip() for c in line.strip().strip("|").split("|")]
    return parts


def _tables(text: str) -> list:
    """Every pipe-table in `text`, as (header_cells_lowercased, [data_row_cells, ...])."""
    lines = text.splitlines()
    out, i, n = [], 0, len(lines)
    while i < n:
        if _is_table_row(lines[i]) and i + 1 < n and _is_separator_row(lines[i + 1]):
            header = [c.lower() for c in _cells(lines[i])]
            rows, j = [], i + 2
            while j < n and _is_table_row(lines[j]) and not _is_separator_row(lines[j]):
                rows.append(_cells(lines[j]))
                j += 1
            out.append((header, rows))
            i = j
        else:
            i += 1
    return out


def _table_with_columns(text: str, columns: tuple) -> "list | None":
    """The data rows of the first table whose leading columns are `columns` (case-insensitive), or None.
    A prefix match — trailing extra columns (e.g. a Notes column) are allowed, so the fixed columns keep
    their positions and an otherwise-valid table is never reported as missing."""
    n = len(columns)
    for header, rows in _tables(text):
        if tuple(header[:n]) == tuple(columns):
            return rows
    return None


def _link_target(cell: str) -> "str | None":
    """The path inside a Markdown link `[text](path)` in `cell`, or the bare cell text if it looks like a
    path, or None when the cell names no document. A trailing #fragment or ?query is dropped so a
    section-anchor link still resolves to its document."""
    m = re.search(r"\[[^\]]*\]\(([^)]+)\)", cell)
    raw = (m.group(1) if m else cell).strip()
    raw = raw.split("#", 1)[0].split("?", 1)[0].strip()
    return raw if raw.endswith(".md") else None


def _normalize_status(cell: str) -> "str | None":
    """Map a status cell (a raw marker OR its plain render) to its canonical token, or None if unrecognized."""
    low = cell.strip().lower()
    if low in _VALID_STATUS:
        return low
    for token, plain in _PLAIN_STATUS.items():
        if low == plain:
            return token
    return None


# --------------------------------------------------------------------------------------------------
# Operator-facing messages (plain language; the lifecycle ladder is rendered, never shown as a raw token)
# --------------------------------------------------------------------------------------------------

_NO_OP_MESSAGE = (
    "Product-spec checking isn't active here yet — this check looks for a product specification under "
    "`docs/spec/` (a master index plus one document per capability) and didn't find one. That's a normal, "
    "expected state for a project that hasn't written its spec yet, not an error: it starts checking your "
    "spec's structure on its own once you add `docs/spec/`. " + _READONLY_TAIL
)


def _missing_index_message() -> str:
    return (
        "Your product spec under `docs/spec/` has documents but no master index (`docs/spec/index.md`). The "
        "index is the one place that lists every capability, its stage, and a link to its document, so the "
        "spec holds together as a whole. To clear this, add `docs/spec/index.md` with a table whose columns "
        "are Capability, Status, and Doc. " + _READONLY_TAIL
    )


def _index_missing_ledger_message() -> str:
    return (
        "Your product spec's master index (`docs/spec/index.md`) is missing its capabilities table — the one "
        "that lists every capability, its stage, and a link to its document. To clear this, add a table to "
        "`docs/spec/index.md` whose columns are Capability, Status, and Doc, with one row per capability. "
        + _READONLY_TAIL
    )


def _bad_status_message(rel: str) -> str:
    return (
        f"The product-spec document `{rel}` isn't marked with a recognized stage. Every spec document needs a "
        f"stage at the top so the engine and you know how far along it is — one of: {_PLAIN_STAGES}. To clear "
        f"this, add a recognized stage marker to the top of `{rel}`. " + _READONLY_TAIL
    )


def _missing_sections_message(rel: str, status: str, missing: list) -> str:
    plain = _PLAIN_STATUS[status]
    names = ", ".join(missing)
    return (
        f"The product-spec document `{rel}` is marked {plain} but is missing the sections a spec needs once "
        f"it is past the not-yet-described stage: {names}. To clear this, add the missing section(s) to "
        f"`{rel}`, or move it back to the not-yet-described stage if it isn't ready. " + _READONLY_TAIL
    )


def _criteria_table_message(rel: str, status: str) -> str:
    plain = _PLAIN_STATUS[status]
    return (
        f"The product-spec document `{rel}` is marked {plain}, so its Acceptance criteria section needs a "
        f"well-formed table — one row per criterion, with columns Criterion, How verified, and Who checks it "
        f"(you, or the engine). To clear this, add or fix that table in `{rel}`. " + _READONLY_TAIL
    )


def _incomplete_criteria_row_message(rel: str, status: str) -> str:
    plain = _PLAIN_STATUS[status]
    return (
        f"The product-spec document `{rel}` is marked {plain}, but a row in its Acceptance criteria table is "
        f"missing a cell — every row needs all three: Criterion, How verified, and Who checks it (you, or the "
        f"engine). A half-filled row would quietly drop that criterion from what your built work is measured "
        f"against. To clear this, complete every row of the table in `{rel}`. " + _READONLY_TAIL
    )


def _discharge_value_message(rel: str, bad: list) -> str:
    shown = ", ".join(f"'{b}'" for b in bad)
    return (
        f"In the product-spec document `{rel}`, the acceptance-criteria table's Who-checks-it column says who "
        f"checks each criterion, and must be either 'operator' (you check it yourself) or 'engine' (the "
        f"engine checks it for you) — but found {shown}. To clear this, set that column to 'operator' or "
        f"'engine' in each row of `{rel}`. " + _READONLY_TAIL
    )


def _orphan_doc_message(rel: str) -> str:
    return (
        f"The product-spec document `{rel}` exists but isn't listed in the master index "
        f"(`docs/spec/index.md`). Every document must appear in the index so the spec stays coherent. To "
        f"clear this, add a row for it to the index's table (Capability, Status, Doc). " + _READONLY_TAIL
    )


def _dangling_index_message(target: str) -> str:
    return (
        f"The master index (`docs/spec/index.md`) points to a product-spec document `{target}` that doesn't "
        f"exist. To clear this, either create that document or fix the link in the index so it points to a "
        f"document that exists. " + _READONLY_TAIL
    )


def _coherence_message(rel: str, ledger_plain: str, doc_plain: str) -> str:
    return (
        f"The master index lists the product-spec document `{rel}` as {ledger_plain}, but the document itself "
        f"is marked {doc_plain}. The index and the document must agree on the stage. To clear this, update "
        f"whichever is out of date so they match. " + _READONLY_TAIL
    )


# --------------------------------------------------------------------------------------------------
# The form findings
# --------------------------------------------------------------------------------------------------

def findings(tier: str, root: "str | None" = None) -> list:
    """The spec-form findings for `root` (defaults to `validate.ROOT`), as a list of finding.v1 dicts.

    Empty list = a genuine clean pass (a well-formed spec, or one that is all not-yet-described slots). A
    single `soft` no-op finding when no `docs/spec/` tree exists yet — said plainly, never a silent pass.
    Otherwise one finding per real structural problem, each at `tier` severity (`hard`) so a malformed
    committed spec blocks the merge; the no-op is always `soft` so a project without a spec is never blocked."""
    root = root or validate.ROOT
    spec_root = _spec_root(root)
    has_index = os.path.isfile(os.path.join(root, _INDEX_REL))
    doc_rels = _capability_doc_rels(root) if os.path.isdir(spec_root) else []

    # Disclosed no-op: no spec tree, or an empty one (no index and no documents) — always soft, never silent.
    if not os.path.isdir(spec_root) or (not has_index and not doc_rels):
        return [validate.disclosed_noop(_NO_OP_MESSAGE, None)]

    out = []
    here = {"file": _INDEX_REL, "line": None}

    # Read every capability document once: rel -> (status_token_or_None, h2_headings, text).
    docs = {}
    for rel in doc_rels:
        text = _read(root, rel)
        docs[rel] = (_frontmatter_status(text), _h2_headings(text), text)

    # (1) Per-document presence/shape, lifecycle-scaled.
    for rel in doc_rels:
        status, headings, text = docs[rel]
        if status not in _VALID_STATUS:
            out.append(validate.finding(tier, _bad_status_message(rel), {"file": rel, "line": None}))
            continue
        if status not in _DRAFTED:
            continue  # a not-yet-described slot needs only a recognized stage marker
        missing = [s for s in _REQUIRED_SECTIONS if s.lower() not in headings]
        if missing:
            out.append(validate.finding(tier, _missing_sections_message(rel, status, missing),
                                        {"file": rel, "line": None}))
        if _CRITERIA_SECTION.lower() in headings:
            body = _section_body(text, _CRITERIA_SECTION) or ""
            rows = _table_with_columns(body, _CRITERIA_COLUMNS)
            if rows is None:
                out.append(validate.finding(tier, _criteria_table_message(rel, status),
                                            {"file": rel, "line": None}))
            else:
                # Every data row must carry all three columns. A row with fewer cells is a half-filled criterion
                # that would silently vanish from the coverage denominator the obligation-matrix derives (a
                # criterion the who-value check below also skips, since it guards on len(r) > 2) — catch it here
                # so a malformed criterion can never leave the record with nothing red (spec-conformance nit, #454).
                if any(len(r) < len(_CRITERIA_COLUMNS) for r in rows):
                    out.append(validate.finding(tier, _incomplete_criteria_row_message(rel, status),
                                                {"file": rel, "line": None}))
                bad = sorted({r[2].strip().lower() for r in rows
                              if len(r) > 2 and r[2].strip().lower() not in _DISCHARGE_VALUES})
                if bad:
                    out.append(validate.finding(tier, _discharge_value_message(rel, bad),
                                                {"file": rel, "line": None}))

    # (2) The master index + (3) coverage + (4) coherence across the tree.
    if not has_index:
        out.append(validate.finding(tier, _missing_index_message(), here))
        return out

    ledger = _table_with_columns(_read(root, _INDEX_REL), _LEDGER_COLUMNS)
    if ledger is None:
        out.append(validate.finding(tier, _index_missing_ledger_message(), here))
        return out

    # Resolve each ledger row's linked document (relative to the index's own directory) to a repo-root path.
    listed = {}  # repo-root-relative doc path -> ledger status cell
    for row in ledger:
        if len(row) < 3:
            continue
        target = _link_target(row[2])
        if not target:
            continue
        resolved = os.path.normpath(os.path.join(_SPEC_DIR, target))
        listed[resolved] = row[1]

    # (3) coverage — every document is listed; every listed document exists.
    for rel in doc_rels:
        if rel not in listed:
            out.append(validate.finding(tier, _orphan_doc_message(rel), {"file": rel, "line": None}))
    for target in sorted(listed):
        if target not in docs:
            out.append(validate.finding(tier, _dangling_index_message(target), here))

    # (4) coherence — the index's stage matches the document's declared stage.
    for rel in sorted(listed):
        if rel not in docs:
            continue
        doc_status = docs[rel][0]
        ledger_status = _normalize_status(listed[rel])
        if doc_status in _VALID_STATUS and ledger_status != doc_status:
            ledger_plain = _PLAIN_STATUS.get(ledger_status, "an unrecognized stage")
            out.append(validate.finding(tier, _coherence_message(rel, ledger_plain,
                                                                 _PLAIN_STATUS[doc_status]),
                                        {"file": rel, "line": None}))
    return out


def emit_findings() -> int:
    """The no-argument path the validator invokes: print the finding.v1 array and return 0. Violations carry
    the rule's declared tier (the validator passes it as ENGINE_RULE_TIER, defaulting hard); the no-op is
    always soft (set inside findings())."""
    # ENGINE_SPEC_ROOT (unset in production) lets the negative-fixture meta-check point the spec scan
    # at a seeded docs/spec tree, so the form gate is witnessed biting a real bad input (#286).
    print(json.dumps(findings(os.environ.get("ENGINE_RULE_TIER", "hard"),
                              validate.env_override_path("ENGINE_SPEC_ROOT"))))
    return 0


def demo() -> int:
    """Prove the inspector: passes a well-formed spec and an all-not-yet-described spec; says the no-op
    plainly (never silently) when there is no spec; flags — at hard severity — a missing index, a missing
    section in an in-progress doc, a malformed/absent acceptance-criteria table, a bad who-can-check value,
    an orphan document, a dangling index link, and an index/document stage disagreement; never shows a raw
    lifecycle token in a finding; and never treats a spec under `.engine/` as the product's own (the engine/product wall). RETURNS NON-ZERO if any invariant is broken (the falsification can fail). Mutation-free: every
    case runs against a throwaway temp root, so the real working tree is never touched."""
    import shutil
    import tempfile

    def _seed(files: dict) -> str:
        d = tempfile.mkdtemp(prefix="engine-spec-form-demo-")
        for rel, body in files.items():
            path = os.path.join(d, rel)
            parent = os.path.dirname(path)
            if parent and not os.path.isdir(parent):
                os.makedirs(parent)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(body)
        return d

    def _doc(status: str, *, sections=True, table=True, discharge="operator") -> str:
        body = f"---\nstatus: {status}\n---\n\n# A capability\n"
        if sections:
            body += "\n## Summary\nWhat and who for.\n\n## Behavior\nHow it behaves.\n\n## Acceptance criteria\n"
            if table:
                body += ("\n| Criterion | How verified | Who checks it |\n"
                         "| --- | --- | --- |\n"
                         f"| It works | a behavioral demo | {discharge} |\n")
        return body

    _LEDGER = ("\n| Capability | Status | Doc |\n| --- | --- | --- |\n")

    def _index(rows: str) -> str:
        return "# Product spec\n" + _LEDGER + rows

    well_formed = {
        "docs/spec/index.md": _index("| Checkout | draft | [Checkout](checkout.md) |\n"
                                     "| Search | stub | [Search](search.md) |\n"),
        "docs/spec/checkout.md": _doc("draft"),
        "docs/spec/search.md": "---\nstatus: stub\n---\n\n# Search (not yet described)\n",
    }

    cases = []  # (label, files, predicate over the findings list)
    cases.append(("a well-formed spec (one in-progress, one not-yet-described) passes cleanly",
                  well_formed, lambda fs: fs == []))
    cases.append(("a well-formed doc with a UTF-8 byte-order mark is accepted, not mis-read as malformed",
                  {"docs/spec/index.md": _index("| Checkout | draft | [Checkout](checkout.md) |\n"),
                   "docs/spec/checkout.md": "﻿" + _doc("draft")},
                  lambda fs: fs == []))
    cases.append(("an index link with a #section-anchor still resolves to its document",
                  {"docs/spec/index.md": _index("| Checkout | draft | [Checkout](checkout.md#summary) |\n"),
                   "docs/spec/checkout.md": _doc("draft")},
                  lambda fs: fs == []))
    cases.append(("a ledger with an extra trailing column is accepted (its fixed columns keep their place)",
                  {"docs/spec/index.md": "# Product spec\n\n| Capability | Status | Doc | Notes |\n"
                                         "| --- | --- | --- | --- |\n"
                                         "| Checkout | draft | [Checkout](checkout.md) | later |\n",
                   "docs/spec/checkout.md": _doc("draft")},
                  lambda fs: fs == []))
    cases.append(("an all-not-yet-described spec passes cleanly",
                  {"docs/spec/index.md": _index("| Checkout | stub | [Checkout](checkout.md) |\n"),
                   "docs/spec/checkout.md": "---\nstatus: stub\n---\n\n# Checkout\n"},
                  lambda fs: fs == []))
    cases.append(("no docs/spec tree says the no-op plainly (soft, never silent)",
                  {"README.md": "hi"},
                  lambda fs: len(fs) == 1 and fs[0]["severity"] == "soft"
                  and "isn't active here yet" in fs[0]["message"]))
    cases.append(("documents but no index is a hard finding naming the index",
                  {"docs/spec/checkout.md": _doc("draft")},
                  lambda fs: any(f["severity"] == "hard" and "no master index" in f["message"] for f in fs)))
    cases.append(("an in-progress doc missing a section is a hard finding naming the section",
                  {"docs/spec/index.md": _index("| Checkout | draft | [Checkout](checkout.md) |\n"),
                   "docs/spec/checkout.md": "---\nstatus: draft\n---\n\n# C\n\n## Summary\nx\n"},
                  lambda fs: any(f["severity"] == "hard" and "Behavior" in f["message"] for f in fs)))
    cases.append(("an in-progress doc with no criteria table is a hard finding",
                  {"docs/spec/index.md": _index("| Checkout | draft | [Checkout](checkout.md) |\n"),
                   "docs/spec/checkout.md": _doc("draft", table=False)},
                  lambda fs: any(f["severity"] == "hard" and "well-formed table" in f["message"] for f in fs)))
    cases.append(("an in-progress doc whose criteria row is missing a cell is a hard finding (row-width)",
                  {"docs/spec/index.md": _index("| Checkout | draft | [Checkout](checkout.md) |\n"),
                   "docs/spec/checkout.md": "---\nstatus: draft\n---\n\n# C\n\n## Summary\nx\n\n## Behavior\ny\n\n"
                                            "## Acceptance criteria\n\n| Criterion | How verified | Who checks it |\n"
                                            "| --- | --- | --- |\n| It works | a demo |\n"},
                  lambda fs: any(f["severity"] == "hard" and "missing a cell" in f["message"] for f in fs)))
    cases.append(("a bad who-can-check value is a hard finding",
                  {"docs/spec/index.md": _index("| Checkout | draft | [Checkout](checkout.md) |\n"),
                   "docs/spec/checkout.md": _doc("draft", discharge="nobody")},
                  lambda fs: any(f["severity"] == "hard" and "'nobody'" in f["message"] for f in fs)))
    cases.append(("an unrecognized stage marker is a hard finding rendered in plain language",
                  {"docs/spec/index.md": _index("| Checkout | draft | [Checkout](checkout.md) |\n"),
                   "docs/spec/checkout.md": "---\nstatus: wip\n---\n\n# C\n"},
                  lambda fs: any(f["severity"] == "hard" and "recognized stage" in f["message"] for f in fs)))
    cases.append(("a document missing from the index is a hard orphan finding",
                  {"docs/spec/index.md": _index("| Checkout | draft | [Checkout](checkout.md) |\n"),
                   "docs/spec/checkout.md": _doc("draft"),
                   "docs/spec/search.md": _doc("draft")},
                  lambda fs: any(f["severity"] == "hard" and "isn't listed in the master index" in f["message"]
                                 for f in fs)))
    cases.append(("an index link to a missing document is a hard dangling finding",
                  {"docs/spec/index.md": _index("| Checkout | draft | [Checkout](checkout.md) |\n"
                                                "| Ghost | draft | [Ghost](ghost.md) |\n"),
                   "docs/spec/checkout.md": _doc("draft")},
                  lambda fs: any(f["severity"] == "hard" and "doesn't exist" in f["message"] for f in fs)))
    cases.append(("an index/document stage disagreement is a hard coherence finding",
                  {"docs/spec/index.md": _index("| Checkout | settled | [Checkout](checkout.md) |\n"),
                   "docs/spec/checkout.md": _doc("draft")},
                  lambda fs: any(f["severity"] == "hard" and "must agree on the stage" in f["message"]
                                 for f in fs)))
    cases.append(("a spec under .engine/ is walled out -> no-op, not a finding",
                  {".engine/docs/spec/index.md": _index("| X | draft | [X](x.md) |\n"),
                   ".engine/docs/spec/x.md": _doc("draft")},
                  lambda fs: len(fs) == 1 and fs[0]["severity"] == "soft"
                  and "isn't active here yet" in fs[0]["message"]))

    failures = []
    for label, files, ok in cases:
        root = _seed(files)
        try:
            result = findings("hard", root=root)
        finally:
            shutil.rmtree(root, ignore_errors=True)
        # The raw lifecycle ladder must never surface in a finding (the operator-communication law).
        for f in result:
            for token in _VALID_STATUS:
                if re.search(rf"\b{token}\b", f["message"]):
                    failures.append(f"{label}: a finding leaked the raw lifecycle token '{token}': {f['message']}")
        if not ok(result):
            failures.append(f"{label}: invariant broken, got {result}")

    if failures:
        print("DEMO FAILED — the spec-form inspector broke an invariant:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("DEMO PASSED — the spec-form inspector passes a well-formed spec, says the no-op plainly when there "
          "is no spec, flags every structural problem at hard severity, never leaks a raw lifecycle token, "
          "and never treats a spec under .engine/ as the product's own.")
    return 0


def main(argv: list) -> int:
    if argv and argv[0] == "demo":
        return demo()
    return emit_findings()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
