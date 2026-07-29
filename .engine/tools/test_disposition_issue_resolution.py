"""Tests for the disposition-issue-resolution check (#292) — disposition_issue_resolution_check.

The transport is injectable, so every path (404 / PR / non-engine / engine / outage / >=400) is exercised
OFFLINE and deterministically — the real findings()/classify() logic runs against a faked network. The live
witness (the sentinel #999999999 resolving against the real repo) is the meta-check's CI run, by design.
Assertions are on (severity, message token), the same shape the meta-check asserts.
"""
from __future__ import annotations
import io
import json
import os
import sys
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate  # noqa: E402
import disposition_issue_resolution_check as dirc  # noqa: E402


def _transport(table):
    """A fake transport: `table` maps issue number -> (status, json|None) | the string 'raise' (network down)."""
    def t(method, path):
        n = int(path.rstrip("/").split("/")[-1])
        entry = table[n]
        if entry == "raise":
            raise dirc._Unevaluable("simulated network failure")
        return entry
    return t


def _resolver(table, token="TOK"):
    return dirc.IssueResolver("owner/repo", token, transport=_transport(table))


def _review(text: str) -> str:
    return f"## Purpose\n\nseed\n\n## Review\n\n{text}\n\n## Files of interest\n\nseed\n"


ENGINE = (200, {"number": 1, "labels": [{"name": "engine"}]})
NON_ENGINE = (200, {"number": 1, "labels": [{"name": "bug"}]})
PR = (200, {"number": 1, "labels": [{"name": "engine"}], "pull_request": {"url": "..."}})
ABSENT = (404, None)


class TestClassify(unittest.TestCase):
    def test_404_is_unresolved(self):
        self.assertEqual(_resolver({5: ABSENT}).classify(5), "unresolved")

    def test_engine_labeled_issue_is_resolved(self):
        self.assertEqual(_resolver({5: ENGINE}).classify(5), "resolved")

    def test_non_engine_issue_is_unresolved(self):
        self.assertEqual(_resolver({5: NON_ENGINE}).classify(5), "unresolved")

    def test_pull_request_is_skipped(self):
        # A PR carries an engine label too here — the pull_request key must win (checked BEFORE the label).
        self.assertEqual(_resolver({5: PR}).classify(5), "skip-pr")

    def test_403_raises_unevaluable(self):
        with self.assertRaises(dirc._Unevaluable):
            _resolver({5: (403, None)}).classify(5)

    def test_5xx_raises_unevaluable(self):
        with self.assertRaises(dirc._Unevaluable):
            _resolver({5: (500, None)}).classify(5)

    def test_network_failure_raises_unevaluable(self):
        with self.assertRaises(dirc._Unevaluable):
            _resolver({5: "raise"}).classify(5)


class TestCitedNumbers(unittest.TestCase):
    def test_only_the_review_section_is_scanned(self):
        body = "## Purpose\n\nrelates to #111\n\n## Review\n\ntracked as #222\n"
        self.assertEqual(dirc.cited_issue_numbers(body), [222])

    def test_distinct_first_seen_order(self):
        self.assertEqual(dirc.cited_issue_numbers(_review("#7 and #3 and #7 again")), [7, 3])

    def test_no_review_section_yields_nothing(self):
        self.assertEqual(dirc.cited_issue_numbers("## Purpose\n\ntracked as #9\n"), [])


