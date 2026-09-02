#!/usr/bin/env python3
"""Resolve every reference citation in the spec against the pinned source.

A citation names a place in the pinned ``jotd666/xevious`` source — a file, a
label, and a line or line range. This tool reads each one in ``docs/spec/*.md``
and ``docs/mechanics/*.md`` and checks it actually points there at the pin: the
file is one of the five sources, the label exists, and the cited range starts at
or sits inside that label's block. A stale, invented, or approximate citation is
a failure, so the spec cannot quietly claim a source it does not have.

It needs a verified checkout (see ``tools/reference_checkout.py``); hashing and
the label index come from ``reference_extract.SourceFile``.

Canonical citation grammar (what this tool checks, and what new prose should use):

    `label` 3289–3321                     label + range, file from the doc default
    `label` 3338                          single line
    `label_a` through `label_b` 375–419   a span from one label to another
    `src/xevious_sub.68k` `label` 300–311 explicit file (before the label)

A spec document that uses the short (file-less) form declares its default once in
its intro:  ``citations are `src/xevious_main.68k` unless noted``.  Within a
parenthetical group an explicit file token overrides the default for the
citations to its right. The include file ``src/xevious.inc`` holds only ``.equ``
constants (no labels), so it is cited file+range, never by label.

Mechanics records are scanned only on their ``- Reference provenance:`` line, and
only when it names ``jotd666/xevious@<pin>``; there a line range must carry a
label, and the pin must equal the index's.

Usage:
    python tools/reference_citations.py --checkout PATH [--paths docs/spec docs/mechanics]
                                        [--report] [--suggest-labels]
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import reference_extract as rx  # noqa: E402  (module object: SourceFile + hashes)

ROOT = Path(__file__).resolve().parent.parent
INDEX = ROOT / "docs" / "spec" / "index.md"

# Files excluded from spec-doc scanning (data, the index's own recipe/table, the
# build order, the mechanics template).
EXCLUDED_NAMES = {"index.md", "build-plan.md", "README.md"}

_FILE_STEMS = ("xevious_main", "xevious_sub", "xevious_ram", "map_rom", "xevious")
FILE_TOKEN = re.compile(
    r"`(?:src/)?(" + "|".join(_FILE_STEMS) + r")\.(68k|inc)`"
)

# A labelled citation: a backticked label, an optional `through` second label,
# then a line or line range. The lookbehind `(?<![\w.-])` before the start number
# keeps a date tail (2026-08-09) or a decimal from reading as a line; only
# whitespace / comma / a single opening paren may sit between the label and the
# number, so `` `flag` … one ~20-frame `` never matches.
LABEL_CITATION = re.compile(
    r"`(?P<label>[A-Za-z_][A-Za-z0-9_]*)`"
    r"(?:\s+through\s+`(?P<label2>[A-Za-z_][A-Za-z0-9_]*)`)?"
    r"[,\s]*\(?"
    r"(?P<approx>~)?(?<![\w.-])(?P<start>\d{2,5})"
    r"(?:\s*[–—-]\s*(?P<end>\d{2,5}))?(?!\d)"
)

DEFAULT_FILE = re.compile(
    r"citations\s+are\s+`((?:src/)?(?:" + "|".join(_FILE_STEMS) + r")\.(?:68k|inc))`\s+unless\s+noted",
    re.IGNORECASE,
)

PROVENANCE_LINE = re.compile(r"^-\s+Reference provenance:\s*(?P<body>.*)$")
PROVENANCE_PIN = re.compile(r"`?jotd666/xevious@(?P<pin>[0-9a-f]{40})`?")
# A provenance file token, backticked (optionally trailed by a colon).
PROV_FILE = re.compile(r"`(?:src/)?(?:" + "|".join(_FILE_STEMS) + r")\.(?:68k|inc)`")
# A provenance citation: a backticked label (optional `through` second label)
# whose range follows in parentheses. Prose numbers (coordinate bounds like
# `X 144-304`, supplementary line refs) carry no adjacent backticked label and
# are deliberately not treated as citations.
PROV_CITATION = re.compile(
    r"`(?P<label>[A-Za-z_][A-Za-z0-9_]*)`"
    r"(?:\s+through\s+`(?P<label2>[A-Za-z_][A-Za-z0-9_]*)`)?"
    r"\s*\((?P<start>\d{2,5})(?:\s*[–—-]\s*(?P<end>\d{2,5}))?"
)


@dataclass(frozen=True)
class Citation:
    doc: str
    line: int
    file: str | None
    label: str | None
    label2: str | None
    start: int
    end: int
    approx: bool
    raw: str


@dataclass(frozen=True)
class Result:
    citation: Citation
    ok: bool
    reason: str | None


def index_pin() -> str:
    text = INDEX.read_text(encoding="utf-8")
    m = re.search(r"^reference_pin:\s*([0-9a-f]{40})", text, re.MULTILINE)
    if not m:
        raise RuntimeError("docs/spec/index.md has no reference_pin in frontmatter")
    return m.group(1)


def normalise_file(token: str) -> str | None:
    """`xevious_sub.68k` or `src/xevious_sub.68k` -> the EXPECTED_SHA256 key, else None."""
    stem = token.strip("`")
    if not stem.startswith("src/"):
        stem = "src/" + stem
    return stem if stem in rx.EXPECTED_SHA256 else None


def load_sources(checkout: Path) -> dict[str, "rx.SourceFile"]:
    """One SourceFile per pinned file (hash-verified on construction)."""
    return {rel: rx.SourceFile(checkout, rel) for rel in rx.EXPECTED_SHA256}


def block_extents(src: "rx.SourceFile") -> dict[str, tuple[int, int]]:
    """label -> (definition line, last line of its block), both 1-based.

    The block runs from the definition line to the line before the next label
    (or end of file). Computed from the label index, never from
    ``bytes_under_label`` (which returns the last .byte line and so collapses to
    the definition line for a code routine that has no data bytes).
    """
    ordered = sorted(src.labels.items(), key=lambda kv: kv[1])  # (label, 0-based idx)
    extents: dict[str, tuple[int, int]] = {}
    for i, (label, idx) in enumerate(ordered):
        start = idx + 1
        end = ordered[i + 1][1] if i + 1 < len(ordered) else len(src.lines)
        extents[label] = (start, end)
    return extents


def resolve(cit: Citation, sources, extents) -> Result:
    if cit.approx:
        return Result(cit, False, "approximate line reference (~); a citation must be exact")
    if cit.file is None:
        return Result(cit, False, "no reference file in scope (declare the doc default or name the file)")
    src = sources.get(cit.file)
    if src is None:
        return Result(cit, False, f"{cit.file} is not one of the pinned source files")
    nlines = len(src.lines)
    if not (1 <= cit.start <= cit.end <= nlines):
        return Result(cit, False, f"range {cit.start}-{cit.end} is outside {cit.file} (which has {nlines} lines)")
    if cit.label is None:
        return Result(cit, True, None)  # file+range, bounds-only (e.g. the .inc file)
    ext = extents[cit.file]
    if cit.label not in ext:
        # Help the reader: is it defined in another file?
        elsewhere = [f for f, e in extents.items() if cit.label in e]
        hint = f" (it is defined in {elsewhere[0]})" if elsewhere else ""
        return Result(cit, False, f"`{cit.label}` is not a label in {cit.file}{hint}")
    defline, blockend = ext[cit.label]
    in_range = cit.start <= defline <= cit.end
    in_block = defline <= cit.start and cit.end <= blockend
    if not (in_range or in_block):
        return Result(cit, False,
                      f"range {cit.start}-{cit.end} neither starts at nor sits inside "
                      f"`{cit.label}` (defined {defline}, block {defline}-{blockend})")
    if cit.label2 is not None:
        if cit.label2 not in ext:
            return Result(cit, False, f"`{cit.label2}` is not a label in {cit.file}")
        d2 = ext[cit.label2][0]
        if not (cit.start <= d2 <= cit.end):
            return Result(cit, False,
                          f"`{cit.label2}` (defined {d2}) is not inside the cited range "
                          f"{cit.start}-{cit.end}")
    return Result(cit, True, None)


def _doc_default(text: str) -> str | None:
    m = DEFAULT_FILE.search(re.sub(r"\s+", " ", text))
    return normalise_file(m.group(1)) if m else None


def _paragraphs(text: str):
    """Yield (paragraph_text, starting_line_number)."""
    line_no = 1
    for para in re.split(r"\n\s*\n", text):
        yield para, line_no
        line_no += para.count("\n") + 2


def scan_spec_document(path: Path) -> list[Citation]:
    text = path.read_text(encoding="utf-8")
    default = _doc_default(text)
    rel = str(path.relative_to(ROOT))
    cits: list[Citation] = []
    # Skip the intro's own grammar sentence lines and any acceptance-criteria
    # table are still scanned for citations — a citation anywhere must resolve.
    for para, base_line in _paragraphs(text):
        # Split the paragraph into parenthetical groups and the text between
        # them, so a file token binds only within its own group.
        pos = 0
        for m in re.finditer(r"\(([^()]*)\)", para):
            _scan_segment(para[pos:m.start()], default, rel, para, base_line, cits, path)
            _scan_segment(m.group(1), default, rel, para, base_line, cits, path)
            pos = m.end()
        _scan_segment(para[pos:], default, rel, para, base_line, cits, path)
    return cits


def _line_of(para: str, offset: int, base_line: int) -> int:
    return base_line + para[:offset].count("\n")


def _scan_segment(segment: str, default: str | None, rel: str, para: str,
                  base_line: int, cits: list[Citation], path: Path) -> None:
    """Scan one group/segment: file tokens (sticky within the segment) then citations."""
    # Interleave file tokens and citations in document order.
    events = []
    for m in FILE_TOKEN.finditer(segment):
        events.append((m.start(), "file", m.group(0)))
    for m in LABEL_CITATION.finditer(segment):
        events.append((m.start(), "cite", m))
    events.sort(key=lambda e: e[0])
    current = default
    seg_offset = para.find(segment) if segment else 0
    for offset, kind, payload in events:
        if kind == "file":
            current = normalise_file(payload)
        else:
            m = payload
            start = int(m.group("start"))
            end = int(m.group("end")) if m.group("end") else start
            abs_line = _line_of(para, seg_offset + offset, base_line)
            cits.append(Citation(
                doc=rel, line=abs_line, file=current,
                label=m.group("label"), label2=m.group("label2"),
                start=start, end=end, approx=bool(m.group("approx")),
                raw=m.group(0).strip(),
            ))


def scan_mechanics_record(path: Path, pin: str) -> list[Citation]:
    rel = str(path.relative_to(ROOT))
    cits: list[Citation] = []
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        pm = PROVENANCE_LINE.match(line)
        if not pm:
            continue
        body = pm.group("body")
        pinm = PROVENANCE_PIN.search(body)
        if not pinm:
            continue  # non-reference provenance (sb3 hash, spriters URL): exempt
        if pinm.group("pin") != pin:
            cits.append(Citation(rel, i, None, None, None, -1, -1, False,
                                 f"PIN:{pinm.group('pin')}"))
        current = None  # a provenance line names its file(s) explicitly
        events = []
        for m in PROV_FILE.finditer(body):
            events.append((m.start(), "file", m.group(0)))
        for m in PROV_CITATION.finditer(body):
            events.append((m.start(), "cite", m))
        events.sort(key=lambda e: e[0])
        for _off, kind, payload in events:
            if kind == "file":
                current = normalise_file(payload)
            else:
                m = payload
                start = int(m.group("start"))
                end = int(m.group("end")) if m.group("end") else start
                cits.append(Citation(
                    rel, i, current, m.group("label"), m.group("label2"),
                    start, end, False, m.group(0).strip(),
                ))
    return cits


def check(checkout: Path, paths: list[Path]) -> tuple[list[Result], list[Citation]]:
    sources = load_sources(checkout)
    extents = {rel: block_extents(src) for rel, src in sources.items()}
    pin = index_pin()
    results: list[Result] = []
    for base in paths:
        for path in sorted(base.rglob("*.md")):
            if base.name == "spec" and "data" in path.parts:
                continue
            if path.name in EXCLUDED_NAMES:
                continue
            if "mechanics" in path.parts:
                cits = scan_mechanics_record(path, pin)
            else:
                cits = scan_spec_document(path)
            for cit in cits:
                results.append(_resolve_special(cit, sources, extents))
    unresolved = [r.citation for r in results if not r.ok]
    return results, unresolved


def _resolve_special(cit: Citation, sources, extents) -> Result:
    if cit.start == -1 and cit.raw.startswith("PIN:"):
        return Result(cit, False,
                      f"provenance pin {cit.raw[4:]} does not match the index pin")
    return resolve(cit, sources, extents)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkout", required=True, type=Path)
    parser.add_argument("--paths", nargs="+", type=Path,
                        default=[ROOT / "docs" / "spec", ROOT / "docs" / "mechanics"])
    parser.add_argument("--report", action="store_true")
    parser.add_argument("--suggest-labels", action="store_true")
    args = parser.parse_args(argv)

    try:
        results, unresolved = check(args.checkout, args.paths)
    except rx.ExtractionError as exc:
        print(f"error: the checkout is not the pinned reference: {exc}", file=sys.stderr)
        return 1
    except (FileNotFoundError, NotADirectoryError) as exc:
        print(f"error: cannot read the checkout: {exc}", file=sys.stderr)
        return 1

    for r in results:
        if r.ok and args.report:
            print(f"OK   {r.citation.doc}:{r.citation.line}  {r.citation.raw}")
        if not r.ok:
            print(f"{r.citation.doc}:{r.citation.line}: {r.reason} [{r.citation.raw}]",
                  file=sys.stderr)

    if args.suggest_labels:
        _suggest(args.checkout, args.paths)

    print(f"{len(results)} citations checked, {len(unresolved)} unresolved",
          file=sys.stderr)
    return 1 if unresolved else 0


def _suggest(checkout: Path, paths: list[Path]) -> None:
    """Advisory: bare ranges in spec prose whose start equals a label definition."""
    sources = load_sources(checkout)
    def_by_line = {}
    for rel, src in sources.items():
        for label, idx in src.labels.items():
            def_by_line.setdefault((rel, idx + 1), label)
    for base in paths:
        if base.name != "spec":
            continue
        for path in sorted(base.rglob("*.md")):
            if path.name in EXCLUDED_NAMES or "data" in path.parts:
                continue
            default = _doc_default(path.read_text(encoding="utf-8"))
            if default is None:
                continue
            for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                for m in re.finditer(r"(?<![\w`.-])(\d{2,5})\s*[–—-]\s*(\d{2,5})", line):
                    if LABEL_CITATION.search(line[:m.start()][-40:] + line[m.start():m.end()]):
                        continue
                    lab = def_by_line.get((default, int(m.group(1))))
                    if lab:
                        print(f"suggest {path.relative_to(ROOT)}:{i}  "
                              f"{m.group(0)} -> `{lab}` {m.group(0)}")


if __name__ == "__main__":
    raise SystemExit(main())
