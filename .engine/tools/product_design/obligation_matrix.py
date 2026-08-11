#!/usr/bin/env python3
"""Spec-obligation matrix (product-design) — the engine's generated, committed criterion-by-criterion record
of a product's SETTLED acceptance criteria, and the drift gate that keeps it honest. A standing audit sweep
reads it (issue StarshipSuperjam/engine-template#449).

WHAT IT IS. When a project settles a capability in its product spec (a `locked` document under `docs/spec/`
carrying an acceptance-criteria table), this tool derives ONE committed JSON file,
`.engine/product-spec-matrix.json`, with ONE ROW PER SETTLED ACCEPTANCE CRITERION: the document it came from,
its position in that document's criteria table, a content digest of the criterion text, and the criterion +
how-it-is-verified + who-checks-it as written. It is DERIVED from the committed spec, so it cannot diverge from
it (the graph.json / self-map.md derive-don't-hand-author pattern). It is the coverage DENOMINATOR
the per-merge conformance floor and the standing audit sweep both read — a DERIVED INDEX, never a trusted second
referent: any conformance judgment re-derives from the `docs/spec/` span itself, never from these rows.

CRITERION-GRANULAR, keyed by content-digest AT the criterion's validated table position: a re-worded
criterion re-opens exactly its own row (its digest moves); an inserted/deleted criterion shifts the position of
the rows after it. Each row's identity is `(doc, position)`; the criterion-cell digest is its content-key — the
thing that moves on a re-wording, and the key the standing sweep attaches a finding to (its
`(doc, digest)`). Where one document carries two identical criterion cells they share a digest, so POSITION is
what keeps them two distinct rows HERE, in the matrix. The standing sweep, though, dedups a finding on
`(doc, digest)` ALONE — two identical criteria in one document collapse to one tracked finding, the
accepted trade for a POSITION-FREE key: position cascades when an edit inserts or removes a criterion, so
folding it in would re-key and re-nag every finding below the edit (the plan-gate finding on this build).

ONE TIER HERE, on purpose. The design names two rigor tiers — criterion-granular over a `docs/spec/` criteria table,
and a weaker heading-span tier over prose design docs with no criteria table. This shipped, TRAVELLING generator
implements the criterion-granular tier ONLY, over the product's OWN in-repo `docs/spec/` (the engine/product wall: it never
reads the engine's design workspace). The span tier has no corpus in a product repo at all — and a `locked`
`docs/spec/` document always carries a well-formed criteria table (product-spec-form
makes a `locked`-without-criteria document a hard finding), so a span-tier row is unreachable in a conformant
product repo. Building it here would be dead code the divergence-hunter would rightly flag.

DRIFT GATE, MVP-SAFE. The committed matrix IS its own fingerprint (no side digest file): the checker re-derives
in memory and byte-compares. A present, non-empty matrix that drifted → a HARD finding naming the one fix
(regenerate + commit). But NOTHING is ever hard merely for the absence of a settled spec: when nothing is locked
the derived matrix is empty, and an in-sync empty matrix is a SOFT disclosed no-op — a staged/MVP product is a
first-class operator choice and is never pressured toward a spec. The matrix travels with the optional
product-design module and is self-removing with it; in the engine's own home repository (no `docs/spec/`) it is
the empty disclosed no-op, exercised only by fixtures.

REGENERATION AT THE COMMIT BOUNDARY. The `hook` verb is the `PreToolUse` entry the module wires (beside
knowledge_gen's graph hook and self_map's map hook): on a `git commit` it refreshes the committed matrix
best-effort and ALWAYS proceeds (the refresh lands UNSTAGED; the CI drift gate is the durable backstop). A
MUTATION, never a gate.

Library + CLI (mirrors self_map.py / coverage.py — plain language first):

  uv run --directory .engine -- python tools/product_design/obligation_matrix.py show      # print the matrix (live)
  uv run --directory .engine -- python tools/product_design/obligation_matrix.py generate  # (re)write the committed matrix
  uv run --directory .engine -- python tools/product_design/obligation_matrix.py check      # is the committed matrix in sync?
  uv run --directory .engine -- python tools/product_design/obligation_matrix.py demo        # safe fail->pass on a temp copy
  uv run --directory .engine -- python tools/product_design/obligation_matrix.py hook-demo    # show the commit-boundary regen (no writes)
  uv run --directory .engine -- python tools/product_design/obligation_matrix.py hook          # the PreToolUse entry the module wires
  uv run --directory .engine -- python tools/product_design/obligation_matrix.py              # (no args) the custom/script check entry — emits finding.v1 JSON

Contract for the no-argument path (the validator's custom/script invocation): prints a finding.v1 JSON array to
stdout and exits 0 — [] when the matrix is in sync and non-empty, one SOFT disclosed-no-op when there is no
settled spec, one HARD finding on drift.
"""
from __future__ import annotations
import hashlib
import json
import os
import sys
import tempfile

