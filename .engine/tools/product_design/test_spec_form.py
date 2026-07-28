#!/usr/bin/env python3
"""Regression tests for the product-spec form inspector
(.engine/tools/product_design/spec_form.py)."""
from __future__ import annotations
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # .engine/tools on sys.path
from product_design import spec_form  # noqa: E402
import quiet_call  # noqa: E402  (capture a demo walkthrough's stdout so it can't bury the suite summary)
import validate  # noqa: E402


def _doc(status: str, *, sections=True, table=True, discharge="operator") -> str:
    body = f"---\nstatus: {status}\n---\n\n# A capability\n"
    if sections:
        body += "\n## Summary\nWhat and who for.\n\n## Behavior\nHow it behaves.\n\n## Acceptance criteria\n"
        if table:
            body += ("\n| Criterion | How verified | Who checks it |\n"
                     "| --- | --- | --- |\n"
                     f"| It works | a behavioral demo | {discharge} |\n")
    return body


def _index(rows: str) -> str:
    return "# Product spec\n\n| Capability | Status | Doc |\n| --- | --- | --- |\n" + rows


class SpecFormTests(unittest.TestCase):
    def _root(self, files: dict) -> str:
        """A throwaway root seeded with {relpath: body}."""
        d = tempfile.mkdtemp(prefix="engine-spec-form-test-")
        self.addCleanup(shutil.rmtree, d, True)
        for rel, body in files.items():
            path = os.path.join(d, rel)
            parent = os.path.dirname(path)
            if parent and not os.path.isdir(parent):
                os.makedirs(parent)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(body)
        return d

    def _severities(self, fs) -> set:
        return {f["severity"] for f in fs}

    def _snapshot(self, root) -> dict:
        out = {}
        for cur, _dirs, names in os.walk(root):
            for n in names:
                p = os.path.join(cur, n)
                out[os.path.relpath(p, root)] = os.path.getsize(p)
        return out

    # --- clean passes ----------------------------------------------------------------------------
    def test_well_formed_spec_passes_cleanly(self):
        fs = spec_form.findings("hard", root=self._root({
            "docs/spec/index.md": _index("| Checkout | draft | [Checkout](checkout.md) |\n"
                                         "| Search | stub | [Search](search.md) |\n"),
            "docs/spec/checkout.md": _doc("draft"),
            "docs/spec/search.md": "---\nstatus: stub\n---\n\n# Search\n"}))
        self.assertEqual(fs, [])

    def test_all_not_yet_described_spec_passes_cleanly(self):
        fs = spec_form.findings("hard", root=self._root({
            "docs/spec/index.md": _index("| Checkout | stub | [Checkout](checkout.md) |\n"),
            "docs/spec/checkout.md": "---\nstatus: stub\n---\n\n# Checkout\n"}))
        self.assertEqual(fs, [])

    def test_index_may_render_stage_in_plain_language(self):
        # the index ledger accepts either the raw marker or its plain render — coherence still holds
        fs = spec_form.findings("hard", root=self._root({
            "docs/spec/index.md": _index("| Checkout | in progress | [Checkout](checkout.md) |\n"),
            "docs/spec/checkout.md": _doc("draft")}))
        self.assertEqual(fs, [])

    # --- the disclosed no-op ---------------------------------------------------------------------
    def test_no_spec_tree_discloses_the_no_op_soft(self):
        fs = spec_form.findings("hard", root=self._root({"README.md": "hi"}))
        self.assertEqual(len(fs), 1)
        self.assertEqual(fs[0]["severity"], "soft")
        self.assertIsNone(fs[0]["location"])
        self.assertIn("isn't active here yet", fs[0]["message"])

    def test_empty_spec_dir_discloses_the_no_op(self):
        fs = spec_form.findings("hard", root=self._root({"docs/spec/.gitkeep": ""}))
        self.assertEqual(len(fs), 1)
        self.assertEqual(fs[0]["severity"], "soft")
        self.assertIn("isn't active here yet", fs[0]["message"])

    # --- the master index ------------------------------------------------------------------------
    def test_documents_without_an_index_is_hard(self):
        fs = spec_form.findings("hard", root=self._root({"docs/spec/checkout.md": _doc("draft")}))
        self.assertTrue(any(f["severity"] == "hard" and "no master index" in f["message"] for f in fs))

    def test_index_without_a_ledger_table_is_hard(self):
        fs = spec_form.findings("hard", root=self._root({
            "docs/spec/index.md": "# Product spec\n\nNo table here.\n",
            "docs/spec/checkout.md": _doc("draft")}))
        self.assertTrue(any(f["severity"] == "hard" and "capabilities table" in f["message"] for f in fs))

    # --- per-document presence/shape, lifecycle-scaled -------------------------------------------
    def test_drafted_doc_missing_a_section_is_hard_naming_the_section(self):
        fs = spec_form.findings("hard", root=self._root({
            "docs/spec/index.md": _index("| Checkout | draft | [Checkout](checkout.md) |\n"),
            "docs/spec/checkout.md": "---\nstatus: draft\n---\n\n# C\n\n## Summary\nx\n"}))
        self.assertTrue(any(f["severity"] == "hard" and "Behavior" in f["message"] for f in fs))

    def test_drafted_doc_without_criteria_table_is_hard(self):
        fs = spec_form.findings("hard", root=self._root({
            "docs/spec/index.md": _index("| Checkout | draft | [Checkout](checkout.md) |\n"),
            "docs/spec/checkout.md": _doc("draft", table=False)}))
        self.assertTrue(any(f["severity"] == "hard" and "well-formed table" in f["message"] for f in fs))

    def test_bad_who_can_check_value_is_hard(self):
        fs = spec_form.findings("hard", root=self._root({
            "docs/spec/index.md": _index("| Checkout | draft | [Checkout](checkout.md) |\n"),
            "docs/spec/checkout.md": _doc("draft", discharge="nobody")}))
        self.assertTrue(any(f["severity"] == "hard" and "'nobody'" in f["message"] for f in fs))

    def test_unrecognized_stage_marker_is_hard(self):
        fs = spec_form.findings("hard", root=self._root({
            "docs/spec/index.md": _index("| Checkout | draft | [Checkout](checkout.md) |\n"),
            "docs/spec/checkout.md": "---\nstatus: wip\n---\n\n# C\n"}))
        self.assertTrue(any(f["severity"] == "hard" and "recognized stage" in f["message"] for f in fs))

    def test_missing_frontmatter_is_hard(self):
        fs = spec_form.findings("hard", root=self._root({
            "docs/spec/index.md": _index("| Checkout | draft | [Checkout](checkout.md) |\n"),
            "docs/spec/checkout.md": "# C\n\nNo frontmatter at all.\n"}))
        self.assertTrue(any(f["severity"] == "hard" and "recognized stage" in f["message"] for f in fs))

    def test_not_yet_described_slot_needs_only_a_marker(self):
        # a stub doc with NO sections passes (lifecycle-scaled) when listed and coherent
        fs = spec_form.findings("hard", root=self._root({
            "docs/spec/index.md": _index("| Checkout | stub | [Checkout](checkout.md) |\n"),
            "docs/spec/checkout.md": "---\nstatus: stub\n---\n"}))
        self.assertEqual(fs, [])

    def test_bom_prefixed_document_is_accepted(self):
        # a well-formed doc carrying a UTF-8 byte-order mark (common from Windows editors) must not be
        # mis-read as malformed — the deliverable gate caught this false positive
        fs = spec_form.findings("hard", root=self._root({
            "docs/spec/index.md": _index("| Checkout | draft | [Checkout](checkout.md) |\n"),
            "docs/spec/checkout.md": "﻿" + _doc("draft")}))
        self.assertEqual(fs, [])

    def test_status_with_trailing_inline_comment_is_accepted(self):
        fs = spec_form.findings("hard", root=self._root({
            "docs/spec/index.md": _index("| Checkout | draft | [Checkout](checkout.md) |\n"),
            "docs/spec/checkout.md": _doc("draft").replace("status: draft",
                                                           "status: draft  # still being written")}))
        self.assertEqual(fs, [])

    # --- coverage + coherence across the tree ----------------------------------------------------
    def test_document_missing_from_index_is_a_hard_orphan(self):
        fs = spec_form.findings("hard", root=self._root({
            "docs/spec/index.md": _index("| Checkout | draft | [Checkout](checkout.md) |\n"),
            "docs/spec/checkout.md": _doc("draft"),
            "docs/spec/search.md": _doc("draft")}))
        self.assertTrue(any(f["severity"] == "hard" and "isn't listed in the master index" in f["message"]
                            for f in fs))

    def test_index_link_to_a_missing_document_is_hard(self):
        fs = spec_form.findings("hard", root=self._root({
            "docs/spec/index.md": _index("| Checkout | draft | [Checkout](checkout.md) |\n"
                                         "| Ghost | draft | [Ghost](ghost.md) |\n"),
            "docs/spec/checkout.md": _doc("draft")}))
        self.assertTrue(any(f["severity"] == "hard" and "doesn't exist" in f["message"] for f in fs))

    def test_index_document_stage_disagreement_is_hard(self):
        fs = spec_form.findings("hard", root=self._root({
            "docs/spec/index.md": _index("| Checkout | locked | [Checkout](checkout.md) |\n"),
            "docs/spec/checkout.md": _doc("draft")}))
        self.assertTrue(any(f["severity"] == "hard" and "must agree on the stage" in f["message"] for f in fs))

    def test_index_link_with_a_section_anchor_resolves(self):
        # a #fragment on an index link must still resolve to its document (no spurious orphan + dangling)
        fs = spec_form.findings("hard", root=self._root({
            "docs/spec/index.md": _index("| Checkout | draft | [Checkout](checkout.md#summary) |\n"),
            "docs/spec/checkout.md": _doc("draft")}))
        self.assertEqual(fs, [])

    def test_ledger_with_an_extra_trailing_column_is_accepted(self):
        fs = spec_form.findings("hard", root=self._root({
            "docs/spec/index.md": "# Product spec\n\n| Capability | Status | Doc | Notes |\n"
                                  "| --- | --- | --- | --- |\n"
                                  "| Checkout | draft | [Checkout](checkout.md) | later |\n",
            "docs/spec/checkout.md": _doc("draft")}))
        self.assertEqual(fs, [])

    # --- engine/product wall / scope: a spec outside the product's docs/spec/ is never the product's own -----
    def test_engine_walled_spec_is_not_a_product_spec(self):
        fs = spec_form.findings("hard", root=self._root({
            ".engine/docs/spec/index.md": _index("| X | draft | [X](x.md) |\n"),
            ".engine/docs/spec/x.md": _doc("draft")}))
        self.assertEqual(len(fs), 1)
        self.assertEqual(fs[0]["severity"], "soft")
        self.assertIn("isn't active here yet", fs[0]["message"])

    def test_vendored_spec_is_not_a_product_spec(self):
        for elsewhere in ("node_modules/dep", "build", "vendor/pkg"):
            fs = spec_form.findings("hard", root=self._root({
                f"{elsewhere}/docs/spec/index.md": _index("| X | draft | [X](x.md) |\n")}))
            self.assertEqual(len(fs), 1, f"a spec under {elsewhere}/ must not count as the product's")
            self.assertIn("isn't active here yet", fs[0]["message"])

    # --- the operator-communication law: no raw lifecycle token in a finding -------------
    def test_findings_never_leak_a_raw_lifecycle_token(self):
        malformed = [
            {"docs/spec/index.md": _index("| Checkout | locked | [Checkout](checkout.md) |\n"),
             "docs/spec/checkout.md": _doc("draft")},                                  # coherence mismatch
            {"docs/spec/index.md": _index("| Checkout | draft | [Checkout](checkout.md) |\n"),
             "docs/spec/checkout.md": "---\nstatus: draft\n---\n\n# C\n## Summary\nx\n"},  # missing sections
            {"docs/spec/index.md": _index("| Checkout | draft | [Checkout](checkout.md) |\n"),
             "docs/spec/checkout.md": "---\nstatus: wip\n---\n\n# C\n"},               # unrecognized marker
        ]
        for files in malformed:
            fs = spec_form.findings("hard", root=self._root(files))
            self.assertTrue(fs, "expected a finding for a malformed spec")
            for f in fs:
                for token in ("stub", "draft", "locked"):
                    self.assertNotRegex(f["message"], rf"\b{token}\b",
                                        f"a finding leaked the raw lifecycle token '{token}': {f['message']}")

    # --- tier discipline: violations carry the passed tier; the no-op is always soft -------------
    def test_violations_carry_the_passed_tier(self):
        files = {"docs/spec/index.md": _index("| Checkout | draft | [Checkout](checkout.md) |\n"),
                 "docs/spec/checkout.md": _doc("draft", table=False)}
        root = self._root(files)
        self.assertIn("hard", self._severities(spec_form.findings("hard", root=root)))

    def test_no_op_is_soft_even_when_violations_would_be_hard(self):
        fs = spec_form.findings("hard", root=self._root({"README.md": "x"}))
        self.assertNotIn("hard", self._severities(fs))

    # --- read-only: a run never changes the tree -------------------------------------------------
    def test_inspection_is_read_only(self):
        root = self._root({"docs/spec/index.md": _index("| Checkout | draft | [Checkout](checkout.md) |\n"),
                           "docs/spec/checkout.md": _doc("draft", table=False)})
        before = self._snapshot(root)
        spec_form.findings("hard", root=root)
        self.assertEqual(self._snapshot(root), before)

    # --- the real repo can never turn engine-ci red from this check ------------------------------
    def test_real_repo_yields_no_hard_finding(self):
        fs = spec_form.findings("hard")  # defaults to validate.ROOT (engine-template itself, no docs/spec/)
        self.assertNotIn("hard", self._severities(fs))

    # --- the falsifiable demo passes on the happy path -------------------------------------------
    def test_demo_passes(self):
        self.assertEqual(quiet_call.run(spec_form.demo), 0)

    # --- the no-arg dispatch emits a JSON array (the custom/script contract) ----------------------
    def test_emit_findings_prints_a_json_array_and_returns_zero(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = spec_form.emit_findings()
        self.assertEqual(rc, 0)
        parsed = json.loads(buf.getvalue())
        self.assertIsInstance(parsed, list)
        for f in parsed:
            self.assertIn("severity", f)
            self.assertIn("message", f)
            self.assertIn("location", f)

    def test_main_routes_demo_and_bare_invocation(self):
        self.assertEqual(quiet_call.run(spec_form.main, ["demo"]), 0)
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(spec_form.main([]), 0)
        self.assertIsInstance(json.loads(buf.getvalue()), list)


if __name__ == "__main__":
    unittest.main()
