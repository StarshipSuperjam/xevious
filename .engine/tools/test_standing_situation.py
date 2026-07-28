#!/usr/bin/env python3
"""Self-tests for the read-only "what merged last" derivation (engine-template #100).

These lock the load-bearing behaviours a non-engineer cannot read code to verify:
  - `milestone` is the list of OPEN Milestone titles (every open one, in the API's earliest-due-first order),
    or an empty list when there are none ("none set" is the honest normal state, never an error) — GitHub has
    no single "current" milestone, so the engine names what is open and elects none (engine-template #496);
  - `phase` is the most-recently-MERGED pull request, formatted "<title> (PR #N)" — read from the merge record
    directly (newest merged_at wins, whatever the PR closed), so it never falls through to an older item the
    way the old closing-ref/engine-label walk could (defect A); a closed-but-unmerged PR is skipped; a
    blank-titled PR is skipped; no merged PR in the window -> None;
  - a READ FAILURE (HTTP >= 400 / null body) RAISES `DeriveUnavailable` and is NEVER swallowed as
    genuine-absence — so boot falls back to the cached line rather than presenting a confident, wrong one;
  - the module performs NO writes (it is a pure read-only projection).
Only the network is faked (an injected transport); the derive logic is the REAL one.

Run: uv run --directory .engine --frozen -- python -m unittest discover -s tools -p 'test_*.py' -b
"""
from __future__ import annotations
import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import standing_situation as ss  # noqa: E402


class DemoFailureBranchReportsTests(unittest.TestCase):
    """`_demo`'s unexpected-result branch writes to `sys.stderr`. `sys` must be imported at module scope so that
    branch reports (returns 1) instead of raising NameError when `_demo` runs off the `__main__` path."""

    def test_sys_is_module_scope(self):
        self.assertIs(ss.sys, sys)

    def test_the_failure_branch_reports_instead_of_raising(self):
        import contextlib
        import io
        with mock.patch.object(ss, "_where_lines", return_value=[]), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            rc = ss._demo()                               # forced unexpected result -> the stderr branch runs
        self.assertEqual(rc, 1)                           # it completed (no NameError)
        self.assertIn("DEMO UNEXPECTED", err.getvalue())


def _gh(transport, *, label="engine", repo="owner/repo"):
    """A duck-typed GitHub reader: just the attributes derive uses (repo, label, _transport)."""
    return SimpleNamespace(repo=repo, label=label, _transport=transport)


def _transport(*, milestones=(200, []), pulls=(200, [])):
    """A fake transport answering the two GETs the derive makes. Each arg is a (status, json) tuple.
    Fakes ONLY the network."""
    def t(method, path, body):
        if "/milestones" in path:
            return milestones
        if "/pulls" in path:
            return pulls
        return (404, None)
    return t


def _merged_pr(number, title, *, merged_at="2026-06-13T00:00:00Z"):
    return {"number": number, "title": title, "merged_at": merged_at}


class TestMilestone(unittest.TestCase):
    def test_single_open_milestone_is_named(self):
        gh = _gh(_transport(milestones=(200, [{"title": "Ship the beta", "due_on": "2026-09-01T00:00:00Z"}])))
        self.assertEqual(ss.derive_milestone(gh), ["Ship the beta"])

    def test_zero_milestones_is_empty_list_not_an_error(self):
        gh = _gh(_transport(milestones=(200, [])))
        self.assertEqual(ss.derive_milestone(gh), [])

    def test_all_open_are_named_not_just_the_earliest(self):
        # The engine elects none: every open milestone is named, in the API's earliest-due-first order — not
        # just the first (the pre-#496 single-election this replaces).
        gh = _gh(_transport(milestones=(200, [{"title": "Earliest"}, {"title": "Later"}])))
        self.assertEqual(ss.derive_milestone(gh), ["Earliest", "Later"])

    def test_blank_and_malformed_titles_are_dropped(self):
        gh = _gh(_transport(milestones=(200, [{"title": "  "}, {"title": "Real"}, {}, "not-a-dict"])))
        self.assertEqual(ss.derive_milestone(gh), ["Real"])

    def test_read_failure_raises_never_reads_as_empty(self):
        gh = _gh(_transport(milestones=(403, None)))
        with self.assertRaises(ss.DeriveUnavailable):
            ss.derive_milestone(gh)


