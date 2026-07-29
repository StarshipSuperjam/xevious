"""Tests for the local-reference declaration shape gate.

Verifies: an absent declaration surfaces nothing (the steady state); a present-but-unparseable one is a hard
finding rather than silence; an unrecognised top-level entry is refused (it would check nothing while looking
as though it did); a single-character entry is refused (it would flag every change forever); the CLI honours
the seeded-path seam and emits a finding.v1 array on stdout; the gate does NOT walk the repository; and the
demo runs.
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import operator_local_references_check as check  # noqa: E402


def _seed(tmp, obj):
    p = os.path.join(tmp, "operator-local-references.json")
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(obj if isinstance(obj, str) else json.dumps(obj))
    return p


class TestFindings(unittest.TestCase):
    def test_absent_surfaces_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(check.findings("hard", check.load_declaration(os.path.join(d, "no.json"))), [])

    def test_unparseable_is_hard_not_silent(self):
        # The reader behind this gate degrades an unreadable declaration to an empty vocabulary. That is safe
        # only because this gate refuses to let one reach the base branch.
        with tempfile.TemporaryDirectory() as d:
            fs = check.findings("hard", check.load_declaration(_seed(d, "{oops")))
        self.assertEqual([f["severity"] for f in fs], ["hard"])
        self.assertIn("not valid JSON", fs[0]["message"])

    def test_a_non_object_declaration_is_hard(self):
        fs = check.findings("hard", ["D-"])
        self.assertEqual(len(fs), 1)
        self.assertIn("must be a single set of entries", fs[0]["message"])

    def test_an_unrecognised_entry_is_refused(self):
        fs = check.findings("hard", {"ticket_numbers": ["ACME-"]})
        self.assertEqual(len(fs), 1)
        self.assertIn("ticket_numbers", fs[0]["message"])

    def test_a_non_list_value_is_refused(self):
        fs = check.findings("hard", {"phrases": "Acme Handbook"})
        self.assertEqual(len(fs), 1)
        self.assertIn("must be a list", fs[0]["message"])

    def test_a_blank_or_non_string_entry_is_refused(self):
        fs = check.findings("hard", {"phrases": ["", "  ", 7]})
        self.assertEqual(len(fs), 3)
        self.assertTrue(all("not a plain word or phrase" in f["message"] for f in fs))

    def test_a_single_character_entry_is_refused_as_matching_everything(self):
        fs = check.findings("hard", {"id_prefixes": ["D"]})
        self.assertEqual(len(fs), 1)
        self.assertIn("would match nearly every line", fs[0]["message"])

    def test_a_well_formed_declaration_passes_clean(self):
        self.assertEqual(check.findings("hard", {
            "id_prefixes": ["D-"], "phrases": ["Acme Handbook"],
            "section_refs": ["repository-topology"]}), [])

    def test_the_tier_is_honoured(self):
        self.assertEqual(check.findings("soft", {"id_prefixes": ["D"]})[0]["severity"], "soft")


class TestNoRepositoryWalk(unittest.TestCase):
    def test_the_gate_reads_only_the_declaration(self):
        # Load-bearing, not a style preference: a custom/script that crashes or overruns its budget becomes a
        # HARD finding whatever its own tier, so walking a deployment's tree here would let that deployment's
        # size or file encoding red its own required CI — the defect class this gate shipped alongside fixing.
        opened = []
        real = open

        def _spy(path, *a, **k):
            opened.append(str(path))
            return real(path, *a, **k)
        with tempfile.TemporaryDirectory() as d:
            p = _seed(d, {"id_prefixes": ["D-"]})
            import builtins
            builtins.open = _spy
            try:
                check.findings("hard", check.load_declaration(p))
            finally:
                builtins.open = real
        self.assertEqual(opened, [p], "the gate must open the declaration and nothing else")


class TestCLI(unittest.TestCase):
    def _run(self, seeded):
        buf = io.StringIO()
        prior = os.environ.get("ENGINE_LOCAL_REFERENCES_PATH")
        os.environ["ENGINE_LOCAL_REFERENCES_PATH"] = seeded
        try:
            with contextlib.redirect_stdout(buf):
                rc = check.main([])
        finally:
            if prior is None:
                os.environ.pop("ENGINE_LOCAL_REFERENCES_PATH", None)
            else:
                os.environ["ENGINE_LOCAL_REFERENCES_PATH"] = prior
        self.assertEqual(rc, 0, "a custom/script returns 0 on a successful evaluation, whatever it found")
        return json.loads(buf.getvalue())

    def test_the_seam_feeds_a_seeded_declaration_and_emits_findings(self):
        with tempfile.TemporaryDirectory() as d:
            emitted = self._run(_seed(d, {"id_prefixes": ["D"]}))
        self.assertEqual(len(emitted), 1)
        self.assertEqual(emitted[0]["severity"], "hard")

    def test_a_clean_declaration_emits_an_empty_array(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(self._run(_seed(d, {"id_prefixes": ["D-"]})), [])

    def test_demo_runs_green(self):
        self.assertEqual(check.main(["demo"]), 0)


if __name__ == "__main__":
    unittest.main()
