#!/usr/bin/env python3
"""Conformance test for the fuller-detail authoring scaffolds
(.engine/modules/product-design/scaffold/{principles,architecture,diataxis-*}.md).

These are the fuller documents the product-intake authoring branch draws from — the product principles, the
architecture overview (with a C4-style diagram), and the four kinds of user guide. As of #553 the full write-up
is the DEFAULT (opt-out to a lighter one), and these documents are now checked for SHAPE by the design-form
check (`design_form.py`), so the backbone (principles + architecture) has its own write-from = check-against tie
in test_scaffold.py's DesignFormScaffoldConventionTests. What the engine still never does — for these or any
document — is judge whether the design is RIGHT; that stays the operator's call. This test guards the two things
that travel with the scaffold itself: the strip-on-author convention (an HTML guidance comment the author
removes, and bracketed placeholders to replace) so the starting shape never silently disappears, and the honest
bound stated in the template prose — checked for shape, but the operator's to get right, never ruled correct. It
reads the templates from disk, so it exercises exactly the bytes that ship.
"""
from __future__ import annotations
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # .engine/tools on sys.path
import validate  # noqa: E402

_SCAFFOLD_DIR = os.path.join(validate.ROOT, ".engine", "modules", "product-design", "scaffold")

# The fuller-detail authoring templates (shape-checked by design_form; the backbone tie is in test_scaffold.py).
_FULLER_TEMPLATES = (
    "principles.md",
    "architecture.md",
    "diataxis-tutorial.md",
    "diataxis-how-to.md",
    "diataxis-reference.md",
    "diataxis-explanation.md",
)


def _read(name: str) -> str:
    with open(os.path.join(_SCAFFOLD_DIR, name), "r", encoding="utf-8") as fh:
        return fh.read()


class FullerScaffoldConventionTests(unittest.TestCase):
    def test_every_fuller_template_is_committed(self):
        for name in _FULLER_TEMPLATES:
            self.assertTrue(os.path.isfile(os.path.join(_SCAFFOLD_DIR, name)),
                            f"the fuller authoring template {name!r} must be committed")

    def test_every_fuller_template_carries_stripped_guidance_and_placeholders(self):
        # The strip-on-author convention: a guidance comment (removed when authoring) and bracketed placeholders
        # (replaced with the real thing) — the same convention the spec-corpus templates use.
        for name in _FULLER_TEMPLATES:
            text = _read(name)
            self.assertIn("<!--", text, f"{name} must carry an HTML guidance comment the author strips")
            self.assertIn("-->", text, f"{name} must close its HTML guidance comment")
            self.assertTrue(re.search(r"<[^>\n]+>", text.split("-->", 1)[-1]),
                            f"{name} must carry at least one bracketed placeholder for the author to replace")

    def test_every_fuller_template_names_its_shape_checked_not_ruled_correct_bound(self):
        # Each template states plainly the honest bound — its shape is checked, but whether the design is RIGHT
        # is the operator's to get right, never ruled by the engine — so the bound travels with the scaffold
        # itself, not only the runbook copy. Whitespace is normalized so the phrase is found regardless of where
        # the guidance prose line-wraps. Guards against a regression to the old "NOT checked by the engine" copy,
        # which became false once design_form began checking these documents' shape (#553).
        for name in _FULLER_TEMPLATES:
            normalized = " ".join(_read(name).split())
            self.assertIn("yours to get right", normalized,
                          f"{name} must state the design is the operator's to get right")
            self.assertIn("the engine never judges that", normalized,
                          f"{name} must state the engine never judges whether the design is right")
            self.assertNotIn("NOT checked by the engine", normalized,
                             f"{name} must not carry the stale 'not checked' copy — its shape is now checked")

    def test_architecture_template_carries_a_mermaid_flowchart_diagram(self):
        # The C4-style container view ships as a mermaid `flowchart` so it renders on GitHub and stays diffable.
        text = _read("architecture.md")
        self.assertIn("```mermaid", text, "the architecture template must embed a mermaid diagram")
        self.assertIn("flowchart", text, "the architecture diagram must be a stable `flowchart`")


if __name__ == "__main__":
    unittest.main()