# Make the sibling `.engine/tools/` modules importable whether imported as `product_design.obligation_matrix`
# or run directly as the wired check script (the spec_form / coverage / lock_integrity idiom).
_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PARENT not in sys.path:
    sys.path.insert(0, _PARENT)

import validate  # noqa: E402 — ROOT (test-redirectable), finding.v1 + disclosed_noop + the env seam
import hooks     # noqa: E402 — the run_hook harness + the git-commit-boundary classifier
from product_design import spec_form  # noqa: E402 — the shared spec-grammar home (parser + path constants)

# The settled lifecycle marker (engine-internal; every operator surface renders it "settled", never this token).
_SETTLED = "locked"

# The committed matrix's home. A top-level `.engine/` FILE (beside self-map.md / suites.json), claimed by
# product-design's provides.foundation so the ownership leg does not flag it an orphan, and a top-level FILE
# (not a new `.engine/<dir>/`) so it is invisible to the catalog-coverage orphan-directory check. Always
# committed (empty rows where no spec is settled) so ownership stays clean and the drift gate has a stable
# committed side to compare. Self-removing: it is the product-design module's own file, removed with it.
MATRIX_PATH = os.path.join(validate.ENGINE_DIR, "product-spec-matrix.json")

_SCHEMA_VERSION = 1
REGEN_CMD = "uv run --directory .engine -- python tools/product_design/obligation_matrix.py generate"

# Plain-language display name for operator-facing findings (never the backstage slug; the leak-guard).
_DISPLAY_NAME = "the record of your settled acceptance criteria (`.engine/product-spec-matrix.json`)"


# ---- pure derivation (no IO beyond the spec_form readers; fixture-testable) -------------------

def _digest(criterion: str) -> str:
    """A content digest of a criterion cell: whitespace canonicalized (runs collapsed, ends stripped) so a
    trivial reflow is stable, then sha256 — so a genuine re-wording moves the digest and re-opens the row,
    while a whitespace-only edit does not."""
    canonical = " ".join(criterion.split())
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def derive_rows(root: str) -> list:
    """One row per settled acceptance criterion across the product's `docs/spec/` capability documents.

    A row is emitted only for a `locked` (settled) document that carries a well-formed acceptance-criteria
    table (product-spec-form guarantees a settled document has one; a malformed one is that check's hard
    finding, not silently dropped here beyond producing no rows). `draft`/`stub` documents contribute none.
    Each row: doc (repo-relative), position (0-based row index in the criteria table — the validated table
    position), digest (of the criterion cell), criterion / how_verified / who as written. Sorted by
    (doc, position) for a deterministic, byte-comparable artifact."""
    rows = []
    spec_root = spec_form._spec_root(root)
    if not os.path.isdir(spec_root):
        return rows
    for rel in spec_form._capability_doc_rels(root):
        text = spec_form._read(root, rel)
        if spec_form._frontmatter_status(text) != _SETTLED:
            continue
        body = spec_form._section_body(text, spec_form._CRITERIA_SECTION)
        if body is None:
            continue
        table = spec_form._table_with_columns(body, spec_form._CRITERIA_COLUMNS)
        if not table:
            continue
        doc = rel.replace(os.sep, "/")
        for position, cells in enumerate(table):
            if len(cells) < 3:
                # A criteria row missing a cell — product-spec-form flags this hard (its criteria-row-width
                # check), so a `locked` document never reaches here carrying one; this is a defensive skip that
                # contributes no obligation row, never the silent path by which a criterion leaves the denominator.
                continue
            criterion, how_verified, who = cells[0].strip(), cells[1].strip(), cells[2].strip().lower()
            rows.append({
                "doc": doc,
                "position": position,
                "digest": _digest(criterion),
                "criterion": criterion,
                "how_verified": how_verified,
                "who": who,
            })
    rows.sort(key=lambda r: (r["doc"], r["position"]))
    return rows


