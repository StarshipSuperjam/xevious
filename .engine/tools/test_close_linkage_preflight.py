#!/usr/bin/env python3
"""Tests for close_linkage_preflight — the submit-time close-linkage consistency pre-flight (#361).

These lock the load-bearing behavior the deliverable gate attests:
  - the pure parse (body closes: same-repo vs cross-repo, the comma-trap leftover, occurrence counts; the
    deliberate line-leading close; the `Part of #N` read through the exact Scope/Out-of-scope headings; the
    commit-message closes);
  - the defang transformation: minimal keyword-only removal, byte-identical elsewhere, and the surface-not-guess
    fallback (a code-fenced/duplicate occurrence, or a not-honored occurrence, is never defanged);
  - classify: scope-contradiction surfaces; an accidental body close defangs+discloses; a deliberate-close or a
    commit-sourced close surfaces instead; comma-trap; cross-repo out-of-reach; a clean PR is silent;
  - the gh/git boundary (the injected subprocess seam): the `--json closingIssuesReferences` read, the graphql
    fallback, and the fail-closed RAISE -> the could-not-read line (never a false clean);
  - the dispatch (demo returns clean; the check verb; usage errors).

Run: uv run --directory .engine --frozen -- python -m unittest discover -s tools -p 'test_*.py' -b
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import close_linkage_preflight as clp  # noqa: E402


_SCOPE = "## Scope\n\n**Adds it.**\n\n- Part of #274\n\n## Out of scope\n\n- nothing\n"


def _runner(*, closing=None, body="", commits=None, fail=None):
    """The faked gh/git subprocess boundary (mirrors clp._fake_runner) — patch NOTHING global; inject this."""
    return clp._fake_runner(closing=closing, body=body, commits=commits, fail=fail)


# ---- the pure parse -----------------------------------------------------------------------------

class ParseTests(unittest.TestCase):
    def test_parse_close_runs_groups_by_keyword(self):
        runs = clp.parse_close_runs("Closes #1, #2 and later Fixes octo/o#3")
        self.assertEqual(runs, [[(None, 1), (None, 2)], [("octo/o", 3)]])

    def test_body_local_closes_same_repo_only(self):
        self.assertEqual(clp.body_local_closes("Closes #7 and fixes octo/o#9"), {7})

    def test_comma_trap_leftovers_only_when_head_honored(self):
        runs = clp.parse_close_runs("Closes #1, #2, #3")
        self.assertEqual(clp.comma_trap_leftovers(runs, honored_local={1}), [2, 3])   # head #1 honored
        self.assertEqual(clp.comma_trap_leftovers(runs, honored_local=set()), [])     # head not honored -> none

    def test_comma_trap_leftover_that_is_itself_honored_is_not_a_trap(self):
        runs = clp.parse_close_runs("Closes #1, #2")
        self.assertEqual(clp.comma_trap_leftovers(runs, honored_local={1, 2}), [])     # #2 will close -> no trap

    def test_deliberate_cross_close_is_line_leading_only(self):
        body = "Closes octo/o#9\n\nprose mentioning Closes octo/o#8 mid-sentence\n"
        self.assertEqual(clp.deliberate_cross_closes(body), ["octo/o#9"])   # #8 buried is NOT surfaced

    def test_word_boundary_does_not_match_discloses(self):
        self.assertEqual(clp.body_local_closes("This discloses #7 nothing"), set())

    def test_deliberate_close_is_line_leading_only(self):
        body = "Closes #40\n\nsome prose that mentions Closes #41 mid-sentence\n"
        self.assertEqual(clp.deliberate_closes(body), {40})   # #41 buried mid-line is NOT deliberate

    def test_part_of_read_from_scope_sections(self):
        self.assertEqual(clp.part_of_declarations(_SCOPE), {274})

    def test_part_of_comma_run(self):
        body = "## Scope\n\n- Part of #10, #11\n\n## Out of scope\n\n- x\n"
        self.assertEqual(clp.part_of_declarations(body), {10, 11})

    def test_part_of_ignored_outside_declared_sections(self):
        # A "Part of #5" in some other section is not a declaration the pre-flight reads.
        body = "## Notes\n\n- Part of #5\n"
        self.assertEqual(clp.part_of_declarations(body), set())

    def test_heading_rename_blinds_part_of_read_failsafe(self):
        # If the template heading is renamed, the Part-of read empties -> the fail-safe direction (surface, never
        # defang) follows, because classify() only defangs when Part-of is declared.
        body = "## Included\n\n- Part of #5\n"
        self.assertEqual(clp.part_of_declarations(body), set())

    def test_commit_will_close_first_of_run_honored_rest_are_trap(self):
        # A commit `Closes #1, #2` closes only #1 (honored); #2 is the comma-trap leftover.
        honored, trap = clp.commit_will_close(["feat\n\nCloses #1, #2", "chore\n\nResolves octo/o#9"])
        self.assertEqual(honored, {1})            # only the first same-repo ref of the run
        self.assertEqual(trap, [2])               # the trailer that silently stays open
        # a cross-repo-led run closes nothing and is not a trap
        self.assertEqual(clp.commit_will_close(["x\n\nCloses octo/o#9, #2"]), (set(), []))


# ---- the defang transformation ------------------------------------------------------------------

class DefangTests(unittest.TestCase):
    def test_minimal_removal_keeps_reference_byte_identical_elsewhere(self):
        body = _SCOPE + "\nThis work Closes #274 as it lands.\n"
        out = clp.defang_body(body, 274)
        self.assertIsNotNone(out)
        self.assertEqual(out, body.replace("Closes #274", "#274"))
        self.assertIn("#274", out)
        self.assertNotIn("Closes #274", out)

    def test_ambiguous_multiple_occurrences_is_not_defanged(self):
        body = "Closes #274 ... and again Closes #274\n"
        self.assertIsNone(clp.defang_body(body, 274))      # two occurrences -> surface, never guess

    def test_absent_number_is_not_defanged(self):
        self.assertIsNone(clp.defang_body("Closes #99\n", 274))

    def test_comma_run_occurrence_is_not_defanged(self):
        # A `Closes #274, #275` run is not a clean lone accidental close; it is left for the surface path.
        self.assertIsNone(clp.defang_body("Closes #274, #275\n", 274))

    def test_cross_repo_occurrence_is_not_defanged(self):
        self.assertIsNone(clp.defang_body("Closes octo/other#274\n", 274))


# ---- classify -----------------------------------------------------------------------------------

class ClassifyTests(unittest.TestCase):
    def test_accidental_body_close_defangs_and_discloses(self):
        body = _SCOPE + "\nThis work Closes #274 as it lands.\n"
        r = clp.classify(body=body, honored_local={274}, commit_honored=set())
        self.assertIsNotNone(r["defang"])
        self.assertEqual(r["defang"]["number"], 274)
        self.assertNotIn("Closes #274", r["defang"]["new_body"])
        self.assertTrue(any("removed an accidental" in ln for ln in r["lines"]))

    def test_deliberate_close_alongside_part_of_surfaces_not_defangs(self):
        body = "## Scope\n\n- Part of #274\n\n## Out of scope\n\n- x\n\nCloses #274\n"
        r = clp.classify(body=body, honored_local={274}, commit_honored=set())
        self.assertIsNone(r["defang"])
        self.assertTrue(any("needs a small edit" in ln for ln in r["lines"]))

    def test_code_fenced_close_not_honored_is_not_flagged(self):
        # A `Closes #274` GitHub did NOT honor (e.g. inside a code fence) is absent from honored_local, so it is
        # neither surfaced nor defanged — the reconciliation against GitHub's own set.
        body = _SCOPE + "\n```\nCloses #274\n```\n"
        r = clp.classify(body=body, honored_local=set(), commit_honored=set())
        self.assertEqual(r["lines"], [])
        self.assertIsNone(r["defang"])

    def test_commit_sourced_accidental_close_surfaces(self):
        r = clp.classify(body=_SCOPE, honored_local=set(), commit_honored={274})
        self.assertIsNone(r["defang"])
        self.assertTrue(any("needs a small edit" in ln for ln in r["lines"]))

    def test_comma_trap_named_when_head_honored(self):
        body = "## Scope\n\n- x\n\n## Out of scope\n\n- y\n\nCloses #1, #2\n"
        r = clp.classify(body=body, honored_local={1}, commit_honored=set())
        self.assertTrue(any("#2" in ln and "stay open" in ln for ln in r["lines"]))

    def test_quoted_comma_trap_example_is_suppressed(self):
        # A `Closes #1, #2` GitHub honored nothing of (a quoted/HTML-comment example, so #1 not in honored_local)
        # raises NO line — the reconciliation the technical-integrity lens asked for.
        body = ('## Scope\n\n- x\n\n## Out of scope\n\n- y\n\n'
                '<!-- the rule: "Closes #1, #2" closes only #1 -->\n')
        r = clp.classify(body=body, honored_local=set(), commit_honored=set())
        self.assertEqual(r["lines"], [])

    def test_commit_comma_trap_surfaced(self):
        # A `Closes #1, #2` in a commit message: #1 will close, #2 silently stays open -> named.
        honored, trap = clp.commit_will_close(["feat\n\nCloses #1, #2"])
        r = clp.classify(body="## Scope\n\n- x\n\n## Out of scope\n\n- y\n",
                         honored_local=set(), commit_honored=honored, commit_trap=trap)
        self.assertTrue(any("#2" in ln and "stay open" in ln for ln in r["lines"]))

    def test_cross_repo_deliberate_surfaced_never_defanged(self):
        body = _SCOPE + "\nCloses octo/other#9\n"
        r = clp.classify(body=body, honored_local=set(), commit_honored=set())
        self.assertIsNone(r["defang"])
        self.assertTrue(any("another repository" in ln for ln in r["lines"]))

    def test_cross_repo_example_in_prose_is_suppressed(self):
        # A buried/quoted `octo/other#9` (not line-leading) raises no out-of-reach line.
        body = _SCOPE + "\nSee the example `Closes octo/other#9` in the docs.\n"
        r = clp.classify(body=body, honored_local=set(), commit_honored=set())
        self.assertEqual(r["lines"], [])

    def test_clean_pr_is_silent(self):
        body = "## Scope\n\n- all of it\n\n## Out of scope\n\n- x\n\nCloses #40\n"
        r = clp.classify(body=body, honored_local={40}, commit_honored=set())
        self.assertEqual(r["lines"], [])
        self.assertIsNone(r["defang"])
        self.assertEqual(clp.render(r), "")

    def test_unavailable_short_circuits_to_could_not_read(self):
        r = clp.classify(body="", honored_local=set(), commit_honored=set(), unavailable=True)
        self.assertIsNone(r["defang"])
        self.assertTrue(any("will close" in ln for ln in r["lines"]))

    def test_only_first_of_several_contradictions_defangs(self):
        # Two accidental body closes both defang-eligible; classify defangs the first and surfaces the rest (one
        # body edit at a time keeps the applied change and its disclosure in lockstep).
        body = ("## Scope\n\n- Part of #10\n- Part of #11\n\n## Out of scope\n\n- x\n\n"
                "prose Closes #10 and prose Fixes #11\n")
        r = clp.classify(body=body, honored_local={10, 11}, commit_honored=set())
        self.assertIsNotNone(r["defang"])
        self.assertIn(r["defang"]["number"], (10, 11))


# ---- the gh / git boundary (the injected subprocess seam) ---------------------------------------

class BoundaryTests(unittest.TestCase):
    def test_read_will_close_from_json_flag(self):
        got = clp.read_will_close(7, runner=_runner(closing=[40, 41]))
        self.assertEqual(got, {40, 41})

    def test_read_will_close_raises_when_unreadable(self):
        with self.assertRaises(clp.PreflightUnavailable):
            clp.read_will_close(7, runner=_runner(fail="gh"))

    def test_graphql_fallback_used_when_flag_read_fails(self):
        # A gh whose `pr view --json closingIssuesReferences` fails but whose `api graphql` succeeds.
        def runner(cmd):
            if cmd[:3] == ["gh", "pr", "view"]:
                return 1, "", "unsupported flag"
            if cmd[:3] == ["gh", "api", "graphql"]:
                nodes = {"data": {"repository": {"pullRequest":
                         {"closingIssuesReferences": {"nodes": [{"number": 88}]}}}}}
                return 0, json.dumps(nodes), ""
            return 1, "", ""
        saved = os.environ.get("GITHUB_REPOSITORY")
        os.environ["GITHUB_REPOSITORY"] = "acme/widgets"
        try:
            self.assertEqual(clp.read_will_close(7, runner=runner), {88})
        finally:
            if saved is None:
                os.environ.pop("GITHUB_REPOSITORY", None)
            else:
                os.environ["GITHUB_REPOSITORY"] = saved

    def test_read_body_raises_on_failure(self):
        with self.assertRaises(clp.PreflightUnavailable):
            clp.read_body(7, runner=_runner(fail="gh"))

    def test_read_commit_messages_splits_and_empties(self):
        msgs = clp.read_commit_messages("main", runner=_runner(commits=["a\n\nCloses #1", "b"]))
        self.assertEqual([m.strip() for m in msgs], ["a\n\nCloses #1", "b"])

    def test_read_commit_messages_raises_on_git_failure(self):
        with self.assertRaises(clp.PreflightUnavailable):
            clp.read_commit_messages("main", runner=_runner(fail="git"))

    def test_preflight_end_to_end_defang(self):
        body = _SCOPE + "\nThis work Closes #274 as it lands.\n"
        r = clp.preflight(7, "main", runner=_runner(closing=[274], body=body, commits=[]))
        self.assertIsNotNone(r["defang"])

    def test_preflight_fails_closed_to_could_not_read(self):
        r = clp.preflight(7, "main", runner=_runner(fail="gh"))
        self.assertTrue(any("will close" in ln for ln in r["lines"]))
        self.assertIsNone(r["defang"])

    def test_preflight_commit_sourced_surfaces(self):
        r = clp.preflight(7, "main",
                          runner=_runner(closing=[], body=_SCOPE, commits=["feat\n\nCloses #274"]))
        self.assertIsNone(r["defang"])
        self.assertTrue(any("needs a small edit" in ln for ln in r["lines"]))


# ---- dispatch -----------------------------------------------------------------------------------

class DispatchTests(unittest.TestCase):
    def test_demo_runs_clean(self):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(clp.main(["demo"]), 0)

    def test_check_without_args_is_usage_error(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(clp.main(["check"]), 2)

    def test_unknown_verb_is_usage_error(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(clp.main(["bogus"]), 2)
            self.assertEqual(clp.main([]), 2)


if __name__ == "__main__":
    unittest.main()
