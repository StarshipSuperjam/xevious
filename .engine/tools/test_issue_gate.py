#!/usr/bin/env python3
"""Tests for issue_gate — the engine-Issue conformance reroute matcher.

These lock the load-bearing behaviours a non-engineer cannot read code to verify: that an engine-labelled
issue-creation with a NON-conforming body is rerouted (a reason returned), that a conforming, unlabelled, or
out-of-scope call is allowed (None), that label detection is PRECISE (an innocent body that merely mentions
"engine"/"label" is never denied), that a heredoc body on stdin is recovered while a true piped stdin fails
open, and — the drift pin — that the helper's real output passes the gate and carries every CONTRACT_MARKER, so
an operator-facing copy change to the framing/headers breaks THIS test rather than the gate silently.
"""
from __future__ import annotations

import os
import shlex
import tempfile
import unittest

import issue_author
import issue_gate
import quiet_call  # capture a demo walkthrough's stdout so it can't bury the suite summary

# A conforming body is whatever the helper actually renders (couples the test to the real contract output).
CONFORMING = issue_author.render_engine_issue_body(what_this_is="a demo item", whats_next="nothing to do")
FREE_TEXT = "just some free text with no contract markers at all"


def _reason(command: str):
    """The gate's verdict for a Bash command string: a reason str (reroute) or None (allow)."""
    return issue_gate.non_conforming_reason("Bash", {"command": command})


def _create(body: str, *, label: str | None = "engine", flag: str = "-b") -> str:
    """A `gh issue create` command with an inline body, optionally labelled."""
    parts = ["gh", "issue", "create", "--title", "t", flag, shlex.quote(body)]
    if label is not None:
        parts += ["--label", label]
    return " ".join(parts)


class TestRerouteDenies(unittest.TestCase):
    """An engine-labelled issue-creation with a non-conforming body returns the redirect reason."""

    def test_inline_free_text_is_rerouted(self):
        self.assertIsNotNone(_reason(_create(FREE_TEXT)))

    def test_label_equals_form_is_rerouted(self):
        cmd = f"gh issue create --label=engine -b {shlex.quote(FREE_TEXT)}"
        self.assertIsNotNone(_reason(cmd))

    def test_engine_in_a_comma_list_is_rerouted(self):
        cmd = f"gh issue create --label engine,bug -b {shlex.quote(FREE_TEXT)}"
        self.assertIsNotNone(_reason(cmd))

    def test_gh_api_field_form_is_rerouted(self):
        cmd = ("gh api repos/o/r/issues -X POST "
               f"-f {shlex.quote('labels[]=engine')} -f {shlex.quote('body=' + FREE_TEXT)}")
        self.assertIsNotNone(_reason(cmd))

    def test_chained_command_is_rerouted(self):
        self.assertIsNotNone(_reason("cd /tmp && " + _create(FREE_TEXT)))

    def test_reason_names_the_helper_and_the_escape_hatch(self):
        reason = _reason(_create(FREE_TEXT))
        self.assertIn(".engine/tools/issue_author.py", reason)            # the in-repo helper, not cross-repo
        self.assertIn("render_engine_issue_body", reason)
        self.assertIn("drop the `engine` label", reason)                  # the not-an-engine-Issue escape hatch


class TestRerouteAllows(unittest.TestCase):
    """A conforming, unlabelled, or out-of-scope call is allowed (None) — the channel stays narrow."""

    def test_conforming_body_is_allowed(self):
        self.assertIsNone(_reason(_create(CONFORMING)))

    def test_unlabelled_free_text_is_allowed(self):
        self.assertIsNone(_reason(_create(FREE_TEXT, label=None)))

    def test_other_label_is_allowed(self):
        self.assertIsNone(_reason(_create(FREE_TEXT, label="bug")))

    def test_reads_and_non_creations_are_allowed(self):
        for cmd in ("gh issue view 5", "gh issue list --label engine",
                    "gh issue comment 5 --body whatever", "gh issue edit 5 --add-label engine"):
            self.assertIsNone(_reason(cmd), f"{cmd!r} must be allowed")

    def test_pr_creation_is_allowed(self):
        self.assertIsNone(_reason(f"gh pr create --label engine -b {shlex.quote(FREE_TEXT)}"))

    def test_non_bash_tool_is_allowed(self):
        self.assertIsNone(issue_gate.non_conforming_reason("Edit", {"file_path": "/x"}))
        self.assertIsNone(issue_gate.non_conforming_reason("Bash", {}))   # empty command

    def test_echoed_creation_command_is_not_a_creation(self):
        # command-position anchored: the verb inside an argument (echo/grep) is not a real invocation
        self.assertIsNone(_reason('echo gh issue create --label engine -b "free text"'))
        self.assertIsNone(_reason('grep "gh issue create" notes.md'))


class TestLabelDetectionPrecise(unittest.TestCase):
    """B1 regression: label detection keys on a REAL label flag/field, never a loose substring on prose —
    an innocent Issue whose body/title merely mentions "engine" and "label" is NOT denied."""

    def test_body_mentioning_engine_and_label_is_allowed(self):
        self.assertIsNone(_reason("gh issue create --title t -b 'please relabel the engine room'"))

    def test_title_mentioning_engine_and_label_is_allowed(self):
        self.assertIsNone(_reason("gh issue create --title 'the engine label gate' -b 'the engine label is off'"))

    def test_engineering_label_is_not_the_engine_label(self):
        self.assertIsNone(_reason(_create(FREE_TEXT, label="engineering")))


