#!/usr/bin/env python3
"""Pre-phase build-readiness — is a build-order phase's spec settled enough to start building it?

build-orchestration (core) confirms, before a phase's work starts, that the phase is ready: every capability
the build order schedules under that phase has a **settled** description (the ground a build works from). This is
the product analogue of this workspace's own pre-build dry-run — a read-only, advisory check that a builder
could construct the phase from what is written, not a gate (the dry-run precedent it mirrors is itself read-only and advisory). The orchestrator runs it at Plan, when
it groups product work into phases (`build-orchestration.md`, "Grouping product work into phases"), and surfaces
the result plainly; a not-ready phase informs the operator's decision, it never blocks a merge.

Readiness is strictly mechanical: a capability is ready when its `docs/spec/` document is **settled**
(`status: locked`). A settled document is already guaranteed well-formed — the product-spec form check reports a
structural problem in a settled document at hard severity, so a form-incomplete settled document could not have
merged — so "settled" is the whole readiness signal; this never re-judges form, and never judges whether the
description is *rich enough* to build from (that stays the operator's call and the plan-review passes'). It reads
the product's own files only and changes nothing (the engine/product wall, R5/R9).

SELF-CONTAINED ON PURPOSE. This is a CORE tool, but the `docs/spec/` corpus is authored by the OPTIONAL
product-design module. So it imports NO product-design code — a required tool must not depend on an optional
module, or it would crash on every repo that never installed product-design (the `spec_referent.py` precedent).
It carries its own minimal markdown parser: a knowing duplicate of the trivial bits of
`product_design/spec_form.py` (the build-plan columns, the frontmatter-status read, the pipe-table + link
parsers). "settled" is `spec_form`'s `locked`.

Absent inputs are a disclosed no-op, never a crash and never a silent "all ready": no `docs/spec/` tree, or no
`docs/spec/build-plan.md` yet, means there are no phases to judge — said plainly. The confined-read guard
(engine/product wall) runs before any capability document is opened: a build-order link that escapes `docs/spec/`
is reported, never read.

Operator-communication law: the engine-internal lifecycle markers (`stub`/`draft`/`locked`) NEVER surface
as raw tokens — every rendered line uses the plain stages ("not yet described" / "in progress" / "settled").

Operator demo (real readiness logic over throwaway spec trees; no real files touched):
  uv run --directory .engine -- python tools/build_readiness.py demo
"""
from __future__ import annotations

import json
import os
import re
import sys

# <repo>/.engine/tools/build_readiness.py -> <repo>. A pure leaf: computed from __file__, no sibling import.
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# The committed product spec tree + the build order, relative to the repository root.
_SPEC_DIR = os.path.join("docs", "spec")
_BUILD_PLAN_REL = os.path.join("docs", "spec", "build-plan.md")

# The spec grammar — a knowing duplicate of product_design/spec_form.py's (a CORE tool must not import the
# OPTIONAL package). The build order is a `| Phase | Capability | Doc |` table; "locked" is the settled stage.
_BUILD_PLAN_COLUMNS = ("phase", "capability", "doc")
_LOCKED = "locked"

# The engine-internal lifecycle ladder -> its plain operator render (the raw token never surfaces).
_PLAIN_STATUS = {"stub": "not yet described", "draft": "in progress", "locked": "settled"}
_PLAIN_MISSING = "no description written yet"     # a scheduled capability whose document does not exist
_PLAIN_UNSETTLED = "not settled yet"              # a document that exists but carries no recognized stage
_PLAIN_UNREADABLE = "a description the engine could not read"  # a link that escapes docs/spec/ (never opened)

_BOUND_TAIL = ("This only reads your files — it never changes them — and it confirms each piece in a phase is "
               "settled, the ground a build works from; it doesn't judge whether the description is rich enough "
               "to build from — that's your call.")


# ---- parse helpers (ported from product_design/spec_form.py; trivial, stdlib-only) -----------------

def _read(real_path: str) -> str:
    # utf-8-sig strips a BOM (Windows editors), exactly as the spec readers do.
    with open(real_path, "r", encoding="utf-8-sig", errors="replace") as fh:
        return fh.read()


def _frontmatter_status(text: str) -> "str | None":
    """The lowercased value of the first `status:` key inside the leading `---` frontmatter block, or None.
    Tolerant of malformed content — it never raises. (Port of spec_form._frontmatter_status.)"""
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


def _is_table_row(line: str) -> bool:
    s = line.strip()
    return s.startswith("|") and s.count("|") >= 2


def _is_separator_row(line: str) -> bool:
    s = line.strip()
    return bool(s) and bool(re.fullmatch(r"\|[\s:\-|]+\|?", s)) and "-" in s