def canonical_matrix(root: str | None = None) -> dict:
    """The canonical matrix object derived from the live committed spec under `root` (defaults to
    validate.ROOT, resolved at call time so a test/fixture may redirect it)."""
    root = validate.ROOT if root is None else root
    return {"schema_version": _SCHEMA_VERSION, "source": "docs/spec", "rows": derive_rows(root)}


def render(matrix: dict) -> str:
    """The matrix as deterministic JSON text: sorted keys, 2-space indent, LF newlines, exactly one trailing
    newline — so regenerate-and-compare is a valid byte-equality test (the graph.json / self-map.md
    fingerprint discipline). `ensure_ascii=False` keeps a criterion's own UTF-8 intact and stable."""
    return json.dumps(matrix, sort_keys=True, indent=2, ensure_ascii=False) + "\n"


def is_empty(matrix: dict) -> bool:
    """True when nothing is settled — no obligation rows (the disclosed-no-op state)."""
    return not matrix.get("rows")


# ---- pure drift logic (no IO; fixture-testable) ----------------------------------------------

def drift_finding(canonical: dict, committed_text: str | None, path: str) -> dict:
    """The fingerprint gate as a pure function, MVP-safe.

    - Nothing settled (canonical is empty) AND the committed side is the empty matrix (or absent): a SOFT
      disclosed no-op — never a hard finding merely because no spec is settled (an MVP is a first-class
      choice, never pressured toward a spec).
    - Present, non-empty, in sync: a `note` (silent pass).
    - Any other case (a non-empty matrix that drifted, a committed side that no longer matches the derivation,
      or an absent/edited file where the derivation is non-empty): a HARD finding naming the one fix."""
    name, where = _DISPLAY_NAME, _loc_opt(path)
    canonical_text = render(canonical)

    # Nothing settled (the derived matrix is empty): a SOFT disclosed no-op ONLY when the committed side is also
    # empty or absent — the genuine never-settled / MVP repo, a first-class operator choice that is NEVER blocked
    # or pressured toward a spec. But a committed matrix that STILL LISTS ROWS while nothing is settled (a
    # capability was un-settled, or the last settled document removed) is real drift the gate must catch — it
    # must not pass green while falsely reporting the record is "empty" (a committed != derived hole). The two
    # cases are distinguished by the committed side, so the no-block guarantee is preserved for the true MVP without hiding a stale
    # rows-bearing file.
    if is_empty(canonical):
        if committed_text is None or committed_text == canonical_text:
            return validate.disclosed_noop(
                f"No settled product spec here yet, so there's nothing to record — {name} stays empty until you "
                f"settle a capability under `docs/spec/`. This is a normal, expected state, not a problem: the "
                f"record fills in on its own once you settle your first capability.",
                where)
        return validate.finding(
            "hard",
            f"{name[0].upper() + name[1:]} is out of date — it still lists acceptance criteria that are no "
            f"longer settled in your product spec. Regenerate it with `{REGEN_CMD}` and commit the result.",
            where)

    # A spec IS settled, so the derived matrix is non-empty: the committed side must match it exactly.
    if committed_text == canonical_text:
        return validate.finding(
            "note", f"{name[0].upper() + name[1:]} is in sync with your settled product spec.", where)

    # Genuine drift of a present derived artifact (a criterion added / re-worded / removed, a hand-edit, or an
    # absent file where a settled spec exists): regenerate. Hard — this is drift, not the no-spec state above.
    return validate.finding(
        "hard",
        f"{name[0].upper() + name[1:]} is out of date — it no longer matches the acceptance criteria in your "
        f"settled product spec (a criterion was added, re-worded, removed, or the file was hand-edited). "
        f"Regenerate it with `{REGEN_CMD}` and commit the result.",
        where)


