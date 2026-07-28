#!/usr/bin/env python3
"""Self-tests for the spec-obligation matrix (product_design/obligation_matrix.py) — the derived-committed
criterion-by-criterion record of a product's settled acceptance criteria and its MVP-safe drift gate.

Run: uv run --directory .engine --frozen -- python -m unittest discover -s tools -p 'test_*.py' -b

These lock the load-bearing facts nothing else does:
  - derivation is criterion-granular over `locked` docs/spec docs (one row per criterion), keyed by a
    content digest AT the validated table position; `draft`/`stub` docs contribute none; the digest is stable
    under whitespace but moves on a real re-wording; two identical criteria are disambiguated by position.
  - render is deterministic (derive-twice-and-compare is a valid equality test — the fingerprint discipline).
  - the drift gate is MVP-safe: nothing settled ⇒ SOFT disclosed no-op whatever the committed side (never a
    hard block); a present, non-empty, in-sync matrix ⇒ note (silent pass); genuine drift ⇒ HARD.
  - the negative fixture the checker-of-checkers witnesses actually bites (a settled spec + a stale committed
    matrix ⇒ HARD), so hard-check-bite is not vacuous.
  - the live committed matrix is in sync with the repo (here: the empty disclosed no-op — this repo has no
    settled docs/spec, the traveling-infra-inert-in-construction property).
"""
from __future__ import annotations
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import validate  # noqa: E402
from product_design import obligation_matrix as om  # noqa: E402


def _seed(files: dict) -> str:
    d = tempfile.mkdtemp(prefix="engine-obligation-matrix-test-")
    for rel, body in files.items():
        path = os.path.join(d, rel)
        parent = os.path.dirname(path)
        if parent and not os.path.isdir(parent):
            os.makedirs(parent)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(body)
    return d


def _cap(criteria_rows: str, status: str = "locked") -> str:
    return (f"---\nstatus: {status}\n---\n\n# A capability\n\n## Summary\nWhat.\n\n## Behavior\nHow.\n\n"
            "## Acceptance criteria\n\n| Criterion | How verified | Who checks it |\n| --- | --- | --- |\n"
            + criteria_rows)


def _index(rows: str) -> str:
    return "# Product spec\n\n| Capability | Status | Doc |\n| --- | --- | --- |\n" + rows


class TestDerivation(unittest.TestCase):
    def test_locked_capability_yields_one_row_per_criterion_positioned(self):
        root = _seed({"docs/spec/index.md": _index("| A | settled | [A](a.md) |\n"),
                      "docs/spec/a.md": _cap("| First thing | a demo | operator |\n"
                                             "| Second thing | a test | engine |\n")})
        try:
            rows = om.derive_rows(root)
        finally:
            import shutil
            shutil.rmtree(root, ignore_errors=True)
        self.assertEqual([r["position"] for r in rows], [0, 1])
        self.assertEqual([r["criterion"] for r in rows], ["First thing", "Second thing"])
        self.assertEqual([r["who"] for r in rows], ["operator", "engine"])
        self.assertEqual(rows[0]["doc"], "docs/spec/a.md")
        self.assertTrue(all(r["digest"].startswith("sha256:") for r in rows))

    def test_draft_and_stub_docs_contribute_no_rows(self):
        root = _seed({"docs/spec/index.md": _index("| A | in progress | [A](a.md) |\n| B | not yet described | [B](b.md) |\n"),
                      "docs/spec/a.md": _cap("| x | y | operator |\n", status="draft"),
                      "docs/spec/b.md": "---\nstatus: stub\n---\n\n# B\n"})
        try:
            self.assertEqual(om.derive_rows(root), [])
        finally:
            import shutil
            shutil.rmtree(root, ignore_errors=True)

    def test_digest_stable_under_whitespace_but_moves_on_rewording(self):
        d1 = om._digest("It   works\tend to end")
        d2 = om._digest("It works end to end")
        d3 = om._digest("It works partially")
        self.assertEqual(d1, d2, "a whitespace-only reflow must not move the digest")
        self.assertNotEqual(d1, d3, "a genuine re-wording must move the digest (the row re-opens)")

    def test_identical_criteria_disambiguated_by_position(self):
        root = _seed({"docs/spec/index.md": _index("| A | settled | [A](a.md) |\n"),
                      "docs/spec/a.md": _cap("| Same | a demo | operator |\n| Same | a demo | operator |\n")})
        try:
            rows = om.derive_rows(root)
        finally:
            import shutil
            shutil.rmtree(root, ignore_errors=True)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["digest"], rows[1]["digest"])  # same content
        self.assertNotEqual(rows[0]["position"], rows[1]["position"])  # distinct identity

    def test_render_is_deterministic(self):
        root = _seed({"docs/spec/index.md": _index("| A | settled | [A](a.md) |\n"),
                      "docs/spec/a.md": _cap("| x | y | operator |\n")})
        try:
            self.assertEqual(om.render(om.canonical_matrix(root)), om.render(om.canonical_matrix(root)))
        finally:
            import shutil
            shutil.rmtree(root, ignore_errors=True)


