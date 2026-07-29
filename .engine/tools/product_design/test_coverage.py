#!/usr/bin/env python3
"""Tests for the product-spec coverage inspector (.engine/tools/product_design/coverage.py).

Drives the real `coverage.findings()` over crafted throwaway `docs/spec/` trees (mutation-free temp roots), so
the verdict table, the operator-communication leak rule, the rule-tier carry, and the spec_form/build-plan integration are all
exercised against the shipped logic — not a reimplementation. A separate dispatch class confirms the
demo/main/emit contract.
"""
from __future__ import annotations
import contextlib
import io
import json
import os
import re
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # .engine/tools on sys.path
from product_design import coverage  # noqa: E402
from product_design import spec_form  # noqa: E402
import quiet_call  # noqa: E402  (capture a demo walkthrough's stdout so it can't bury the suite summary)


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


class CoverageFindingsTests(unittest.TestCase):
    def _seed(self, files: dict) -> str:
        d = tempfile.mkdtemp(prefix="engine-coverage-test-")
        self.addCleanup(shutil.rmtree, d, True)
        for rel, body in files.items():
            path = os.path.join(d, rel.replace("/", os.sep))
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(body)
        return d

    # --- clean passes -----------------------------------------------------------------------------

    def test_build_order_scheduling_every_settled_capability_is_a_clean_pass(self):
        root = self._seed({
            "docs/spec/index.md": _index("| A | settled | [A](a.md) |\n| B | settled | [B](b.md) |\n"),
            "docs/spec/a.md": _cap("locked"), "docs/spec/b.md": _cap("locked"),
            "docs/spec/build-plan.md": _plan("| 1 | A | [A](a.md) |\n| 2 | B | [B](b.md) |\n"),
        })
        self.assertEqual(coverage.findings("hard", root=root), [])

    def test_a_capability_scheduled_across_two_phases_is_clean(self):
        root = self._seed({
            "docs/spec/index.md": _index("| A | settled | [A](a.md) |\n"),
            "docs/spec/a.md": _cap("locked"),
            "docs/spec/build-plan.md": _plan("| 1 | A | [A](a.md) |\n| 2 | A | [A](a.md) |\n"),
        })
        self.assertEqual(coverage.findings("hard", root=root), [])

    def test_re_sequencing_to_a_later_phase_is_clean(self):
        # The living-build-plan invariant: moving a settled capability to a different phase is free — coverage
        # cares only that it appears somewhere, never which phase.
        root = self._seed({
            "docs/spec/index.md": _index("| A | settled | [A](a.md) |\n"),
            "docs/spec/a.md": _cap("locked"),
            "docs/spec/build-plan.md": _plan("| 3 | A | [A](a.md) |\n"),
        })
        self.assertEqual(coverage.findings("hard", root=root), [])

    def test_a_well_formed_build_order_with_nothing_settled_is_clean(self):
        # The verbatim-scaffold shape: a well-formed build order scheduling only in-progress work, nothing
        # settled yet -> clean (not a no-op, because a build order exists; not hard, because nothing is orphaned).
        root = self._seed({
            "docs/spec/index.md": _index("| A | in progress | [A](a.md) |\n"),
            "docs/spec/a.md": _cap("draft"),
            "docs/spec/build-plan.md": _plan("| 1 | A | [A](a.md) |\n"),
        })
        self.assertEqual(coverage.findings("hard", root=root), [])

    def test_a_settled_capability_in_a_subdirectory_is_covered_when_scheduled(self):
        root = self._seed({
            "docs/spec/index.md": _index("| A | settled | [A](sub/a.md) |\n"),
            "docs/spec/sub/a.md": _cap("locked"),
            "docs/spec/build-plan.md": _plan("| 1 | A | [A](sub/a.md) |\n"),
        })
        self.assertEqual(coverage.findings("hard", root=root), [])

    # --- soft no-op / nudge -----------------------------------------------------------------------

    def test_no_spec_tree_is_a_soft_no_op(self):
        root = self._seed({"README.md": "hi"})
        fs = coverage.findings("hard", root=root)
        self.assertEqual(len(fs), 1)
        self.assertEqual(fs[0]["severity"], "soft")
        self.assertIn("isn't active here yet", fs[0]["message"])

    def test_nothing_settled_and_no_build_order_is_a_soft_no_op(self):
        root = self._seed({"docs/spec/index.md": _index("| A | in progress | [A](a.md) |\n"),
                           "docs/spec/a.md": _cap("draft")})
        fs = coverage.findings("hard", root=root)
        self.assertEqual(len(fs), 1)
        self.assertEqual(fs[0]["severity"], "soft")
        self.assertIn("Nothing is settled yet", fs[0]["message"])

    def test_settled_work_with_no_build_order_is_a_soft_nudge_at_both_tiers(self):
        files = {"docs/spec/index.md": _index("| A | settled | [A](a.md) |\n"), "docs/spec/a.md": _cap("locked")}
        for tier in ("hard", "soft"):
            fs = coverage.findings(tier, root=self._seed(files))
            self.assertEqual(len(fs), 1, tier)
            self.assertEqual(fs[0]["severity"], "soft", f"the no-build-order nudge must stay soft at tier={tier}")
            self.assertIn("haven't started a build order", fs[0]["message"])

    # --- hard teeth -------------------------------------------------------------------------------

    def test_a_settled_capability_missing_from_an_existing_build_order_is_a_hard_self_clearing_orphan(self):
        root = self._seed({
            "docs/spec/index.md": _index("| A | settled | [A](a.md) |\n| B | settled | [B](b.md) |\n"),
            "docs/spec/a.md": _cap("locked"), "docs/spec/b.md": _cap("locked"),
            "docs/spec/build-plan.md": _plan("| 1 | A | [A](a.md) |\n"),
        })
        fs = coverage.findings("hard", root=root)
        orphans = [f for f in fs if f["severity"] == "hard" and "docs/spec/b.md" in f["message"]]
        self.assertEqual(len(orphans), 1, f"the orphaned settled capability must be named: {fs}")
        self.assertIn("To clear this", orphans[0]["message"], "the orphan finding must be self-clearing")
        self.assertNotIn("docs/spec/a.md", "".join(f["message"] for f in fs), "the scheduled cap is not orphaned")

    def test_a_settled_capability_in_a_subdirectory_unscheduled_is_a_hard_orphan(self):
        root = self._seed({
            "docs/spec/index.md": _index("| A | settled | [A](sub/a.md) |\n| B | settled | [B](b.md) |\n"),
            "docs/spec/sub/a.md": _cap("locked"), "docs/spec/b.md": _cap("locked"),
            "docs/spec/build-plan.md": _plan("| 1 | B | [B](b.md) |\n"),
        })
        fs = coverage.findings("hard", root=root)
        self.assertTrue(any(f["severity"] == "hard" and os.path.join("sub", "a.md") in f["message"] for f in fs),
                        f"a settled subdir capability left out of the build order must be a hard orphan: {fs}")

    def test_a_build_order_with_no_phases_is_a_hard_finding(self):
        root = self._seed({
            "docs/spec/index.md": _index("| A | settled | [A](a.md) |\n"), "docs/spec/a.md": _cap("locked"),
            "docs/spec/build-plan.md": "# Build order\n\n(nothing here yet)\n",
        })
        fs = coverage.findings("hard", root=root)
        self.assertTrue(any(f["severity"] == "hard" and "well-formed list of phases" in f["message"] for f in fs))

    def test_a_dangling_build_order_link_is_a_hard_finding(self):
        root = self._seed({
            "docs/spec/index.md": _index("| A | settled | [A](a.md) |\n"), "docs/spec/a.md": _cap("locked"),
            "docs/spec/build-plan.md": _plan("| 1 | A | [A](a.md) |\n| 2 | G | [G](ghost.md) |\n"),
        })
        fs = coverage.findings("hard", root=root)
        self.assertTrue(any(f["severity"] == "hard" and "doesn't exist" in f["message"] for f in fs))

    def test_a_build_order_row_pointing_at_the_index_is_not_a_capability(self):
        root = self._seed({
            "docs/spec/index.md": _index("| A | settled | [A](a.md) |\n"), "docs/spec/a.md": _cap("locked"),
            "docs/spec/build-plan.md": _plan("| 1 | A | [A](a.md) |\n| 1 | I | [I](index.md) |\n"),
        })
        fs = coverage.findings("hard", root=root)
        self.assertTrue(any(f["severity"] == "hard" and "isn't a capability document" in f["message"] for f in fs))

    def test_a_build_order_row_pointing_at_itself_is_not_a_capability(self):
        root = self._seed({
            "docs/spec/index.md": _index("| A | settled | [A](a.md) |\n"), "docs/spec/a.md": _cap("locked"),
            "docs/spec/build-plan.md": _plan("| 1 | A | [A](a.md) |\n| 1 | S | [S](build-plan.md) |\n"),
        })
        fs = coverage.findings("hard", root=root)
        self.assertTrue(any(f["severity"] == "hard" and "isn't a capability document" in f["message"] for f in fs))

    # --- robustness / integration -----------------------------------------------------------------

    def test_a_build_order_with_a_byte_order_mark_is_read_correctly(self):
        root = self._seed({
            "docs/spec/index.md": _index("| A | settled | [A](a.md) |\n"), "docs/spec/a.md": _cap("locked"),
            "docs/spec/build-plan.md": "﻿" + _plan("| 1 | A | [A](a.md) |\n"),
        })
        self.assertEqual(coverage.findings("hard", root=root), [])

    def test_spec_form_does_not_treat_the_build_order_as_a_capability(self):
        # The exact-path exclusion: spec_form must ignore docs/spec/build-plan.md (no "unrecognized stage"
        # finding for its missing frontmatter), so adding a build order never trips the form check.
        root = self._seed({
            "docs/spec/index.md": _index("| A | in progress | [A](a.md) |\n"), "docs/spec/a.md": _cap("draft"),
            "docs/spec/build-plan.md": _plan("| 1 | A | [A](a.md) |\n"),
        })
        self.assertEqual(spec_form.findings("hard", root=root), [],
                         "spec_form must ignore the build-plan doc, not flag it as a malformed capability")

    def test_a_nested_build_plan_named_file_stays_an_ordinary_capability(self):
        # The exclusion is by EXACT top-level path, not basename: a capability the operator happens to name
        # build-plan.md in a subdirectory is still validated/covered as an ordinary capability (no widening).
        root = self._seed({
            "docs/spec/index.md": _index("| A | settled | [A](sub/build-plan.md) |\n"),
            "docs/spec/sub/build-plan.md": _cap("locked"),
            "docs/spec/build-plan.md": _plan("| 1 | A | [A](sub/build-plan.md) |\n"),
        })
        # spec_form validates the nested doc (it is a real capability); coverage requires it be scheduled.
        self.assertEqual(spec_form.findings("hard", root=root), [])
        self.assertEqual(coverage.findings("hard", root=root), [])

    def test_build_order_present_but_not_listed_in_the_index_passes_both_checks(self):
        # The build order is a separate artifact, NOT a capability listed in the index. A clean tree (build
        # order present, not in the index, every settled capability scheduled) passes both checks.
        root = self._seed({
            "docs/spec/index.md": _index("| A | settled | [A](a.md) |\n"), "docs/spec/a.md": _cap("locked"),
            "docs/spec/build-plan.md": _plan("| 1 | A | [A](a.md) |\n"),
        })
        self.assertEqual(spec_form.findings("hard", root=root), [])
        self.assertEqual(coverage.findings("hard", root=root), [])

    def test_tier_is_carried_for_structural_and_orphan_findings(self):
        # A hard case run at the soft tier drops to soft (the tier is not hard-coded).
        root = self._seed({
            "docs/spec/index.md": _index("| A | settled | [A](a.md) |\n"), "docs/spec/a.md": _cap("locked"),
            "docs/spec/build-plan.md": _plan("| 1 | G | [G](ghost.md) |\n"),  # A orphaned + ghost dangling
        })
        fs = coverage.findings("soft", root=root)
        self.assertTrue(fs, "the case must produce findings")
        self.assertTrue(all(f["severity"] == "soft" for f in fs), "structural/orphan findings must carry the tier")

    def test_no_finding_leaks_a_raw_lifecycle_token(self):
        # The operator-communication law across a representative spread of cases.
        cases = [
            {"docs/spec/index.md": _index("| A | settled | [A](a.md) |\n"), "docs/spec/a.md": _cap("locked")},
            {"docs/spec/index.md": _index("| A | settled | [A](a.md) |\n"), "docs/spec/a.md": _cap("locked"),
             "docs/spec/build-plan.md": _plan("| 1 | G | [G](ghost.md) |\n")},
            {"docs/spec/index.md": _index("| A | settled | [A](a.md) |\n| B | settled | [B](b.md) |\n"),
             "docs/spec/a.md": _cap("locked"), "docs/spec/b.md": _cap("locked"),
             "docs/spec/build-plan.md": _plan("| 1 | A | [A](a.md) |\n")},
        ]
        for files in cases:
            for f in coverage.findings("hard", root=self._seed(files)):
                for token in spec_form._VALID_STATUS:
                    self.assertIsNone(re.search(rf"\b{token}\b", f["message"]),
                                      f"finding leaked raw lifecycle token '{token}': {f['message']}")


class DispatchTests(unittest.TestCase):
    def test_demo_passes(self):
        self.assertEqual(quiet_call.run(coverage.demo), 0)

    def test_main_routes_demo(self):
        self.assertEqual(quiet_call.run(coverage.main, ["demo"]), 0)

    def test_emit_findings_prints_a_json_array(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = coverage.emit_findings()
        self.assertEqual(rc, 0)
        self.assertIsInstance(json.loads(buf.getvalue()), list)


if __name__ == "__main__":
    unittest.main()
