#!/usr/bin/env python3
"""Product-design form inspector — the read-only `custom/script` entry for engine/check/product-design-form
(the product-design module's form check over the *fuller* design documents that live beside the `docs/spec/`
corpus: the guiding principles, the architecture overview with its diagram, and the user guides).

Why this exists (issue #553): the intake now produces the full structured description by DEFAULT — the
corpus PLUS a structural backbone (guiding principles + an architecture overview) — and the operator opts
*out* to a lighter, corpus-only description. So the backbone is owed by default, and "the engine drafts these
but does not check them" is no longer true: this check inspects the fuller documents for FORM, exactly as
`spec_form` does for the corpus. What it never does — for these documents or any other — is judge whether the
design is *right*; that stays the operator's call and the review lenses'.

The depth choice is recorded, not guessed, and the recorded choice is what decides whether a problem BLOCKS.
The intake writes a `spec_depth:` marker into the master index's frontmatter (`docs/spec/index.md`): `full`
(the default) or `light` (the recorded opt-out). Findings are hard (blocking) ONLY when the operator has
recorded the full write-up; in every other state the check still surfaces a problem, but as a soft nudge that
never blocks:

- `spec_depth: full`  → the operator is in the checked regime: the backbone (`docs/principles.md` +
  `docs/architecture.md`) is required and well-formed, and a present guide is well-formed — any gap is HARD.
- `spec_depth: light` → the recorded opt-out: a missing backbone is silent, and a document the operator did
  write (a stray backbone doc, a guide) is nudged toward good form at SOFT severity, never blocked.
- marker absent        → a description that predates this default, or was hand-authored. Everything is a SOFT
  nudge — a missing backbone, a malformed present document, a malformed guide — so a project written before
  this became the default (when these documents were explicitly *not* checked) is never retroactively blocked.
- an unrecognized marker (a typo like `fll`) is surfaced as a soft note, so it is not silently read as
  "unrecorded" and the operator can fix it to arm the full write-up.

The user guides (the Diátaxis tree under `docs/tutorials|how-to|reference|explanation/`) are always
discretionary — "only the ones this product needs" — so a guide is checked for FORM only when it is present,
and its absence is never flagged, at any depth.

Disclosed-no-op: when a project has no `docs/spec/` tree at all, there is no description to have a backbone
for, so the whole check is a disclosed no-op (one soft note, never a silent pass) — the same posture as
`spec_form`.

Honest floor — the engine/product wall and the read-only firewall: it inspects the product's own `docs/`
files ONLY (the fuller documents beside `docs/spec/`), never the engine's `.engine/` tooling; it reads to
check structure but never writes; and it checks FORM, not correctness. Its file selection is by explicit path
and explicit guide directories — never a blanket `docs/` walk — and it never follows a symlink out of the
tree, so it and `spec_form` partition `docs/` cleanly: `docs/spec/` is `spec_form`'s alone and out of scope
here.

Shared spec-grammar home: this reuses `spec_form`'s markdown readers (`_read`, `_h2_headings`, the frontmatter
helpers) rather than re-implementing the grammar — one grammar home for the whole package (`spec_form` is the
de-facto shared library its siblings import). It never edits `spec_form`.

Contract: invoked by the validator with NO arguments, it prints a finding.v1 JSON array to stdout and exits
0. A separate `demo` subcommand runs a falsifiable self-check.
"""
from __future__ import annotations
import json
import os
import re
import sys

# Make the sibling `.engine/tools/` modules importable whether imported as `product_design.design_form`
# or run directly as the wired check script (the spec_form / coverage / lock_integrity idiom).
_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PARENT not in sys.path:
    sys.path.insert(0, _PARENT)

import validate  # noqa: E402 — ROOT (test-redirectable) + the finding.v1 helpers
from product_design import spec_form  # noqa: E402 — the shared spec-grammar readers (imported, never edited)

# --------------------------------------------------------------------------------------------------
# The fuller documents this check governs — explicit paths only (never an os.walk of docs/).
# --------------------------------------------------------------------------------------------------

