"""Tests for issue_conformance_ci — the on:issues backstop for the engine-Issue body contract.

These lock the load-bearing behaviours a non-engineer cannot read code to verify: that the workflow is an
engine-owned traveler (FOUNDATION_INFRA → CODEOWNERS + upgrade overlay, the same treatment as the other engine
workflows); that conformance is the SAME predicate the in-session gate uses, over issue_gate's single-source
markers (so the two layers can't drift); that a non-conforming engine Issue is flagged with exactly ONE comment
(idempotent — a re-fire never double-comments), that a conform-after-edit removes the label (never a lingering
chore), and that out-of-scope / unactionable inputs no-op while a genuine API failure surfaces (the
safety-net-not-a-gate fail contract). The posted comment is a STATIC template (it takes no issue argument, so it
cannot echo attacker-controlled title/body) whose first line plainly disowns the operator chore.
"""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate                 # noqa: E402
import module_coherence         # noqa: E402
import module_manager           # noqa: E402
import issue_author             # noqa: E402
import issue_gate               # noqa: E402
import issue_conformance_ci as icc   # noqa: E402
import quiet_call                     # noqa: E402  (capture a demo/CLI walkthrough's stdout so it can't bury the suite summary)

WORKFLOW_REL = ".github/workflows/engine-issue-conformance.yml"

CONFORMING = issue_author.render_engine_issue_body(what_this_is="a demo item", whats_next="nothing to do")
FREE_TEXT = "just some free text with no contract markers at all"
ENGINE = [{"name": "engine"}]
ENGINE_FLAGGED = [{"name": "engine"}, {"name": icc.NEEDS_REAUTHORING_LABEL}]


class _Recorder:
    """A scripted GitHub transport that records calls and returns canned (status, json) by a per-test rule —
    so the REAL client/reconcile logic runs with no network."""

    def __init__(self, rule):
        self.calls = []
        self._rule = rule

    def __call__(self, method, path, body=None):
        self.calls.append((method, path, body))
        return self._rule(method, path, body)

    def methods_paths(self):
        return [(m, p) for m, p, _ in self.calls]


def _ok_rule(label_exists=True, comments=None):
    """The all-green rule the reconcile-path tests use: label-existence per `label_exists`, list-comments
    returns `comments`, every write succeeds."""
    comments = comments or []

    def rule(method, path, body=None):
        if "/comments" in path:
            return (200, list(comments)) if method == "GET" else (201, {"id": 1})
        if "/issues/" in path and "/labels" in path:
            return 200, []
        if path.endswith("/labels"):
            return 201, {}
        if "/labels/" in path:
            return (200 if label_exists else 404), None
        return 200, None
    return rule


class TestWorkflowIsEngineOwnedTraveler(unittest.TestCase):
    """The workflow is a FOUNDATION_INFRA member, so it travels on upgrade (FOUNDATION_CODE) and is owned in
    CODEOWNERS (foundation_infra_paths) — the same treatment as the other engine workflows (test_audit_prep)."""

    def test_workflow_is_present_in_the_tree(self):
        self.assertTrue(os.path.isfile(os.path.join(validate.ROOT, WORKFLOW_REL)),
                        f"{WORKFLOW_REL} must exist")

    def test_is_a_foundation_infra_member(self):
        self.assertIn(WORKFLOW_REL, module_coherence.FOUNDATION_INFRA)

    def test_travels_on_upgrade_via_foundation_code(self):
        self.assertIn(WORKFLOW_REL, module_manager.FOUNDATION_CODE)

    def test_renders_into_codeowners_via_foundation_infra_paths(self):
        owned = module_coherence.foundation_infra_paths()
        self.assertIn(WORKFLOW_REL, owned)
        self.assertFalse(any("*" in p for p in owned), "paths are concrete, never bare globs")


class TestConformancePredicate(unittest.TestCase):
    """Conformance is the single-source predicate over issue_gate.CONTRACT_MARKERS — drop any marker and a
    body stops conforming, so the CI net and the in-session gate can never disagree."""

    def test_helper_output_conforms(self):
        self.assertTrue(icc._is_conforming(CONFORMING))

    def test_free_text_does_not_conform(self):
        self.assertFalse(icc._is_conforming(FREE_TEXT))

    def test_each_marker_is_load_bearing(self):
        for marker in issue_gate.CONTRACT_MARKERS:
            self.assertFalse(icc._is_conforming(CONFORMING.replace(marker, "")),
                             f"removing {marker!r} must break conformance")


