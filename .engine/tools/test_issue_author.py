#!/usr/bin/env python3
"""Self-tests for the shared issue-authoring helper.

Run: uv run --directory .engine --frozen -- python tools/selftest.py

Each test locks one law of the control-plane engine-authored-issue body contract: the two required
parts cannot be omitted (TypeError at the call boundary — the by-construction enforcement) nor left
blank (ValueError); the assembled body carries the fixed plainness floor plus both parts under plain
headings; backstage references render as plain markdown links and a bare id (no label/url) is refused
(never a bare id dump), while the references part stays optional; and telemetry — the first in-repo
producer — authors its body THROUGH the helper (the single issue-authoring path, no second route).
The deliverable-gate cold review attests each test's assertion matches its name.
"""
from __future__ import annotations

import os
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import issue_author  # noqa: E402
import telemetry      # noqa: E402


class TestRequiredParts(unittest.TestCase):
    def test_omitting_a_required_part_raises_typeerror(self):
        # The "a call omitting a part cannot run" enforcement: keyword-only, no default.
        with self.assertRaises(TypeError):
            issue_author.render_engine_issue_body(what_this_is="only one part")  # type: ignore[call-arg]
        with self.assertRaises(TypeError):
            issue_author.render_engine_issue_body(whats_next="only one part")  # type: ignore[call-arg]

    def test_blank_required_part_raises_valueerror(self):
        for blank in ("", "   ", "\n\t"):
            with self.assertRaises(ValueError):
                issue_author.render_engine_issue_body(what_this_is=blank, whats_next="ok")
            with self.assertRaises(ValueError):
                issue_author.render_engine_issue_body(what_this_is="ok", whats_next=blank)

    def test_non_string_part_raises_valueerror(self):
        with self.assertRaises(ValueError):
            issue_author.render_engine_issue_body(what_this_is=None, whats_next="ok")  # type: ignore[arg-type]


class TestBodyShape(unittest.TestCase):
    def test_body_carries_floor_and_both_parts(self):
        body = issue_author.render_engine_issue_body(
            what_this_is="WHAT_IT_IS", whats_next="WHAT_NEXT")
        self.assertIn(issue_author._FRAMING, body)         # the fixed plainness floor (part 1)
        self.assertIn("**What this is.** WHAT_IT_IS", body)
        self.assertIn("**What happens next.** WHAT_NEXT", body)

    def test_references_optional_absent_by_default(self):
        body = issue_author.render_engine_issue_body(what_this_is="a", whats_next="b")
        self.assertNotIn("More detail", body)

    def test_references_render_as_markdown_links(self):
        body = issue_author.render_engine_issue_body(
            what_this_is="a", whats_next="b",
            references=[("The failing run", "https://example.com/run/1"),
                        ("The policy", "https://example.com/policy")])
        self.assertIn("**More detail.**", body)
        self.assertIn("- [The failing run](https://example.com/run/1)", body)
        self.assertIn("- [The policy](https://example.com/policy)", body)

    def test_part_renders_structured_markdown_verbatim(self):
        # Readability guidance is realizable: a producer may shape a part as a one-line summary plus
        # markdown bullets, and the helper renders it verbatim (it never forces structure, but it
        # supports the readable summary->bullets shape rather than only flat prose).
        what = "Summary line.\n\n- first detail\n- second detail"
        body = issue_author.render_engine_issue_body(what_this_is=what, whats_next="b")
        self.assertIn("**What this is.** Summary line.\n\n- first detail\n- second detail", body)


class TestNoBareIdDump(unittest.TestCase):
    def test_reference_without_label_or_url_is_refused(self):
        for bad in (
            [("", "https://example.com")],   # blank label
            [("label only", "")],            # blank url
            [("rule:abc",)],                 # 1-tuple
            ["rule:abc"],                    # bare string (length != 2)
            ["ab"],                          # 2-char string would unpack to ('a','b') — must be refused
            [("a", "b", "c")],               # 3-tuple
            [{"k": "v"}],                    # a non-pair container
        ):
            with self.assertRaises(ValueError):
                issue_author.render_engine_issue_body(what_this_is="a", whats_next="b", references=bad)


class TestImportOrderStaysCycleFree(unittest.TestCase):
    def test_both_import_orders_work_in_a_fresh_interpreter(self):
        # telemetry imports issue_author at module load; issue_author must therefore import telemetry only
        # function-locally (inside render_engine_issue_body). A module-scope `import telemetry` sneaking back
        # into issue_author would crash whichever order loads telemetry first — this pins BOTH orders in
        # fresh interpreters, which the in-process suite (one fixed order) cannot exercise.
        tools = os.path.dirname(os.path.abspath(__file__))
        for order in ("import telemetry, issue_author", "import issue_author, telemetry"):
            proc = subprocess.run([sys.executable, "-c", f"import sys; sys.path.insert(0, {tools!r}); {order}"],
                                  capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, f"{order!r} failed:\n{proc.stderr}")


class TestUrgencyAtFiling(unittest.TestCase):
    def test_default_unrated_leaves_body_unchanged(self):
        # Omitting urgency (the default) must render byte-for-byte what a pre-urgency caller got — no marker.
        without = issue_author.render_engine_issue_body(what_this_is="a", whats_next="b")
        explicit_none = issue_author.render_engine_issue_body(what_this_is="a", whats_next="b", urgency=None)
        self.assertEqual(without, explicit_none)
        self.assertIsNone(telemetry.parse_severity(without))

    def test_each_class_appends_the_marker_last_and_round_trips(self):
        for sev in (telemetry.TRUST_CRITICAL, telemetry.PERSISTENT_BENIGN):
            body = issue_author.render_engine_issue_body(what_this_is="a", whats_next="b", urgency=sev)
            # The marker telemetry writes, recovered by the same reader — appended LAST so a forged prose
            # marker cannot win (parse_severity takes the last match).
            self.assertEqual(telemetry.parse_severity(body), sev)
            self.assertTrue(body.rstrip().endswith(f"<!-- engine-severity: {sev} -->"))

    def test_urgency_outside_the_two_classes_is_refused(self):
        for bad in ("high", "trust_critical", "", "TRUST-CRITICAL"):
            with self.assertRaises(ValueError):
                issue_author.render_engine_issue_body(what_this_is="a", whats_next="b", urgency=bad)


class TestSingleAuthoringPath(unittest.TestCase):
    def test_telemetry_authors_through_the_helper(self):
        # The roadmap's "route producers through it / avoid two issue-authoring paths": telemetry's
        # body must carry the helper's framing floor, proving it is assembled via the one helper.
        rec = {"source_id": "rule:x", "message": "A check could not run.", "severity": "trust-critical"}
        body = telemetry.issue_body(rec, "2026-06-06T00:00:00Z", "2026-06-06T00:00:00Z")
        self.assertIn(issue_author._FRAMING, body)
        # ...and telemetry still appends its own trailers the helper does not own.
        self.assertIn("First noticed", body)
        self.assertEqual(telemetry.parse_source_id(body), "rule:x")

    def test_unpunctuated_message_does_not_run_on(self):
        # The finding sits in its own paragraph, so an operator concern lacking trailing punctuation
        # (e.g. via close.py) cannot collide with the following prose (deliverable-gate regression).
        rec = {"source_id": "rule:z", "message": "validator timing out", "severity": "trust-critical"}
        body = telemetry.issue_body(rec, "2026-06-06T00:00:00Z", "2026-06-06T00:00:00Z")
        self.assertIn("**What it noticed.** validator timing out\n", body)
        self.assertNotIn("validator timing out It", body)


if __name__ == "__main__":
    unittest.main()
