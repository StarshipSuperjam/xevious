#!/usr/bin/env python3
"""Tests for the decision-record form inspector (.engine/tools/product_design/adr_form.py).

Covers the load-bearing claims: only the engine's own records (frontmatter-marked) are checked and a
foreign-style record is left untouched (the engine/product wall); a record missing or emptying its
`## What we ruled out` section bites at hard; the disclosed no-op fires whenever no engine record is present
(absent tree, empty tree, or only foreign records) and never silently passes; a non-record file under
`docs/adr/` is not gated; a finding's prose never leaks a raw framework token; the `.engine/` tree is walled
out; and the ENGINE_ADR_ROOT redirect the negative-fixture meta-check relies on works. Every case runs against
a throwaway temp root, so the real working tree is never touched.
"""
from __future__ import annotations
import io
import json
import os
import re
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # .engine/tools on sys.path
from product_design import adr_form  # noqa: E402
import validate  # noqa: E402

_ADR = os.path.join("docs", "adr")

_MARK = "---\nstatus: accepted\nengine_record: true\n---\n\n"
_GOOD = (_MARK + "# Pick a datastore\n\n## The decision\n\nUse Postgres.\n\n"
         "## Why\n\nRelational fit.\n\n## What we ruled out\n\n- **A document store.** Weak joins.\n")
_NO_SECTION = (_MARK + "# Pick a datastore\n\n## The decision\n\nUse Postgres.\n\n## Why\n\nRelational fit.\n")
_EMPTY_SECTION = (_MARK + "# Pick a datastore\n\n## The decision\n\nUse Postgres.\n\n"
                  "## What we ruled out\n\n<!-- nothing yet -->\n")
# A record kept in the classic public style: status is a `## Status` section, no frontmatter — not the engine's.
_FOREIGN = ("# 1. Record architecture decisions\n\n## Status\n\nAccepted\n\n## Context\n\nWe need records.\n\n"
            "## Decision\n\nKeep them.\n")
# A record in the MADR style: frontmatter WITH a `status:` key but no engine marker — a dominant public
# convention that must NOT be mistaken for the engine's own (the wall). Missing the ruled-out section.
_MADR = ("---\nstatus: accepted\n---\n\n# Pick a datastore\n\n## Context and Problem Statement\n\nNeed a store.\n\n"
         "## Considered Options\n\n- Postgres\n- A document store\n\n## Decision Outcome\n\nPostgres.\n")


