#!/usr/bin/env python3
"""Tests for the product-design form inspector (.engine/tools/product_design/design_form.py).

Drives the real `design_form.findings()` over crafted throwaway `docs/` trees (mutation-free temp roots), so
the depth-marker teeth (full requires the backbone, light opts out, unrecorded only nudges), the
well-formedness checks (sections + the architecture diagram), the discretionary-guide handling, the
disclosed no-op, and the operator-communication bound are all exercised against the shipped logic — not a
reimplementation. A separate dispatch class confirms the demo/main/emit contract.
"""
from __future__ import annotations
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # .engine/tools on sys.path
from product_design import design_form  # noqa: E402


def _index(depth: "str | None") -> str:
    fm = f"---\nspec_depth: {depth}\n---\n\n" if depth else ""
    return (fm + "# Product spec\n\n| Capability | Status | Doc |\n| --- | --- | --- |\n"
            "| Checkout | settled | [Checkout](checkout.md) |\n")


_GOOD_PRINCIPLES = ("# Product principles\n\n## What this product is for\nx\n\n## Principles\ny\n\n"
                    "## What these rule out\nz\n")
_GOOD_ARCHITECTURE = ("# Architecture\n\n## Overview and context\nx\n\n## The main parts\n\n"
                      "```mermaid\nflowchart TD\n  A --> B\n```\n\n## How it behaves at runtime\ny\n\n"
                      "## Key decisions\nz\n")