_PRINCIPLES_REL = os.path.join("docs", "principles.md")
_ARCHITECTURE_REL = os.path.join("docs", "architecture.md")

# The recorded depth values the marker may carry. `full` is the default the intake writes; `light` is the
# recorded opt-out. Anything else (including a typo) is neither — surfaced, then treated as unrecorded.
_VALID_DEPTHS = ("full", "light")

# The required level-2 sections each fuller document needs to be well-formed. Lowercased for comparison,
# matching spec_form._h2_headings. These mirror the section headings the scaffold templates ship
# (.engine/modules/product-design/scaffold/{principles,architecture,diataxis-*}.md) — the one home for the
# concrete shape; if a scaffold heading changes, this set changes with it.
_PRINCIPLES_SECTIONS = ("What this product is for", "Principles", "What these rule out")
_ARCHITECTURE_SECTIONS = ("Overview and context", "The main parts", "How it behaves at runtime", "Key decisions")

# The Diátaxis guide tree: one directory per kind, each guide well-formed for its kind. A reference guide is
# deliberately free-form (any sections), so it needs only at least one section heading, not a fixed set.
_GUIDE_SECTIONS = {
    os.path.join("docs", "tutorials"): ("Before you start", "Steps", "What you did"),
    os.path.join("docs", "how-to"): ("Goal", "Steps", "Check it worked"),
    os.path.join("docs", "explanation"): ("The question this answers", "Background", "Why it works this way"),
    os.path.join("docs", "reference"): None,  # free-form: needs >=1 section, no fixed headings
}

# A mermaid diagram fence: a ``` or ~~~ fence opening a `mermaid` block, tolerating up to three leading spaces
# (a valid CommonMark indent) so a correctly-drawn diagram is never mistaken for a missing one.
_MERMAID_RE = re.compile(r"(?m)^ {0,3}(?:```|~~~)mermaid\b")


def _spec_dir(root: str) -> str:
    return os.path.join(root, spec_form._SPEC_DIR)


def _is_real_file(root: str, rel: str) -> bool:
    """A regular file that is not a symlink — so the engine/product wall is never crossed by following a link
    out of the product's own tree. A symlink at a fuller-document path is treated as not-present."""
    path = os.path.join(root, rel)
    return os.path.isfile(path) and not os.path.islink(path)


def _frontmatter_field(text: str, key: str) -> "str | None":
    """The lowercased value of `key` inside the leading `---` frontmatter block, or None when there is no
    frontmatter or no such key. Tolerant of malformed content — it never raises. (spec_form has a
    status-specific reader; this is the generic one the depth marker needs.)"""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    pattern = re.compile(rf"\s*{re.escape(key)}\s*:\s*(.+?)\s*$")
    for line in lines[1:]:
        if line.strip() == "---":
            break
        m = pattern.match(line)
        if m:
            value = re.sub(r"\s+#.*$", "", m.group(1).strip())  # drop a trailing inline YAML comment
            return value.strip().strip("'\"").lower()
    return None


def _guide_rels(root: str, guide_dir: str) -> list:
    """The repo-root-relative `*.md` files directly under one guide directory (non-recursive), sorted.
    Dotfiles and symlinks skipped (the wall); missing directory → empty."""
    abs_dir = os.path.join(root, guide_dir)
    if not os.path.isdir(abs_dir):
        return []
    out = []
    for name in os.listdir(abs_dir):
        if name.startswith(".") or not name.endswith(".md"):
            continue
        rel = os.path.join(guide_dir, name)
        if _is_real_file(root, rel):
            out.append(rel)
    return sorted(out)


# --------------------------------------------------------------------------------------------------
# Plain-language finding messages. Never a raw check id; never a framework name (arc42/C4/Diátaxis);
# always the "checks structure, not whether the design is right" bound, mirroring product-spec-form.
# --------------------------------------------------------------------------------------------------

_READONLY_TAIL = ("This checks that the deeper design documents are present and well-formed — not whether the "
                  "design is right; that is your call and the review lenses'. It only reads your files.")

