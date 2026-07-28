#!/usr/bin/env python3
"""Product-spec coverage inspector — the read-only `custom/script` entry for engine/check/product-spec-coverage
(the product-design module's acceptance-criteria coverage check over a committed `docs/spec/` tree).

What it does: once a project has SETTLED capabilities in its product spec, it checks that every settled
capability has a PLACE IN THE BUILD ORDER — the committed build-plan at `docs/spec/build-plan.md` that lists,
in order, the capabilities the project will build. A settled capability with no place in the build order could
be silently overlooked, so this check is the mechanical floor that keeps settled work from being dropped from
the plan. It is the *traceability* half of the spec floor; the *conformance* half — whether the built work
actually matches the spec — is a separate judgment, never this check's job.

How it scales (the verdict, branching on whether a build order exists yet):
- no `docs/spec/` tree, or one with nothing settled yet → it says so plainly and does nothing (a soft note,
  never a silent pass) — the normal state before there is settled work to put in a build order;
- settled capabilities but no build order started yet → a soft nudge (never a merge block): a build may still
  proceed without a build order, so this only points out that starting one would keep the settled work visible;
- a build order exists → it has teeth: a settled capability missing from it, or a structurally broken build
  order (no phases, a row pointing at a document that doesn't exist or at something that isn't a capability),
  is a real, mechanically-decidable problem reported at the rule's tier (hard, so it blocks the merge). Moving a
  capability between phases is free — only OMITTING a settled capability is caught.

Honest floor — the engine/product wall and the read-only firewall: it inspects the product's own
`docs/spec/` tree ONLY, never the engine's `.engine/` tooling; it reads file contents to check structure but
never writes; and it checks FORM (is the settled work scheduled?), never correctness or freshness (whether the
built work matches the spec stays the separate conformance judgment).

Operator-communication law: the engine-internal lifecycle ladder (the stub/draft/locked markers a
document carries) NEVER surfaces to the operator as a raw token — findings say "settled", "in the build order",
"capability", in plain language. The raw marker lives only in document frontmatter and in this script's logic.

Shared grammar: this check imports the product-spec readers and path constants from `spec_form` (the package's
shared spec-grammar home) — `_capability_doc_rels`, `_read`, `_frontmatter_status`, the pipe-table and link
parsers, `_SPEC_DIR` / `_BUILD_PLAN_REL` — so the build order is parsed with exactly the grammar the rest of the
module uses, and `spec_form` already excludes the build-plan doc from the capability walk.

Contract: invoked by the validator with NO arguments, it prints a finding.v1 JSON array to stdout and exits 0.
A separate `demo` subcommand runs a falsifiable self-check.
"""
from __future__ import annotations
import json
import os
import sys

# Make the sibling `.engine/tools/` modules importable whether imported as `product_design.coverage` or run
# directly as the wired check script (the spec_form / lock_integrity idiom).
_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PARENT not in sys.path:
    sys.path.insert(0, _PARENT)

import validate  # noqa: E402 — ROOT (test-redirectable) + the finding.v1 helper
from product_design import spec_form  # noqa: E402 — the shared spec-grammar home (readers + path constants)

# The settled lifecycle marker (engine-internal; rendered as "settled" on every operator surface).
_SETTLED = "locked"
# The build-order table columns. A prefix match (spec_form._table_with_columns) allows a trailing Notes column.
_BUILD_PLAN_COLUMNS = ("phase", "capability", "doc")

_BOUND_TAIL = ("This check only reads your files — it never changes them — and it checks that every settled "
               "capability has a place in the build order, not whether the built work matches it.")


# --------------------------------------------------------------------------------------------------
# Operator-facing messages (plain language; the lifecycle ladder is rendered "settled", never a raw token)
# --------------------------------------------------------------------------------------------------

_NO_SPEC_MESSAGE = (
    "Build-order checking isn't active here yet — this check looks for a product specification under "
    "`docs/spec/` and didn't find one. That's a normal, expected state for a project that hasn't written its "
    "spec yet, not an error: it starts checking your build order on its own once you add `docs/spec/` and "
    "settle a capability. " + _BOUND_TAIL
)