class AdrFormTests(unittest.TestCase):
    def _root(self, files: dict) -> str:
        d = tempfile.mkdtemp(prefix="engine-adr-test-")
        self.addCleanup(shutil.rmtree, d, True)
        for rel, body in files.items():
            path = os.path.join(d, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(body)
        return d

    def _hard(self, fs):
        return [f for f in fs if f["severity"] == "hard"]

    def test_wellformed_engine_record_passes_cleanly(self):
        fs = adr_form.findings("hard", root=self._root({os.path.join(_ADR, "0001-datastore.md"): _GOOD}))
        self.assertEqual(fs, [], f"a well-formed engine record must pass with no findings: {fs}")

    def test_no_adr_tree_is_a_disclosed_noop(self):
        fs = adr_form.findings("hard", root=self._root({os.path.join("docs", "readme.md"): "hi\n"}))
        self.assertEqual(len(fs), 1)
        self.assertTrue(fs[0].get("not_applicable"), "no docs/adr tree must be a disclosed no-op, not silent")
        self.assertEqual(fs[0]["severity"], "soft")

    def test_only_foreign_records_is_a_disclosed_noop(self):
        # Records with no engine marker are not the engine's to check — a Nygard record (no frontmatter) AND a
        # MADR record (frontmatter status: but no marker). A tree of only those reads as "no engine records
        # yet" — the disclosed no-op, never a hard finding (the engine/product wall).
        fs = adr_form.findings("hard", root=self._root({
            os.path.join(_ADR, "0001-nygard.md"): _FOREIGN,
            os.path.join(_ADR, "0002-madr.md"): _MADR,
        }))
        self.assertEqual(len(fs), 1)
        self.assertTrue(fs[0].get("not_applicable"))

    def test_madr_record_with_frontmatter_status_is_never_gated(self):
        # The wall's load-bearing case: MADR uses frontmatter with a `status:` key and is a dominant public
        # convention. It carries no engine marker, so it must be left untouched even though it has frontmatter
        # and is missing the ruled-out section. (Provenance is the marker, never merely 'has frontmatter'.)
        root = self._root({
            os.path.join(_ADR, "0001-datastore.md"): _GOOD,   # a real engine record, checked
            os.path.join(_ADR, "0002-madr.md"): _MADR,        # foreign despite its frontmatter status
        })
        self.assertEqual(adr_form.findings("hard", root=root), [],
                         "a MADR record (frontmatter status, no engine marker) must be left untouched")

    def test_foreign_record_missing_section_is_never_gated(self):
        # Even alongside a good engine record, a Nygard record missing the section draws no finding.
        root = self._root({
            os.path.join(_ADR, "0001-datastore.md"): _GOOD,
            os.path.join(_ADR, "0002-foreign.md"): _FOREIGN,
        })
        self.assertEqual(adr_form.findings("hard", root=root), [],
                         "a foreign record must be left untouched even when it lacks the section")

    def test_missing_section_bites_hard_and_names_the_record(self):
        root = self._root({os.path.join(_ADR, "0001-datastore.md"): _NO_SECTION})
        hard = self._hard(adr_form.findings("hard", root=root))
        self.assertEqual(len(hard), 1)
        self.assertIn("0001-datastore.md", hard[0]["message"])
        self.assertIn("What we ruled out", hard[0]["message"])

    def test_empty_section_bites_hard(self):
        root = self._root({os.path.join(_ADR, "0001-datastore.md"): _EMPTY_SECTION})
        hard = self._hard(adr_form.findings("hard", root=root))
        self.assertEqual(len(hard), 1, "a present-but-empty section must bite")

    def test_non_record_file_is_not_gated(self):
        # A README/index under docs/adr/ doesn't match the NNNN-*.md record pattern, so it is never forced to
        # carry the section; with no real records present this is the disclosed no-op.
        root = self._root({os.path.join(_ADR, "README.md"): "# Decision records\n\nAn index.\n"})
        fs = adr_form.findings("hard", root=root)
        self.assertEqual(len(fs), 1)
        self.assertTrue(fs[0].get("not_applicable"))

    def test_findings_never_leak_a_raw_framework_token_in_prose(self):
        # Code spans (the `docs/adr/…` path, the plain `## What we ruled out` heading) are legitimate; the
        # acronym must never surface as operator prose.
        for body in (_NO_SECTION, _EMPTY_SECTION):
            root = self._root({os.path.join(_ADR, "0001-x.md"): body})
            for f in adr_form.findings("hard", root=root):
                prose = re.sub(r"`[^`]*`", "", f["message"]).lower()
                self.assertNotIn("anti-choice", prose)
                self.assertIsNone(re.search(r"\badr\b", prose),
                                  f"a finding leaked a raw framework token: {f['message']}")

    def test_engine_tree_is_walled_out(self):
        # A record under .engine/ is never the product's own: the scan is rooted at <root>/docs/adr, so a root
        # holding only .engine/docs/adr sees no product records — a disclosed no-op, never a finding. A future
        # refactor that widened the scan to .engine/ would break this.
        root = self._root({os.path.join(".engine", "docs", "adr", "0001-x.md"): _NO_SECTION})
        fs = adr_form.findings("hard", root=root)
        self.assertEqual(len(fs), 1)
        self.assertTrue(fs[0].get("not_applicable"), "a record under .engine/ must be walled out")

    def test_unreadable_record_is_a_soft_note_not_a_crash(self):
        # An odd-but-legitimate tree (a record-named broken symlink) must not crash the scan into an opaque
        # fail-closed; it yields a soft "couldn't read" note, honoring the print-array/exit-0 contract.
        d = tempfile.mkdtemp(prefix="engine-adr-test-")
        self.addCleanup(shutil.rmtree, d, True)
        adr = os.path.join(d, _ADR)
        os.makedirs(adr)
        try:
            os.symlink(os.path.join(d, "does-not-exist"), os.path.join(adr, "0001-broken.md"))
        except (OSError, NotImplementedError):
            self.skipTest("symlinks not supported here")
        fs = adr_form.findings("hard", root=d)
        self.assertEqual(len(fs), 1)
        self.assertEqual(fs[0]["severity"], "soft", "an unreadable record must be a soft note, never a crash")
        self.assertIn("0001-broken.md", fs[0]["message"])

    def test_read_only_never_writes(self):
        root = self._root({os.path.join(_ADR, "0001-datastore.md"): _NO_SECTION})
        before = {p: os.stat(os.path.join(dp, p)).st_mtime
                  for dp, _dn, fns in os.walk(root) for p in fns}
        adr_form.findings("hard", root=root)
        after = {p: os.stat(os.path.join(dp, p)).st_mtime
                 for dp, _dn, fns in os.walk(root) for p in fns}
        self.assertEqual(before, after, "the check must never write to the tree it inspects")

    def test_emit_honors_ENGINE_ADR_ROOT_redirect(self):
        # The seam the negative-fixture meta-check uses: point ENGINE_ADR_ROOT at a seeded tree and confirm the
        # no-arg emit path scans it (prints the finding.v1 array to stdout, exits 0).
        root = self._root({os.path.join(_ADR, "0001-datastore.md"): _NO_SECTION})
        old = os.environ.get("ENGINE_ADR_ROOT")
        os.environ["ENGINE_ADR_ROOT"] = root
        try:
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = adr_form.emit_findings()
        finally:
            if old is None:
                os.environ.pop("ENGINE_ADR_ROOT", None)
            else:
                os.environ["ENGINE_ADR_ROOT"] = old
        self.assertEqual(rc, 0)
        emitted = json.loads(buf.getvalue())
        self.assertTrue(any(f["severity"] == "hard" and "0001-datastore.md" in f["message"] for f in emitted),
                        f"the redirected emit must report the seeded bad record: {emitted}")

    def test_demo_self_check_passes(self):
        # The in-tool falsification must hold (returns 0) — captured so its walkthrough print doesn't bury output.
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = adr_form.demo()
        self.assertEqual(rc, 0, f"the adr_form demo must pass: {buf.getvalue()}")


if __name__ == "__main__":
    unittest.main()
