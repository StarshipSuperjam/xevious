"""Network-free corpus guards over the spec's reference citations.

These run in the ordinary test job with no reference checkout: they cannot open
the source (that is ``tools/reference_citations.py --checkout``, run in the
reference-fidelity workflow and by the build session's handover tool), but they
hold the citation *form* consistent so a broken form cannot land silently:

  1. every reference-cited mechanics record names the index pin;
  2. every citation in the spec has a reference file in scope; and
  3. no approximate ("~NNN") line reference survives in a citation.
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import reference_citations as rc  # noqa: E402

SPEC = ROOT / "docs" / "spec"
MECH = ROOT / "docs" / "mechanics"


def _spec_docs():
    for p in sorted(SPEC.glob("*.md")):
        if p.name not in rc.EXCLUDED_NAMES:
            yield p


def _mech_docs():
    for p in sorted(MECH.glob("*.md")):
        if p.name not in rc.EXCLUDED_NAMES:
            yield p


class CorpusGuards(unittest.TestCase):
    def test_every_reference_provenance_line_names_the_index_pin(self):
        pin = rc.index_pin()
        offenders = []
        for p in _mech_docs():
            for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
                m = rc.PROVENANCE_LINE.match(line)
                if not m:
                    continue
                pm = rc.PROVENANCE_PIN.search(m.group("body"))
                if pm and pm.group("pin") != pin:
                    offenders.append(f"{p.name}:{i} cites pin {pm.group('pin')}")
        self.assertEqual(offenders, [], "provenance pins must equal the index pin")

    def test_every_spec_citation_has_a_file_in_scope(self):
        offenders = []
        for p in _spec_docs():
            for cit in rc.scan_spec_document(p):
                if cit.file is None:
                    offenders.append(f"{cit.doc}:{cit.line} `{cit.label}` {cit.start}")
        self.assertEqual(
            offenders, [],
            "each citation needs a reference file: declare the doc default "
            "(`citations are `src/...` unless noted`) or name the file inline",
        )

    def test_no_approximate_line_reference_survives(self):
        # A bare `~NNN` that is a line reference — anywhere, not only next to a
        # label. A line reference is followed by punctuation or end (`~514)`),
        # whereas a count, duration, or distance is followed by a unit *word*
        # (`~56 frames`, `~224 arcade-frame`, `~128 half-px`, `~16 areas`) or is a
        # decimal (`~12.4 s`) — those are left alone. So flag a whole-number `~NNN`
        # only when it is NOT followed by an optional dash/space and a letter or `%`.
        # Whitespace is normalised first so a unit wrapped onto the next line
        # (`~744\nframes`) is still recognised.
        approx = re.compile(r"~(\d{2,5})(?!\d)(?!\.\d)(?!\s*-?\s*[A-Za-z%])")
        offenders = []
        for p in list(_spec_docs()) + list(_mech_docs()):
            text = re.sub(r"\s+", " ", p.read_text(encoding="utf-8"))
            for m in approx.finditer(text):
                offenders.append(f"{p.name}: ~{m.group(1)}")
        self.assertEqual(offenders, [],
                         "cite an exact line or range, never a bare ~approximate line number")


if __name__ == "__main__":
    unittest.main()