class TestSkeletonComment(unittest.TestCase):
    """The posted comment is a STATIC template: it carries the dedup marker and the contract skeleton, LEADS
    with a plain operator line, and structurally cannot echo issue content (it takes no arguments)."""

    def test_carries_dedup_marker_and_every_contract_marker(self):
        body = icc.skeleton_comment()
        self.assertIn(icc.COMMENT_MARKER, body)
        for marker in issue_gate.CONTRACT_MARKERS:
            self.assertIn(marker, body)

    def test_leads_with_the_plain_no_chore_line(self):
        self.assertIn("Nothing for you to do", icc.skeleton_comment())

    def test_is_static_takes_no_issue_argument(self):
        import inspect
        self.assertEqual(len(inspect.signature(icc.skeleton_comment).parameters), 0,
                         "skeleton_comment must take no args — it cannot echo attacker-controlled issue text")


class TestReconcileFlags(unittest.TestCase):
    """A non-conforming engine Issue is flagged idempotently: the label is ensured + added and exactly one
    advisory comment is posted; a re-fire over an already-commented Issue posts no second comment."""

    def _client(self, rule):
        self.rec = _Recorder(rule)
        return icc.IssueConformanceClient("o/r", "tok", transport=self.rec)

    def test_flags_with_one_comment(self):
        client = self._client(_ok_rule(label_exists=True, comments=[]))
        action = icc.reconcile({"number": 1, "labels": ENGINE, "body": FREE_TEXT}, client)
        posts = [(m, p) for m, p in self.rec.methods_paths() if m == "POST"]
        self.assertEqual(action, "flagged")
        self.assertEqual(sum(1 for m, p in posts if p.endswith("/comments")), 1)
        self.assertEqual(sum(1 for m, p in posts if "/issues/" in p and p.endswith("/labels")), 1)

    def test_re_fire_does_not_double_comment(self):
        client = self._client(_ok_rule(comments=[{"body": icc.COMMENT_MARKER + "\nprior"}]))
        icc.reconcile({"number": 1, "labels": ENGINE_FLAGGED, "body": FREE_TEXT}, client)
        self.assertFalse(any(m == "POST" and p.endswith("/comments") for m, p in self.rec.methods_paths()))

    def test_label_not_re_added_when_already_present(self):
        client = self._client(_ok_rule(comments=[{"body": icc.COMMENT_MARKER}]))
        icc.reconcile({"number": 1, "labels": ENGINE_FLAGGED, "body": FREE_TEXT}, client)
        self.assertFalse(any(m == "POST" and "/issues/" in p and p.endswith("/labels")
                             for m, p in self.rec.methods_paths()))

    def test_creates_label_when_absent(self):
        client = self._client(_ok_rule(label_exists=False, comments=[]))
        icc.reconcile({"number": 1, "labels": ENGINE, "body": FREE_TEXT}, client)
        self.assertTrue(any(m == "POST" and p == "/repos/o/r/labels" for m, p in self.rec.methods_paths()))


class TestReconcileClears(unittest.TestCase):
    """A conform-after-edit removes the label (never a lingering chore); a conforming, unflagged Issue is a
    pure no-op (no GitHub call at all)."""

    def test_conform_after_edit_removes_label(self):
        rec = _Recorder(_ok_rule())
        client = icc.IssueConformanceClient("o/r", "tok", transport=rec)
        action = icc.reconcile({"number": 1, "labels": ENGINE_FLAGGED, "body": CONFORMING}, client)
        self.assertEqual(action, "cleared")
        self.assertTrue(any(m == "DELETE" and "/labels/" in p for m, p in rec.methods_paths()))

    def test_conforming_unflagged_is_pure_noop(self):
        rec = _Recorder(_ok_rule())
        client = icc.IssueConformanceClient("o/r", "tok", transport=rec)
        action = icc.reconcile({"number": 1, "labels": ENGINE, "body": CONFORMING}, client)
        self.assertEqual(action, "conforming")
        self.assertEqual(rec.calls, [])


