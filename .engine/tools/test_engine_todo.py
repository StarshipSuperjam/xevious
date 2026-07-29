#!/usr/bin/env python3
"""Tests for the deferred-work marker parser and its form check (eADR-0035).

Every trigger below is ASSEMBLED FROM PARTS rather than written literally. A literal would be a real marker
in a real tracked file, so this file would show up in `list` forever and the check would grade the test
fixtures as production markers. Assembling keeps the authoring rule the contract states.

The recognition cases carry most of the weight here. The rule reached its shipped form after two wrong ones —
anchoring only at line start missed a trailing comment after code, and requiring only that a leader precede
the trigger matched every heading and issue citation naming the form — so each of those failures has a test
that fails if the rule regresses to it.
"""
from __future__ import annotations
import json
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import engine_todo
import engine_todo_form_check
import quiet_call          # noqa: E402  (capture the demo walkthrough so it can't bury the summary)
import validate
import census_completeness_check as _ccc   # noqa: E402  (reuse its construction-repo marker read, not a new copy)

T = engine_todo.TOKEN + ":"                    # the bare trigger
R = engine_todo.TOKEN + "(#412):"              # the trigger carrying an issue reference


class RecognisedPositions(unittest.TestCase):
    """The two positions the frozen rule accepts."""

    def test_a_trailing_comment_after_code_is_a_marker(self):
        # The regression that anchoring at line start alone got wrong: the author believes a deferral was
        # recorded while nothing can see it, which is worse than the prose it replaced.
        found = engine_todo.scan_text("    return _append(record)   # " + T + " no retry path yet")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].description, "no retry path yet")

    def test_a_docstring_line_is_a_marker(self):
        # Where this engine's real notes actually sit — no comment leader anywhere on the line.
        found = engine_todo.scan_text('"""Append one record.\n\n    ' + T + ' the envelope is not written.\n"""')
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].description, "the envelope is not written.")

    def test_an_html_comment_is_a_marker(self):
        found = engine_todo.scan_text("<!-- " + T + " the seed is not rendered yet -->")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].description, "the seed is not rendered yet")

    def test_an_issue_reference_is_captured(self):
        found = engine_todo.scan_text("# " + R + " no retry path")
        self.assertEqual(found[0].ref, "#412")

    def test_a_bare_marker_reports_no_reference(self):
        self.assertIsNone(engine_todo.scan_text("# " + T + " nothing cited")[0].ref)


class RejectedPositions(unittest.TestCase):
    """Shapes that name the form without being one. Each of these matched under a rejected rule."""

    def _none(self, line, why):
        self.assertEqual(engine_todo.scan_text(line), [], why)

    def test_a_markdown_heading_is_not_a_marker(self):
        # Matched when the rule required only that a leader PRECEDE the trigger.
        self._none("## Writing an " + T + " marker", "a heading naming the form is not a marker")

    def test_an_issue_citation_is_not_a_marker(self):
        self._none("Issue #412 tracks the " + T + " grammar", "a citation naming the form is not a marker")

    def test_an_inline_prose_mention_is_not_a_marker(self):
        self._none("the parser -- see `" + T + "` above -- is offline", "an inline mention is not a marker")

    def test_a_string_literal_is_not_a_marker(self):
        self._none('MESSAGE = "' + T + ' this is data"', "a string literal is not a marker")

    def test_a_markdown_bullet_is_not_a_marker(self):
        # The bullet character is deliberately absent from the leader set; including it made an ordinary
        # list item a marker.
        self._none("* " + T + " an ordinary list item", "a markdown bullet is not a comment leader")

    def test_the_bare_token_without_its_colon_is_not_a_marker(self):
        self._none("# the " + engine_todo.TOKEN + " grammar is frozen", "the token alone is not a trigger")


class LeaderDetection(unittest.TestCase):
    """A leader-looking substring earlier on the line must not hide the real comment behind it. Taking only
    the leftmost match made a URL, a `//` division or a `--` inside quotes swallow the marker silently —
    the same false-negative class the rule was rewritten twice to remove."""

    def _one(self, line):
        found = engine_todo.scan_text(line)
        self.assertEqual(len(found), 1, f"no marker recognised in: {line}")
        return found[0]

    def test_a_url_in_a_string_does_not_hide_a_trailing_marker(self):
        self.assertEqual(self._one('BASE = "https://api.example.com"   # ' + T + " no retry path").description,
                         "no retry path")

    def test_a_floor_division_does_not_hide_a_trailing_marker(self):
        self.assertEqual(self._one("mid = lo // 2   # " + T + " no bounds check").description, "no bounds check")

    def test_a_hash_inside_a_string_does_not_hide_a_trailing_marker(self):
        self.assertEqual(self._one('h = {"#": 1}   # ' + T + " no escaping").description, "no escaping")

    def test_a_double_dash_inside_a_string_does_not_hide_a_trailing_marker(self):
        self.assertEqual(self._one("q = 'a--b'   # " + T + " no quoting").description, "no quoting")