_NOTHING_SETTLED_MESSAGE = (
    "Nothing is settled yet, so there's nothing to put in a build order — this check starts working once you've "
    "settled a capability and recorded the order you'll build your settled capabilities in. " + _BOUND_TAIL
)


def _capabilities_phrase(n: int) -> str:
    # The caller already says "You've settled …", so this phrase carries only the count + noun (no second
    # "settled", which would read as a stutter — "You've settled 1 settled capability").
    return "1 capability" if n == 1 else f"{n} capabilities"


def _no_build_plan_message(n: int) -> str:
    return (
        f"You've settled {_capabilities_phrase(n)} but haven't started a build order yet. A build order "
        f"(`docs/spec/build-plan.md`) lists your settled capabilities in the order you'll build them, so none is "
        f"overlooked. This is only a heads-up, not a problem — a build can still go ahead. To start one, add "
        f"`docs/spec/build-plan.md` with a table whose columns are Phase, Capability, and Doc, one row per "
        f"capability you plan to build. " + _BOUND_TAIL
    )


def _orphan_message(rel: str) -> str:
    return (
        f"The settled capability `{rel}` has no place in your build order (`docs/spec/build-plan.md`) — it's "
        f"settled, but the build order doesn't say when it will be built, so it could be overlooked. To clear "
        f"this, add a row for it to the build order's table (Phase, Capability, Doc), or reopen the capability "
        f"if it isn't settled after all. " + _BOUND_TAIL
    )


def _malformed_build_plan_message() -> str:
    return (
        "Your build order (`docs/spec/build-plan.md`) isn't a well-formed list of phases. It needs a table "
        "whose columns are Phase, Capability, and Doc, with one row per capability you plan to build, in the "
        "order you'll build them. To clear this, add or fix that table in `docs/spec/build-plan.md`. "
        + _BOUND_TAIL
    )


def _dangling_message(target: str) -> str:
    return (
        f"Your build order (`docs/spec/build-plan.md`) points to a capability document `{target}` that doesn't "
        f"exist. To clear this, either create that document or fix the link in the build order so it points to "
        f"a document that exists. " + _BOUND_TAIL
    )


def _not_a_capability_message(target: str) -> str:
    return (
        f"Your build order (`docs/spec/build-plan.md`) lists `{target}`, which isn't a capability document — the "
        f"build order schedules capabilities, not the index or the build order itself. To clear this, point "
        f"that row at a capability document, or remove the row. " + _BOUND_TAIL
    )


# --------------------------------------------------------------------------------------------------
# The coverage findings
# --------------------------------------------------------------------------------------------------