class TestEngineIssueScope(unittest.TestCase):
    """engine_issue_or_none keys on a real `engine` label and a numeric id; everything else is out of scope
    (the caller no-ops before any GitHub call)."""

    def test_engine_labelled_is_in_scope(self):
        issue = icc.engine_issue_or_none({"issue": {"number": 3, "labels": ENGINE, "body": FREE_TEXT}})
        self.assertIsNotNone(issue)

    def test_non_engine_label_is_out_of_scope(self):
        self.assertIsNone(icc.engine_issue_or_none({"issue": {"number": 3, "labels": [{"name": "bug"}], "body": FREE_TEXT}}))

    def test_no_labels_is_out_of_scope(self):
        self.assertIsNone(icc.engine_issue_or_none({"issue": {"number": 3, "labels": [], "body": FREE_TEXT}}))

    def test_malformed_events_are_out_of_scope(self):
        for event in ({"issue": None}, {"issue": {"labels": ENGINE}}, {}, None, "not-a-dict"):
            self.assertIsNone(icc.engine_issue_or_none(event), f"{event!r} must be out of scope")


class TestClientRestOps(unittest.TestCase):
    """The five REST ops use the correct verbs/paths/bodies and honour the fail contract (a real API failure
    raises; a tolerated 404-on-remove does not)."""

    def test_ensure_label_creates_when_absent(self):
        rec = _Recorder(lambda m, p, b=None: (404, None) if m == "GET" else (201, {}))
        icc.IssueConformanceClient("o/r", "tok", transport=rec).ensure_label("needs-reauthoring", "d4c5f9", "d")
        self.assertEqual(rec.methods_paths()[0], ("GET", "/repos/o/r/labels/needs-reauthoring"))
        self.assertIn(("POST", "/repos/o/r/labels"), rec.methods_paths())

    def test_ensure_label_noop_when_present(self):
        rec = _Recorder(lambda m, p, b=None: (200, None))
        icc.IssueConformanceClient("o/r", "tok", transport=rec).ensure_label("needs-reauthoring", "d4c5f9", "d")
        self.assertNotIn("POST", [m for m, _ in rec.methods_paths()])

    def test_ensure_label_raises_on_server_error(self):
        rec = _Recorder(lambda m, p, b=None: (500, None))
        with self.assertRaises(icc.DegradedWriteError):
            icc.IssueConformanceClient("o/r", "tok", transport=rec).ensure_label("x", "y", "z")

    def test_add_label_posts_to_issue_labels(self):
        rec = _Recorder(lambda m, p, b=None: (200, []))
        icc.IssueConformanceClient("o/r", "tok", transport=rec).add_label(7, "needs-reauthoring")
        self.assertEqual(rec.calls, [("POST", "/repos/o/r/issues/7/labels", {"labels": ["needs-reauthoring"]})])

    def test_remove_label_tolerates_absent(self):
        rec = _Recorder(lambda m, p, b=None: (404, None))   # label wasn't on the issue — a no-op, not an error
        icc.IssueConformanceClient("o/r", "tok", transport=rec).remove_label(7, "needs-reauthoring")
        self.assertEqual(rec.calls, [("DELETE", "/repos/o/r/issues/7/labels/needs-reauthoring", None)])

    def test_label_name_is_url_encoded_in_the_path(self):
        # the label is an operator-picked build-spec leaf that could carry a space (e.g. `engine: needs
        # formatting`); it must be percent-encoded into the URL path, never interpolated raw.
        rec = _Recorder(lambda m, p, b=None: (200, []))
        icc.IssueConformanceClient("o/r", "tok", transport=rec).remove_label(7, "needs reauthoring")
        self.assertEqual(rec.calls[0][1], "/repos/o/r/issues/7/labels/needs%20reauthoring")

    def test_remove_label_raises_on_server_error(self):
        rec = _Recorder(lambda m, p, b=None: (500, None))
        with self.assertRaises(icc.DegradedWriteError):
            icc.IssueConformanceClient("o/r", "tok", transport=rec).remove_label(7, "needs-reauthoring")

    def test_list_comments_pages_to_exhaustion(self):
        pages = {1: [{"id": i} for i in range(100)], 2: [{"id": 100}]}
        rec = _Recorder(lambda m, p, b=None: (200, pages[int(p.split("&page=")[1])]))
        comments = icc.IssueConformanceClient("o/r", "tok", transport=rec).list_comments(7)
        self.assertEqual(len(comments), 101)
        self.assertEqual(len(rec.calls), 2, "must request the second page")

    def test_list_comments_raises_on_error(self):
        rec = _Recorder(lambda m, p, b=None: (502, None))
        with self.assertRaises(icc.DegradedWriteError):
            icc.IssueConformanceClient("o/r", "tok", transport=rec).list_comments(7)

    def test_post_comment_raises_on_error(self):
        rec = _Recorder(lambda m, p, b=None: (403, None))
        with self.assertRaises(icc.DegradedWriteError):
            icc.IssueConformanceClient("o/r", "tok", transport=rec).post_comment(7, "hi")