_NO_OP_MESSAGE = ("No product description exists yet under `docs/spec/`, so there is no deeper design write-up "
                  "to check — nothing to do here. This check starts working once a description is written. "
                  "It only reads your files; it never changes them.")


def _missing_backbone_hard(rel: str, human: str) -> str:
    return (f"Your description is set to the full write-up, but its {human} document (`{rel}`) is not written. "
            f"Add it, or — if you meant to keep this description light — record that lighter choice so the "
            f"fuller write-up is not expected. " + _READONLY_TAIL)


def _missing_backbone_nudge(rel: str, human: str) -> str:
    return (f"This description has no recorded depth, and its {human} document (`{rel}`) is not written. The "
            f"full write-up is the default now; consider adding it, or recording that you are keeping this "
            f"description light. This is a suggestion, not a blocker. " + _READONLY_TAIL)


def _unrecognized_depth(value: str) -> str:
    return (f"Your description's depth is recorded as `{value}`, which isn't one I recognize — it should be the "
            f"full write-up or the lighter one. Until it reads as one of those, I'm treating this description "
            f"as if its depth weren't recorded. " + _READONLY_TAIL)


def _missing_sections(rel: str, human: str, missing: list) -> str:
    names = ", ".join(f"'{m}'" for m in missing)
    return (f"Your {human} (`{rel}`) is missing the sections {names} it needs to be complete. Add them. "
            + _READONLY_TAIL)


def _missing_diagram(rel: str) -> str:
    return (f"Your architecture overview (`{rel}`) has no diagram. Add the simple diagram that shows the main "
            f"parts and how they connect. " + _READONLY_TAIL)


def _guide_missing_sections(rel: str, missing: list) -> str:
    names = ", ".join(f"'{m}'" for m in missing)
    return (f"Your guide (`{rel}`) is missing the sections {names} a guide of its kind needs. Add them, or move "
            f"the file out of this guide folder if it is not that kind of guide. " + _READONLY_TAIL)


def _guide_no_sections(rel: str) -> str:
    return (f"Your reference guide (`{rel}`) has no sections. A reference needs at least one section so a reader "
            f"can find what they are looking for. " + _READONLY_TAIL)


# --------------------------------------------------------------------------------------------------
# The check.
# --------------------------------------------------------------------------------------------------

def _document_problems(root: str, rel: str, required_sections, need_diagram: bool) -> list:
    """Well-formedness problem messages for one present fuller document. The CALLER assigns severity from the
    recorded depth (hard only in the full write-up; a soft nudge otherwise), so a document written under the
    old "not checked" regime is never retroactively blocked."""
    text = spec_form._read(root, rel)
    headings = spec_form._h2_headings(text)
    human = "guiding principles" if rel == _PRINCIPLES_REL else "architecture overview"
    out = []
    missing = [s for s in required_sections if s.lower() not in headings]
    if missing:
        out.append(_missing_sections(rel, human, missing))
    if need_diagram and not _MERMAID_RE.search(text):
        out.append(_missing_diagram(rel))
    return out