class ReservedReference(unittest.TestCase):
    """A reference the grammar does not define must be SEEN and reported, never silently dropped."""

    def test_an_unrecognised_reference_is_still_recognised_as_a_marker(self):
        found = engine_todo.scan_text("# " + engine_todo.TOKEN + "(slice-7): the retry path is missing")
        self.assertEqual(len(found), 1, "an unrecognised reference must not make the marker invisible")
        self.assertEqual(found[0].ref, "slice-7")

    def test_an_unrecognised_reference_is_reported_soft_and_never_hard(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "a.py"), "w", encoding="utf-8") as fh:
                fh.write("x = 1   # " + engine_todo.TOKEN + "(slice-7): the retry path is missing\n")
            out = engine_todo_form_check.findings("hard", root=tmp)
        self.assertTrue(out, "the reserved-reference warning must actually fire, not pass vacuously")
        self.assertTrue(all(f["severity"] != "hard" for f in out))
        self.assertIn("reserved", out[0]["message"])


class Continuation(unittest.TestCase):
    """Multi-line markers. An older parser reading only the first line gets a truncated description, never
    a wrong one — which is what makes widening the rule later safe."""

    def test_a_commented_marker_joins_lines_carrying_the_same_leader(self):
        found = engine_todo.scan_text("# " + T + " the module manager is not wired\n#   the caller raises instead\n")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].description, "the module manager is not wired the caller raises instead")

    def test_a_docstring_marker_joins_lines_indented_deeper(self):
        found = engine_todo.scan_text("    " + T + " the envelope is missing\n        callers read the header\n")
        self.assertIn("callers read the header", found[0].description)

    def test_a_blank_line_closes_the_marker(self):
        found = engine_todo.scan_text("# " + T + " first\n\n# unrelated trailing comment\n")
        self.assertEqual(found[0].description, "first")

    def test_a_second_trigger_closes_the_first_and_starts_its_own(self):
        found = engine_todo.scan_text("# " + T + " first\n# " + T + " second\n")
        self.assertEqual([m.description for m in found], ["first", "second"])

    def test_a_comment_at_a_different_column_closes_the_marker(self):
        found = engine_todo.scan_text("    # " + T + " first\n# a comment further left\n")
        self.assertEqual(found[0].description, "first")

    def test_a_trailing_marker_after_code_never_continues(self):
        # The next line of code very often carries its own unrelated trailing comment. Absorbing it produced
        # descriptions that were not truncated but simply wrong — and, worse, gave an empty marker a
        # borrowed description so the hard check stopped seeing it.
        found = engine_todo.scan_text("x = 1  # " + T + "\ny = 2  # convert this to an int\n")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].description, "", "a trailing note is one line; it must not borrow the next")

    def test_an_empty_trailing_marker_still_reds_when_the_next_line_has_a_comment(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "a.py"), "w", encoding="utf-8") as fh:
                fh.write("x = 1  # " + T + "\ny = 2  # convert this to an int\n")
            out = engine_todo_form_check.findings("hard", root=tmp)
        self.assertTrue(any(f["severity"] == "hard" for f in out),
                        "the one case the hard tier exists for must not be masked by a neighbouring comment")

    def test_a_standalone_comment_block_does_not_absorb_a_differently_indented_neighbour(self):
        found = engine_todo.scan_text("# " + T + " not wired\n    # a deeper unrelated comment\n")
        self.assertEqual(found[0].description, "not wired")

    def test_a_docstring_marker_joins_a_same_indent_wrap(self):
        # Prose wraps at the same indent; requiring a deeper one truncated the description silently.
        found = engine_todo.scan_text("    " + T + " the envelope is missing\n    callers read the header\n")
        self.assertIn("callers read the header", found[0].description)

    def test_a_closing_triple_quote_closes_a_docstring_marker(self):
        found = engine_todo.scan_text("    " + T + " the envelope is missing\n    \"\"\"\n    code = 1\n")
        self.assertEqual(found[0].description, "the envelope is missing")