class TestLastMerged(unittest.TestCase):
    def test_phase_is_the_most_recently_merged_pr(self):
        gh = _gh(_transport(pulls=(200, [_merged_pr(567, "Contribute-back leak check")])))
        self.assertEqual(ss.derive_last_merged(gh), "Contribute-back leak check (PR #567)")

    def test_newest_merged_at_wins_not_the_api_list_order(self):
        # API list order (sort=updated) is NOT merge order: PR #99 comes first in the list but #100 merged
        # LATER, so #100 is the answer — the derive orders by merged_at, not list order.
        pulls = (200, [_merged_pr(99, "Older", merged_at="2026-06-01T00:00:00Z"),
                       _merged_pr(100, "Newer", merged_at="2026-06-10T00:00:00Z")])
        self.assertEqual(ss.derive_last_merged(_gh(_transport(pulls=pulls))), "Newer (PR #100)")

    def test_skips_a_closed_but_unmerged_pr_then_finds_the_merged_one(self):
        pulls = (200, [{"number": 7, "title": "Closed unmerged", "merged_at": None},
                       _merged_pr(6, "The real last merge")])
        self.assertEqual(ss.derive_last_merged(_gh(_transport(pulls=pulls))), "The real last merge (PR #6)")

    def test_any_merged_pr_counts_no_closing_ref_or_engine_label_needed(self):
        # Defect A: the old walk required a closing keyword to an ENGINE-labelled issue and could fall through
        # to an OLDER item when a newer PR listed a non-engine issue first. The new derive takes the actual last
        # merge whatever it closed — a PR that closes nothing still counts.
        gh = _gh(_transport(pulls=(200, [_merged_pr(42, "A standalone slice that closes nothing")])))
        self.assertEqual(ss.derive_last_merged(gh), "A standalone slice that closes nothing (PR #42)")

    def test_blank_titled_pr_is_skipped_to_the_next_named_one(self):
        pulls = (200, [_merged_pr(9, "   "), _merged_pr(8, "Named")])
        self.assertEqual(ss.derive_last_merged(_gh(_transport(pulls=pulls))), "Named (PR #8)")

    def test_no_merged_pr_in_window_is_none(self):
        self.assertIsNone(ss.derive_last_merged(_gh(_transport(pulls=(200, [])))))

    def test_only_unmerged_prs_is_none(self):
        pulls = (200, [{"number": 7, "title": "Closed unmerged", "merged_at": None}])
        self.assertIsNone(ss.derive_last_merged(_gh(_transport(pulls=pulls))))

    def test_pulls_read_failure_raises_never_reads_as_nothing_merged(self):
        with self.assertRaises(ss.DeriveUnavailable):
            ss.derive_last_merged(_gh(_transport(pulls=(503, None))))


class TestDeriveStandingSituation(unittest.TestCase):
    def test_returns_both_fields_on_success(self):
        gh = _gh(_transport(
            milestones=(200, [{"title": "Ship the beta"}]),
            pulls=(200, [_merged_pr(99, "The drift fix")])))
        self.assertEqual(ss.derive_standing_situation(gh),
                         {"milestone": ["Ship the beta"], "phase": "The drift fix (PR #99)"})

    def test_genuine_absence_is_empty_milestone_and_none_phase(self):
        gh = _gh(_transport(milestones=(200, []), pulls=(200, [])))
        self.assertEqual(ss.derive_standing_situation(gh), {"milestone": [], "phase": None})

    def test_all_or_nothing_a_read_failure_raises_for_the_whole_derive(self):
        # milestone read fails -> the whole derive raises (boot then shows the cached line), never a
        # half-live "{none set, <phase>}" that masquerades as a current answer.
        gh = _gh(_transport(milestones=(403, None), pulls=(200, [_merged_pr(99, "T")])))
        with self.assertRaises(ss.DeriveUnavailable):
            ss.derive_standing_situation(gh)


class TestNoWrites(unittest.TestCase):
    def test_module_source_performs_no_file_writes(self):
        # The projection is read-only: no file-write code at all (scan for write CODE tokens, not prose —
        # the docstring legitimately says it "never touches state.json").
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "standing_situation.py")
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
        for forbidden in ('open(', '.write(', 'json.dump'):
            self.assertNotIn(forbidden, src,
                             f"the read-only derive module must contain no '{forbidden}' (no writes)")


class TestWhereLinesRendersTheRealCard(unittest.TestCase):
    """`_where_lines` renders the REAL boot dashboard over a hand-built signals dict to extract the
    'What merged last' block. render_dashboard reads its signals by HARD SUBSCRIPT for the base keys, so this
    guards that the hand-built dict stays complete: a new hard-subscript boot signal not added here would
    KeyError this operator-runnable demo path — caught here in the suite, not only when the operator runs the
    demo. (The whole-backlog total/URL keys are read via `.get()`, so this isolated dict deliberately omits
    them and the standing block still renders.)"""

    def test_renders_without_keyerror_and_returns_the_standing_block(self):
        import boot  # lazy: boot imports this module at top — importing here mirrors the demo and avoids a cycle
        live = {"milestone": ["Ship the beta"], "phase": "Add the checkout page (PR #42)"}
        lines = ss._where_lines(boot, live=live, state=None)
        self.assertTrue(any(ln.startswith("**What merged last:**") for ln in lines),
                        "the standing block must carry the 'What merged last' line")
        self.assertTrue(any("Ship the beta" in ln for ln in lines),
                        "and the milestone the live source named")


if __name__ == "__main__":
    unittest.main()
