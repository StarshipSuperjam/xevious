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


if __name__ == "__main__":
    unittest.main()