def _cells(line: str) -> list:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _tables(text: str) -> list:
    """Every pipe-table in `text`, as (header_cells_lowercased, [data_row_cells, ...]). (Port of spec_form._tables.)"""
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
    """The data rows of the first table whose leading columns are `columns` (case-insensitive); a prefix match,
    so a trailing extra column (e.g. Notes) is allowed. (Port of spec_form._table_with_columns.)"""
    n = len(columns)
    for header, rows in _tables(text):
        if tuple(header[:n]) == tuple(columns):
            return rows
    return None


def _link_target(cell: str) -> "str | None":
    """The path inside a Markdown link `[text](path)` in `cell`, or the bare cell text if it looks like a
    path, or None when the cell names no `.md` document. A trailing #fragment/?query is dropped. A FAITHFUL
    copy of spec_form._link_target — the bare-path fallback matters: a build-order row that names its document
    as a bare path (`b.md`) rather than a link still schedules that capability, so readiness must not drop it
    (dropping it would wrongly report the phase ready when that capability is not settled)."""
    m = re.search(r"\[[^\]]*\]\(([^)]+)\)", cell)
    raw = (m.group(1) if m else cell).strip()
    raw = raw.split("#", 1)[0].split("?", 1)[0].strip()
    return raw if raw.endswith(".md") else None


# ---- reading a scheduled capability's stage (the confined read) ------------------------------------

def _capability_stage(root: str, target: str) -> str:
    """The plain stage of a build-order Doc target (a path relative to `docs/spec/`): "settled" when its document
    is `status: locked`, else the plain render of its stage, or a plain reason when it can't be read. The
    CONFINED-READ guard runs first — a target whose real path escapes `docs/spec/` is `_PLAIN_UNREADABLE` and the
    file is NEVER opened (the engine/product wall)."""
    spec_real = os.path.realpath(os.path.join(root, _SPEC_DIR))
    real = os.path.realpath(os.path.join(spec_real, target))
    if not (real == spec_real or real.startswith(spec_real + os.sep)):
        return _PLAIN_UNREADABLE                              # escapes docs/spec/ — reported, never opened
    if not os.path.isfile(real):
        return _PLAIN_MISSING
    status = _frontmatter_status(_read(real))
    if status == _LOCKED:
        return _PLAIN_STATUS[_LOCKED]
    # A document that exists but carries no recognized stage reads as not-settled — distinct from a document
    # that does not exist (both are not-ready, but the plain reason differs). A committed doc could not reach a
    # merge with an unrecognized status — the product-spec form check blocks that at hard tier — so this is the
    # honest render for the pre-merge / in-progress state, never a state a settled build order carries.
    return _PLAIN_STATUS.get(status, _PLAIN_UNSETTLED)


# ---- readiness over the build order ----------------------------------------------------------------

def readiness(root: "str | None" = None) -> dict:
    """Per-phase build-readiness for `root` (defaults to the repo root). Returns a dict:

      {"ok": True, "phases": [{"phase", "ready", "pieces": [{"capability", "stage", "ready"}, ...]}, ...]}
    when there is a build order to judge — phases in build-order appearance order, each capability carrying its
    plain stage and whether it is settled; a phase is ready when every piece is settled. Or a disclosed no-op

      {"ok": False, "no_op_reason", "detail"}
    when there is no `docs/spec/` tree or no build order yet (nothing to judge — said plainly, never a crash and
    never a silent "all ready"). A build-order row that names no `.md` document schedules nothing and is skipped
    (matching the coverage grammar). Readiness does NOT replicate coverage's structural checks — a dangling link,
    or a row pointing at the index or build-plan, reads here as an ordinary not-yet-settled piece, so the phase
    reads not-ready (the safe direction); coverage is what blocks such a malformed build order at merge."""
    root = root or _ROOT
    if not os.path.isdir(os.path.join(root, _SPEC_DIR)):
        return {"ok": False, "no_op_reason": "no-spec-installed",
                "detail": "there's no settled description for this project yet, so there are no phases to check"}
    if not os.path.isfile(os.path.join(root, _BUILD_PLAN_REL)):
        return {"ok": False, "no_op_reason": "no-build-order",
                "detail": "there's no build order yet, so there are no phases to check readiness for"}

    rows = _table_with_columns(_read(os.path.join(root, _BUILD_PLAN_REL)), _BUILD_PLAN_COLUMNS)
    if not rows:                                             # None (no recognizable table) or [] (no phases)
        return {"ok": False, "no_op_reason": "no-phases",
                "detail": "the build order doesn't list any phases yet, so there's nothing to check"}

    order: list = []                                        # phase names in first-appearance order
    by_phase: dict = {}
    for row in rows:
        if len(row) < 3:
            continue
        phase, capability, doc_cell = row[0].strip(), row[1].strip(), row[2]
        target = _link_target(doc_cell)
        if not target:
            continue                                        # a row that names no document schedules nothing
        if phase not in by_phase:
            by_phase[phase] = []
            order.append(phase)
        stage = _capability_stage(root, target)
        by_phase[phase].append({"capability": capability, "stage": stage, "ready": stage == _PLAIN_STATUS[_LOCKED]})

    phases = []
    for phase in order:
        pieces = by_phase[phase]
        phases.append({"phase": phase, "ready": all(p["ready"] for p in pieces), "pieces": pieces})
    return {"ok": True, "phases": phases}


