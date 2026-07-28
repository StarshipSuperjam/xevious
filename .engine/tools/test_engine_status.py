"""Tests for `/engine-status`'s tool (issue #83) — the operator's on-demand pull view.

Verifies: the thin reuse — `render()` is exactly `boot.render_dashboard` over `boot.gather_signals`, with
the session id passed through (so the real stance shows); the operator-facing dashboard markers are carried;
the always-answers guarantee (a renderer failure degrades to a plain line, never raises); the CLI
(`main([])` prints; `--session X` is resolved and passed through; `demo` runs and shows a clearly-labelled
made-up EXAMPLE so a real alarm is never mistaken for the operator's own); and that the strings THIS tool
adds leak no raw code identifier or exception fragment (a leaked internal would be a bug, not a word choice —
the dashboard body itself is boot's, vetted in test_boot). gather_signals (boot's I/O boundary) is faked so the tests are deterministic and offline;
the REAL render/degrade/demo logic runs ([[demo-must-exercise-real-logic]]).
"""
import contextlib
import io
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import engine_status as es  # noqa: E402
import boot  # noqa: E402
import test_boot  # noqa: E402  (reuse `_signals(**over)`, the COMPLETE signals dict render_dashboard needs)


# A raw code identifier or exception fragment surfacing in operator text means an internal name or a traceback
# leaked there — a correctness bug, not a word choice. This guards SYMBOLS, not vocabulary, so it is not a
# banned-word list: each name below is a real internal of this tool's render path.
_RAW_CODE_IDENTIFIERS = ("gather_signals", "render_dashboard", "subscript", "keyerror")


class TestRenderReusesBootSeam(unittest.TestCase):
    def test_render_is_the_dashboard_over_gathered_signals(self):
        # The whole value of the slice: ONE renderer, two callers. render() must be byte-identical to
        # render_dashboard over the gathered signals — never a second, drifting status view.
        known = test_boot._signals()
        with mock.patch.object(boot, "gather_signals", return_value=known):
            out = es.render()
        self.assertEqual(out, boot.render_dashboard(known))

    def test_render_passes_the_session_through(self):
        # The session id must reach gather_signals so the dashboard shows the REAL stance, not a default.
        seen = {}

        def fake_gather(session_id=None):
            seen["session"] = session_id
            return test_boot._signals()

        with mock.patch.object(boot, "gather_signals", fake_gather):
            es.render("sess-abc")
        self.assertEqual(seen["session"], "sess-abc")

    def test_render_carries_the_operator_dashboard_markers(self):
        with mock.patch.object(boot, "gather_signals", return_value=test_boot._signals()):
            out = es.render()
        for marker in (f"## {boot.PRESENT_MARKER}", "What merged last", "Needs your attention", "Recently shipped"):
            self.assertIn(marker, out, f"the pulled dashboard must carry the '{marker}' section")


class TestAlwaysAnswers(unittest.TestCase):
    def test_a_renderer_failure_degrades_never_raises(self):
        # If assembling the dashboard raises, the operator still gets a plain answer, not a crash.
        with mock.patch.object(boot, "render_dashboard", side_effect=RuntimeError("boom")):
            out = es.render()  # must NOT raise
        self.assertTrue(out.startswith(f"## {boot.PRESENT_MARKER}"))
        self.assertIn(es._DEGRADED, out)


class TestCLI(unittest.TestCase):
    def test_main_prints_the_status(self):
        buf = io.StringIO()
        with mock.patch.object(boot, "gather_signals", return_value=test_boot._signals()), \
                contextlib.redirect_stdout(buf):
            rc = es.main([])
        self.assertEqual(rc, 0)
        self.assertIn(f"## {boot.PRESENT_MARKER}", buf.getvalue())

    def test_main_resolves_and_passes_the_explicit_session(self):
        seen = {}

        def fake_gather(session_id=None):
            seen["session"] = session_id
            return test_boot._signals()

        with mock.patch.object(boot, "gather_signals", fake_gather), \
                contextlib.redirect_stdout(io.StringIO()):
            rc = es.main(["--session", "X"])
        self.assertEqual(rc, 0)
        self.assertEqual(seen["session"], "X", "the --session value is resolved and passed to gather_signals")

    def test_demo_runs_and_shows_a_labelled_example(self):
        # Fake only the I/O boundary; run the REAL demo logic (the example render is pure data).
        buf = io.StringIO()
        with mock.patch.object(boot, "gather_signals", return_value=test_boot._signals()), \
                contextlib.redirect_stdout(buf):
            rc = es.main(["demo"])
        self.assertEqual(rc, 0)
        out = buf.getvalue()
        self.assertIn("/engine-status", out)                 # the real-status intro
        self.assertIn("EXAMPLE", out)                          # the made-up example is clearly banner-labelled
        self.assertIn("NOT your project", out)
        self.assertIn("safety gate is off", out)               # the example's gate-off alarm actually rendered


class TestNoRawCodeIdentifierLeak(unittest.TestCase):
    def test_the_tools_own_strings_leak_no_raw_code_identifier(self):
        # The dashboard body is boot's (vetted in test_boot); these are the strings THIS tool adds. A raw
        # identifier or exception name here would be a leaked internal (a bug), not a vocabulary choice.
        mine = "\n".join([es._DEGRADED, es._DEMO_INTRO, es._DEMO_EXAMPLE_BANNER,
                          es._DEMO_EXAMPLE_INTRO]).lower()
        for sym in _RAW_CODE_IDENTIFIERS:
            self.assertNotIn(sym, mine,
                             f"raw code identifier / exception fragment {sym!r} must not reach the operator")


if __name__ == "__main__":
    unittest.main()