class FormCheck(unittest.TestCase):
    """The hard tier is held to one unambiguous case."""

    def _run(self, source):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "seeded.py"), "w", encoding="utf-8") as fh:
                fh.write(source)
            return engine_todo_form_check.findings("hard", root=tmp)

    def test_a_marker_with_no_description_is_hard(self):
        out = self._run("x = 1   # " + T + "\n")
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["severity"], "hard")
        self.assertIn("no description", out[0]["message"])

    def test_a_marker_with_a_description_is_clean(self):
        self.assertEqual(self._run("x = 1   # " + T + " the retry path is missing\n"), [])

    def test_a_description_supplied_only_by_a_continuation_line_is_clean(self):
        # The emptiness test applies to the JOINED description, so substance on the next line counts.
        self.assertEqual(self._run("# " + T + "\n#   the retry path is missing\n"), [])

    def test_an_unrecognised_parenthetical_is_soft_never_hard(self):
        # Reserved for a later extension of the grammar: widening it must not redden committed source.
        out = self._run("x = 1   # " + engine_todo.TOKEN + "(slice-7): the retry path is missing\n")
        self.assertTrue(all(f["severity"] != "hard" for f in out))


class FixtureAndScope(unittest.TestCase):

    def test_the_committed_negative_fixture_makes_the_check_bite(self):
        root = os.path.join(validate.ROOT, ".engine", "_fixtures", "engine-todo-form")
        with open(os.path.join(root, "expect.json"), encoding="utf-8") as fh:
            expect = json.load(fh)
        out = engine_todo_form_check.findings("hard", root=os.path.join(root, "tree"))
        self.assertTrue(out, "the seeded fixture must produce a finding, or the meta-check passes vacuously")
        self.assertTrue(any(f["severity"] == expect["severity"] and expect["message_contains"] in f["message"]
                            for f in out))

    def test_the_fixture_tree_is_pruned_from_a_repository_scan(self):
        # Base-relative, so the fixture prunes from a repo scan but never from its own.
        self.assertTrue(any(p.startswith(engine_todo._FIXTURE_PREFIX)
                            for p in engine_todo.tracked_files(validate.ROOT)),
                        "the fixture must be tracked, or this test proves nothing")
        self.assertFalse(any(m.path.startswith(engine_todo._FIXTURE_PREFIX)
                             for m in engine_todo.markers()))

    def test_the_live_tree_carries_no_malformed_marker(self):
        self.assertEqual(engine_todo_form_check.findings("hard"), [])

    @unittest.skipUnless(
        _ccc._in_home_repo(),
        "construction-only invariant: engine files ARE the work here, so nothing is skipped. In a deployed "
        "copy the skip is legitimately non-empty — an engine update overwrites those files — so asserting an "
        "empty set there would fail the deployed self-test suite.")
    def test_the_engine_owned_skip_is_empty_in_the_home_repository(self):
        self.assertEqual(engine_todo.engine_owned_skip(), set())

    def test_an_unreadable_file_list_is_reported_not_returned_as_empty(self):
        # The worst output this tool can produce is a confident clean answer that means "I could not look".
        with tempfile.TemporaryDirectory() as tmp:          # a directory that is not a git working copy
            with self.assertRaises(engine_todo.Unreadable):
                engine_todo.tracked_files(tmp)
        out = engine_todo_form_check.findings("hard", root=None)
        self.assertIsInstance(out, list)   # the real repo is readable, so this stays the clean path


class DemoAndCli(unittest.TestCase):

    def test_the_demo_exercises_the_real_parser_and_passes(self):
        self.assertEqual(quiet_call.run(lambda: engine_todo._demo([])), 0)

    def test_the_demo_can_fail(self):
        # A demo that cannot fail proves nothing. Break recognition and the demo must notice.
        original = engine_todo.TRIGGER
        try:
            engine_todo.TRIGGER = engine_todo.re.compile(r"THIS-MATCHES-NOTHING:")
            self.assertEqual(quiet_call.run(lambda: engine_todo._demo([])), 1)
        finally:
            engine_todo.TRIGGER = original

    def test_list_runs_and_reports_json(self):
        done = subprocess.run([sys.executable, os.path.join(validate.ROOT, ".engine", "tools", "engine_todo.py"),
                               "list", "--json"], capture_output=True, text=True, timeout=120)
        self.assertEqual(done.returncode, 0, done.stderr)
        payload = json.loads(done.stdout)
        self.assertIsInstance(payload["markers"], list)
        # The deployed-repo skip is disclosed in the machine-readable output too, so a consumer can tell a
        # short list from a complete one.
        self.assertIn("engine_owned_skipped", payload)


if __name__ == "__main__":
    unittest.main()