def findings(tier: str, root: "str | None" = None) -> list:
    """The product-design form findings for `root` (defaults to `validate.ROOT`), as finding.v1 dicts.

    `tier` is accepted for interface parity with the sibling form checks; the severities here are fixed by
    meaning and by the recorded depth, not taken from the rule tier: a problem is `hard` only when the
    operator has recorded the full write-up (`spec_depth: full`); in every other state (a recorded light
    opt-out, an unrecorded/brownfield description, or an unrecognized marker) the same problem is a `soft`
    nudge that never blocks. The no-spec case is a disclosed `soft` no-op."""
    root = root or validate.ROOT

    # Disclosed no-op: no product description at all → nothing to have a backbone for.
    if not os.path.isdir(_spec_dir(root)):
        return [validate.disclosed_noop(_NO_OP_MESSAGE, None)]

    index_rel = spec_form._INDEX_REL
    depth = None
    if os.path.isfile(os.path.join(root, index_rel)):
        depth = _frontmatter_field(spec_form._read(root, index_rel), "spec_depth")
    full = depth == "full"

    out = []
    here = {"file": index_rel, "line": None}

    # An unrecognized non-empty marker (a typo) is surfaced as a soft note so it is not silently read as
    # "unrecorded" — a broken marker is a real thing to fix, so it is visible, not dormant.
    if depth is not None and depth not in _VALID_DEPTHS:
        out.append(validate.finding("soft", _unrecognized_depth(depth), here))

    def emit(rel, message):
        # A problem in a document that EXISTS is hard only in the full write-up; otherwise a visible-but-non-
        # blocking soft nudge (brownfield safety — a doc written under the old "not checked" regime is surfaced,
        # never retroactively blocked). Absence-based nudges below stay dormant (disclosed_noop) by contrast.
        loc = {"file": rel, "line": None}
        return validate.finding("hard" if full else "soft", message, loc)

    # (1) The structural backbone — required in full mode, opt-out in light mode, nudged otherwise.
    for rel, required, need_diagram, human in (
        (_PRINCIPLES_REL, _PRINCIPLES_SECTIONS, False, "guiding principles"),
        (_ARCHITECTURE_REL, _ARCHITECTURE_SECTIONS, True, "architecture overview"),
    ):
        if _is_real_file(root, rel):
            for message in _document_problems(root, rel, required, need_diagram):
                out.append(emit(rel, message))
        elif full:
            out.append(validate.finding("hard", _missing_backbone_hard(rel, human), {"file": rel, "line": None}))
        elif depth != "light":  # unrecorded → nudge; "light" is the recorded opt-out → silent
            out.append(validate.disclosed_noop(_missing_backbone_nudge(rel, human), {"file": rel, "line": None}))

    # (2) The user guides — always discretionary: form-checked when present, never flagged when absent.
    for guide_dir, required in _GUIDE_SECTIONS.items():
        for rel in _guide_rels(root, guide_dir):
            text = spec_form._read(root, rel)
            headings = spec_form._h2_headings(text)
            if required is None:  # reference: free-form, needs >=1 section
                if not headings:
                    out.append(emit(rel, _guide_no_sections(rel)))
            else:
                missing = [s for s in required if s.lower() not in headings]
                if missing:
                    out.append(emit(rel, _guide_missing_sections(rel, missing)))

    return out


def emit_findings() -> int:
    """The no-argument path the validator invokes: print the finding.v1 array and return 0. ENGINE_DESIGN_ROOT
    (unset in production) lets the negative-fixture meta-check point the scan at a seeded docs tree, so the
    form gate is witnessed biting a real bad input — its own env var, distinct from spec_form's
    ENGINE_SPEC_ROOT (which resolves to the wrong subtree)."""
    print(json.dumps(findings(os.environ.get("ENGINE_RULE_TIER", "hard"),
                              validate.env_override_path("ENGINE_DESIGN_ROOT"))))
    return 0


