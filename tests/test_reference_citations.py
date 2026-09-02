"""Guards for tools/reference_citations.py — the citation resolver.

Offline: a fake checkout with synthetic labels at known lines, and
``reference_extract.EXPECTED_SHA256`` patched to the fakes' digests (shared
module object, plain import, so the patch reaches the ``SourceFile`` the resolver
builds). Each citation shape in the real corpus has a positive test; each hard
failure has a negative test asserting its message.
"""

from __future__ import annotations

import hashlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import reference_extract as rx  # noqa: E402
import reference_citations as rc  # noqa: E402


def _source_text(labels: dict[str, int], nlines: int) -> bytes:
    """A .68k-like file with `name:` at the given 1-based lines, else filler."""
    at = {ln: name for name, ln in labels.items()}
    out = []
    for ln in range(1, nlines + 1):
        out.append(f"{at[ln]}:" if ln in at else "\tnop")
    return ("\n".join(out) + "\n").encode()


class _FakeCheckout:
    """Build the five source files with chosen labels and patch the hash table."""

    def __init__(self, tmp: Path, main_labels, sub_labels):
        self.root = tmp
        files = {
            "src/xevious_main.68k": _source_text(main_labels, 6000),
            "src/xevious_sub.68k": _source_text(sub_labels, 1600),
            "src/xevious_ram.68k": _source_text({"ram_a": 5}, 300),
            "src/map_rom.68k": _source_text({"map_a": 5}, 1000),
            "src/xevious.inc": ("\n".join("X\t.equ\t1" for _ in range(112)) + "\n").encode(),
        }
        self.digests = {}
        for rel, body in files.items():
            p = tmp / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(body)
            self.digests[rel] = hashlib.sha256(body).hexdigest()


def _index_pin() -> str:
    return rc.index_pin()


def _scan(md_text: str, tmp: Path, name: str = "player-craft-and-weapons.md"):
    """Write a spec doc, scan+resolve it against a fake checkout, return results."""
    fc = _FakeCheckout(tmp, MAIN, SUB)
    doc = ROOT / "docs" / "spec" / name  # real path so relative_to(ROOT) works
    # Scan an in-memory doc without touching the repo: write to a temp spec dir.
    specdir = tmp / "docs" / "spec"
    specdir.mkdir(parents=True, exist_ok=True)
    (specdir / name).write_text(md_text, encoding="utf-8")
    with mock.patch.object(rx, "EXPECTED_SHA256", fc.digests), \
         mock.patch.object(rc, "ROOT", tmp):
        sources = rc.load_sources(fc.root)
        extents = {rel: rc.block_extents(s) for rel, s in sources.items()}
        cits = rc.scan_spec_document(specdir / name)
        return [rc.resolve(c, sources, extents) for c in cits]


# main: routine at 1000 (block 1000-1099), an internal sub-cite target;
# a through-pair a=2000/b=2005 (block of a is 2000-2049); a code routine 464-547.
MAIN = {
    "handle_x": 1000, "handle_y": 1100,
    "span_a": 2000, "span_b": 2005, "span_next": 2050,
    "loop_start": 3000, "loop_next": 3100,
    "solo": 4000, "solo_next": 4100,
}
SUB = {"mask_a": 300, "mask_b": 400, "mask_next": 500}


def _ok(results):
    return all(r.ok for r in results) and len(results) >= 1


class PositiveShapes(unittest.TestCase):
    DEFAULT = "citations are `src/xevious_main.68k` unless noted.\n\n"

    def test_label_with_endash_range_definition_in_range(self):
        with tempfile.TemporaryDirectory() as t:
            md = self.DEFAULT + "Text (`handle_x` 1000–1050).\n"
            self.assertTrue(_ok(_scan(md, Path(t))))

    def test_single_line_citation(self):
        with tempfile.TemporaryDirectory() as t:
            md = self.DEFAULT + "Text (`solo` 4000).\n"
            self.assertTrue(_ok(_scan(md, Path(t))))

    def test_through_pair(self):
        with tempfile.TemporaryDirectory() as t:
            md = self.DEFAULT + "Text (`span_a` through `span_b` 2000–2010).\n"
            self.assertTrue(_ok(_scan(md, Path(t))))

    def test_subrange_inside_block(self):
        with tempfile.TemporaryDirectory() as t:
            # 3010–3050 does not contain the def line 3000, but sits in the block.
            md = self.DEFAULT + "Text (`loop_start` 3010–3050).\n"
            self.assertTrue(_ok(_scan(md, Path(t))))

    def test_explicit_file_overrides_default(self):
        with tempfile.TemporaryDirectory() as t:
            md = self.DEFAULT + "Text (`src/xevious_sub.68k` `mask_a` 300–350).\n"
            self.assertTrue(_ok(_scan(md, Path(t))))

    def test_bare_spelling_file_token(self):
        with tempfile.TemporaryDirectory() as t:
            md = self.DEFAULT + "Text (`xevious_sub.68k` `mask_a` 300–350).\n"
            self.assertTrue(_ok(_scan(md, Path(t))))

    def test_sub_default_document(self):
        with tempfile.TemporaryDirectory() as t:
            md = "citations are `src/xevious_sub.68k` unless noted.\n\nText (`mask_a` 300–350).\n"
            self.assertTrue(_ok(_scan(md, Path(t))))


