#!/usr/bin/env python3
"""Self-tests for build_readiness.py — the core, read-only pre-phase build-readiness helper.

Run: uv run --directory .engine --frozen -- python -m unittest discover -s tools -p 'test_*.py' -b

These lock the helper's contract: a build-order phase is ready when every capability it schedules is settled
(`status: locked`); an in-progress, not-yet-described, or not-yet-written piece makes the phase not ready and is
named with its plain stage; a build-order link that escapes `docs/spec/` is reported and NEVER opened (the
engine/product wall); absent inputs (no spec tree, no build order, no phases) are disclosed no-ops, never a crash
and never a silent "all ready"; a named-phase selection narrows, an unknown phase name no-ops; the rendered block
carries its bound and leaks no raw lifecycle token; and the demo runs its real falsifiable self-check.
"""
from __future__ import annotations
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import build_readiness as br  # noqa: E402
import quiet_call  # noqa: E402


def _cap(status: str) -> str:
    return f"---\nstatus: {status}\n---\n\n# A capability\n\n## Summary\nx\n"


def _plan(rows: str) -> str:
    return "# Build order\n\n| Phase | Capability | Doc |\n| --- | --- | --- |\n" + rows


class _Seeded(unittest.TestCase):
    def seed(self, files: dict) -> str:
        d = tempfile.mkdtemp(prefix="engine-build-readiness-test-")
        self.addCleanup(shutil.rmtree, d, True)
        for rel, text in files.items():
            path = os.path.join(d, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(text)
        return d


class ReadinessLogicTests(_Seeded):
    def test_phase_all_settled_is_ready(self):
        root = self.seed({"docs/spec/a.md": _cap("locked"), "docs/spec/b.md": _cap("locked"),
                          "docs/spec/build-plan.md": _plan("| P | A | [A](a.md) |\n| P | B | [B](b.md) |\n")})
        r = br.readiness(root)
        self.assertTrue(r["ok"])
        self.assertEqual(len(r["phases"]), 1)
        self.assertTrue(r["phases"][0]["ready"])

    def test_in_progress_piece_makes_phase_not_ready_and_is_named(self):
        root = self.seed({"docs/spec/a.md": _cap("locked"), "docs/spec/c.md": _cap("draft"),
                          "docs/spec/build-plan.md": _plan("| P | A | [A](a.md) |\n| P | C | [C](c.md) |\n")})
        phase = br.readiness(root)["phases"][0]
        self.assertFalse(phase["ready"])
        not_ready = [p for p in phase["pieces"] if not p["ready"]]
        self.assertEqual([(p["capability"], p["stage"]) for p in not_ready], [("C", "in progress")])

    def test_stub_piece_renders_not_yet_described(self):
        root = self.seed({"docs/spec/s.md": _cap("stub"),
                          "docs/spec/build-plan.md": _plan("| P | S | [S](s.md) |\n")})
        self.assertEqual(br.readiness(root)["phases"][0]["pieces"][0]["stage"], "not yet described")

    def test_missing_document_is_not_ready(self):
        root = self.seed({"docs/spec/a.md": _cap("locked"),
                          "docs/spec/build-plan.md": _plan("| P | A | [A](a.md) |\n| P | G | [G](ghost.md) |\n")})
        pieces = {p["capability"]: p for p in br.readiness(root)["phases"][0]["pieces"]}
        self.assertEqual(pieces["G"]["stage"], br._PLAIN_MISSING)
        self.assertFalse(pieces["G"]["ready"])

    def test_phases_are_in_first_appearance_order(self):
        root = self.seed({"docs/spec/a.md": _cap("locked"), "docs/spec/b.md": _cap("locked"),
                          "docs/spec/build-plan.md": _plan("| Later | B | [B](b.md) |\n"
                                                           "| Foundation | A | [A](a.md) |\n")})
        self.assertEqual([p["phase"] for p in br.readiness(root)["phases"]], ["Later", "Foundation"])

    def test_bare_path_doc_cell_is_scheduled_not_dropped(self):
        # A Doc cell that names its document as a bare path (not a markdown link) still schedules the capability,
        # so an unsettled bare-path piece is not dropped — else the phase would falsely read ready.
        root = self.seed({"docs/spec/a.md": _cap("locked"), "docs/spec/c.md": _cap("draft"),
                          "docs/spec/build-plan.md": _plan("| P | A | [A](a.md) |\n| P | C | c.md |\n")})
        phase = br.readiness(root)["phases"][0]
        self.assertEqual(len(phase["pieces"]), 2, "the bare-path row must not be dropped")
        self.assertFalse(phase["ready"], "a phase with an unsettled bare-path piece is not ready")
        self.assertIn("C", [p["capability"] for p in phase["pieces"] if not p["ready"]])

    def test_non_md_doc_cell_schedules_nothing(self):
        # A Doc cell naming a non-.md target schedules no document (matching the coverage grammar), so the phase
        # has no pieces from it — never a false not-ready on a target readiness can't resolve.
        root = self.seed({"docs/spec/build-plan.md": _plan("| P | A | [A](a.txt) |\n")})
        r = br.readiness(root)
        self.assertTrue(r["ok"])
        self.assertEqual(r["phases"], [], "a non-.md target schedules nothing, so no phase is formed")

    def test_document_with_unknown_status_reads_not_settled(self):
        # A document that exists but carries an unrecognized stage reads as "not settled yet" — distinct from a
        # missing document ("no description written yet"); both are not-ready.
        root = self.seed({"docs/spec/w.md": _cap("wip"),
                          "docs/spec/build-plan.md": _plan("| P | W | [W](w.md) |\n")})
        piece = br.readiness(root)["phases"][0]["pieces"][0]
        self.assertEqual(piece["stage"], br._PLAIN_UNSETTLED)
        self.assertFalse(piece["ready"])


class WallTests(_Seeded):
    def test_escaping_link_is_reported_never_read(self):
        root = self.seed({"docs/spec/build-plan.md": _plan("| P | Esc | [Esc](../../secret.md) |\n"),
                          "secret.md": "TOP SECRET — must never be read\n"})
        piece = br.readiness(root)["phases"][0]["pieces"][0]
        self.assertEqual(piece["stage"], br._PLAIN_UNREADABLE)
        self.assertFalse(piece["ready"])

    def test_capability_stage_never_opens_an_escaping_target(self):
        # A direct read: an escaping target returns the unreadable render without touching the file.
        root = self.seed({"secret.md": "SECRET\n"})
        os.makedirs(os.path.join(root, "docs", "spec"))
        self.assertEqual(br._capability_stage(root, "../../secret.md"), br._PLAIN_UNREADABLE)


class NoOpTests(_Seeded):
    def test_no_spec_tree_is_a_disclosed_no_op(self):
        r = br.readiness(self.seed({"README.md": "hi"}))
        self.assertFalse(r["ok"])
        self.assertEqual(r["no_op_reason"], "no-spec-installed")

    def test_no_build_order_is_a_disclosed_no_op(self):
        r = br.readiness(self.seed({"docs/spec/a.md": _cap("locked")}))
        self.assertFalse(r["ok"])
        self.assertEqual(r["no_op_reason"], "no-build-order")

    def test_build_order_with_no_phases_is_a_disclosed_no_op(self):
        r = br.readiness(self.seed({"docs/spec/a.md": _cap("locked"),
                                    "docs/spec/build-plan.md": "# Build order\n\n(no table yet)\n"}))
        self.assertFalse(r["ok"])
        self.assertEqual(r["no_op_reason"], "no-phases")


class SelectTests(_Seeded):
    def _tree(self) -> str:
        return self.seed({"docs/spec/a.md": _cap("locked"), "docs/spec/c.md": _cap("draft"),
                          "docs/spec/build-plan.md": _plan("| Foundation | A | [A](a.md) |\n"
                                                           "| Core | C | [C](c.md) |\n")})

    def test_named_phase_narrows_case_insensitively(self):
        r = br._select(br.readiness(self._tree()), "core")
        self.assertTrue(r["ok"])
        self.assertEqual([p["phase"] for p in r["phases"]], ["Core"])

    def test_unknown_phase_is_a_disclosed_no_op(self):
        r = br._select(br.readiness(self._tree()), "Nonexistent")
        self.assertFalse(r["ok"])
        self.assertEqual(r["no_op_reason"], "no-such-phase")


class RenderTests(_Seeded):
    def test_render_carries_the_bound_and_leaks_no_raw_token(self):
        root = self.seed({"docs/spec/a.md": _cap("locked"), "docs/spec/c.md": _cap("draft"),
                          "docs/spec/build-plan.md": _plan("| P | A | [A](a.md) |\n| P | C | [C](c.md) |\n")})
        out = br.render(br.readiness(root))
        self.assertIn(br._BOUND_TAIL, out)
        for token in ("stub", "draft", "locked"):
            self.assertNotIn(token, out, f"the render leaked the raw lifecycle token '{token}'")

    def test_render_of_a_no_op_states_the_plain_reason_and_bound(self):
        out = br.render(br.readiness(self.seed({"README.md": "hi"})))
        self.assertIn("no settled description", out)
        self.assertIn(br._BOUND_TAIL, out)

    def test_render_agrees_in_number_for_a_single_unsettled_piece(self):
        # One unsettled piece reads "1 of 1 piece isn't settled" — singular noun and verb, never "pieces aren't".
        root = self.seed({"docs/spec/c.md": _cap("draft"),
                          "docs/spec/build-plan.md": _plan("| P | C | [C](c.md) |\n")})
        out = br.render(br.readiness(root))
        self.assertIn("1 of 1 piece isn't settled", out)
        self.assertNotIn("pieces aren't", out)


class DispatchTests(unittest.TestCase):
    def test_demo_runs_clean(self):
        self.assertEqual(quiet_call.run(br.main, ["demo"]), 0)

    def test_check_runs_against_the_repo(self):
        # The live repo has no docs/spec tree, so `check` is a disclosed no-op that still exits 0 (advisory).
        self.assertEqual(quiet_call.run(br.main, ["check"]), 0)

    def test_unknown_verb_is_usage_error(self):
        self.assertEqual(quiet_call.run(br.main, ["bogus"]), 2)


if __name__ == "__main__":
    unittest.main()
