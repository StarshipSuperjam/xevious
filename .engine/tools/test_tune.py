"""Tests for the `/engine-tune` tool.

Verifies: the effective value is the shipped default with the operator override merged per-key (and the
default alone when there is no override); eligibility excludes attention's structural keys and includes the
threshold policies' keys; validate_value refuses a fixed (structural) setting with the pinned plain sentence,
an unknown setting, and a non-number value (a bool is not a number); write_override creates the file, merges,
and preserves every other saved setting; set_value writes only after validation passes, opens a reviewed PR
through the INJECTED opener (faked — no real commit), saves-without-PR on request, and degrades when the
opener fails. The demo runs.
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tune  # noqa: E402


def _fake_opener(branch, title, body, paths):
    """A practice-run stand-in for the real git+PR boundary — records the call, opens nothing."""
    _fake_opener.calls.append({"branch": branch, "title": title, "body": body, "paths": paths})
    return {"number": 7, "html_url": "https://example.test/pull/7"}


_fake_opener.calls = []


class TestEffectiveAndEligibility(unittest.TestCase):
    def test_effective_without_override_is_the_default(self):
        self.assertEqual(tune.effective("triage-threshold"), tune.default_values("triage-threshold"))

    def test_effective_merges_the_override_per_key(self):
        eff = tune.effective("triage-threshold", {"persistence": 99})
        self.assertEqual(eff["persistence"], 99)
        self.assertEqual(eff["auto_resolve"], tune.default_values("triage-threshold")["auto_resolve"])

    def test_eligible_excludes_attention_structural_keys(self):
        eligible = tune.eligible_keys("attention")
        self.assertIn("budget_orientation", eligible)
        self.assertNotIn("precedence_blocking_debt", eligible, "precedence is a fixed structural law")
        self.assertNotIn("trim_orientation", eligible, "trim order is a fixed structural law")

    def test_threshold_policy_has_no_structural_keys(self):
        self.assertEqual(tune.structural_keys("triage-threshold"), set())
        self.assertEqual(sorted(tune.eligible_keys("triage-threshold")),
                         sorted(tune.default_values("triage-threshold")))


class TestValidateValue(unittest.TestCase):
    def test_structural_key_refused_with_pinned_sentence(self):
        ok, msg = tune.validate_value("attention", "precedence_blocking_debt", 1)
        self.assertFalse(ok)
        self.assertEqual(msg, tune._REFUSE_STRUCTURAL)

    def test_unknown_setting_refused(self):
        ok, msg = tune.validate_value("triage-threshold", "made_up_setting", 5)
        self.assertFalse(ok)
        self.assertIn("don't have a setting", msg)

    def test_non_number_refused(self):
        ok, msg = tune.validate_value("triage-threshold", "persistence", "lots")
        self.assertFalse(ok)
        self.assertIn("number", msg)

    def test_bool_is_not_a_number(self):
        ok, _msg = tune.validate_value("triage-threshold", "persistence", True)
        self.assertFalse(ok, "a bool must not pass as a number")

    def test_infinity_and_not_a_number_are_refused(self):
        # They survive float() and json.dumps (as the non-standard `Infinity`/`NaN` literals), so without this
        # they save cleanly and then quietly break the setting they tune. Concretely, on the debt-blocking bar:
        # an endless bar defers even the class that must never be deferred (a safety check that could not run),
        # and "not a number" compares false against everything, so it blocks what it should let past.
        for value in (float("inf"), float("-inf"), float("nan")):
            ok, msg = tune.validate_value("attention", "debt_blocking_threshold", value)
            self.assertFalse(ok, f"{value} was accepted as a dial the engine can measure against")
            self.assertIn("number", msg)

    def test_the_refusal_covers_every_setting_not_just_the_one_that_exposed_it(self):
        for policy, key in (("triage-threshold", "persistence"), ("attention", "weight_recency")):
            ok, _msg = tune.validate_value(policy, key, float("inf"))
            self.assertFalse(ok, f"{policy}.{key} accepted an endless value")

    def test_valid_value_accepted(self):
        ok, msg = tune.validate_value("triage-threshold", "persistence", 5)
        self.assertTrue(ok)
        self.assertEqual(msg, "")


class TestWriteOverride(unittest.TestCase):
    def test_write_creates_and_merges_preserving_others(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "operator-overrides.json")
            tune.write_override("triage-threshold", "persistence", 5, path=p)
            tune.write_override("triage-threshold", "auto_resolve", 1, path=p)
            tune.write_override("attention", "budget_orientation", 0.4, path=p)
            with open(p, encoding="utf-8") as fh:
                data = json.load(fh)
            self.assertEqual(data["triage-threshold"], {"persistence": 5, "auto_resolve": 1},
                             "the second write preserves the first")
            self.assertEqual(data["attention"], {"budget_orientation": 0.4},
                             "a different policy's slice is preserved alongside")


class TestSetValue(unittest.TestCase):
    def setUp(self):
        _fake_opener.calls = []

    def test_invalid_change_writes_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "operator-overrides.json")
            res = tune.set_value("attention", "precedence_blocking_debt", 1, override_path=p,
                                 opener=_fake_opener)
            self.assertFalse(res["ok"])
            self.assertEqual(res["message"], tune._REFUSE_STRUCTURAL)
            self.assertFalse(os.path.exists(p), "a refused change must never touch the file")
            self.assertEqual(_fake_opener.calls, [], "a refused change opens no pull request")

    def test_valid_change_writes_and_opens_a_pull_request(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "operator-overrides.json")
            res = tune.set_value("triage-threshold", "persistence", 5, override_path=p, opener=_fake_opener)
            self.assertTrue(res["ok"])
            self.assertEqual(res["message"], tune._CONFIRM)
            self.assertEqual(res["pr"]["number"], 7)
            self.assertTrue(os.path.exists(p), "the change is saved")
            self.assertEqual(len(_fake_opener.calls), 1, "exactly one pull request is opened")

    def test_no_pr_saves_without_opening(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "operator-overrides.json")
            res = tune.set_value("triage-threshold", "persistence", 5, override_path=p, open_pr=False)
            self.assertTrue(res["ok"])
            self.assertIsNone(res["pr"])
            self.assertTrue(os.path.exists(p))

    def test_opener_failure_degrades_to_saved_not_lost(self):
        def boom(branch, title, body, paths):
            raise RuntimeError("network down")
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "operator-overrides.json")
            res = tune.set_value("triage-threshold", "persistence", 5, override_path=p, opener=boom)
            self.assertTrue(res["ok"], "the value was saved even though the PR could not open")
            self.assertIsNone(res["pr"])
            self.assertIn("could not be opened", res["message"])
            self.assertTrue(os.path.exists(p))


class TestCLIAndDemo(unittest.TestCase):
    def test_show_prints_the_eligible_settings(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = tune.main(["show", "triage-threshold"])
        self.assertEqual(rc, 0)
        self.assertIn("persistence", buf.getvalue())

    def test_demo_runs_and_narrates(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = tune.main(["demo"])
        self.assertEqual(rc, 0)
        text = buf.getvalue()
        self.assertIn("pull request", text.lower())
        self.assertIn("budget_orientation", text, "the demo shows the live attention read change")


class TestOpenTunePrPushRetry(unittest.TestCase):
    """The real _open_tune_pr git+PR boundary: it rides out a transient missing-origin on the push (#704), and
    on a PERSISTENT git-step or POST failure it raises a DIAGNOSABLE, phase-aware RuntimeError (#874, mirroring
    module_manager's #672 opener) — naming the failed step, surfacing git's/GitHub's own reason (never the
    token), and giving the finish-by-hand recourse. Every tune test injects a fake opener, so this real path is
    exercised only here — driven directly with git (subprocess.run), the wait (time.sleep), and the PR POST
    (urlopen) faked, and repo=/token= passed so boot is never consulted."""

    def test_a_transient_push_failure_is_retried_and_the_pull_request_opens(self):
        import subprocess
        seen = {"push": 0, "checkout": 0}

        def flaky(args, **kw):
            if "push" in args:
                seen["push"] += 1
                if seen["push"] < 2:                          # fail once (the blip), then self-heal
                    raise subprocess.CalledProcessError(1, args, stderr=b"fatal: could not read from remote\n")
            if "checkout" in args:
                seen["checkout"] += 1
            return None
        resp = mock.MagicMock()
        resp.read.return_value = json.dumps({"number": 4}).encode()
        resp.__enter__.return_value = resp
        resp.__exit__.return_value = False
        with mock.patch("subprocess.run", side_effect=flaky), \
             mock.patch("time.sleep") as slept, \
             mock.patch("urllib.request.urlopen", side_effect=lambda req, timeout=None: resp):
            out = tune._open_tune_pr(branch="engine-tune-x", title="t", body="b", paths=["p"],
                                     repo="acme/widget", token="secret-token-xyz")
        self.assertEqual(out["number"], 4)                    # recovered -> the pull request opened
        self.assertEqual(seen["push"], 2)                     # one retry
        self.assertEqual(seen["checkout"], 1)                 # checkout is NOT retried
        slept.assert_called_once()

    def test_a_persistent_push_failure_is_diagnosable_and_keeps_the_branch(self):
        # #874: a persistent push failure exhausts the bound and raises a DIAGNOSABLE RuntimeError — names the
        # step, surfaces git's stderr, tells the operator the branch holds their saved change (in effect, as a
        # successful open leaves it), and gives the finish-by-hand recourse. It NEVER checks out another branch
        # (that would revert the not-yet-merged setting) and NEVER tells them to delete the branch they are on.
        import subprocess
        calls = []
        pushes = {"n": 0}

        def fail_push(args, **kw):
            calls.append(list(args))
            if "push" in args:
                pushes["n"] += 1
                raise subprocess.CalledProcessError(1, args, stderr=b"fatal: Authentication failed\n")
            return None
        with mock.patch("subprocess.run", side_effect=fail_push), \
             mock.patch("time.sleep"), \
             mock.patch("urllib.request.urlopen", side_effect=AssertionError("POST must not be reached")):
            with self.assertRaises(RuntimeError) as ctx:
                tune._open_tune_pr(branch="engine-tune-x", title="t", body="b", paths=["p"],
                                   repo="acme/widget", token="secret-token-xyz")
        msg = str(ctx.exception)
        self.assertEqual(pushes["n"], tune._ORIGIN_RETRY_ATTEMPTS)  # the push was bounded-retried
        # NEVER checks out another branch — a plain `git checkout <base>` would revert the saved setting off the
        # working tree; the only checkout is the initial `checkout -B`:
        self.assertFalse(any(c[:2] == ["git", "checkout"] and "-B" not in c for c in calls),
                         "must not check out another branch — that would revert the just-saved setting")
        self.assertIn("git push", msg)                        # names the failed step
        self.assertIn("Authentication failed", msg)           # surfaces git's real stderr (the reason)
        self.assertIn("was created", msg)                     # the branch holds the change
        self.assertIn("not lost", msg)                        # the setting is safe (in effect on the branch)
        self.assertNotIn("git branch -D", msg)                # NEVER advise deleting the branch they are standing on
        self.assertIn("gh pr create", msg)                    # the finish-by-hand recourse
        self.assertNotIn("secret-token-xyz", msg)             # the token NEVER surfaces
        self.assertNotIn("Bearer", msg)

    def test_a_git_error_with_an_embedded_credential_is_redacted(self):
        # #877: git writes the remote URL into push errors; an HTTPS remote can embed a token in its userinfo.
        # The surfaced message must redact the credential (host + reason preserved). Mirrors module_manager's
        # opener, which carries the identical _redact_credentials copy.
        import subprocess

        def fail_push(args, **kw):
            if "push" in args:
                raise subprocess.CalledProcessError(
                    128, args,
                    stderr=b"fatal: unable to access "
                           b"'https://x-access-token:ghs_SECRETTOKEN@github.com/acme/widget.git/': 403\n")
            return None
        with mock.patch("subprocess.run", side_effect=fail_push), \
             mock.patch("time.sleep"), \
             mock.patch("urllib.request.urlopen", side_effect=AssertionError("POST must not be reached")):
            with self.assertRaises(RuntimeError) as ctx:
                tune._open_tune_pr(branch="engine-tune-x", title="t", body="b", paths=["p"],
                                   repo="acme/widget", token="secret-token-xyz")
        msg = str(ctx.exception)
        self.assertNotIn("ghs_SECRETTOKEN", msg)              # the embedded git credential is redacted
        self.assertIn("***@github.com", msg)                  # ...the host survives
        self.assertIn("github.com/acme/widget", msg)          # ...and the useful part of git's reason remains
        self.assertNotIn("secret-token-xyz", msg)

    def test_a_checkout_failure_is_diagnosable_and_not_retried(self):
        # #874: a genuine `checkout -B` failure (e.g. the branch is checked out in another worktree) raises a
        # DIAGNOSABLE RuntimeError naming the step, confirms the setting is untouched, advises a re-run — and is
        # NOT retried. `-B` (create-or-reset) means a leftover branch no longer collides, so there is no
        # delete-the-branch-you-are-on dead-end.
        import subprocess
        checkouts = {"n": 0}

        def fail_checkout(args, **kw):
            if "checkout" in args:
                checkouts["n"] += 1
                raise subprocess.CalledProcessError(128, args,
                                                    stderr=b"fatal: 'engine-tune-x' is already used by worktree\n")
            return None
        with mock.patch("subprocess.run", side_effect=fail_checkout), \
             mock.patch("time.sleep"), \
             mock.patch("urllib.request.urlopen", side_effect=AssertionError("POST must not be reached")):
            with self.assertRaises(RuntimeError) as ctx:
                tune._open_tune_pr(branch="engine-tune-x", title="t", body="b", paths=["p"],
                                   repo="acme/widget", token="secret-token-xyz")
        msg = str(ctx.exception)
        self.assertEqual(checkouts["n"], 1)                   # tried once, not retried
        self.assertIn("checkout -B engine-tune-x", msg)       # names the failed step
        self.assertIn("already used by worktree", msg)        # surfaces git's real stderr
        self.assertIn("saved setting is untouched", msg)      # the operator's value is safe
        self.assertIn("re-run the setting", msg)              # the recovery
        self.assertNotIn("git branch -D", msg)                # no delete-the-branch dead-end with -B
        self.assertNotIn("secret-token-xyz", msg)

    def test_a_post_failure_after_push_surfaces_githubs_reason(self):
        # #874: git all succeeds but the PR POST fails — the branch IS pushed, so the recovery is to open the PR
        # by hand. The message must surface GitHub's OWN reason (not just the HTTP code) and never leak the token.
        import urllib.error

        def run(args, **kw):
            return None
        body = json.dumps({"message": "Validation Failed",
                           "errors": [{"message": "A pull request already exists for acme:engine-tune-x."}]}).encode()

        def http_error(req, timeout=None):
            raise urllib.error.HTTPError("https://api.github.com/repos/acme/widget/pulls", 422,
                                         "Unprocessable Entity", {}, io.BytesIO(body))
        with mock.patch("subprocess.run", side_effect=run), \
             mock.patch("time.sleep"), \
             mock.patch("urllib.request.urlopen", side_effect=http_error):
            with self.assertRaises(RuntimeError) as ctx:
                tune._open_tune_pr(branch="engine-tune-x", title="t", body="b", paths=["p"],
                                   repo="acme/widget", token="secret-token-xyz")
        msg = str(ctx.exception)
        self.assertIn("was pushed", msg)                      # the branch IS pushed (post-push failure)
        self.assertIn("422", msg)                             # GitHub's own HTTP code
        self.assertIn("A pull request already exists", msg)   # ...AND GitHub's own reason, not just the code
        self.assertIn("gh pr create", msg)                    # open it by hand
        self.assertIn("saved", msg)                           # the operator's setting is safe
        self.assertNotIn("secret-token-xyz", msg)
        self.assertNotIn("Bearer", msg)

    def test_a_malformed_error_body_still_diagnoses_without_crashing(self):
        # #874 hardening: an unexpected non-array `errors` (a proxy / WAF / future-API body) must NOT crash the
        # reason extractor — the operator must still get the diagnosable RuntimeError (never a raw TypeError). The
        # reason-reader guards `errors` being a list; the top-level message still surfaces.
        import urllib.error

        def run(args, **kw):
            return None
        body = json.dumps({"message": "Validation Failed", "errors": 42}).encode()   # errors is NOT a list

        def http_error(req, timeout=None):
            raise urllib.error.HTTPError("u", 422, "x", {}, io.BytesIO(body))
        with mock.patch("subprocess.run", side_effect=run), \
             mock.patch("time.sleep"), \
             mock.patch("urllib.request.urlopen", side_effect=http_error):
            with self.assertRaises(RuntimeError) as ctx:      # a RuntimeError, never a raw TypeError
                tune._open_tune_pr(branch="engine-tune-x", title="t", body="b", paths=["p"],
                                   repo="acme/widget", token="secret-token-xyz")
        msg = str(ctx.exception)
        self.assertIn("422", msg)
        self.assertIn("Validation Failed", msg)               # the top-level message still surfaces

    def test_a_no_op_commit_says_nothing_to_change_not_push_the_branch(self):
        # #874: setting a value already in effect makes `git commit` fail "nothing to commit" — a reason git
        # writes to STDOUT, not stderr, and exits 1. The message must surface that real reason (never a bare
        # "exit 1") and must NOT advise pushing the branch (which would open an empty, dead-end pull request).
        import subprocess

        def fake(args, **kw):
            if args[:2] == ["git", "commit"]:
                raise subprocess.CalledProcessError(1, args, output=b"nothing to commit, working tree clean\n",
                                                    stderr=b"")
            if args[:2] == ["git", "diff"] and "--cached" in args:
                return subprocess.CompletedProcess(args, 0)   # nothing staged
            return None
        with mock.patch("subprocess.run", side_effect=fake), \
             mock.patch("time.sleep"), \
             mock.patch("urllib.request.urlopen", side_effect=AssertionError("POST must not be reached")):
            with self.assertRaises(RuntimeError) as ctx:
                tune._open_tune_pr(branch="engine-tune-x", title="t", body="b", paths=["p"],
                                   repo="acme/widget", token="secret-token-xyz")
        msg = str(ctx.exception)
        self.assertIn("nothing to commit", msg)               # the real reason surfaces (from STDOUT), not "exit 1"
        self.assertNotIn("(exit 1)", msg)
        self.assertIn("nothing to change", msg.lower())       # correct diagnosis: the value already holds
        self.assertNotIn("gh pr create", msg)                 # NEVER advise opening an empty pull request
        self.assertNotIn("secret-token-xyz", msg)


if __name__ == "__main__":
    unittest.main()
