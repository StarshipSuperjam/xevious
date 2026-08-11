"""Protection-detection guard: the local (no-token) note is a disclosed no-op.

The guard runs as a `custom/script` check. With no token it fails open with a soft "not checked here —
the real check runs in CI" note. That note is a disclosed not-applicable (#322): marked so the validator
collapses it away from actionable notes, never left to masquerade as the one note needing action. Run in a
subprocess with a scrubbed env so the no-token branch is deterministic and never touches the network."""
import json
import os
import subprocess
import sys
import unittest
import urllib.error
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import protection_guard  # noqa: E402
import repo_identity     # noqa: E402


class TestLocalNoteIsDisclosedNoop(unittest.TestCase):
    def _run_without_token(self) -> list:
        env = {k: v for k, v in os.environ.items()
               if k not in ("GITHUB_TOKEN", "GITHUB_REPOSITORY")}
        proc = subprocess.run([sys.executable, os.path.join(HERE, "protection_guard.py")],
                              cwd=HERE, env=env, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout)

    def test_local_no_token_note_is_marked_not_applicable(self):
        findings = self._run_without_token()
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["severity"], "soft")
        self.assertIn("Branch protection was not checked here", findings[0]["message"])
        self.assertIs(findings[0].get("not_applicable"), True)


class TestMainProbesTheResolvedBranch(unittest.TestCase):
    """The CI merge-gate script (`main()`, the literal check engine-ci runs with a token) probes the RESOLVED
    default branch — never a hard-coded 'main' — and URL-quotes it before the token-bearing rules API path."""

    def _probe(self, branch: str):
        seen = {}
        with mock.patch.dict(os.environ, {"GITHUB_REPOSITORY": "o/r", "GITHUB_TOKEN": "t"}, clear=False), \
             mock.patch.object(repo_identity, "resolve_default_branch", return_value=branch), \
             mock.patch.object(protection_guard, "resolve_tier", return_value="solo"), \
             mock.patch.object(protection_guard, "missing_floor", return_value=[]), \
             mock.patch.object(protection_guard, "get_json",
                               side_effect=lambda path, token, **kw: (seen.__setitem__("path", path) or [])):
            rc = protection_guard.main()
        return rc, seen.get("path")

    def test_probes_the_resolved_default_branch(self):
        rc, path = self._probe("master")
        self.assertEqual(rc, 0)
        self.assertEqual(path, "/repos/o/r/rules/branches/master")

    def test_url_quotes_a_slash_containing_branch(self):
        _, path = self._probe("release/1.0")
        self.assertEqual(path, "/repos/o/r/rules/branches/release%2F1.0")


class TestPlatformForbidsRulesets(unittest.TestCase):
    """The single load-bearing predicate: only GitHub's genuine plan-limitation 403 counts — every transient
    or permission 403 stays a hard failure, so a stale/forged posture cannot ride a rate-limit blip to a soft."""

    def test_plan_limitation_message_matches(self):
        for msg in ("Upgrade to GitHub Team to enable this feature.",
                    "Upgrade to GitHub Enterprise to enable this feature.",
                    "Your rulesets won't be enforced on this private repository until you upgrade this "
                    "organization account to GitHub Team."):
            self.assertTrue(protection_guard.platform_forbids_rulesets(403, {"message": msg}), msg)

    def test_rate_limit_403_is_excluded_by_message(self):
        self.assertFalse(protection_guard.platform_forbids_rulesets(
            403, {"message": "You have exceeded a secondary rate limit. Please wait a few minutes."}))
        self.assertFalse(protection_guard.platform_forbids_rulesets(
            403, {"message": "API rate limit exceeded for user."}))

    def test_rate_limit_403_is_excluded_by_header(self):
        # A throttle whose body somehow said 'upgrade' is still excluded by its rate-limit headers.
        self.assertFalse(protection_guard.platform_forbids_rulesets(
            403, {"message": "Upgrade to GitHub Team feature."}, {"Retry-After": "60"}))
        self.assertFalse(protection_guard.platform_forbids_rulesets(
            403, {"message": "Upgrade to GitHub Team feature."}, {"X-RateLimit-Remaining": "0"}))

    def test_ordinary_not_admin_403_does_not_match(self):
        self.assertFalse(protection_guard.platform_forbids_rulesets(
            403, {"message": "Resource not accessible by personal access token"}))

    def test_non_403_never_matches(self):
        self.assertFalse(protection_guard.platform_forbids_rulesets(404, {"message": "Upgrade to GitHub Team"}))
        self.assertFalse(protection_guard.platform_forbids_rulesets(500, {"message": "Upgrade to GitHub Team"}))

    def test_unreadable_body_does_not_match(self):
        # An unrecognizable/empty body fails toward HARD (the safe direction), never a false soften.
        self.assertFalse(protection_guard.platform_forbids_rulesets(403, {}))
        self.assertFalse(protection_guard.platform_forbids_rulesets(403, None))


