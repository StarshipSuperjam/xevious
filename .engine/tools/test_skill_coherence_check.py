"""Tests for the skill-set self-election guard — the live consumer that runs
validate.skill_coherence_findings over the present ENGINE skills and is wired as the engine/check/
skill-coherence custom/script CI rule. Verifies discovery + engine-only scoping, the leak-guard firing
on a planted operator-typed-without-flag skill while staying silent on a clean set and on un-prefixed
operator product skills, and that the check + demo CLI modes run.
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import skill_coherence_check as scc  # noqa: E402
import validate  # noqa: E402


def _write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


_OP_TYPED = ("---\nname: engine-start\ndescription: Start building.\ninvocation: operator-typed\n"
             "disable-model-invocation: true\n---\n\n## Steps\n\n1. Go.\n")
_OP_TYPED_NO_FLAG = ("---\nname: engine-bad\ndescription: A bad one.\ninvocation: operator-typed\n---\n\n"
                     "## Steps\n\n1. Go.\n")
_AUTO = ("---\nname: engine-auto\ndescription: An auto one.\n---\n\n## Steps\n\n1. Go.\n")


class TestEngineSkillsDiscovery(unittest.TestCase):
    def test_discovers_engine_prefixed_skills_only(self):
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, ".claude/skills/engine-start/SKILL.md"), _OP_TYPED)
            _write(os.path.join(d, ".claude/skills/my-product/SKILL.md"), _AUTO)  # un-prefixed → ignored
            names = sorted(s.get("name") for s in scc.engine_skills(root=d))
            self.assertEqual(names, ["engine-start"], "only engine-prefixed skills are governed")

    def test_injects_directory_name_when_frontmatter_omits_name(self):
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, ".claude/skills/engine-help/SKILL.md"),
                   "---\ndescription: List commands.\ninvocation: operator-typed\n"
                   "disable-model-invocation: true\n---\n\n## Steps\n\n1. Go.\n")
            self.assertEqual(scc.engine_skills(root=d)[0].get("name"), "engine-help",
                             "the typed name is the directory when frontmatter omits `name`")

    def test_legacy_command_filename_is_the_typed_name(self):
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, ".claude/commands/engine-legacy.md"),
                   "---\ndescription: A legacy command.\ninvocation: operator-typed\n"
                   "disable-model-invocation: true\n---\n\nbody\n")
            self.assertEqual(scc.engine_skills(root=d)[0].get("name"), "engine-legacy")


class TestSelfElectionGuard(unittest.TestCase):
    def test_clean_operator_typed_skill_no_finding(self):
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, ".claude/skills/engine-start/SKILL.md"), _OP_TYPED)
            findings = validate.skill_coherence_findings(scc.engine_skills(root=d), "hard", scc._MESSAGE)
            self.assertEqual(findings, [], "an operator-typed skill carrying the flag is clean")

    def test_operator_typed_without_flag_is_flagged(self):
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, ".claude/skills/engine-bad/SKILL.md"), _OP_TYPED_NO_FLAG)
            findings = validate.skill_coherence_findings(scc.engine_skills(root=d), "hard", scc._MESSAGE)
            self.assertEqual(len(findings), 1, "the self-election leak is caught")
            self.assertEqual(findings[0]["severity"], "hard")
            self.assertIn("engine-bad", findings[0]["message"])

    def test_un_prefixed_operator_skill_not_governed(self):
        with tempfile.TemporaryDirectory() as d:
            # an un-prefixed product skill that WOULD fail the guard is ignored (engine-only scoping)
            _write(os.path.join(d, ".claude/skills/my-skill/SKILL.md"), _OP_TYPED_NO_FLAG)
            findings = validate.skill_coherence_findings(scc.engine_skills(root=d), "hard", scc._MESSAGE)
            self.assertEqual(findings, [], "operator product skills are not engine-governed")

    def test_malformed_engine_skill_raises_fail_closed(self):
        # a malformed engine SKILL.md makes parsing RAISE, which propagates out of the script as a
        # non-zero exit → the custom/script runner turns that into a hard fail-closed finding (a broken
        # guard can never silently pass). Pin the raise this slice's contribution depends on.
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, ".claude/skills/engine-broken/SKILL.md"),
                   "---\ndescription: [unclosed\n---\n\n## Steps\n\n1. Go.\n")
            with self.assertRaises(Exception):
                scc.engine_skills(root=d)


class TestScriptModes(unittest.TestCase):
    def test_check_mode_emits_json_array_clean_on_real_repo(self):
        # main() with no args globs the REAL repo (validate.ROOT); the shipped engine commands are clean.
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = scc.main([])
        self.assertEqual(rc, 0)
        out = json.loads(buf.getvalue())
        self.assertIsInstance(out, list)
        self.assertEqual(out, [], "the shipped engine commands carry their operator-only flag")

    def test_demo_runs_and_narrates(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = scc.main(["demo"])
        self.assertEqual(rc, 0)
        text = buf.getvalue()
        self.assertIn("operator-only", text)
        self.assertIn("engine-start", text)


if __name__ == "__main__":
    unittest.main()