class NegativeShapes(unittest.TestCase):
    DEFAULT = "citations are `src/xevious_main.68k` unless noted.\n\n"

    def _reasons(self, md, t):
        return [r.reason for r in _scan(md, Path(t)) if not r.ok]

    def test_missing_label(self):
        with tempfile.TemporaryDirectory() as t:
            r = self._reasons(self.DEFAULT + "Text (`nope` 1000–1010).\n", t)
            self.assertTrue(any("is not a label" in x for x in r))

    def test_range_outside_the_label(self):
        with tempfile.TemporaryDirectory() as t:
            r = self._reasons(self.DEFAULT + "Text (`handle_x` 1200–1210).\n", t)
            self.assertTrue(any("neither starts at nor sits inside" in x for x in r))

    def test_range_past_end_of_file(self):
        with tempfile.TemporaryDirectory() as t:
            r = self._reasons(self.DEFAULT + "Text (`solo` 4000–99999).\n", t)
            self.assertTrue(any("is outside" in x for x in r))

    def test_approximate_reference(self):
        with tempfile.TemporaryDirectory() as t:
            r = self._reasons(self.DEFAULT + "Text (`handle_x` ~1000).\n", t)
            self.assertTrue(any("approximate" in x for x in r))

    def test_no_file_in_scope(self):
        with tempfile.TemporaryDirectory() as t:
            r = self._reasons("No default here.\n\nText (`handle_x` 1000–1050).\n", t)
            self.assertTrue(any("no reference file in scope" in x for x in r))

    def test_wrong_file_by_bounds(self):
        with tempfile.TemporaryDirectory() as t:
            # mask_a is a sub label; under the main default its range is fine by
            # bounds but the label is absent from main.
            r = self._reasons(self.DEFAULT + "Text (`mask_a` 300–350).\n", t)
            self.assertTrue(any("is not a label in src/xevious_main.68k" in x for x in r))


class MechanicsProvenance(unittest.TestCase):
    def _scan_record(self, body: str, t: Path):
        fc = _FakeCheckout(t, MAIN, SUB)
        mechdir = t / "docs" / "mechanics"
        mechdir.mkdir(parents=True, exist_ok=True)
        (mechdir / "099-x.md").write_text(f"- Reference provenance: {body}\n", encoding="utf-8")
        with mock.patch.object(rx, "EXPECTED_SHA256", fc.digests), \
             mock.patch.object(rc, "ROOT", t):
            sources = rc.load_sources(fc.root)
            extents = {rel: rc.block_extents(s) for rel, s in sources.items()}
            cits = rc.scan_mechanics_record(mechdir / "099-x.md", rc.index_pin())
            return [rc._resolve_special(c, sources, extents) for c in cits]

    def test_good_backticked_citation_resolves(self):
        with tempfile.TemporaryDirectory() as t:
            pin = rc.index_pin()
            body = f"`jotd666/xevious@{pin}`; `src/xevious_sub.68k`: `mask_a` (300-350)."
            self.assertTrue(all(r.ok for r in self._scan_record(body, Path(t))))

    def test_bad_label_is_reported(self):
        with tempfile.TemporaryDirectory() as t:
            pin = rc.index_pin()
            body = f"`jotd666/xevious@{pin}`; `src/xevious_sub.68k`: `mask_zz` (300-350)."
            r = self._scan_record(body, Path(t))
            self.assertTrue(any(not x.ok and "is not a label" in x.reason for x in r))

    def test_wrong_pin_is_reported(self):
        with tempfile.TemporaryDirectory() as t:
            body = f"`jotd666/xevious@{'0'*40}`; `src/xevious_sub.68k`: `mask_a` (300-350)."
            r = self._scan_record(body, Path(t))
            self.assertTrue(any(not x.ok and "does not match the index pin" in x.reason for x in r))

    def test_coordinate_range_is_not_a_citation(self):
        with tempfile.TemporaryDirectory() as t:
            pin = rc.index_pin()
            body = f"`jotd666/xevious@{pin}`; the craft-clamp bounds (X 144-304 / Y 16-224)."
            self.assertEqual(self._scan_record(body, Path(t)), [])

    def test_non_reference_record_is_exempt(self):
        with tempfile.TemporaryDirectory() as t:
            body = "the historical `.sb3` at sha256 abcd; no arcade reference used."
            self.assertEqual(self._scan_record(body, Path(t)), [])


if __name__ == "__main__":
    unittest.main()