def demo() -> int:
    """Prove the inspector on throwaway trees: a full-mode backbone that is present and well-formed passes; a
    full-mode backbone with a missing/malformed document (or a missing diagram) bites HARD; a light-mode
    description with no backbone is a clean pass (the recorded opt-out); an unrecorded description is never
    blocked — a missing OR malformed document is only a soft nudge (brownfield safety); a present guide missing
    its sections bites hard in full mode but only nudges when the depth isn't full; an absent guide is silent;
    an unrecognized marker is surfaced; and no `docs/spec/` at all is a disclosed no-op. RETURNS NON-ZERO if
    any invariant is broken (the falsification can fail). Mutation-free."""
    import shutil
    import tempfile

    def _seed(files: dict) -> str:
        d = tempfile.mkdtemp(prefix="engine-design-form-demo-")
        for rel, body in files.items():
            path = os.path.join(d, rel)
            parent = os.path.dirname(path)
            if parent and not os.path.isdir(parent):
                os.makedirs(parent)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(body)
        return d

    def index(depth):
        fm = f"---\nspec_depth: {depth}\n---\n\n" if depth else ""
        return (fm + "# Product spec\n\n| Capability | Status | Doc |\n| --- | --- | --- |\n"
                "| Checkout | settled | [Checkout](checkout.md) |\n")
    good_principles = ("# Product principles\n\n## What this product is for\nx\n\n## Principles\ny\n\n"
                       "## What these rule out\nz\n")
    good_architecture = ("# Architecture\n\n## Overview and context\nx\n\n## The main parts\n\n"
                         "```mermaid\nflowchart TD\n  A --> B\n```\n\n## How it behaves at runtime\ny\n\n"
                         "## Key decisions\nz\n")
    no_diagram_architecture = ("# Architecture\n\n## Overview and context\nx\n\n## The main parts\nno diagram\n\n"
                              "## How it behaves at runtime\ny\n\n## Key decisions\nz\n")

    def hard(result):
        return sorted(f["severity"] for f in result if not f.get("not_applicable") and f["severity"] == "hard")

    def is_noop(result):
        return len(result) == 1 and result[0].get("not_applicable")

    cases = []  # (label, files, predicate)
    cases.append(("full+well-formed",
                  {"docs/spec/index.md": index("full"), "docs/principles.md": good_principles,
                   "docs/architecture.md": good_architecture}, lambda r: not hard(r)))
    cases.append(("full+missing-backbone",
                  {"docs/spec/index.md": index("full"), "docs/principles.md": good_principles},
                  lambda r: hard(r) == ["hard"]))
    cases.append(("full+no-diagram",
                  {"docs/spec/index.md": index("full"), "docs/principles.md": good_principles,
                   "docs/architecture.md": no_diagram_architecture}, lambda r: hard(r) == ["hard"]))
    cases.append(("light+no-backbone", {"docs/spec/index.md": index("light")}, lambda r: not r or not hard(r)))
    cases.append(("unrecorded+no-backbone", {"docs/spec/index.md": index(None)},
                  lambda r: not hard(r) and any(f.get("not_applicable") for f in r)))
    # brownfield safety: an unrecorded description with a MALFORMED present document is never hard.
    cases.append(("brownfield+malformed-present",
                  {"docs/spec/index.md": index(None), "docs/principles.md": good_principles,
                   "docs/architecture.md": no_diagram_architecture}, lambda r: not hard(r)))
    cases.append(("full+malformed-guide",
                  {"docs/spec/index.md": index("full"), "docs/principles.md": good_principles,
                   "docs/architecture.md": good_architecture,
                   "docs/how-to/deploy.md": "# How to deploy\n\n## Goal\nx\n"}, lambda r: hard(r) == ["hard"]))
    cases.append(("light+malformed-guide",
                  {"docs/spec/index.md": index("light"),
                   "docs/how-to/deploy.md": "# How to deploy\n\n## Goal\nx\n"}, lambda r: not hard(r)))
    cases.append(("unrecognized-marker", {"docs/spec/index.md": index("fll")},
                  lambda r: any("isn't one I recognize" in f["message"] for f in r) and not hard(r)))
    cases.append(("no-spec", {"README.md": "# hi\n"}, is_noop))

    failures = []
    for label, files, ok in cases:
        root_dir = _seed(files)
        try:
            result = findings("hard", root_dir)
        finally:
            shutil.rmtree(root_dir, ignore_errors=True)
        if not ok(result):
            failures.append(f"{label}: invariant broken, got {json.dumps(result)}")

    if failures:
        print("DEMO FAILED — the design-form inspector broke an invariant:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("DEMO PASSED — the design-form inspector blocks (hard) only when the full write-up is recorded, "
          "honors the recorded light opt-out, never retroactively blocks an unrecorded/brownfield description "
          "(soft nudges only), surfaces an unrecognized marker, form-checks present guides while leaving absent "
          "ones silent, and says the no-op plainly when there is no description.")
    return 0


def main(argv: list) -> int:
    if argv and argv[0] == "demo":
        return demo()
    return emit_findings()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