class TestReconcilePropagatesFailure(unittest.TestCase):
    """A genuine API failure mid-reconcile propagates (→ the workflow goes red), never a silent success."""

    def test_add_label_failure_propagates(self):
        def rule(m, p, b=None):
            if "/issues/" in p and "/labels" in p and m == "POST":
                return 500, None
            return _ok_rule(comments=[])(m, p, b)
        client = icc.IssueConformanceClient("o/r", "tok", transport=_Recorder(rule))
        with self.assertRaises(icc.DegradedWriteError):
            icc.reconcile({"number": 1, "labels": ENGINE, "body": FREE_TEXT}, client)


class TestRunFailContract(unittest.TestCase):
    """_run reads the event from $GITHUB_EVENT_PATH and applies the safety-net-not-a-gate fail contract:
    no/partial event or a non-engine Issue → quiet exit 0; an engine Issue with no token → exit 1 (the net's
    own breakage is visible). These paths reach no network."""

    def _env(self, **overrides):
        keys = ("GITHUB_EVENT_PATH", "GITHUB_TOKEN", "GITHUB_REPOSITORY")
        saved = {k: os.environ.get(k) for k in keys}

        def restore():
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        self.addCleanup(restore)
        for k in keys:
            os.environ.pop(k, None)
        for k, v in overrides.items():
            if v is not None:
                os.environ[k] = v

    def _event_file(self, event) -> str:
        fd, path = tempfile.mkstemp(suffix=".json")
        self.addCleanup(os.remove, path)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(event, fh)
        return path

    def test_no_event_exits_zero(self):
        self._env()  # GITHUB_EVENT_PATH unset
        self.assertEqual(quiet_call.run(icc.main, []), 0)

    def test_non_engine_issue_exits_zero(self):
        path = self._event_file({"issue": {"number": 1, "labels": [{"name": "bug"}], "body": FREE_TEXT}})
        self._env(GITHUB_EVENT_PATH=path, GITHUB_TOKEN="tok", GITHUB_REPOSITORY="o/r")
        self.assertEqual(quiet_call.run(icc.main, []), 0)

    def test_engine_issue_without_token_exits_one(self):
        path = self._event_file({"issue": {"number": 1, "labels": ENGINE, "body": FREE_TEXT}})
        self._env(GITHUB_EVENT_PATH=path)  # engine-labelled but no token/repo → visible failure
        self.assertEqual(quiet_call.run(icc.main, []), 1)

    def test_conforming_engine_issue_exits_zero_without_network(self):
        # a conforming, unflagged engine Issue makes reconcile a pure no-op → no transport call → no network,
        # so _run returns 0 even with a dummy token.
        path = self._event_file({"issue": {"number": 1, "labels": ENGINE, "body": CONFORMING}})
        self._env(GITHUB_EVENT_PATH=path, GITHUB_TOKEN="dummy", GITHUB_REPOSITORY="o/r")
        self.assertEqual(quiet_call.run(icc.main, []), 0)

    def test_engine_issue_api_failure_exits_one(self):
        # the fail-LOUD contract: a token-present engine Issue whose GitHub write fails mid-reconcile must
        # exit non-zero (the net's own breakage is visible), never a silent pass. Guards _run's
        # `except DegradedWriteError -> return 1` join, which the other cases never drive through a raise.
        path = self._event_file({"issue": {"number": 1, "labels": ENGINE, "body": FREE_TEXT}})
        self._env(GITHUB_EVENT_PATH=path, GITHUB_TOKEN="tok", GITHUB_REPOSITORY="o/r")
        with mock.patch.object(icc.IssueConformanceClient, "_http",
                               lambda self, method, path, body=None: (500, None)):
            self.assertEqual(quiet_call.run(icc.main, []), 1)


class TestDemo(unittest.TestCase):
    def test_demo_self_check_passes(self):
        self.assertEqual(quiet_call.run(icc.main, ["demo"]), 0)


if __name__ == "__main__":
    unittest.main()