def _select(result: dict, phase: "str | None") -> dict:
    """Narrow a readiness result to a single named phase (case-insensitive), or leave it whole when phase is None.
    An unknown phase name becomes a disclosed no-op, so a mistyped `--phase` is never read as "all ready"."""
    if not result.get("ok") or phase is None:
        return result
    want = phase.strip().lower()
    hit = [p for p in result["phases"] if p["phase"].strip().lower() == want]
    if not hit:
        return {"ok": False, "no_op_reason": "no-such-phase",
                "detail": f"there's no phase named \"{phase}\" in your build order"}
    return {"ok": True, "phases": hit}


# ---- rendering (plain language; the lifecycle ladder is rendered, never shown as a raw token) -------

def render(result: dict) -> str:
    """The plain-language readiness block the orchestrator surfaces verbatim. A disclosed no-op renders its plain
    detail; otherwise one line per phase — ready, or naming each piece that isn't settled yet and its plain
    stage — closing with the bound so a "ready" line is never read as a promise the design is good enough."""
    if not result.get("ok"):
        return f"Build-readiness: {result['detail']}.\n\n{_BOUND_TAIL}"

    lines = ["Build-readiness — whether each phase's pieces are settled enough to start building:", ""]
    for p in result["phases"]:
        if p["ready"]:
            lines.append(f"- **{p['phase']}**: ready — every piece is settled.")
        else:
            not_ready = [pc for pc in p["pieces"] if not pc["ready"]]
            n, total = len(not_ready), len(p["pieces"])
            piece_word = "piece" if total == 1 else "pieces"
            verb = "isn't" if n == 1 else "aren't"
            lines.append(f"- **{p['phase']}**: not ready yet — {n} of {total} {piece_word} {verb} settled:")
            for pc in not_ready:
                lines.append(f"    - {pc['capability']} ({pc['stage']})")
    lines.append("")
    lines.append(_BOUND_TAIL)
    return "\n".join(lines)


# ---- operator-runnable demo (real readiness logic; only throwaway spec trees) ----------------------