def _loc_opt(path: str):
    """A finding.v1 location (repo-relative) — or None when the path is outside the repo (mirrors self_map)."""
    rel = os.path.relpath(path, validate.ROOT)
    return None if rel.startswith("..") else {"file": rel.replace(os.sep, "/"), "line": None}


# ---- IO / source layer -----------------------------------------------------------------------

def read_committed(path: str):
    """The committed matrix's exact bytes-as-text, or None if it does not exist. Read with newline='' so
    universal-newline translation cannot mask a CRLF-vs-LF difference in the equality test."""
    if not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8", newline="") as fh:
        return fh.read()


def write_matrix(text: str, path: str) -> None:
    """Write the matrix verbatim (newline='' so the LF content is not platform-translated)."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)


def _spec_root_override():
    """The repo root the spec is read from — validate.ROOT in production, redirectable by the fixture seam
    ENGINE_SPEC_ROOT (the same seam coverage.py / spec_form use), so the negative fixture can seed a settled
    `docs/spec/` while the committed matrix seam points at a stale file."""
    return validate.env_override_path("ENGINE_SPEC_ROOT") or validate.ROOT


def _matrix_path_override():
    """The committed matrix path — MATRIX_PATH in production, redirectable by the fixture seam
    ENGINE_OBLIGATION_MATRIX_PATH so the negative fixture can point the committed side at a seeded stale
    matrix and witness the gate biting (StarshipSuperjam/engine-template#286-style input substitution)."""
    return validate.env_override_path("ENGINE_OBLIGATION_MATRIX_PATH") or MATRIX_PATH


def generate(path: str | None = None, root: str | None = None) -> dict:
    """(Re)write the committed matrix from the live committed spec. Returns a `note` finding stating whether
    the file changed or was already current. `path`/`root` default to the live locations (resolved at call
    time so a test may redirect them)."""
    path = MATRIX_PATH if path is None else path
    canonical_text = render(canonical_matrix(root))
    changed = read_committed(path) != canonical_text
    write_matrix(canonical_text, path)
    msg = ("Wrote the settled-acceptance-criteria record." if changed
           else "The settled-acceptance-criteria record was already up to date.")
    return validate.finding("note", msg, _loc_opt(path))


def check(path: str | None = None, root: str | None = None) -> dict:
    """The fingerprint gate over the live committed spec + the committed matrix at `path` (defaults resolved
    at call time so a test/fixture may redirect them)."""
    path = MATRIX_PATH if path is None else path
    return drift_finding(canonical_matrix(root), read_committed(path), path)


# ---- the commit-boundary regen hook ----------------------------------------------------------
# Fires at the `git commit` boundary — the classifier is hooks._is_git_commit, shared with the other
# commit-boundary hooks (knowledge_gen's graph regen, self_map's map regen) rather than copied.
def _regen_handler(payload: dict) -> dict:
    """The `PreToolUse` regen behaviour: on a `git commit`, refresh the committed matrix best-effort, then
    ALWAYS proceed. Like the graph/self-map hooks this is a MUTATION that legitimately writes the real
    MATRIX_PATH (UNSTAGED); it NEVER blocks and NEVER injects — a regen failure proceeds (the CI drift gate is
    the durable backstop), and is never silent on failure (a plain note to stderr)."""
    if not hooks._is_git_commit(payload):
        return hooks.proceed()
    try:
        result = generate()
    except Exception as exc:  # noqa: BLE001 — a best-effort MUTATION, never a gate: proceed, never block.
        sys.stderr.write(
            f"(product-design) the commit-boundary refresh of the settled-acceptance-criteria record could "
            f"not run ({type(exc).__name__}: {exc}); your commit was not affected — the merge-time check will "
            f"catch any staleness.\n")
        return hooks.proceed()
    if (result.get("message") or "").startswith("Wrote"):
        sys.stderr.write(
            "(product-design) refreshed the settled-acceptance-criteria record (.engine/product-spec-matrix.json) "
            "for this commit; it is left in your working tree for the next commit — your commit was not "
            "affected.\n")
    return hooks.proceed()


# ---- the custom/script check entry (no-argument path) ----------------------------------------

def emit_findings() -> int:
    """The no-argument path the validator invokes: print the finding.v1 array and return 0. Emits [] on a
    clean, in-sync, non-empty matrix (a silent pass); the SOFT disclosed no-op when nothing is settled (never
    silent — the operator sees the record is inactive here); the HARD drift finding otherwise."""
    f = check(_matrix_path_override(), _spec_root_override())
    print(json.dumps([] if f["severity"] == "note" else [f]))
    return 0


# ---- CLI -------------------------------------------------------------------------------------

def _hook_demo(_argv: list) -> int:
    """Show the commit-boundary regen WITHOUT touching the committed matrix: which tool calls trigger it,
    that a refresh writes the matrix, and that it never blocks. The real matrix file is untouched."""
    commit = {"tool_name": "Bash", "tool_input": {"command": "git add -A && git commit -m 'x'"}}
    status = {"tool_name": "Bash", "tool_input": {"command": "git status"}}
    a_read = {"tool_name": "Read", "tool_input": {"file_path": "x"}}
    print("Which tool calls fire the commit-boundary regen (the PreToolUse hook tests this in-script):")
    ok = True
    for label, p, expected in (("git add -A && git commit", commit, True), ("git status", status, False),
                               ("a Read", a_read, False)):
        fired = hooks._is_git_commit(p)
        ok = ok and fired == expected
        print(f"    {'FIRES' if fired else 'skips'} - {label}")
    with tempfile.TemporaryDirectory() as d:
        scratch = os.path.join(d, "product-spec-matrix.json")
        print("\nWhen it fires it refreshes the record (shown on a throwaway copy):")
        gen = generate(scratch)
        print("    " + validate.fmt(gen))
        ok = ok and (gen.get("message") or "").startswith(("Wrote", "The settled"))
    print("\nThe hook ALWAYS proceeds: a commit is never blocked, and on any failure the commit still goes "
          "through (the merge-time drift check catches any staleness). Your real .engine/product-spec-matrix.json "
          "was never touched.")
    if not ok:
        print("\nDEMO UNEXPECTED: a `git commit` must fire the regen (a status/read must not) and the refresh "
              "must run.", file=sys.stderr)
        return 1
    return 0


def _demo(_argv: list) -> int:
    """A safe, scripted fail->pass on THROWAWAY COPIES — never touches the committed matrix. It shows: a
    settled criterion derives a row; a drifted committed matrix is caught (HARD) and regeneration heals it; and
    a repo with no settled spec is the SOFT disclosed no-op, never a hard block (the MVP guarantee)."""
    import shutil

    def _seed(files: dict) -> str:
        d = tempfile.mkdtemp(prefix="engine-obligation-matrix-demo-")
        for rel, body in files.items():
            path = os.path.join(d, rel)
            parent = os.path.dirname(path)
            if parent and not os.path.isdir(parent):
                os.makedirs(parent)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(body)
        return d

    def _cap() -> str:
        return ("---\nstatus: locked\n---\n\n# A capability\n\n## Summary\nWhat and who for.\n\n## Behavior\n"
                "How it behaves.\n\n## Acceptance criteria\n\n| Criterion | How verified | Who checks it |\n"
                "| --- | --- | --- |\n| It works end to end | a behavioral demo | operator |\n")

    ok = True
    # (A) A settled criterion derives exactly one row.
    settled_root = _seed({"docs/spec/index.md": "# Product spec\n\n| Capability | Status | Doc |\n| --- | --- | --- |\n"
                                                "| A | settled | [A](a.md) |\n",
                          "docs/spec/a.md": _cap()})
    empty_root = _seed({"README.md": "hi"})
    try:
        matrix = canonical_matrix(settled_root)
        print(f"(A) A settled capability contributes {len(matrix['rows'])} acceptance criterion(s) to the record.")
        ok = ok and len(matrix["rows"]) == 1 and matrix["rows"][0]["who"] == "operator"

        with tempfile.TemporaryDirectory() as d:
            scratch = os.path.join(d, "product-spec-matrix.json")
            print("(B) Generating the record onto a throwaway copy, then checking it — should be in sync...")
            print("    " + validate.fmt(generate(scratch, settled_root)))
            c1 = check(scratch, settled_root)
            print("    " + validate.fmt(c1))
            print("(C) Hand-editing the copy to simulate drift...")
            with open(scratch, "a", encoding="utf-8", newline="") as fh:
                fh.write("drift the generator would never write\n")
            c2 = check(scratch, settled_root)
            print("    " + validate.fmt(c2))
            print("(D) Regenerating to heal it...")
            print("    " + validate.fmt(generate(scratch, settled_root)))
            c3 = check(scratch, settled_root)
            print("    " + validate.fmt(c3))
            ok = ok and c1["severity"] == "note" and c2["severity"] == "hard" and c3["severity"] == "note"

        # (E) No settled spec -> the committed empty matrix is a SOFT disclosed no-op, never hard.
        with tempfile.TemporaryDirectory() as d:
            scratch = os.path.join(d, "product-spec-matrix.json")
            generate(scratch, empty_root)
            c4 = check(scratch, empty_root)
            print("(E) With no settled spec, the empty record is a calm 'nothing to do here yet' note, never a block:")
            print("    " + validate.fmt(c4))
            ok = ok and c4["severity"] == "soft"
    finally:
        shutil.rmtree(settled_root, ignore_errors=True)
        shutil.rmtree(empty_root, ignore_errors=True)

    print("Done — a settled criterion is recorded, an out-of-date record is caught and healed by regenerating, "
          "and a project with no settled spec is a calm no-op, never blocked. Your real "
          ".engine/product-spec-matrix.json was never touched.")
    if not ok:
        print("\nDEMO UNEXPECTED: expected one row, then in-sync -> drift -> in-sync, then a soft empty no-op.",
              file=sys.stderr)
        return 1
    return 0


def main(argv: list) -> int:
    cmd = argv[0] if argv else None
    try:
        if cmd is None:
            return emit_findings()  # the custom/script check entry
        if cmd == "show":
            sys.stdout.write(render(canonical_matrix()))
            return 0
        if cmd == "generate":
            path = argv[1] if len(argv) > 1 else None
            print(validate.fmt(generate(path)))
            return 0
        if cmd == "check":
            f = check(_matrix_path_override(), _spec_root_override())
            print(validate.fmt(f))
            return 1 if f["severity"] == "hard" else 0
        if cmd == "demo":
            return _demo(argv[1:])
        if cmd == "hook-demo":
            return _hook_demo(argv[1:])
        if cmd == "hook":  # the PreToolUse entry the module wires: regen at the git-commit boundary
            return hooks.run_hook("PreToolUse", _regen_handler)
        print(f"usage: obligation_matrix.py {{show|generate|check|demo|hook-demo|hook}} [path]\n"
              f"unknown command {cmd!r}", file=sys.stderr)
        return 2
    except (OSError, ValueError) as exc:  # a malformed source / unwritable path -> plain, no traceback
        print(f"CONFIG ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