def findings(tier: str, root: "str | None" = None) -> list:
    """The build-order coverage findings for `root` (defaults to `validate.ROOT`), as finding.v1 dicts.

    Empty list = a genuine clean pass (a well-formed build order that schedules every settled capability, or a
    well-formed build order with nothing settled yet). A single `soft` note when there is no spec tree, nothing
    settled, or settled work but no build order yet — said plainly, never a silent pass. When a build order
    EXISTS, structural problems and orphaned settled capabilities are reported at `tier` (hard) so they block
    the merge; the no-spec / nothing-settled / no-build-order-yet notes are always `soft` so a project is never
    blocked merely for not having started a build order."""
    root = root or validate.ROOT
    spec_root = spec_form._spec_root(root)

    # Disclosed no-op: no spec tree at all — always soft, never silent.
    if not os.path.isdir(spec_root):
        return [validate.disclosed_noop(_NO_SPEC_MESSAGE, None)]

    # Which capabilities are settled? (spec_form._capability_doc_rels already excludes the index + build-plan.)
    settled = []
    for rel in spec_form._capability_doc_rels(root):
        if spec_form._frontmatter_status(spec_form._read(root, rel)) == _SETTLED:
            settled.append(rel)

    bp_rel = spec_form._BUILD_PLAN_REL
    bp_exists = os.path.isfile(os.path.join(root, bp_rel))

    # No build order yet: a soft note, never a block — a build may proceed without one.
    if not bp_exists:
        if not settled:
            return [validate.disclosed_noop(_NOTHING_SETTLED_MESSAGE, None)]
        return [validate.finding("soft", _no_build_plan_message(len(settled)), {"file": bp_rel, "line": None})]

    # A build order exists — evaluate its structure and its coverage of the settled capabilities (hard teeth).
    here = {"file": bp_rel, "line": None}
    out = []
    rows = spec_form._table_with_columns(spec_form._read(root, bp_rel), _BUILD_PLAN_COLUMNS)
    if not rows:  # None (no recognizable table) or [] (no phases)
        out.append(validate.finding(tier, _malformed_build_plan_message(), here))
        # With no readable phases, every settled capability is unscheduled — report those too, so the operator
        # sees the full picture rather than fixing the table only to meet a second wave of findings.
        for rel in settled:
            out.append(validate.finding(tier, _orphan_message(rel), {"file": rel, "line": None}))
        return out

    # Resolve each row's Doc cell to a repo-root path (relative to docs/spec/, exactly as the index resolves).
    referenced = set()
    flagged = set()  # de-dup structural findings by resolved target
    for row in rows:
        if len(row) < 3:
            continue
        target = spec_form._link_target(row[2])
        if not target:
            continue  # a row that names no document schedules nothing (lenient — a settled cap still surfaces)
        resolved = os.path.normpath(os.path.join(spec_form._SPEC_DIR, target))
        if resolved in flagged:
            continue
        if resolved in (spec_form._INDEX_REL, bp_rel):
            flagged.add(resolved)
            out.append(validate.finding(tier, _not_a_capability_message(target), here))
            continue
        if not os.path.isfile(os.path.join(root, resolved)):
            flagged.add(resolved)
            out.append(validate.finding(tier, _dangling_message(target), here))
            continue
        referenced.add(resolved)

    # The teeth: a settled capability the build order never references is an orphan (could be silently dropped).
    for rel in settled:
        if rel not in referenced:
            out.append(validate.finding(tier, _orphan_message(rel), {"file": rel, "line": None}))

    return out


def emit_findings() -> int:
    """The no-argument path the validator invokes: print the finding.v1 array and return 0. Violations carry
    the rule's declared tier (ENGINE_RULE_TIER, defaulting hard); the no-spec / nothing-settled / no-build-order
    notes are always soft (set inside findings())."""
    # ENGINE_SPEC_ROOT (unset in production) lets the negative-fixture meta-check point the spec scan
    # at a seeded docs/spec tree, so the build-order gate is witnessed biting a real bad input (#286).
    print(json.dumps(findings(os.environ.get("ENGINE_RULE_TIER", "hard"),
                              validate.env_override_path("ENGINE_SPEC_ROOT"))))
    return 0