class TestRecordedPosture(unittest.TestCase):
    """The posture reader honors only a well-formed unsupported-platform record; anything else reads as None
    (fail toward the hard check)."""

    def _write(self, tmp, manifest):
        with open(os.path.join(tmp, "engine.json"), "w", encoding="utf-8") as fh:
            json.dump(manifest, fh)

    def test_well_formed_posture_is_returned(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            self._write(tmp, {"protection_posture": {"status": "unsupported-platform",
                                                     "reason": "x", "operator_login": "me",
                                                     "recorded_on": "2026-08-08"}})
            posture = protection_guard.recorded_posture(engine_dir=tmp)
            self.assertIsNotNone(posture)
            self.assertEqual(posture["operator_login"], "me")

    def test_absent_or_wrong_status_reads_as_none(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            self._write(tmp, {"identity": "solo"})
            self.assertIsNone(protection_guard.recorded_posture(engine_dir=tmp))
            self._write(tmp, {"protection_posture": {"status": "something-else"}})
            self.assertIsNone(protection_guard.recorded_posture(engine_dir=tmp))

    def test_missing_manifest_reads_as_none(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(protection_guard.recorded_posture(engine_dir=tmp))


class TestMainPostureSoftening(unittest.TestCase):
    """main()'s soft/hard decision: soften ONLY on (recorded posture AND a live plan-limitation 403); every
    other outcome stays hard, and a read-success never softens regardless of a recorded posture."""

    def _http_error(self, code, message="", headers=None):
        import email.message
        import io
        hdrs = email.message.Message()
        for k, v in (headers or {}).items():
            hdrs[k] = v
        return urllib.error.HTTPError("https://api.github.com/x", code, message, hdrs,
                                      io.BytesIO(json.dumps({"message": message}).encode()))

    def _run(self, *, posture, get_json_side_effect, missing=None):
        captured = []
        with mock.patch.dict(os.environ, {"GITHUB_REPOSITORY": "o/r", "GITHUB_TOKEN": "t"}, clear=False), \
             mock.patch.object(repo_identity, "resolve_default_branch", return_value="main"), \
             mock.patch.object(protection_guard, "resolve_tier", return_value="solo"), \
             mock.patch.object(protection_guard, "recorded_posture", return_value=posture), \
             mock.patch.object(protection_guard, "missing_floor", return_value=(missing or [])), \
             mock.patch.object(protection_guard, "get_json", side_effect=get_json_side_effect), \
             mock.patch.object(protection_guard, "emit", side_effect=lambda f: captured.append(f) or 0):
            protection_guard.main()
        return captured[0]

    _POSTURE = {"status": "unsupported-platform", "reason": "plan can't host rulesets",
                "operator_login": "owner", "recorded_on": "2026-08-08"}

    def _raise(self, err):
        def _side(path, token, **kw):
            raise err
        return _side

    def test_posture_plus_plan_limitation_403_softens(self):
        findings = self._run(
            posture=self._POSTURE,
            get_json_side_effect=self._raise(self._http_error(403, "Upgrade to GitHub Team to enable this feature.")))
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["severity"], "soft")
        self.assertIn("isn't available on this repository's GitHub plan", findings[0]["message"])
        self.assertIn("2026-08-08", findings[0]["message"])

    def test_plan_limitation_403_without_posture_stays_hard(self):
        findings = self._run(
            posture=None,
            get_json_side_effect=self._raise(self._http_error(403, "Upgrade to GitHub Team to enable this feature.")))
        self.assertEqual(findings[0]["severity"], "hard")
        self.assertIn("could not be verified", findings[0]["message"])

    def test_transient_rate_limit_403_with_posture_stays_hard(self):
        findings = self._run(
            posture=self._POSTURE,
            get_json_side_effect=self._raise(self._http_error(403, "You have exceeded a secondary rate limit.")))
        self.assertEqual(findings[0]["severity"], "hard")

    def test_read_success_floor_missing_with_posture_stays_hard_and_nudges(self):
        findings = self._run(
            posture=self._POSTURE,
            get_json_side_effect=lambda path, token, **kw: [],
            missing=["a pull request is not required before merging"])
        self.assertEqual(findings[0]["severity"], "hard")
        self.assertIn("stale", findings[0]["message"])

    def test_non_list_200_fails_closed_hard(self):
        findings = self._run(
            posture=self._POSTURE,
            get_json_side_effect=lambda path, token, **kw: {"unexpected": "object"})
        self.assertEqual(findings[0]["severity"], "hard")
        self.assertIn("not in the expected form", findings[0]["message"])

    def test_read_success_floor_present_passes_clean(self):
        findings = self._run(
            posture=self._POSTURE,
            get_json_side_effect=lambda path, token, **kw: [{"type": "pull_request"}],
            missing=[])
        self.assertEqual(findings, [])

    def test_non_dict_list_200_fails_closed_without_crashing(self):
        # A 200 whose body is a list of NON-dict elements must NOT crash missing_floor's r.get("type") into an
        # uncaught exception (missing_floor runs outside the read's try) — the element guard fails it closed to
        # a hard finding. missing_floor is deliberately NOT mocked here, so a regression would raise, not pass.
        captured = []
        with mock.patch.dict(os.environ, {"GITHUB_REPOSITORY": "o/r", "GITHUB_TOKEN": "t"}, clear=False), \
             mock.patch.object(repo_identity, "resolve_default_branch", return_value="main"), \
             mock.patch.object(protection_guard, "resolve_tier", return_value="solo"), \
             mock.patch.object(protection_guard, "recorded_posture", return_value=None), \
             mock.patch.object(protection_guard, "get_json", return_value=[1, 2, "x"]), \
             mock.patch.object(protection_guard, "emit", side_effect=lambda f: captured.append(f) or 0):
            protection_guard.main()   # must not raise
        self.assertEqual(captured[0][0]["severity"], "hard")
        self.assertIn("not in the expected form", captured[0][0]["message"])


if __name__ == "__main__":
    unittest.main()
