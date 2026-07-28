"""Tests for the interface-resolution coherence guard — the live consumer that runs
validate.interface_resolution_findings over the present interface declarations, the .mcp.json handles,
and the present non-default implementations, wired as the engine/check/interface-coherence custom/script
CI rule. Verifies discovery, the tolerant .mcp.json read (product-owned — must never crash the check),
the genuinely-live built-in-not-wired note, the fixture-bite single-active path, and the CLI modes.
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import interface_coherence_check as icc  # noqa: E402
import validate  # noqa: E402


def _write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


class TestDiscovery(unittest.TestCase):
    def test_engine_interfaces_globs_and_parses(self):
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, ".engine/interfaces/search.json"),
                   json.dumps({"id": "search", "title": "Memory recall", "operations": [{"name": "search"}],
                               "fallback": {"kind": "mcp", "handle": "engine-memory"}}))
            decls = icc.engine_interfaces(root=d)
            self.assertEqual([x.get("id") for x in decls], ["search"])

    def test_present_handles_reads_mcp_server_keys(self):
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, ".mcp.json"),
                   json.dumps({"mcpServers": {"engine-memory": {}, "engine-knowledge-graph": {}}}))
            self.assertEqual(icc.present_handles(root=d), {"engine-memory", "engine-knowledge-graph"})

    def test_present_handles_is_tolerant_of_a_product_owned_file(self):
        # .mcp.json is operator/product-owned: absent, unreadable-JSON, or mcpServers-absent must all yield
        # an empty set and NEVER raise — a crash would be the only way this check could red on product content.
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(icc.present_handles(root=d), set())  # absent
            _write(os.path.join(d, ".mcp.json"), "{ not valid json")
            self.assertEqual(icc.present_handles(root=d), set())  # unreadable
            _write(os.path.join(d, ".mcp.json"), json.dumps({"other": {}}))
            self.assertEqual(icc.present_handles(root=d), set())  # mcpServers absent

    def test_present_impls_is_empty_without_the_fixture_seam(self):
        self.assertEqual(icc.present_impls(), {})


_SEARCH = {"id": "search", "title": "Memory recall", "operations": [{"name": "search"}],
           "fallback": {"kind": "mcp", "handle": "engine-memory"}}


class TestResolutionLegs(unittest.TestCase):
    def test_clean_set_yields_no_findings(self):
        f = validate.interface_resolution_findings([_SEARCH], {}, {"engine-memory"}, "hard", icc._MESSAGE)
        self.assertEqual(f, [])

    def test_two_non_default_impls_is_single_active_hard(self):
        impls = {"search": [{"handle": "a", "operations": ["search"]},
                            {"handle": "b", "operations": ["search"]}]}
        f = validate.interface_resolution_findings([_SEARCH], impls, {"engine-memory", "a", "b"},
                                                   "hard", icc._MESSAGE)
        self.assertTrue(any(x["severity"] == "hard" and "only one can be active" in x["message"] for x in f))

    def test_built_in_not_wired_is_a_live_note(self):
        # The genuinely-live leg: with engine-memory absent from the present handles, the built-in that
        # answers memory recall is not wired — a plain-language setup NOTE, never the interface id.
        f = validate.interface_resolution_findings([_SEARCH], {}, set(), "hard", icc._MESSAGE)
        self.assertEqual([x["severity"] for x in f], ["note"])
        self.assertIn("not currently wired", f[0]["message"])
        self.assertIn("Memory recall", f[0]["message"])
        self.assertNotIn("'search'", f[0]["message"])

    def test_non_conforming_impl_is_hard(self):
        impls = {"search": [{"handle": "a", "operations": []}]}  # missing the declared 'search' op
        f = validate.interface_resolution_findings([_SEARCH], impls, {"engine-memory", "a"},
                                                   "hard", icc._MESSAGE)
        self.assertTrue(any(x["severity"] == "hard" and "can't reliably stand in" in x["message"] for x in f))

    def test_a_structurally_malformed_declaration_does_not_crash(self):
        # A file that is valid JSON but wrong-shaped (operations not objects, fallback not an object) must not
        # raise out of the leg — its shape is caught by the declaration's own schema check, not here.
        bad = {"id": "x", "title": "X", "operations": ["not-an-object"], "fallback": "not-an-object"}
        f = validate.interface_resolution_findings([bad], {}, set(), "hard", icc._MESSAGE)
        self.assertIsInstance(f, list)  # no AttributeError


class TestScriptModes(unittest.TestCase):
    def test_check_mode_clean_on_real_repo(self):
        # main() globs the REAL repo: both built-ins are wired and no non-default impl is present -> [].
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = icc.main([])
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(buf.getvalue()), [])

    def test_fixture_seam_makes_the_single_active_leg_bite(self):
        # The hard-check-bite path: pointing the present-impl set at the committed fixture surfaces the
        # single-active HARD finding, proving the guard bites even though production present_impls is empty.
        os.environ["ENGINE_INTERFACE_FIXTURE_DIR"] = ".engine/_fixtures/interface-coherence"
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = icc.main([])
            self.assertEqual(rc, 0)
            out = json.loads(buf.getvalue())
            self.assertTrue(any(x["severity"] == "hard" and "only one can be active" in x["message"] for x in out))
        finally:
            del os.environ["ENGINE_INTERFACE_FIXTURE_DIR"]

    def test_demo_runs_and_narrates(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = icc.main(["demo"])
        self.assertEqual(rc, 0)
        text = buf.getvalue()
        self.assertIn("RED", text)


if __name__ == "__main__":
    unittest.main()