class TestDriftGateIsSection20Safe(unittest.TestCase):
    def test_nothing_settled_is_soft_even_with_absent_committed(self):
        root = _seed({"README.md": "hi"})  # no docs/spec at all
        try:
            f = om.check(os.path.join(root, "does-not-exist.json"), root)
        finally:
            import shutil
            shutil.rmtree(root, ignore_errors=True)
        self.assertEqual(f["severity"], "soft", "no settled spec must never be a hard block")

    def test_stale_nonempty_committed_against_empty_derivation_is_hard(self):
        """A capability un-settled (locked->draft) leaves the committed matrix with stale rows while the
        derivation is empty — real drift the gate must catch, not falsely report as 'nothing to record'
        (deliverable-gate serious, #454). An empty/absent committed side stays soft (the true MVP)."""
        root = _seed({"README.md": "hi"})  # no docs/spec -> canonical empty
        try:
            with tempfile.TemporaryDirectory() as d:
                p = os.path.join(d, "m.json")
                om.write_matrix(om.render({"schema_version": 1, "source": "docs/spec",
                                           "rows": [{"doc": "docs/spec/a.md", "position": 0, "digest": "sha256:x",
                                                     "criterion": "stale", "how_verified": "d", "who": "operator"}]}), p)
                self.assertEqual(om.check(p, root)["severity"], "hard",
                                 "a stale rows-bearing matrix against an empty spec must be hard, not a false no-op")
                om.write_matrix(om.render({"schema_version": 1, "source": "docs/spec", "rows": []}), p)
                self.assertEqual(om.check(p, root)["severity"], "soft", "an empty committed side stays soft")
        finally:
            import shutil
            shutil.rmtree(root, ignore_errors=True)

    def test_in_sync_nonempty_is_note(self):
        root = _seed({"docs/spec/index.md": _index("| A | settled | [A](a.md) |\n"),
                      "docs/spec/a.md": _cap("| x | y | operator |\n")})
        try:
            with tempfile.TemporaryDirectory() as d:
                p = os.path.join(d, "m.json")
                om.generate(p, root)
                self.assertEqual(om.check(p, root)["severity"], "note")
        finally:
            import shutil
            shutil.rmtree(root, ignore_errors=True)

    def test_drift_of_present_nonempty_matrix_is_hard(self):
        root = _seed({"docs/spec/index.md": _index("| A | settled | [A](a.md) |\n"),
                      "docs/spec/a.md": _cap("| x | y | operator |\n")})
        try:
            with tempfile.TemporaryDirectory() as d:
                p = os.path.join(d, "m.json")
                om.write_matrix(om.render({"schema_version": 1, "source": "docs/spec", "rows": []}), p)
                self.assertEqual(om.check(p, root)["severity"], "hard",
                                 "an empty committed matrix against a settled spec is drift (hard)")
        finally:
            import shutil
            shutil.rmtree(root, ignore_errors=True)


class TestNegativeFixtureBites(unittest.TestCase):
    """The hard-check-bite fixture must actually bite — a settled spec + a stale committed matrix ⇒ HARD."""

    def test_fixture_env_produces_the_hard_finding(self):
        fixture = os.path.join(validate.ROOT, ".engine", "_fixtures", "product-spec-matrix")
        f = om.check(os.path.join(fixture, "stale-matrix.json"), fixture)
        self.assertEqual(f["severity"], "hard")
        self.assertIn("is out of date", f["message"])


class TestLiveRepository(unittest.TestCase):
    def test_committed_matrix_is_in_sync(self):
        """The committed .engine/product-spec-matrix.json matches a fresh derivation (no drift). This repo has
        no settled docs/spec, so that is the empty disclosed no-op — the traveling-infra-inert-here property."""
        f = om.check()
        self.assertNotEqual(f["severity"], "hard", "the committed matrix must not be drifted")

    def test_check_entry_emits_finding_v1_json(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = om.main([])
        self.assertEqual(rc, 0)
        out = json.loads(buf.getvalue())
        self.assertIsInstance(out, list)
        for finding in out:
            self.assertIn(finding["severity"], ("soft", "hard"))
            self.assertTrue(finding["message"])

    def test_demo_runs_its_real_fail_then_pass(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = om.main(["demo"])
        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
