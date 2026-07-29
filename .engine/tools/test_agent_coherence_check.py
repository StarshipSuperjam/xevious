"""Tests for the persona-set coherence guard — the live consumer that runs
validate.agent_coherence_findings over the present personas and is wired as the engine/check/
agent-coherence custom/script CI rule. Verifies discovery + name injection, the read-only write-lock
guard firing on a planted lockless read-only persona while staying silent on a clean set, and that the
check + demo CLI modes run on the real repo.
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import agent_coherence_check as acc  # noqa: E402
import validate  # noqa: E402


def _write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


_LOCKED_REVIEWER = ("---\nname: design-review-architecture\ndescription: Reviews the plan.\n"
                    "role: plan-review\nlens: architecture\nmodel-tier: judgment\n"
                    "permissions: read-only\noutput-contract: plan-review-finding.v1\n"
                    "disallowedTools: [Edit, Write, NotebookEdit, Bash]\n---\n\nbody\n")
_LOCKLESS_REVIEWER = ("---\nname: leaky-review\ndescription: Reviews the plan.\n"
                      "role: plan-review\nlens: architecture\nmodel-tier: judgment\n"
                      "permissions: read-only\noutput-contract: plan-review-finding.v1\n---\n\nbody\n")


class TestEngineAgentsDiscovery(unittest.TestCase):
    def test_discovers_personas_and_parses_frontmatter(self):
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, ".claude/agents/design-review-architecture.md"), _LOCKED_REVIEWER)
            agents = acc.engine_agents(root=d)
            self.assertEqual([a.get("name") for a in agents], ["design-review-architecture"])
            self.assertEqual(agents[0].get("disallowedTools"), ["Edit", "Write", "NotebookEdit", "Bash"])

    def test_injects_filename_stem_when_frontmatter_omits_name(self):
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, ".claude/agents/audit.md"),
                   "---\ndescription: Self-audit.\nrole: audit\nmodel-tier: judgment\n"
                   "permissions: read-only\noutput-contract: audit-finding.v1\n"
                   "disallowedTools: [Edit, Write, NotebookEdit]\n---\n\nbody\n")
            self.assertEqual(acc.engine_agents(root=d)[0].get("name"), "audit")


class TestReadOnlyWriteLockGuard(unittest.TestCase):
    def test_clean_locked_reviewer_no_finding(self):
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, ".claude/agents/design-review-architecture.md"), _LOCKED_REVIEWER)
            findings = validate.agent_coherence_findings(acc.engine_agents(root=d), "hard", acc._MESSAGE)
            self.assertEqual(findings, [], "a read-only persona that blocks the write tools is clean")

    def test_lockless_readonly_persona_is_flagged(self):
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, ".claude/agents/leaky-review.md"), _LOCKLESS_REVIEWER)
            findings = validate.agent_coherence_findings(acc.engine_agents(root=d), "hard", acc._MESSAGE)
            self.assertEqual(len(findings), 1, "the inherit-all read-only persona is caught")
            self.assertEqual(findings[0]["severity"], "hard")
            self.assertIn("leaky-review", findings[0]["message"])

    def test_malformed_persona_raises_fail_closed(self):
        # a malformed persona makes parsing RAISE, which propagates out of the script as a non-zero
        # exit → the custom/script runner turns that into a hard fail-closed finding.
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, ".claude/agents/broken.md"), "---\ndescription: [unclosed\n---\n\nbody\n")
            with self.assertRaises(Exception):
                acc.engine_agents(root=d)


class TestScriptModes(unittest.TestCase):
    def test_check_mode_emits_json_array_clean_on_real_repo(self):
        # main() with no args globs the REAL repo (validate.ROOT); the shipped personas are all locked.
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = acc.main([])
        self.assertEqual(rc, 0)
        out = json.loads(buf.getvalue())
        self.assertIsInstance(out, list)
        self.assertEqual(out, [], "every shipped read-only persona blocks the write tools")

    def test_demo_runs_and_narrates(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = acc.main(["demo"])
        self.assertEqual(rc, 0)
        text = buf.getvalue()
        self.assertIn("read-only", text)
        self.assertIn("RED", text)


if __name__ == "__main__":
    unittest.main()