class TestFindings(unittest.TestCase):
    def test_fabricated_citation_is_a_hard_act_red(self):
        fs = dirc.findings("hard", _review("tracked as #4242"), _resolver({4242: ABSENT}))
        self.assertEqual(len(fs), 1)
        self.assertEqual(fs[0]["severity"], "hard")
        self.assertIn("isn't a real engine-tracked issue", fs[0]["message"])
        self.assertIn("#4242", fs[0]["message"])

    def test_non_engine_citation_is_a_hard_act_red(self):
        fs = dirc.findings("hard", _review("tracked as #50"), _resolver({50: NON_ENGINE}))
        self.assertTrue(fs and fs[0]["severity"] == "hard")
        self.assertIn("isn't a real engine-tracked issue", fs[0]["message"])

    def test_outage_is_a_distinct_hard_wait_red(self):
        fs = dirc.findings("hard", _review("tracked as #4242"), _resolver({4242: (403, None)}))
        self.assertEqual(len(fs), 1)
        self.assertEqual(fs[0]["severity"], "hard")
        self.assertIn("unreachable", fs[0]["message"])
        self.assertNotIn("isn't a real engine-tracked issue", fs[0]["message"])  # not confusable with the act red

    def test_resolved_citation_emits_the_green_warrant_soft_note(self):
        fs = dirc.findings("hard", _review("tracked as #292"), _resolver({292: ENGINE}))
        self.assertEqual(len(fs), 1)
        self.assertEqual(fs[0]["severity"], "soft")
        self.assertIn("#292", fs[0]["message"])
        self.assertIn("not that a cited issue is the right one", fs[0]["message"])

    def test_pull_request_citation_is_silently_skipped(self):
        # A lone PR citation: nothing to resolve, nothing flagged, no green note (no issue was resolved).
        self.assertEqual(dirc.findings("hard", _review("rides #349"), _resolver({349: PR})), [])

    def test_no_citation_no_finding(self):
        self.assertEqual(dirc.findings("hard", "## Review\n\nnothing cited\n", _resolver({})), [])

    def test_a_fabrication_blocks_even_beside_a_real_one(self):
        fs = dirc.findings("hard", _review("tracked as #292 and #4242"), _resolver({292: ENGINE, 4242: ABSENT}))
        # The hard act red wins; no green note is emitted when anything is unresolved.
        self.assertTrue(any(f["severity"] == "hard" and "#4242" in f["message"] for f in fs))
        self.assertFalse(any(f["severity"] == "soft" for f in fs))

    def test_token_never_appears_in_a_finding_message(self):
        secret = "ghp_SUPERSECRETTOKEN"
        r = dirc.IssueResolver("owner/repo", secret, transport=_transport({4242: ABSENT}))
        for f in dirc.findings("hard", _review("tracked as #4242"), r):
            self.assertNotIn(secret, f["message"])


class TestMainNoTokenBranches(unittest.TestCase):
    """The no-token branch must fail OPEN locally (soft) but CLOSED in CI (hard) — never a silent CI bypass."""

    def _run_main(self, env):
        saved = dict(os.environ)
        for k in ("GITHUB_TOKEN", "GITHUB_REPOSITORY", "GITHUB_ACTIONS", "CI",
                  "ENGINE_DISPOSITION_PR_BODY", "ENGINE_RULE_TIER", "GITHUB_EVENT_PATH"):
            os.environ.pop(k, None)
        os.environ.update(env)
        try:
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = dirc.main([])
            return rc, json.loads(buf.getvalue())
        finally:
            os.environ.clear()
            os.environ.update(saved)

    def test_no_token_local_is_soft(self):
        rc, fs = self._run_main({"ENGINE_RULE_TIER": "hard"})
        self.assertEqual(rc, 0)
        self.assertTrue(fs and all(f["severity"] == "soft" for f in fs))

    def test_no_token_in_ci_is_hard(self):
        rc, fs = self._run_main({"ENGINE_RULE_TIER": "hard", "GITHUB_ACTIONS": "true"})
        self.assertEqual(rc, 0)
        self.assertTrue(any(f["severity"] == "hard" for f in fs))


class TestRuleIsWellFormed(unittest.TestCase):
    def test_rule_conforms_and_is_live_with_pass_token(self):
        from jsonschema import Draft202012Validator
        rule = validate.load_json(os.path.join(validate.ROOT, ".engine", "check",
                                                "disposition-issue-resolution.json"))
        schema = validate.load_json(os.path.join(validate.ROOT, ".engine", "schemas", "check.v1.json"))
        self.assertEqual(list(Draft202012Validator(schema).iter_errors(rule)), [])
        self.assertEqual(rule["tier"], "hard")
        self.assertEqual(rule["suites"], ["CI"])
        self.assertTrue(rule["params"]["pass_token"])  # needs the token to resolve issues


class TestDemoRunsAndCanFail(unittest.TestCase):
    def test_demo_passes_over_the_real_logic(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(dirc.main(["demo"]), 0)
        self.assertIn("DEMO OK", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