def _demo() -> int:
    import shutil
    import tempfile

    print("Whether a build-order phase is ready to start — the engine checks that every piece a phase schedules\n"
          "has a settled description. No real files are touched.\n")

    def _seed(files: dict) -> str:
        d = tempfile.mkdtemp(prefix="engine-build-readiness-demo-")
        for rel, text in files.items():
            path = os.path.join(d, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(text)
        return d

    def _cap(status: str) -> str:
        return f"---\nstatus: {status}\n---\n\n# A capability\n\n## Summary\nx\n"

    def _index(rows: str) -> str:
        return "# Product spec\n\n| Capability | Status | Doc |\n| --- | --- | --- |\n" + rows

    def _plan(rows: str) -> str:
        return "# Build order\n\n| Phase | Capability | Doc |\n| --- | --- | --- |\n" + rows

    # A build order with three phases: Foundation is all settled (ready); Core has one settled + one in-progress
    # piece scheduled by a BARE PATH (`c.md`, not a link — it must not be dropped, or the phase would falsely
    # read ready); Later schedules a piece whose document doesn't exist yet (not ready).
    ready_tree = _seed({
        "docs/spec/index.md": _index("| A | settled | [A](a.md) |\n| B | settled | [B](b.md) |\n"
                                     "| C | in progress | [C](c.md) |\n"),
        "docs/spec/a.md": _cap("locked"),
        "docs/spec/b.md": _cap("locked"),
        "docs/spec/c.md": _cap("draft"),
        "docs/spec/build-plan.md": _plan("| Foundation | A | [A](a.md) |\n"
                                         "| Core | B | [B](b.md) |\n"
                                         "| Core | C | c.md |\n"
                                         "| Later | Ghost | [Ghost](ghost.md) |\n"),
    })
    r = readiness(ready_tree)
    rendered = render(r)

    # Escaping-pointer tree: a build-order link that walks out of docs/spec/ is reported, never read.
    walled = _seed({"docs/spec/index.md": _index("| A | settled | [A](a.md) |\n"),
                    "docs/spec/a.md": _cap("locked"),
                    "secret.md": "TOP SECRET — must never be read\n",
                    "docs/spec/build-plan.md": _plan("| P | Esc | [Esc](../../secret.md) |\n")})
    r_walled = readiness(walled)

    # Absent inputs: no docs/spec tree, and a spec tree with no build order — both disclosed no-ops.
    bare = _seed({"README.md": "hi"})
    r_bare = readiness(bare)
    no_plan = _seed({"docs/spec/index.md": _index("| A | settled | [A](a.md) |\n"), "docs/spec/a.md": _cap("locked")})
    r_no_plan = readiness(no_plan)

    # A named-phase selection, and an unknown phase name (a disclosed no-op, never "all ready").
    r_core = _select(readiness(ready_tree), "Core")
    r_unknown = _select(readiness(ready_tree), "Nonexistent")

    print("The readiness block the engine would surface:\n")
    for ln in rendered.splitlines():
        print("   " + ln)
    print()

    phases = {p["phase"]: p for p in (r.get("phases") or [])}
    checks = {
        "a phase whose pieces are all settled is ready":
            "Foundation" in phases and phases["Foundation"]["ready"],
        "a phase with an in-progress piece is not ready":
            "Core" in phases and not phases["Core"]["ready"],
        "a bare-path Doc cell is scheduled, not dropped (else the phase would falsely read ready)":
            "Core" in phases and len(phases["Core"]["pieces"]) == 2,
        "the not-ready phase names the in-progress piece with its plain stage":
            "Core" in phases and any(pc["capability"] == "C" and pc["stage"] == "in progress" and not pc["ready"]
                                     for pc in phases["Core"]["pieces"]),
        "a scheduled piece with no document is not ready (no description written yet)":
            "Later" in phases and any(pc["stage"] == _PLAIN_MISSING and not pc["ready"]
                                      for pc in phases["Later"]["pieces"]),
        "an escaping build-order link is reported, never read":
            r_walled["ok"] and r_walled["phases"][0]["pieces"][0]["stage"] == _PLAIN_UNREADABLE,
        "no docs/spec tree is a disclosed no-op": (not r_bare["ok"]) and r_bare["no_op_reason"] == "no-spec-installed",
        "a spec tree with no build order is a disclosed no-op":
            (not r_no_plan["ok"]) and r_no_plan["no_op_reason"] == "no-build-order",
        "selecting a named phase narrows to it": r_core["ok"] and len(r_core["phases"]) == 1
            and r_core["phases"][0]["phase"] == "Core",
        "an unknown phase name is a disclosed no-op, never all-ready":
            (not r_unknown["ok"]) and r_unknown["no_op_reason"] == "no-such-phase",
        "the render carries the bound": _BOUND_TAIL in rendered,
    }
    # Operator-communication law: no rendered line may leak a raw lifecycle token.
    leaks = [t for t in ("stub", "draft", "locked") if re.search(rf"\b{t}\b", rendered)]

    for d in (ready_tree, walled, bare, no_plan):
        shutil.rmtree(d, ignore_errors=True)

    bad = [name for name, ok in checks.items() if not ok]
    if bad or leaks:
        print("DEMO UNEXPECTED — these invariants did not hold:", file=sys.stderr)
        for name in bad:
            print(f"  - {name}", file=sys.stderr)
        if leaks:
            print(f"  - a rendered line leaked a raw lifecycle token: {leaks}", file=sys.stderr)
        return 1
    print("All readiness invariants held: an all-settled phase is ready; a phase with an in-progress or "
          "not-yet-written piece is not ready and names it; an escaping link is reported and never read; absent "
          "inputs are disclosed no-ops; a named phase narrows and an unknown one no-ops; no raw lifecycle token "
          "leaks.")
    return 0


# ---- CLI -------------------------------------------------------------------------------------------

def _parse_phase(argv: list) -> "str | None":
    for i, a in enumerate(argv):
        if a == "--phase" and i + 1 < len(argv):
            return argv[i + 1]
    return None


def main(argv: "list | None" = None) -> int:
    argv = list(argv if argv is not None else sys.argv[1:])
    if argv and argv[0] == "demo":
        return _demo()
    if argv and argv[0] == "check":
        result = _select(readiness(_ROOT), _parse_phase(argv[1:]))
        if "--json" in argv:
            print(json.dumps(result))
        else:
            print(render(result))
        return 0
    print("usage: build_readiness.py [check [--phase NAME] [--json] | demo]", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
