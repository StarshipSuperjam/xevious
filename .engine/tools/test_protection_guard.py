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

HERE = os.path.dirname(os.path.abspath(__file__))


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


if __name__ == "__main__":
    unittest.main()