class TestHeredocBody(unittest.TestCase):
    """A heredoc body (on stdin via `--body-file -`) is recovered from the raw command string; a true piped
    stdin or an inline body containing `<<` is handled correctly."""

    def test_heredoc_free_text_is_rerouted(self):
        cmd = "gh issue create --label engine --body-file - <<'EOF'\n" + FREE_TEXT + "\nEOF"
        self.assertIsNotNone(_reason(cmd))

    def test_heredoc_conforming_body_is_allowed(self):
        cmd = "gh issue create --label engine --body-file - <<'EOF'\n" + CONFORMING + "\nEOF"
        self.assertIsNone(_reason(cmd))

    def test_unquoted_and_dash_heredoc_forms_are_recovered(self):
        for opener in ("<<EOF", "<<-EOF", '<<"EOF"'):
            cmd = f"gh issue create --label engine --body-file - {opener}\n{FREE_TEXT}\nEOF"
            self.assertIsNotNone(_reason(cmd), f"{opener} heredoc must be recovered")

    def test_piped_stdin_without_heredoc_fails_open(self):
        # echo … | gh … --body-file -  : the body is on a real pipe, invisible — fail open (CI backstop catches).
        self.assertIsNone(_reason("printf 'free text' | gh issue create --label engine --body-file -"))

    def test_inline_body_with_double_angle_is_checked_inline_not_as_heredoc(self):
        # S2: an inline --body that happens to contain `<<` must be checked as the inline body, never mis-read
        # as a heredoc. A conforming inline body containing `<<` must therefore ALLOW.
        body = CONFORMING + "\n\nNote: compare a << b in the code."
        self.assertIsNone(_reason(_create(body)))

    def test_crlf_heredoc_free_text_is_rerouted(self):
        cmd = "gh issue create --label engine --body-file - <<'EOF'\r\n" + FREE_TEXT + "\r\nEOF"
        self.assertIsNotNone(_reason(cmd))


class TestBodyFileOnDisk(unittest.TestCase):
    """A `--body-file <path>` already on disk is read and checked (the temp-file-then-create form)."""

    def _create_with_file(self, contents: str) -> str:
        fd, path = tempfile.mkstemp(suffix=".md")
        self.addCleanup(os.remove, path)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(contents)
        return f"gh issue create --label engine --body-file {shlex.quote(path)}"

    def test_free_text_file_is_rerouted(self):
        self.assertIsNotNone(_reason(self._create_with_file(FREE_TEXT)))

    def test_conforming_file_is_allowed(self):
        self.assertIsNone(_reason(self._create_with_file(CONFORMING)))

    def test_unreadable_file_fails_open(self):
        self.assertIsNone(_reason("gh issue create --label engine --body-file /no/such/path-xyz.md"))


class TestFailOpen(unittest.TestCase):
    """Anything the matcher cannot parse resolves to None (allow) — the nudge, never a wall."""

    def test_unparseable_shell_fails_open(self):
        self.assertIsNone(_reason('gh issue create --label engine -b "unterminated'))

    def test_gh_api_with_uninspectable_payload_fails_open(self):
        # body supplied via --input <file> (not read) — labelled but no inspectable body → fail open.
        self.assertIsNone(_reason("gh api repos/o/r/issues -X POST -f 'labels[]=engine' --input payload.json"))

    def test_non_string_or_absent_command_fails_open_without_raising(self):
        # the matcher must not raise on a weird payload shape (a non-str command would make shlex.split call
        # .read()); it resolves to None, never an exception.
        for bad in (123, ["a", "b"], None):
            self.assertIsNone(issue_gate.non_conforming_reason("Bash", {"command": bad}))
        self.assertIsNone(issue_gate.non_conforming_reason("Bash", "not-a-dict"))
        self.assertIsNone(issue_gate.non_conforming_reason("Bash", None))


class TestHelperCoupling(unittest.TestCase):
    """The drift pins: the gate's pinned markers ARE in the helper's real output, and the helper's output
    passes the gate end-to-end — so a copy change to the framing/headers breaks a test, not the gate."""

    def test_helper_output_carries_every_contract_marker(self):
        body = issue_author.render_engine_issue_body(what_this_is="a", whats_next="b")
        for marker in issue_gate.CONTRACT_MARKERS:
            self.assertIn(marker, body, f"the helper output must carry the gate marker {marker!r}")

    def test_helper_authored_creation_passes_the_gate_end_to_end(self):
        body = issue_author.render_engine_issue_body(
            what_this_is="The engine noticed something.", whats_next="The operator decides X.")
        fd, path = tempfile.mkstemp(suffix=".md")
        self.addCleanup(os.remove, path)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(body)
        cmd = f"gh issue create --label engine --body-file {shlex.quote(path)}"
        self.assertIsNone(_reason(cmd), "a body authored through the helper must pass the reroute gate")


class TestDemo(unittest.TestCase):
    def test_demo_self_check_passes(self):
        self.assertEqual(quiet_call.run(issue_gate.main, ["demo"]), 0)


if __name__ == "__main__":
    unittest.main()