def demo() -> int:
    """Prove the coverage inspector: a build order that schedules every settled capability passes (including
    when a capability rides two phases, when it moved to a later phase, and when nothing is settled yet); a
    settled capability missing from an existing build order is a HARD, self-clearing finding naming it; a
    structurally broken build order (no phases / a dangling link / a row pointing at the index) is HARD; the
    no-spec / nothing-settled / no-build-order-yet states are SOFT, never a block, never silent; a build order
    with a UTF-8 byte-order mark is read correctly; no finding ever leaks a raw lifecycle token; and the
    rule tier is carried (a hard case run soft drops to soft, while the no-build-order nudge stays soft).
    RETURNS NON-ZERO if any invariant is broken (the falsification can fail). Mutation-free: every case runs
    against a throwaway temp root, so the real working tree is never touched."""
    import re
    import shutil
    import tempfile

    def _seed(files: dict) -> str:
        d = tempfile.mkdtemp(prefix="engine-spec-coverage-demo-")
        for rel, body in files.items():
            path = os.path.join(d, rel)
            parent = os.path.dirname(path)
            if parent and not os.path.isdir(parent):
                os.makedirs(parent)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(body)
        return d

    def _cap(status: str) -> str:
        body = f"---\nstatus: {status}\n---\n\n# A capability\n"
        if status in ("draft", "locked"):
            body += ("\n## Summary\nWhat and who for.\n\n## Behavior\nHow it behaves.\n\n## Acceptance criteria\n"
                     "\n| Criterion | How verified | Who checks it |\n| --- | --- | --- |\n"
                     "| It works | a behavioral demo | operator |\n")
        return body

    def _index(rows: str) -> str:
        return "# Product spec\n\n| Capability | Status | Doc |\n| --- | --- | --- |\n" + rows

    def _plan(rows: str) -> str:
        return "# Build order\n\n| Phase | Capability | Doc |\n| --- | --- | --- |\n" + rows

    cases = []  # (label, files, predicate over findings("hard", root))
    cases.append(("a build order scheduling both settled capabilities passes cleanly",
                  {"docs/spec/index.md": _index("| A | settled | [A](a.md) |\n| B | settled | [B](b.md) |\n"),
                   "docs/spec/a.md": _cap("locked"), "docs/spec/b.md": _cap("locked"),
                   "docs/spec/build-plan.md": _plan("| 1 | A | [A](a.md) |\n| 2 | B | [B](b.md) |\n")},
                  lambda fs: fs == []))
    cases.append(("a settled capability scheduled across two phases passes cleanly",
                  {"docs/spec/index.md": _index("| A | settled | [A](a.md) |\n"),
                   "docs/spec/a.md": _cap("locked"),
                   "docs/spec/build-plan.md": _plan("| 1 | A | [A](a.md) |\n| 2 | A | [A](a.md) |\n")},
                  lambda fs: fs == []))
    cases.append(("a settled capability moved to a later phase passes cleanly (re-sequencing is free)",
                  {"docs/spec/index.md": _index("| A | settled | [A](a.md) |\n"),
                   "docs/spec/a.md": _cap("locked"),
                   "docs/spec/build-plan.md": _plan("| 2 | A | [A](a.md) |\n")},
                  lambda fs: fs == []))
    cases.append(("a well-formed build order with nothing settled (only in-progress) passes cleanly",
                  {"docs/spec/index.md": _index("| A | in progress | [A](a.md) |\n"),
                   "docs/spec/a.md": _cap("draft"),
                   "docs/spec/build-plan.md": _plan("| 1 | A | [A](a.md) |\n")},
                  lambda fs: fs == []))
    cases.append(("no docs/spec tree says the no-op plainly (soft, never silent)",
                  {"README.md": "hi"},
                  lambda fs: len(fs) == 1 and fs[0]["severity"] == "soft"
                  and "isn't active here yet" in fs[0]["message"]))
    cases.append(("a spec with nothing settled and no build order says so plainly (soft)",
                  {"docs/spec/index.md": _index("| A | in progress | [A](a.md) |\n"),
                   "docs/spec/a.md": _cap("draft")},
                  lambda fs: len(fs) == 1 and fs[0]["severity"] == "soft"
                  and "Nothing is settled yet" in fs[0]["message"]))
    cases.append(("settled work but no build order yet is a soft nudge, never a block",
                  {"docs/spec/index.md": _index("| A | settled | [A](a.md) |\n"),
                   "docs/spec/a.md": _cap("locked")},
                  lambda fs: len(fs) == 1 and fs[0]["severity"] == "soft"
                  and "haven't started a build order" in fs[0]["message"]))
    cases.append(("a settled capability missing from an existing build order is a hard, self-clearing finding",
                  {"docs/spec/index.md": _index("| A | settled | [A](a.md) |\n| B | settled | [B](b.md) |\n"),
                   "docs/spec/a.md": _cap("locked"), "docs/spec/b.md": _cap("locked"),
                   "docs/spec/build-plan.md": _plan("| 1 | A | [A](a.md) |\n")},
                  lambda fs: any(f["severity"] == "hard" and "docs/spec/b.md" in f["message"]
                                 and "To clear this" in f["message"] for f in fs)))
    cases.append(("a build order with no phases is a hard finding",
                  {"docs/spec/index.md": _index("| A | settled | [A](a.md) |\n"),
                   "docs/spec/a.md": _cap("locked"),
                   "docs/spec/build-plan.md": "# Build order\n\n(no table yet)\n"},
                  lambda fs: any(f["severity"] == "hard" and "well-formed list of phases" in f["message"]
                                 for f in fs)))
    cases.append(("a build order pointing at a document that doesn't exist is a hard dangling finding",
                  {"docs/spec/index.md": _index("| A | settled | [A](a.md) |\n"),
                   "docs/spec/a.md": _cap("locked"),
                   "docs/spec/build-plan.md": _plan("| 1 | A | [A](a.md) |\n| 2 | Ghost | [Ghost](ghost.md) |\n")},
                  lambda fs: any(f["severity"] == "hard" and "doesn't exist" in f["message"] for f in fs)))
    cases.append(("a build order row pointing at the index is a hard not-a-capability finding",
                  {"docs/spec/index.md": _index("| A | settled | [A](a.md) |\n"),
                   "docs/spec/a.md": _cap("locked"),
                   "docs/spec/build-plan.md": _plan("| 1 | A | [A](a.md) |\n| 1 | Idx | [Idx](index.md) |\n")},
                  lambda fs: any(f["severity"] == "hard" and "isn't a capability document" in f["message"]
                                 for f in fs)))
    cases.append(("a build order with a UTF-8 byte-order mark is read correctly, not mis-parsed",
                  {"docs/spec/index.md": _index("| A | settled | [A](a.md) |\n"),
                   "docs/spec/a.md": _cap("locked"),
                   "docs/spec/build-plan.md": "﻿" + _plan("| 1 | A | [A](a.md) |\n")},
                  lambda fs: fs == []))

    failures = []
    for label, files, ok in cases:
        root = _seed(files)
        try:
            result = findings("hard", root=root)
        finally:
            shutil.rmtree(root, ignore_errors=True)
        # The raw lifecycle ladder must never surface in a finding (the operator-communication law).
        for f in result:
            for token in spec_form._VALID_STATUS:
                if re.search(rf"\b{token}\b", f["message"]):
                    failures.append(f"{label}: a finding leaked the raw lifecycle token '{token}': {f['message']}")
        if not ok(result):
            failures.append(f"{label}: invariant broken, got {result}")

    # Tier is carried: a hard structural case run at soft drops to soft; the no-build-order nudge stays soft.
    hard_case = {"docs/spec/index.md": _index("| A | settled | [A](a.md) |\n"),
                 "docs/spec/a.md": _cap("locked"),
                 "docs/spec/build-plan.md": _plan("| 1 | B | [B](b.md) |\n")}  # A orphaned; B dangling
    root = _seed(hard_case)
    try:
        if any(f["severity"] != "soft" for f in findings("soft", root=root)):
            failures.append("tier not carried: a structural/orphan finding stayed hard when run at soft")
    finally:
        shutil.rmtree(root, ignore_errors=True)
    nudge_case = {"docs/spec/index.md": _index("| A | settled | [A](a.md) |\n"), "docs/spec/a.md": _cap("locked")}
    root = _seed(nudge_case)
    try:
        if any(f["severity"] != "soft" for f in findings("hard", root=root)):
            failures.append("the no-build-order nudge must stay soft even when the rule tier is hard")
    finally:
        shutil.rmtree(root, ignore_errors=True)

    if failures:
        print("DEMO FAILED — the coverage inspector broke an invariant:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("DEMO PASSED — the coverage inspector passes a build order that schedules every settled capability "
          "(including re-sequencing and the nothing-settled-yet case), says the no-op/nudge states plainly and "
          "softly, flags an orphaned settled capability and a broken build order at hard severity with a "
          "self-clearing message, reads a BOM build order correctly, never leaks a raw lifecycle token, and "
          "carries the rule tier.")
    return 0


def main(argv: list) -> int:
    if argv and argv[0] == "demo":
        return demo()
    return emit_findings()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