class DesignFormFindingsTests(unittest.TestCase):
    def _seed(self, files: dict) -> str:
        d = tempfile.mkdtemp(prefix="engine-design-form-test-")
        self.addCleanup(shutil.rmtree, d, True)
        for rel, body in files.items():
            path = os.path.join(d, rel)
            parent = os.path.dirname(path)
            if parent and not os.path.isdir(parent):
                os.makedirs(parent)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(body)
        return d

    def _actionable(self, result):
        return [f for f in result if not f.get("not_applicable")]

    def _hard(self, result):
        return [f for f in result if f["severity"] == "hard"]

    def _soft_visible(self, result):
        # soft findings that are NOT disclosed no-ops (dormant) — the visible, actionable nudges.
        return [f for f in result if f["severity"] == "soft" and not f.get("not_applicable")]

    # ---- the depth-marker teeth ------------------------------------------------------------------

    def test_full_with_wellformed_backbone_is_clean(self):
        root = self._seed({"docs/spec/index.md": _index("full"), "docs/principles.md": _GOOD_PRINCIPLES,
                           "docs/architecture.md": _GOOD_ARCHITECTURE})
        self.assertEqual(design_form.findings("hard", root), [], "a full, well-formed backbone passes clean")

    def test_full_with_missing_backbone_document_is_hard(self):
        root = self._seed({"docs/spec/index.md": _index("full"), "docs/principles.md": _GOOD_PRINCIPLES})
        result = design_form.findings("hard", root)
        actionable = self._actionable(result)
        self.assertEqual([f["severity"] for f in actionable], ["hard"],
                         "a missing architecture overview in full mode blocks")
        self.assertIn("docs/architecture.md", actionable[0]["message"])

    def test_light_with_no_backbone_is_a_clean_opt_out(self):
        root = self._seed({"docs/spec/index.md": _index("light")})
        self.assertEqual(self._actionable(design_form.findings("hard", root)), [],
                         "the recorded light opt-out means no backbone is expected")

    def test_unrecorded_depth_with_no_backbone_is_a_soft_nudge_never_hard(self):
        root = self._seed({"docs/spec/index.md": _index(None)})
        result = design_form.findings("hard", root)
        self.assertEqual(self._actionable(result), [], "an unrecorded description is never blocked")
        nudges = [f for f in result if f.get("not_applicable")]
        self.assertTrue(nudges, "an unrecorded description with no backbone is nudged")
        self.assertTrue(all(f["severity"] == "soft" for f in nudges))

    def test_unrecognized_depth_marker_is_surfaced_as_soft(self):
        # a typo (e.g. `fll` for `full`) must not silently drop the teeth: it is surfaced as a soft note and
        # the description is treated as unrecorded, never hard-blocked.
        root = self._seed({"docs/spec/index.md": _index("fll")})
        result = design_form.findings("hard", root)
        self.assertEqual(self._hard(result), [], "an unrecognized marker never hard-blocks")
        self.assertTrue(any("isn't one I recognize" in f["message"] for f in self._soft_visible(result)),
                        "the unrecognized marker is surfaced plainly")

    def test_mermaid_diagram_accepts_indented_and_tilde_fences(self):
        # a correctly-drawn diagram in a valid CommonMark variant (up to 3-space indent, or a ~~~ fence) must
        # not be mistaken for a missing one and hard-block the merge.
        for fence in ("   ```mermaid\n   flowchart TD\n     A --> B\n   ```", "~~~mermaid\nflowchart TD\n~~~"):
            arch = _GOOD_ARCHITECTURE.replace("```mermaid\nflowchart TD\n  A --> B\n```", fence)
            root = self._seed({"docs/spec/index.md": _index("full"), "docs/principles.md": _GOOD_PRINCIPLES,
                               "docs/architecture.md": arch})
            self.assertEqual(self._hard(design_form.findings("hard", root)), [],
                             f"a {fence.strip()[:6]}-style diagram must be recognized")

    # ---- well-formedness of a present backbone document ------------------------------------------

    def test_architecture_missing_diagram_is_hard(self):
        no_diagram = _GOOD_ARCHITECTURE.replace("```mermaid\nflowchart TD\n  A --> B\n```\n\n", "just prose\n\n")
        root = self._seed({"docs/spec/index.md": _index("full"), "docs/principles.md": _GOOD_PRINCIPLES,
                           "docs/architecture.md": no_diagram})
        result = self._actionable(design_form.findings("hard", root))
        self.assertEqual([f["severity"] for f in result], ["hard"])
        self.assertIn("has no diagram", result[0]["message"])

    def test_principles_missing_section_is_hard(self):
        broken = _GOOD_PRINCIPLES.replace("## What these rule out\nz\n", "")
        root = self._seed({"docs/spec/index.md": _index("full"), "docs/principles.md": broken,
                           "docs/architecture.md": _GOOD_ARCHITECTURE})
        result = self._actionable(design_form.findings("hard", root))
        self.assertEqual([f["severity"] for f in result], ["hard"])
        self.assertIn("what these rule out", result[0]["message"].lower())

    # a malformed present backbone document under a light opt-out is SURFACED (soft), never HARD-blocked:
    # the operator opted out of the fuller write-up, so a stray/partial doc is a nudge, not a merge gate.
    def test_present_document_under_light_is_a_soft_nudge_not_hard(self):
        no_diagram = _GOOD_ARCHITECTURE.replace("```mermaid\nflowchart TD\n  A --> B\n```\n\n", "prose\n\n")
        root = self._seed({"docs/spec/index.md": _index("light"), "docs/architecture.md": no_diagram})
        result = design_form.findings("hard", root)
        self.assertEqual(self._hard(result), [], "a present-but-malformed doc under light must not hard-block")
        self.assertTrue(self._soft_visible(result), "it is surfaced as a soft nudge")

    # THE BROWNFIELD REGRESSION (deliverable-gate SERIOUS): a repo that adopted product-design under the old
    # "not checked" regime — a docs/spec tree, no depth marker, and a freely-authored (malformed) architecture
    # doc — must NEVER be retroactively hard-blocked. It is surfaced as a soft nudge only.
    def test_brownfield_present_malformed_document_is_soft_never_hard(self):
        no_diagram = _GOOD_ARCHITECTURE.replace("```mermaid\nflowchart TD\n  A --> B\n```\n\n", "prose\n\n")
        root = self._seed({"docs/spec/index.md": _index(None), "docs/architecture.md": no_diagram})
        result = design_form.findings("hard", root)
        self.assertEqual(self._hard(result), [], "an unrecorded/brownfield description is never hard-blocked")
        self.assertTrue(self._soft_visible(result), "the malformed doc is still surfaced as a soft nudge")

    # ---- discretionary guides --------------------------------------------------------------------

    def test_present_guide_missing_sections_is_hard_in_full_mode(self):
        root = self._seed({"docs/spec/index.md": _index("full"), "docs/principles.md": _GOOD_PRINCIPLES,
                           "docs/architecture.md": _GOOD_ARCHITECTURE,
                           "docs/how-to/deploy.md": "# How to deploy\n\n## Goal\nx\n"})  # missing 2 sections
        result = self._hard(design_form.findings("hard", root))
        self.assertEqual(len(result), 1)
        self.assertIn("docs/how-to/deploy.md", result[0]["message"])

    def test_present_guide_under_light_is_a_soft_nudge_not_hard(self):
        root = self._seed({"docs/spec/index.md": _index("light"),
                           "docs/how-to/deploy.md": "# How to deploy\n\n## Goal\nx\n"})
        result = design_form.findings("hard", root)
        self.assertEqual(self._hard(result), [], "a guide the operator didn't opt into must not hard-block")
        self.assertTrue(self._soft_visible(result), "it is surfaced as a soft nudge")

    def test_reference_guide_needs_at_least_one_section(self):
        root = self._seed({"docs/spec/index.md": _index("full"), "docs/principles.md": _GOOD_PRINCIPLES,
                           "docs/architecture.md": _GOOD_ARCHITECTURE,
                           "docs/reference/settings.md": "# Settings\n\nno sections here\n"})
        self.assertEqual(len(self._hard(design_form.findings("hard", root))), 1)

    def test_absent_guides_are_never_flagged(self):
        root = self._seed({"docs/spec/index.md": _index("full"), "docs/principles.md": _GOOD_PRINCIPLES,
                           "docs/architecture.md": _GOOD_ARCHITECTURE})
        self.assertEqual(self._actionable(design_form.findings("hard", root)), [],
                         "no guides present is not a problem — guides are discretionary")

    def test_wellformed_guide_passes(self):
        root = self._seed({"docs/spec/index.md": _index("light"),
                           "docs/how-to/deploy.md": "# How to deploy\n\n## Goal\nx\n\n## Steps\ny\n\n"
                                                    "## Check it worked\nz\n"})
        self.assertEqual(self._actionable(design_form.findings("hard", root)), [])

    # ---- disclosed no-op + operator-communication bound ------------------------------------------

    def test_no_spec_tree_is_a_disclosed_noop(self):
        root = self._seed({"README.md": "# hi\n"})
        result = design_form.findings("hard", root)
        self.assertEqual(len(result), 1)
        self.assertTrue(result[0].get("not_applicable"))
        self.assertEqual(result[0]["severity"], "soft")

    def test_no_finding_leaks_a_framework_name_or_raw_check_id(self):
        # every rendered message must speak plainly — no arc42/C4/Diátaxis, no raw check id.
        no_diagram = _GOOD_ARCHITECTURE.replace("```mermaid\nflowchart TD\n  A --> B\n```\n\n", "prose\n\n")
        roots = [
            self._seed({"docs/spec/index.md": _index("full")}),  # missing both backbone docs
            self._seed({"docs/spec/index.md": _index(None)}),     # nudge
            self._seed({"docs/spec/index.md": _index("full"), "docs/principles.md": _GOOD_PRINCIPLES,
                        "docs/architecture.md": no_diagram}),     # diagram
            self._seed({"README.md": "# hi\n"}),                  # no-op
        ]
        banned = ("arc42", "c4", "diátaxis", "diataxis", "product-design-form", "engine/check")
        for root in roots:
            for f in design_form.findings("hard", root):
                low = f["message"].lower()
                for token in banned:
                    self.assertNotIn(token, low, f"a finding leaked '{token}': {f['message']}")


class DesignFormDispatchTests(unittest.TestCase):
    def test_demo_passes(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(design_form.demo(), 0, "the falsifiable self-check passes")

    def test_emit_findings_prints_a_json_array_and_returns_zero(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = design_form.emit_findings()
        self.assertEqual(rc, 0)
        parsed = json.loads(buf.getvalue())
        self.assertIsInstance(parsed, list)

    def test_main_routes_demo_and_default(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(design_form.main(["demo"]), 0)
            self.assertEqual(design_form.main([]), 0)


if __name__ == "__main__":
    unittest.main()
