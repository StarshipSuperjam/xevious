#!/usr/bin/env python3
"""Tests for checkout_health — the stranded-checkout detector (issue #80).

Lock the behaviours a non-engineer cannot read code to verify: a healthy folder reads CLEAR, a folder
stuck off its branch or missing the engine's files reads STRANDED (with the right reason), and a folder the
detector cannot resolve degrades QUIETLY to None (never a false alarm, never a crash). Fixtures are throwaway
git repos (the 27d collision-check pattern) so the detection is proven offline and deterministically.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import checkout_health
import license_seeds


def _git(root: str, *args: str) -> None:
    subprocess.run(["git", "-C", root, *args], capture_output=True, text=True, check=False)


def _repo(tmp: str, name: str, *, detach: bool = False, drop: tuple = ()) -> str:
    """A throwaway git checkout: engine files present, one commit. `detach` leaves HEAD detached; `drop`
    removes the named engine paths from the working tree (a missing-files strand)."""
    root = os.path.join(tmp, name)
    os.makedirs(os.path.join(root, ".claude"))
    os.makedirs(os.path.join(root, ".engine"))
    with open(os.path.join(root, ".claude", "settings.json"), "w") as fh:
        fh.write("{}")
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "-c", "user.email=e@x", "-c", "user.name=n", "commit", "-q", "-m", "seed", "--allow-empty")
    if detach:
        sha = subprocess.run(["git", "-C", root, "rev-parse", "HEAD"],
                             capture_output=True, text=True).stdout.strip()
        _git(root, "checkout", "-q", "--detach", sha)
    for rel in drop:
        p = os.path.join(root, rel)
        os.rmdir(p) if os.path.isdir(p) else os.remove(p)
    return root


class TestDetectStrand(unittest.TestCase):
    def test_healthy_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(checkout_health.detect_strand(cwd=_repo(tmp, "ok")))

    def test_detached_head_is_stranded(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = checkout_health.detect_strand(cwd=_repo(tmp, "det", detach=True))
            self.assertIsNotNone(r)
            self.assertIn("detached", r["states"])
            self.assertNotIn("missing-files", r["states"])   # files still present, only HEAD detached

    def test_missing_settings_is_stranded(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = checkout_health.detect_strand(
                cwd=_repo(tmp, "nos", drop=(os.path.join(".claude", "settings.json"),)))
            self.assertEqual(r["states"], ["missing-files"])

    def test_missing_engine_dir_is_stranded(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = checkout_health.detect_strand(cwd=_repo(tmp, "noe", drop=(".engine",)))
            self.assertEqual(r["states"], ["missing-files"])

    def test_detached_and_missing_reports_both(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = checkout_health.detect_strand(cwd=_repo(tmp, "both", detach=True, drop=(".engine",)))
            self.assertEqual(set(r["states"]), {"detached", "missing-files"})

    def test_main_path_is_the_resolved_checkout(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _repo(tmp, "p", detach=True)
            self.assertTrue(os.path.samefile(checkout_health.detect_strand(cwd=root)["main"], root))

    def test_behind_origin_is_not_alarmed(self):
        # Ordinary "behind" is the NORMAL state under the worktree-and-PR model: a healthy branch that is
        # simply behind its (here absent) upstream must read CLEAR — only detached / missing-files strand.
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(checkout_health.detect_strand(cwd=_repo(tmp, "behind")))

    def test_non_git_dir_degrades_quietly_to_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            # no resolvable main checkout -> quiet None (fail-soft), never a crash or a false alarm
            self.assertIsNone(checkout_health.detect_strand(cwd=tmp))

    def test_bare_repo_is_not_a_strand(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = os.path.join(tmp, "b.git")
            subprocess.run(["git", "init", "--bare", "-q", root], capture_output=True, text=True, check=False)
            # a bare repo has no working checkout -> not an operator checkout -> None, never "missing-files"
            self.assertIsNone(checkout_health.detect_strand(cwd=root))


class TestIsolatedWorktree(unittest.TestCase):
    """is_isolated_worktree — the POSITIVE isolation gate the unattended Routine stance-entry requires."""

    def test_main_checkout_is_not_isolated(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertFalse(checkout_health.is_isolated_worktree(cwd=_repo(tmp, "main")))

    def test_linked_worktree_is_isolated_and_its_main_is_not(self):
        with tempfile.TemporaryDirectory() as tmp:
            main = _repo(tmp, "main")
            wt = os.path.join(tmp, "wt")
            _git(main, "worktree", "add", "-q", "--detach", wt)
            self.assertTrue(checkout_health.is_isolated_worktree(cwd=wt),
                            "a dedicated linked worktree is isolated")
            self.assertFalse(checkout_health.is_isolated_worktree(cwd=main),
                             "the same repo's main checkout is not")

    def test_non_git_dir_is_not_isolated(self):
        with tempfile.TemporaryDirectory() as tmp:
            # git can't answer -> False, the safe floor: isolation must be proven, never merely un-disproven
            self.assertFalse(checkout_health.is_isolated_worktree(cwd=tmp))

    def test_invariant_holds_from_a_subdirectory(self):
        # The production caller runs from the .engine/ subdir with no cwd; pin the subdir invariant BOTH ways
        # (git resolves the toplevel from any subdir), so a future cwd-sensitive refactor can't silently break it.
        with tempfile.TemporaryDirectory() as tmp:
            main = _repo(tmp, "main")               # _repo already creates .engine/
            self.assertFalse(checkout_health.is_isolated_worktree(cwd=os.path.join(main, ".engine")),
                             "a subdir of the operator's main checkout is still not isolated")
            wt = os.path.join(tmp, "wt")
            _git(main, "worktree", "add", "-q", "--detach", wt)
            sub = os.path.join(wt, "sub")
            os.makedirs(sub)
            self.assertTrue(checkout_health.is_isolated_worktree(cwd=sub),
                            "a subdir of a dedicated worktree is isolated")


class TestDemo(unittest.TestCase):
    def test_demo_runs(self):
        # the operator-runnable demo classifies fixtures + prints the warm strand line; rc 0, never raises.
        with contextlib.redirect_stdout(io.StringIO()):   # keep the suite output clean
            self.assertEqual(checkout_health.main(["demo"]), 0)


class TestCheckoutCLI(unittest.TestCase):
    def _output(self, argv, *, detected=None, correction=None):
        out = io.StringIO()
        with mock.patch.object(checkout_health, "detect_behind_origin", return_value=detected), \
             mock.patch.object(checkout_health, "catch_up", return_value=correction) as catch, \
             contextlib.redirect_stdout(out):
            checkout_health.main(argv)
        return out.getvalue().lower(), catch

    def test_behind_distinguishes_calm_notice_warning_and_unavailable(self):
        base = {"state": "behind", "on_default": True}
        calm, _ = self._output(["behind"], detected={**base, "presentation": "notice"})
        firm, _ = self._output(["behind"], detected={**base, "presentation": "warning"})
        unavailable, _ = self._output(["behind"], detected={"state": "unavailable"})
        self.assertIn("newer shared work available", calm)
        self.assertIn("fallen behind", firm)
        self.assertIn("won't call this folder up to date", unavailable)

    def test_catchup_reports_changed_diverged_and_clashing_causes_honestly(self):
        changed, _ = self._output(["catchup", "--apply"],
                                  correction={"status": "blocked", "reason": "target-changed"})
        diverged, _ = self._output(["catchup", "--apply"],
                                   correction={"status": "blocked", "reason": "diverged"})
        clash, _ = self._output(["catchup", "--apply"], correction={"status": "blocked"})
        self.assertIn("changed since it was checked", changed)
        self.assertIn("deliberate reconciliation", diverged)
        self.assertIn("unsaved changes", clash)

    def test_apply_cli_passes_the_exact_target(self):
        _, catch = self._output(["catchup", "--apply", "--target", "abc123"],
                                correction={"status": "fixed"})
        catch.assert_called_once_with(apply=True, expected_target="abc123")

    def test_dry_run_prints_the_complete_pinned_apply_command(self):
        output, _ = self._output(["catchup"], correction={"status": "behind", "presentation": "notice",
                                                               "target_oid": "abc123"})
        self.assertIn("catchup --apply --target abc123", output)

    def test_unavailable_configuration_fault_does_not_say_only_retry(self):
        output, _ = self._output(["catchup"], correction={"status": "unavailable",
                                                                "reason": "default-unresolved"})
        self.assertIn("inspect the repository address", output)
        self.assertNotIn("check the connection", output)

    def test_machine_snapshot_omits_origin_credentials_and_local_paths(self):
        raw = {"state": "behind", "origin": "https://user:secret@example.invalid/private.git",
               "main": "/private/project", "target_oid": "abc123", "presentation": "notice", "fresh": True}
        out = io.StringIO()
        with mock.patch.object(checkout_health, "checkout_snapshot", return_value=raw), \
             contextlib.redirect_stdout(out):
            checkout_health.main(["snapshot"])
        rendered = out.getvalue()
        self.assertIn("abc123", rendered)
        self.assertNotIn("secret", rendered)
        self.assertNotIn("/private/project", rendered)
        self.assertNotIn("origin", rendered)


def _commit(root: str, msg: str) -> None:
    _git(root, "add", "-A")
    _git(root, "-c", "user.email=e@x", "-c", "user.name=n", "commit", "-q", "-m", msg)


def _head(root: str) -> str:
    return subprocess.run(["git", "-C", root, "rev-parse", "HEAD"], capture_output=True, text=True).stdout


def _consent_target(root: str) -> str:
    snapshot = checkout_health._checkout_snapshot(root, do_fetch=True)
    if snapshot.get("state") == "unavailable":
        raise AssertionError(f"fixture snapshot unexpectedly unavailable: {snapshot}")
    return snapshot["target_oid"]


def _consented_catch_up(root: str, **kwargs) -> dict:
    return checkout_health.catch_up(cwd=root, apply=True, do_fetch=True,
                                    expected_target=_consent_target(root), **kwargs)


def _consented_return(root: str, **kwargs) -> dict:
    return checkout_health.return_to_default(cwd=root, apply=True, do_fetch=True,
                                             expected_target=_consent_target(root), **kwargs)


def _gcommit(root: str, date: str, *args: str) -> None:
    """A git command with a FIXED author+committer date (YYYY-MM-DD) and identity — so merge-commit dates,
    and therefore the velocity span, are deterministic (never the wall clock)."""
    env = dict(os.environ, GIT_AUTHOR_DATE=f"{date}T12:00:00", GIT_COMMITTER_DATE=f"{date}T12:00:00")
    subprocess.run(["git", "-C", root, "-c", "user.email=e@x", "-c", "user.name=n", *args],
                   capture_output=True, text=True, check=False, env=env)


def _origin_and_work(tmp: str, *, merge_dates: list, touch_shared_on_last: bool = False) -> tuple:
    """A local 'origin' (default branch `main`, engine files + a tracked `shared.txt`) and a `work` clone of
    it. origin is then advanced by one MERGE commit per date in `merge_dates` (each merges a fresh side branch
    — a 'merged PR'); `work` stays at the seed, behind by len(merge_dates) merges. With touch_shared_on_last,
    the final PR also edits `shared.txt`, so a work-side edit to `shared.txt` will CLASH on fast-forward.
    Returns (work, origin). Dates ('YYYY-MM-DD') drive the deterministic velocity span. The behind detector
    fetches from this local origin — hermetic, no network."""
    origin = os.path.join(tmp, "origin")
    os.makedirs(os.path.join(origin, ".claude"))
    os.makedirs(os.path.join(origin, ".engine"))
    with open(os.path.join(origin, ".claude", "settings.json"), "w") as fh:
        fh.write("{}")
    with open(os.path.join(origin, ".engine", "marker"), "w") as fh:
        fh.write("e")           # a tracked file so .engine survives the clone (git does not track empty dirs)
    with open(os.path.join(origin, "shared.txt"), "w") as fh:
        fh.write("base\n")
    _git(origin, "init", "-q", "-b", "main")
    base = merge_dates[0] if merge_dates else "2026-06-01"
    _gcommit(origin, base, "add", "-A")
    _gcommit(origin, base, "commit", "-q", "-m", "seed")
    work = os.path.join(tmp, "work")
    subprocess.run(["git", "clone", "-q", origin, work], capture_output=True, text=True, check=False)
    for i, date in enumerate(merge_dates, start=1):
        _git(origin, "checkout", "-q", "-b", f"pr{i}", "main")
        with open(os.path.join(origin, f"f{i}.txt"), "w") as fh:
            fh.write(f"pr{i}\n")
        if touch_shared_on_last and i == len(merge_dates):
            with open(os.path.join(origin, "shared.txt"), "w") as fh:
                fh.write(f"origin change in pr{i}\n")
        _gcommit(origin, date, "add", "-A")
        _gcommit(origin, date, "commit", "-q", "-m", f"work {i}")
        _git(origin, "checkout", "-q", "main")
        _gcommit(origin, date, "merge", "--no-ff", "-q", "-m", f"Merge pull request #{i}", f"pr{i}")
    return work, origin


class TestBehindOrigin(unittest.TestCase):
    """The ONLINE checkout snapshot reports ANY missing upstream commit on the default or a side branch.
    Merge velocity changes calm/firm presentation only. The ancestry/clean-ff question lives in the correction;
    this signal never mutates and never calls a stale cached view current."""

    def test_fires_when_missing_exceeds_velocity_bar(self):
        with tempfile.TemporaryDirectory() as tmp:
            # 4 merges over 4 distinct days -> span 3 -> per_day ~1.33 -> threshold 1; missing 4 > 1 -> FIRES
            work, _ = _origin_and_work(tmp, merge_dates=["2026-06-02", "2026-06-03", "2026-06-04", "2026-06-05"])
            r = checkout_health.detect_behind_origin(cwd=work, do_fetch=True)
            self.assertIsNotNone(r)
            self.assertEqual(r["state"], "behind")
            self.assertEqual(r["behind_commits"], 8)       # four work commits plus four merge commits
            self.assertEqual(r["missing_merges"], 4)
            self.assertEqual(r["presentation"], "warning")
            self.assertEqual(r["branch"], "main")
            self.assertEqual(r["latest"], "2026-06-05")     # newest missing merge's date, for the felt line
            self.assertTrue(r["on_default"])                # on main -> the on-default arm (catch_up)
            self.assertEqual(r["advisory"], "merged")       # the checkout carries no own work -> fully absorbed

    def test_calm_notice_when_below_velocity_bar(self):
        with tempfile.TemporaryDirectory() as tmp:
            # 4 merges ALL the same day -> threshold 4; drift remains visible but calm.
            work, _ = _origin_and_work(tmp, merge_dates=["2026-06-02"] * 4)
            r = checkout_health.detect_behind_origin(cwd=work, do_fetch=True)
            self.assertEqual(r["state"], "behind")
            self.assertEqual(r["presentation"], "notice")
            self.assertEqual(r["behind_commits"], 8)

    def test_fires_branch_agnostic_on_a_side_branch_missing_merged_work(self):
        # the #342 incident shape: parked on a side branch AND missing merged work past the bar -> firm warning (the
        # old on-default-only gate is gone). on_default is False -> the correction is return_to_default, not ff.
        with tempfile.TemporaryDirectory() as tmp:
            work, _ = _origin_and_work(tmp, merge_dates=["2026-06-02", "2026-06-04", "2026-06-06"])
            _git(work, "checkout", "-q", "-b", "my-feature")
            r = checkout_health.detect_behind_origin(cwd=work, do_fetch=True)
            self.assertIsNotNone(r)
            self.assertEqual(r["state"], "behind")
            self.assertEqual(r["missing_merges"], 3)
            self.assertEqual(r["presentation"], "warning")
            self.assertEqual(r["branch"], "main")           # the default it is behind
            self.assertEqual(r["current"], "my-feature")    # where it is parked
            self.assertFalse(r["on_default"])

    def test_feature_branch_below_the_bar_is_still_visible_but_calm(self):
        with tempfile.TemporaryDirectory() as tmp:
            work, _ = _origin_and_work(tmp, merge_dates=["2026-06-02"] * 3)   # span 1 -> threshold 3; missing 3 !> 3
            _git(work, "checkout", "-q", "-b", "my-feature")
            r = checkout_health.detect_behind_origin(cwd=work, do_fetch=True)
            self.assertEqual(r["state"], "behind")
            self.assertEqual(r["presentation"], "notice")
            self.assertFalse(r["on_default"])

    def test_none_when_detached(self):
        # a detached HEAD is the strand detector's territory, not this tail
        with tempfile.TemporaryDirectory() as tmp:
            work, _ = _origin_and_work(tmp, merge_dates=["2026-06-02", "2026-06-04", "2026-06-06"])
            _git(work, "checkout", "-q", "--detach", "HEAD")
            self.assertIsNone(checkout_health.detect_behind_origin(cwd=work, do_fetch=True))

    def test_single_direct_upstream_commit_is_visible(self):
        with tempfile.TemporaryDirectory() as tmp:
            work, origin = _origin_and_work(tmp, merge_dates=[])
            with open(os.path.join(origin, "direct.txt"), "w") as fh:
                fh.write("direct\n")
            _commit(origin, "direct main commit")
            r = checkout_health.detect_behind_origin(cwd=work, do_fetch=True)
            self.assertEqual(r["state"], "behind")
            self.assertEqual(r["behind_commits"], 1)
            self.assertEqual(r["missing_merges"], 0)
            self.assertEqual(r["presentation"], "notice")

    def test_refresh_failure_is_explicitly_unavailable_even_with_a_stale_ref(self):
        with tempfile.TemporaryDirectory() as tmp:
            work, _ = _origin_and_work(tmp, merge_dates=[])
            with mock.patch.object(checkout_health, "_refresh_origin", return_value=False):
                r = checkout_health.detect_behind_origin(cwd=work, do_fetch=True)
            self.assertEqual(r["state"], "unavailable")
            self.assertEqual(r["reason"], "refresh-failed")
            self.assertFalse(r["fresh"])

    def test_remote_head_parse_failure_keeps_a_structured_cause(self):
        with tempfile.TemporaryDirectory() as tmp:
            work, _ = _origin_and_work(tmp, merge_dates=[])
            malformed = subprocess.CompletedProcess(args=[], returncode=0, stdout="not-a-symref\n", stderr="")
            with mock.patch.object(checkout_health.subprocess, "run", return_value=malformed):
                refreshed = checkout_health._refresh_origin(work)
            self.assertFalse(refreshed["ok"])
            self.assertEqual(refreshed["reason"], "remote-head-unresolved")

    def test_unconfirmed_remote_default_is_explicitly_unavailable(self):
        with tempfile.TemporaryDirectory() as tmp:
            work, _ = _origin_and_work(tmp, merge_dates=[])
            with mock.patch.object(checkout_health, "_remote_default_branch", return_value=None):
                r = checkout_health.detect_behind_origin(cwd=work, do_fetch=True)
            self.assertEqual(r["state"], "unavailable")
            self.assertEqual(r["reason"], "default-unresolved")

    def test_remote_default_change_never_uses_stale_origin_head(self):
        with tempfile.TemporaryDirectory() as tmp:
            work, origin = _origin_and_work(tmp, merge_dates=[])
            _git(work, "branch", "trunk", "main")
            _git(origin, "checkout", "-q", "-b", "trunk", "main")
            with open(os.path.join(origin, "trunk.txt"), "w") as fh:
                fh.write("new default\n")
            _commit(origin, "advance new default")
            _git(origin, "symbolic-ref", "HEAD", "refs/heads/trunk")
            self.assertEqual(checkout_health._remote_default_branch(work), "main")  # cached before refresh
            r = checkout_health.detect_behind_origin(cwd=work, do_fetch=True)
            self.assertEqual(r["state"], "behind")
            self.assertEqual(r["branch"], "trunk")
            self.assertEqual(r["behind_commits"], 1)
            self.assertEqual(checkout_health._remote_default_branch(work), "trunk")

    def test_none_when_level(self):
        with tempfile.TemporaryDirectory() as tmp:
            work, _ = _origin_and_work(tmp, merge_dates=[])   # origin never advanced -> current
            self.assertIsNone(checkout_health.detect_behind_origin(cwd=work, do_fetch=True))

    def test_fires_when_diverged_with_a_carries_work_advisory(self):
        # a local commit on work's main diverges it AND it is still missing merged work -> the widened detector
        # SURFACES it (ancestry no longer gates detection); the advisory reads 'carries-work' (own commit not in
        # origin/main), and the CORRECTION (catch_up) is what blocks it losslessly — see TestCatchUp.
        with tempfile.TemporaryDirectory() as tmp:
            work, _ = _origin_and_work(tmp, merge_dates=["2026-06-02", "2026-06-04", "2026-06-06"])
            with open(os.path.join(work, "local.txt"), "w") as fh:
                fh.write("local\n")
            _commit(work, "local divergent work")
            r = checkout_health.detect_behind_origin(cwd=work, do_fetch=True)
            self.assertIsNotNone(r)
            self.assertEqual(r["state"], "behind")
            self.assertTrue(r["on_default"])                # still on main, just diverged
            self.assertEqual(r["advisory"], "carries-work")  # the local commit is not absorbed into origin/main

    def test_fetch_leaves_working_tree_and_head_unchanged(self):
        # the online fetch touches ONLY the remote-tracking ref — never HEAD or the working tree (read-only)
        with tempfile.TemporaryDirectory() as tmp:
            work, _ = _origin_and_work(tmp, merge_dates=["2026-06-02", "2026-06-04"])
            head_before = _head(work)
            with open(os.path.join(work, "shared.txt")) as fh:
                shared_before = fh.read()
            checkout_health.detect_behind_origin(cwd=work, do_fetch=True)   # performs the fetch
            self.assertEqual(_head(work), head_before)
            with open(os.path.join(work, "shared.txt")) as fh:
                self.assertEqual(fh.read(), shared_before)


class TestCatchUp(unittest.TestCase):
    """The named-ref fast-forward correction brings a clean checkout current and REFUSES (no mutation, no loss)
    local work or divergence. Naming the ref prevents a concurrent checkout from advancing the wrong branch."""

    def test_up_to_date_is_a_noop(self):
        with tempfile.TemporaryDirectory() as tmp:
            work, _ = _origin_and_work(tmp, merge_dates=[])
            self.assertEqual(checkout_health.catch_up(cwd=work, apply=True, do_fetch=True)["status"], "healthy")

    def test_apply_requires_the_consent_time_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            work, _ = _origin_and_work(tmp, merge_dates=["2026-06-02"])
            before = _head(work)
            r = checkout_health.catch_up(cwd=work, apply=True, do_fetch=True)
            self.assertEqual(r["status"], "blocked")
            self.assertEqual(r["reason"], "consent-target-required")
            self.assertEqual(_head(work), before)

    def test_skipped_refresh_is_unavailable_never_healthy(self):
        with tempfile.TemporaryDirectory() as tmp:
            work, _ = _origin_and_work(tmp, merge_dates=[])
            detected = checkout_health.detect_behind_origin(cwd=work, do_fetch=False)
            self.assertEqual(detected["state"], "unavailable")
            self.assertEqual(detected["reason"], "refresh-skipped")
            result = checkout_health.catch_up(cwd=work, apply=True, do_fetch=False)
            self.assertEqual(result["status"], "unavailable")

    def test_clean_fast_forward_brings_current(self):
        with tempfile.TemporaryDirectory() as tmp:
            work, _ = _origin_and_work(tmp, merge_dates=["2026-06-02", "2026-06-03", "2026-06-04", "2026-06-05"])
            r = _consented_catch_up(work)
            self.assertEqual(r["status"], "fixed")
            self.assertEqual(r["brought_in"], 8)
            self.assertEqual(r["after"], r["target_oid"])
            self.assertIsNone(checkout_health.detect_behind_origin(cwd=work, do_fetch=True))   # current now

    def test_below_velocity_drift_can_be_brought_current(self):
        with tempfile.TemporaryDirectory() as tmp:
            work, _ = _origin_and_work(tmp, merge_dates=["2026-06-02"])
            before = checkout_health.detect_behind_origin(cwd=work, do_fetch=True)
            self.assertEqual(before["presentation"], "notice")
            self.assertEqual(_consented_catch_up(work)["status"], "fixed")
            self.assertIsNone(checkout_health.detect_behind_origin(cwd=work, do_fetch=True))

    def test_refresh_failure_never_mutates_from_a_stale_ref(self):
        with tempfile.TemporaryDirectory() as tmp:
            work, _ = _origin_and_work(tmp, merge_dates=["2026-06-02"])
            before = _head(work)
            with mock.patch.object(checkout_health, "_refresh_origin", return_value=False):
                r = checkout_health.catch_up(cwd=work, apply=True, do_fetch=True)
            self.assertEqual(r["status"], "unavailable")
            self.assertEqual(_head(work), before)

    def test_apply_refuses_when_consent_target_does_not_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            work, _ = _origin_and_work(tmp, merge_dates=["2026-06-02"])
            before = _head(work)
            r = checkout_health.catch_up(cwd=work, apply=True, do_fetch=True, expected_target="0" * 40)
            self.assertEqual(r["status"], "blocked")
            self.assertEqual(r["reason"], "target-changed")
            self.assertEqual(_head(work), before)

    def test_apply_refuses_when_snapshot_revalidation_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            work, _ = _origin_and_work(tmp, merge_dates=["2026-06-02"])
            before = _head(work)
            target = _consent_target(work)
            with mock.patch.object(checkout_health, "_snapshot_unchanged", return_value=False):
                r = checkout_health.catch_up(cwd=work, apply=True, do_fetch=True, expected_target=target)
            self.assertEqual(r["status"], "blocked")
            self.assertEqual(r["reason"], "checkout-changed")
            self.assertEqual(_head(work), before)

    def test_snapshot_revalidation_detects_branch_head_and_target_changes(self):
        def snapshot_for(tmp):
            work, origin = _origin_and_work(tmp, merge_dates=["2026-06-02"])
            return work, origin, checkout_health._checkout_snapshot(work, do_fetch=True)

        with self.subTest("branch"):
            with tempfile.TemporaryDirectory() as tmp:
                work, _, snapshot = snapshot_for(tmp)
                _git(work, "checkout", "-q", "-b", "moved")
                self.assertFalse(checkout_health._snapshot_unchanged(snapshot))
        with self.subTest("head"):
            with tempfile.TemporaryDirectory() as tmp:
                work, _, snapshot = snapshot_for(tmp)
                with open(os.path.join(work, "local.txt"), "w") as fh:
                    fh.write("moved\n")
                _commit(work, "move head")
                self.assertFalse(checkout_health._snapshot_unchanged(snapshot))
        with self.subTest("target"):
            with tempfile.TemporaryDirectory() as tmp:
                work, origin, snapshot = snapshot_for(tmp)
                with open(os.path.join(origin, "later.txt"), "w") as fh:
                    fh.write("later\n")
                _commit(origin, "move remote target")
                _git(work, "fetch", "-q", "origin")
                self.assertFalse(checkout_health._snapshot_unchanged(snapshot))
        with self.subTest("origin identity"):
            with tempfile.TemporaryDirectory() as tmp:
                work, _, snapshot = snapshot_for(tmp)
                replacement = os.path.join(tmp, "replacement")
                _git(replacement, "init", "-q", "-b", "main")
                _git(work, "remote", "set-url", "origin", replacement)
                self.assertFalse(checkout_health._snapshot_unchanged(snapshot))
        with self.subTest("remote default"):
            with tempfile.TemporaryDirectory() as tmp:
                work, _, snapshot = snapshot_for(tmp)
                _git(work, "update-ref", "refs/remotes/origin/other", snapshot["target_oid"])
                _git(work, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/other")
                self.assertFalse(checkout_health._snapshot_unchanged(snapshot))

    def test_dry_run_mutates_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            work, _ = _origin_and_work(tmp, merge_dates=["2026-06-02", "2026-06-03", "2026-06-04", "2026-06-05"])
            before = _head(work)
            r = checkout_health.catch_up(cwd=work, apply=False, do_fetch=True)
            self.assertEqual(r["status"], "behind")
            self.assertFalse(r["applied"])
            self.assertEqual(_head(work), before)

    def test_unrelated_uncommitted_edit_blocks_and_survives(self):
        with tempfile.TemporaryDirectory() as tmp:
            work, _ = _origin_and_work(tmp, merge_dates=["2026-06-02", "2026-06-03", "2026-06-04", "2026-06-05"])
            with open(os.path.join(work, "shared.txt"), "w") as fh:   # origin's PRs do NOT touch shared.txt
                fh.write("my local edit\n")
            r = _consented_catch_up(work)
            self.assertEqual(r["status"], "blocked")
            self.assertEqual(r["reason"], "local-work")
            with open(os.path.join(work, "shared.txt")) as fh:
                self.assertEqual(fh.read(), "my local edit\n")
            self.assertFalse(os.path.exists(os.path.join(work, "f4.txt")))  # no partial incoming work

    def test_clashing_uncommitted_edit_blocks_with_no_loss(self):
        with tempfile.TemporaryDirectory() as tmp:
            # origin's LAST PR edits shared.txt; work also edits shared.txt -> ff would clobber -> git refuses
            work, _ = _origin_and_work(tmp, merge_dates=["2026-06-02", "2026-06-04", "2026-06-06"],
                                       touch_shared_on_last=True)
            with open(os.path.join(work, "shared.txt"), "w") as fh:
                fh.write("MY UNSAVED EDIT\n")
            before = _head(work)
            r = _consented_catch_up(work)
            self.assertEqual(r["status"], "blocked")
            self.assertFalse(r["applied"])
            self.assertEqual(_head(work), before)                     # no mutation
            with open(os.path.join(work, "shared.txt")) as fh:
                self.assertEqual(fh.read(), "MY UNSAVED EDIT\n")       # the unsaved edit intact -> nothing lost

    def test_late_tracked_edit_is_refused_at_materialization_and_ref_is_rolled_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            work, _ = _origin_and_work(tmp, merge_dates=["2026-06-02"], touch_shared_on_last=True)
            target = _consent_target(work)
            before = _head(work)
            real_advance = checkout_health._advance_named_default

            def advance_then_edit(main, branch, old, new):
                advanced = real_advance(main, branch, old, new)
                with open(os.path.join(main, "shared.txt"), "w") as fh:
                    fh.write("LATE EDIT\n")
                return advanced

            with mock.patch.object(checkout_health, "_advance_named_default", side_effect=advance_then_edit):
                result = checkout_health.catch_up(cwd=work, apply=True, do_fetch=True,
                                                  expected_target=target)
            self.assertEqual(result["status"], "blocked")
            self.assertFalse(result["applied"])
            self.assertEqual(_head(work), before)
            with open(os.path.join(work, "shared.txt")) as fh:
                self.assertEqual(fh.read(), "LATE EDIT\n")

    def test_unreadable_status_is_not_treated_as_clean(self):
        with tempfile.TemporaryDirectory() as tmp:
            work, _ = _origin_and_work(tmp, merge_dates=[])
            real_run = checkout_health._run

            def fail_status(cmd, cwd=None, timeout=30):
                if "status" in cmd and "--porcelain" in cmd:
                    return None
                return real_run(cmd, cwd=cwd, timeout=timeout)

            with mock.patch.object(checkout_health, "_run", side_effect=fail_status):
                safe, reasons = checkout_health._is_lossless(work)
            self.assertFalse(safe)
            self.assertIn("status-unreadable", reasons)

    def test_diverged_is_refused_never_force_merged(self):
        # the behavioural guard that REPLACES the --ff-only source-scan: a diverged checkout is never advanced
        # or force-merged. The widened detector now SURFACES it (it IS missing merged work), so the protection
        # moves to the CORRECTION — `--ff-only` aborts on the non-fast-forward, so catch_up BLOCKS, no mutation.
        with tempfile.TemporaryDirectory() as tmp:
            work, _ = _origin_and_work(tmp, merge_dates=["2026-06-02", "2026-06-04", "2026-06-06"])
            with open(os.path.join(work, "local.txt"), "w") as fh:
                fh.write("local\n")
            _commit(work, "divergent local work")
            before = _head(work)
            r = _consented_catch_up(work)
            self.assertEqual(r["status"], "blocked")                  # diverged -> --ff-only aborts -> blocked
            self.assertEqual(r["reason"], "diverged")
            self.assertFalse(r["applied"])
            self.assertEqual(_head(work), before)                     # HEAD never moved (no ff, no force-merge)
            merges = checkout_health._run(["git", "-C", work, "rev-list", "--merges", "--count", "HEAD"])
            self.assertEqual((merges or "").strip(), "0")             # no merge commit was ever created

    def test_declines_on_a_side_branch_never_fast_forwards_it(self):
        # catch_up is the ON-DEFAULT arm: parked on a side branch + behind -> it DECLINES ('off-main') and never
        # fast-forwards the side branch (that is return_to_default's job). No mutation.
        with tempfile.TemporaryDirectory() as tmp:
            work, _ = _origin_and_work(tmp, merge_dates=["2026-06-02", "2026-06-04", "2026-06-06"])
            _git(work, "checkout", "-q", "-b", "my-feature")
            before = _head(work)
            r = _consented_catch_up(work)
            self.assertEqual(r["status"], "off-main")
            self.assertFalse(r["applied"])
            self.assertEqual(_head(work), before)                     # the side branch was never advanced


class TestUnstrand(unittest.TestCase):
    """The un-stranding fix: lossless-or-it-does-not-run. The load-bearing proof for a folder-mutating change —
    every at-risk artifact must survive (on the rescue branch / untouched), and an unresolvable case refuses."""

    def _show(self, root, ref, path):
        return checkout_health._run(["git", "-C", root, "show", f"{ref}:{path}"])

    def test_healthy_is_a_noop(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(checkout_health.unstrand(cwd=_repo(tmp, "ok"), apply=True)["status"], "healthy")

    def test_detached_lossless_reattaches_with_no_rescue(self):
        # detached at the branch tip (on-branch commit), clean tree -> lossless re-attach, no rescue branch
        with tempfile.TemporaryDirectory() as tmp:
            root = _repo(tmp, "det", detach=True)
            r = checkout_health.unstrand(cwd=root, apply=True)
            self.assertEqual(r["status"], "fixed")
            self.assertIsNone(r["rescue"])
            self.assertIsNone(checkout_health.detect_strand(cwd=root))   # healthy: back on its branch

    def test_offbranch_committed_work_survives_on_the_rescue_branch(self):
        # the scary case: COMMITTED work on a detached HEAD, reachable from no branch
        with tempfile.TemporaryDirectory() as tmp:
            root = _repo(tmp, "atrisk", detach=True)
            with open(os.path.join(root, "note.txt"), "w") as fh:
                fh.write("KEEP ME")
            _commit(root, "off-branch work")
            r = checkout_health.unstrand(cwd=root, apply=True)
            self.assertEqual(r["status"], "fixed")
            self.assertIsNotNone(r["rescue"])
            self.assertIsNone(checkout_health.detect_strand(cwd=root))     # healthy now
            self.assertEqual(self._show(root, r["rescue"], "note.txt"), "KEEP ME")  # the work SURVIVED

    def test_uncommitted_work_on_a_detached_head_survives(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _repo(tmp, "dirty", detach=True)
            with open(os.path.join(root, "wip.txt"), "w") as fh:   # untracked WIP, never committed
                fh.write("WIP CONTENT")
            r = checkout_health.unstrand(cwd=root, apply=True)
            self.assertEqual(r["status"], "fixed")
            self.assertIsNotNone(r["rescue"])
            self.assertEqual(self._show(root, r["rescue"], "wip.txt"), "WIP CONTENT")  # WIP saved, not lost

    def test_a_stash_is_left_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _repo(tmp, "stash", detach=True)
            with open(os.path.join(root, "x.txt"), "w") as fh:
                fh.write("v1")
            _commit(root, "x")                              # an off-branch commit (so HEAD has x.txt tracked)
            with open(os.path.join(root, "x.txt"), "w") as fh:
                fh.write("v2")
            _git(root, "stash")                             # stash the v2 change
            before = checkout_health._run(["git", "-C", root, "stash", "list"])
            self.assertTrue(before and before.strip())      # there IS a stash
            checkout_health.unstrand(cwd=root, apply=True)
            self.assertEqual(checkout_health._run(["git", "-C", root, "stash", "list"]), before)  # untouched

    def test_missing_engine_files_are_rematerialized(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _repo(tmp, "miss", drop=(os.path.join(".claude", "settings.json"),))
            r = checkout_health.unstrand(cwd=root, apply=True)
            self.assertEqual(r["status"], "fixed")
            self.assertTrue(os.path.exists(os.path.join(root, ".claude", "settings.json")))
            self.assertIsNone(checkout_health.detect_strand(cwd=root))

    def test_rematerialize_is_per_path_never_tracked_does_not_block_others(self):
        # HEAD has .engine but NOT .claude/settings.json; both absent from the tree. Restoring must handle each
        # path independently — the never-tracked .claude/settings.json must not abort restoring .engine.
        with tempfile.TemporaryDirectory() as tmp:
            root = os.path.join(tmp, "partial")
            os.makedirs(os.path.join(root, ".engine"))
            with open(os.path.join(root, ".engine", "marker"), "w") as fh:
                fh.write("e")
            _git(root, "init", "-q")
            _commit(root, "engine only")                    # HEAD has .engine/marker, no .claude/settings.json
            import shutil
            shutil.rmtree(os.path.join(root, ".engine"))     # now .engine is missing too
            r = checkout_health.unstrand(cwd=root, apply=True)
            self.assertEqual(r["status"], "fixed")
            self.assertTrue(os.path.exists(os.path.join(root, ".engine", "marker")))  # .engine restored anyway

    def test_unresolvable_branch_refuses_without_mutating(self):
        # detached, no origin/HEAD, two branches and neither main nor master -> can't resolve -> REFUSE
        with tempfile.TemporaryDirectory() as tmp:
            root = _repo(tmp, "ambi")
            _git(root, "branch", "-M", "feature-a")          # rename current branch
            _git(root, "branch", "feature-b")                # a second branch
            _git(root, "checkout", "-q", "--detach", "HEAD")
            before = _head(root)
            r = checkout_health.unstrand(cwd=root, apply=True)
            self.assertEqual(r["status"], "needs-manual")
            self.assertEqual(r["reason"], "no-default-branch")
            self.assertEqual(_head(root), before)            # NO mutation — still where it was
            self.assertIn("detached", checkout_health.detect_strand(cwd=root)["states"])

    def test_dry_run_mutates_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _repo(tmp, "dry", detach=True)
            before = _head(root)
            r = checkout_health.unstrand(cwd=root, apply=False)
            self.assertFalse(r["applied"])
            self.assertEqual(r["status"], "fixable")
            self.assertEqual(_head(root), before)            # unchanged
            self.assertIsNotNone(checkout_health.detect_strand(cwd=root))  # still stranded

    def test_a_rescue_that_cannot_save_refuses_and_keeps_the_work(self):
        # defense-in-depth: if the rescue commit can't capture the dirty work, the fix REFUSES (needs-manual)
        # and never moves HEAD onward — the work stays intact on disk rather than being put at any risk.
        import unittest.mock as mock
        with tempfile.TemporaryDirectory() as tmp:
            root = _repo(tmp, "norescue", detach=True)
            with open(os.path.join(root, "wip.txt"), "w") as fh:
                fh.write("KEEP")
            real_ok = checkout_health._ok

            def fake_ok(cmd, cwd=None):
                return True if "commit" in cmd else real_ok(cmd, cwd=cwd)   # the commit reports success but no-ops

            with mock.patch.object(checkout_health, "_ok", side_effect=fake_ok):
                r = checkout_health.unstrand(cwd=root, apply=True)
            self.assertEqual(r["status"], "needs-manual")
            self.assertEqual(r["reason"], "rescue-failed")
            with open(os.path.join(root, "wip.txt")) as fh:
                self.assertEqual(fh.read(), "KEEP")        # the work is intact on disk, nothing lost

    def test_fix_source_names_no_destructive_git_tokens(self):
        # defense-in-depth: the fix must never reach for a force/destructive git operation. Scan for the
        # QUOTED command tokens (so a backtick mention in a docstring is not a false positive). `--ff-only`
        # is DELIBERATELY absent from this set (#335): it is git's own refuse-if-not-a-fast-forward guard —
        # the one sanctioned non-additive verb, used only by catch_up. The behavioural guard that it can never
        # force a diverged/clashing checkout is TestCatchUp (test_diverged_is_refused_never_force_merged,
        # test_clashing_uncommitted_edit_blocks_with_no_loss) — that, not this source-scan, protects the verb.
        with open(checkout_health.__file__, encoding="utf-8") as fh:
            src = fh.read()
        for token in ('"reset"', '"clean"', '"-f"', '"--force"', '"--hard"',
                      '"drop"', '"clear"', '"push"'):
            self.assertNotIn(token, src, f"the un-stranding fix must never use the git token {token}")


def _rev(root: str, ref: str) -> str:
    return subprocess.run(["git", "-C", root, "rev-parse", ref], capture_output=True, text=True).stdout.strip()


def _branch(root: str) -> str:
    return subprocess.run(["git", "-C", root, "symbolic-ref", "--quiet", "--short", "HEAD"],
                          capture_output=True, text=True).stdout.strip()


def _healthy_repo(tmp: str, name: str = "r", *, branch: str = "main", default_branch=None) -> str:
    """A healthy local checkout (engine files present, one commit) initialised ON `branch`, NO remote. With
    `default_branch`, persists that name in an engine.json manifest (the derived config) — so the
    CONFIDENT default resolves with no origin/HEAD."""
    root = os.path.join(tmp, name)
    os.makedirs(os.path.join(root, ".claude"))
    os.makedirs(os.path.join(root, ".engine"))
    with open(os.path.join(root, ".claude", "settings.json"), "w") as fh:
        fh.write("{}")
    if default_branch is not None:
        manifest = {"engine_release": "0.0.0-dev", "packages": {"core": "0.0.0-dev"},
                    "identity": "solo", "default_branch": default_branch}
        with open(os.path.join(root, ".engine", "engine.json"), "w") as fh:
            json.dump(manifest, fh)
    _git(root, "init", "-q", "-b", branch)
    _git(root, "add", "-A")
    _git(root, "-c", "user.email=e@x", "-c", "user.name=n", "commit", "-q", "-m", "seed")
    return root


def _clone_on_branch(tmp: str, branch: str) -> str:
    """A `work` clone of a tiny origin (default 'main', so `origin/HEAD` -> main is a CONFIDENT default), left
    checked out on a NEW side branch carrying its own committed work. Returns the `work` path."""
    work, _ = _origin_and_work(tmp, merge_dates=[])      # clone on main; clone sets refs/remotes/origin/HEAD
    _git(work, "checkout", "-q", "-b", branch)
    with open(os.path.join(work, "feature-work.txt"), "w") as fh:
        fh.write("FEATURE WIP")
    _commit(work, "my feature work")
    return work


class TestOffMain(unittest.TestCase):
    """#342 Stage-1 off-main: a HEALTHY checkout parked on a non-default branch reads OFF-MAIN — but only when
    the default is KNOWN with confidence (persisted / origin-HEAD), never on a heuristic guess (risk-S2)."""

    def test_fires_on_a_side_branch_with_confident_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = checkout_health.detect_off_main(cwd=_clone_on_branch(tmp, "my-feature"))
            self.assertIsNotNone(r)
            self.assertEqual(r["state"], "off-main")
            self.assertEqual(r["branch"], "my-feature")
            self.assertEqual(r["main_branch"], "main")

    def test_none_on_the_default_branch(self):
        with tempfile.TemporaryDirectory() as tmp:
            work, _ = _origin_and_work(tmp, merge_dates=[])      # on main, origin/HEAD -> main
            self.assertIsNone(checkout_health.detect_off_main(cwd=work))

    def test_persisted_default_enables_off_main_without_a_remote(self):
        # no clone, no origin/HEAD: the persisted manifest name (validated as a real local branch) is the
        # confident default, so off-main still fires (exercises the persisted read).
        with tempfile.TemporaryDirectory() as tmp:
            root = _healthy_repo(tmp, branch="main", default_branch="main")
            _git(root, "checkout", "-q", "-b", "my-feature")
            r = checkout_health.detect_off_main(cwd=root)
            self.assertIsNotNone(r)
            self.assertEqual(r["branch"], "my-feature")
            self.assertEqual(r["main_branch"], "main")

    def test_silent_when_default_is_only_a_guess(self):
        # no persisted name, no origin/HEAD -> the default would only be a heuristic guess -> NO standing nag
        with tempfile.TemporaryDirectory() as tmp:
            root = _healthy_repo(tmp, branch="my-feature")       # sole branch, no remote, no manifest default
            self.assertIsNone(checkout_health.detect_off_main(cwd=root))

    def test_none_when_detached(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = _clone_on_branch(tmp, "my-feature")
            _git(work, "checkout", "-q", "--detach", "HEAD")     # a strand is the strand detector's territory
            self.assertIsNone(checkout_health.detect_off_main(cwd=work))


class TestAbsentHome(unittest.TestCase):
    """#367: an installed engine whose manifest records no update home reads ABSENT-HOME (boot offers to
    record it); a home recorded, no manifest, or a broken strand all read clean (None)."""

    @staticmethod
    def _write_manifest(root, home=None):
        m = {"engine_release": "0.0.0-dev", "packages": {"core": "0.0.0-dev"}, "identity": "solo"}
        if home is not None:
            m["home_repository"] = home
        with open(os.path.join(root, ".engine", "engine.json"), "w") as fh:
            json.dump(m, fh)

    def test_fires_when_manifest_records_no_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _healthy_repo(tmp, default_branch="main")     # writes a manifest WITHOUT a home
            r = checkout_health.detect_absent_home(cwd=root)
            self.assertIsNotNone(r)
            self.assertEqual(r["state"], "absent-home")
            self.assertTrue(os.path.samefile(r["main"], root))

    def test_none_when_a_home_is_recorded(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _healthy_repo(tmp, default_branch="main")
            self._write_manifest(root, home="acme/engine-template")   # a home is recorded -> normal state
            self.assertIsNone(checkout_health.detect_absent_home(cwd=root))

    def test_none_when_no_manifest_present(self):
        # a checkout with no engine manifest is not an installed engine we can judge -> quiet (None)
        with tempfile.TemporaryDirectory() as tmp:
            root = _healthy_repo(tmp, branch="main")             # no default_branch -> no manifest written
            self.assertIsNone(checkout_health.detect_absent_home(cwd=root))


class TestRecordedProduct(unittest.TestCase):
    """eADR-0026: a manifest recording an external product (a repo different from the one the engine is deployed
    into) reads that slug; no product recorded / no manifest / a broken strand all read None — the common
    self-building case, where the product is this repo itself and is derived live from origin, never stored."""

    @staticmethod
    def _write_manifest(root, product=None):
        m = {"engine_release": "0.0.0-dev", "packages": {"core": "0.0.0-dev"}, "identity": "solo"}
        if product is not None:
            m["product_repository"] = product
        with open(os.path.join(root, ".engine", "engine.json"), "w") as fh:
            json.dump(m, fh)

    def test_reads_the_recorded_external_product(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _healthy_repo(tmp, default_branch="main")
            self._write_manifest(root, product="acme/upstream")
            self.assertEqual(checkout_health.recorded_product_repository(cwd=root), "acme/upstream")

    def test_none_when_no_product_recorded(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _healthy_repo(tmp, default_branch="main")   # manifest without a product -> self-building
            self.assertIsNone(checkout_health.recorded_product_repository(cwd=root))

    def test_none_when_no_manifest_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _healthy_repo(tmp, branch="main")           # no default_branch -> no manifest written
            self.assertIsNone(checkout_health.recorded_product_repository(cwd=root))

    def test_none_on_a_broken_strand(self):
        # a detached/missing strand is the strand detector's territory -> this signal stays quiet (None),
        # never reading a manifest off a broken checkout (mirrors detect_absent_home's strand guard).
        orig = checkout_health._resolve_state
        checkout_health._resolve_state = lambda cwd=None: ("/nonexistent", True, False, "abc123")  # detached
        try:
            self.assertIsNone(checkout_health.recorded_product_repository())
        finally:
            checkout_health._resolve_state = orig


class TestReturnToDefault(unittest.TestCase):
    """#342 off-main correction: point a side-branch park back at its default, LOSSLESS — the side-branch work
    stays on its branch; a dirty / paused state BLOCKS with no mutation."""

    def test_lossless_return_keeps_side_branch_work_on_its_branch(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = _clone_on_branch(tmp, "my-feature")           # 'feature-work.txt' committed on my-feature
            feature_sha = _rev(work, "my-feature")
            r = _consented_return(work)
            self.assertEqual(r["status"], "fixed")
            self.assertEqual(_branch(work), "main")              # back on the default branch
            self.assertIsNone(checkout_health.detect_off_main(cwd=work))
            self.assertEqual(_rev(work, "my-feature"), feature_sha)   # the branch ref still holds the work
            self.assertEqual(checkout_health._run(["git", "-C", work, "show", "my-feature:feature-work.txt"]),
                             "FEATURE WIP")                       # the side-branch work survived, untouched

    def test_dry_run_reports_without_mutating(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = _clone_on_branch(tmp, "my-feature")
            r = checkout_health.return_to_default(cwd=work, apply=False)
            self.assertEqual(r["status"], "off-main")
            self.assertFalse(r["applied"])
            self.assertEqual(_branch(work), "my-feature")        # still parked on the side branch

    def test_dirty_tree_blocks_with_no_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = _clone_on_branch(tmp, "my-feature")
            with open(os.path.join(work, "feature-work.txt"), "w") as fh:
                fh.write("UNSAVED EDIT")                          # an uncommitted change
            r = _consented_return(work)
            self.assertEqual(r["status"], "blocked")
            self.assertFalse(r["applied"])
            self.assertEqual(_branch(work), "my-feature")        # never left the side branch
            with open(os.path.join(work, "feature-work.txt")) as fh:
                self.assertEqual(fh.read(), "UNSAVED EDIT")      # nothing lost

    def test_on_the_default_branch_is_a_noop(self):
        with tempfile.TemporaryDirectory() as tmp:
            work, _ = _origin_and_work(tmp, merge_dates=[])
            self.assertEqual(checkout_health.return_to_default(cwd=work, apply=True)["status"], "healthy")

    def test_diverged_default_refuses_before_switching_branches(self):
        # The local default and shared default both moved. Refuse BEFORE checkout: the operator consented to a
        # lossless catch-up, not a partial branch switch that discovers divergence afterward.
        with tempfile.TemporaryDirectory() as tmp:
            work, _ = _origin_and_work(tmp, merge_dates=["2026-06-02", "2026-06-04"])   # origin advanced on main
            with open(os.path.join(work, "local-main.txt"), "w") as fh:
                fh.write("local main work")
            _commit(work, "divergent local commit on main")     # local main now diverges from origin/main
            _git(work, "checkout", "-q", "-b", "my-feature")     # park off-main at that diverged tip
            r = _consented_return(work)
            self.assertEqual(r["status"], "blocked")
            self.assertEqual(r["reason"], "diverged")
            self.assertEqual(_branch(work), "my-feature")         # no mutation before the refusal

    def test_return_requires_the_consent_time_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = _clone_on_branch(tmp, "my-feature")
            r = checkout_health.return_to_default(cwd=work, apply=True, do_fetch=True)
            self.assertEqual(r["status"], "blocked")
            self.assertEqual(r["reason"], "consent-target-required")
            self.assertEqual(_branch(work), "my-feature")

    def test_return_refuses_concurrent_change_before_switching(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = _clone_on_branch(tmp, "my-feature")
            target = _consent_target(work)
            with mock.patch.object(checkout_health, "_snapshot_unchanged", return_value=False):
                r = checkout_health.return_to_default(cwd=work, apply=True, do_fetch=True,
                                                      expected_target=target)
            self.assertEqual(r["status"], "blocked")
            self.assertEqual(r["reason"], "checkout-changed")
            self.assertEqual(_branch(work), "my-feature")

    def test_fresh_remote_default_overrides_stale_persisted_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            work, origin = _origin_and_work(tmp, merge_dates=[])
            with open(os.path.join(origin, ".engine", "engine.json"), "w") as fh:
                json.dump({"engine_release": "0.0.0-dev", "packages": {"core": "0.0.0-dev"},
                           "identity": "solo", "default_branch": "main"}, fh)
            _commit(origin, "persist old default")
            _git(work, "fetch", "-q", "origin", "main")
            _git(work, "merge", "--ff-only", "origin/main")
            _git(work, "branch", "trunk", "main")
            _git(origin, "checkout", "-q", "-b", "trunk", "main")
            with open(os.path.join(origin, "trunk.txt"), "w") as fh:
                fh.write("new default\n")
            _commit(origin, "advance new default")
            _git(origin, "symbolic-ref", "HEAD", "refs/heads/trunk")
            self.assertIsNone(checkout_health.detect_off_main(cwd=work))  # stale persisted main says healthy
            snapshot = checkout_health.checkout_snapshot(cwd=work, do_fetch=True)
            self.assertEqual(snapshot["branch"], "trunk")
            self.assertFalse(snapshot["on_default"])
            result = checkout_health.return_to_default(cwd=work, apply=True, do_fetch=True,
                                                       expected_target=snapshot["target_oid"])
            self.assertEqual(result["status"], "fixed")
            self.assertEqual(_branch(work), "trunk")

    def test_failed_postcondition_restores_the_original_side_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            work, _ = _origin_and_work(tmp, merge_dates=["2026-06-02"])
            _git(work, "checkout", "-q", "-b", "my-feature")
            target = _consent_target(work)
            real_succeeds = checkout_health._succeeds

            def fail_postcondition(cmd, cwd=None):
                if "merge-base" in cmd and cmd[-1] == "HEAD":
                    return False
                return real_succeeds(cmd, cwd=cwd)

            with mock.patch.object(checkout_health, "_succeeds", side_effect=fail_postcondition):
                result = checkout_health.return_to_default(cwd=work, apply=True, do_fetch=True,
                                                           expected_target=target)
            self.assertEqual(result["status"], "blocked")
            self.assertEqual(result["reason"], "postcondition-failed")
            self.assertTrue(result["restored"])
            self.assertFalse(result["applied"])
            self.assertEqual(_branch(work), "my-feature")


class TestOpInProgress(unittest.TestCase):
    """The lossless gate's load-bearing probe (#342): a paused git operation must block the fix
    even though `git status --porcelain` is CLEAN. Proven with a REAL paused `rebase -i` (a leading 'break'
    stops it with an empty porcelain), not a planted sentinel file."""

    def _pause_rebase(self, root: str) -> None:
        for i in (1, 2, 3):
            with open(os.path.join(root, "f.txt"), "w") as fh:
                fh.write(f"c{i}")
            _commit(root, f"c{i}")
        edit = 'import sys;f=sys.argv[1];c=open(f).read();open(f,"w").write("break\\n"+c)'
        env = dict(os.environ, GIT_SEQUENCE_EDITOR=f"{sys.executable} -c '{edit}'")
        subprocess.run(["git", "-C", root, "-c", "user.email=e@x", "-c", "user.name=n",
                        "rebase", "-i", "HEAD~2"], capture_output=True, text=True, check=False, env=env)

    def test_paused_rebase_is_detected_with_a_clean_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _repo(tmp, "rb")                              # healthy engine files, on a branch
            self._pause_rebase(root)
            porcelain = subprocess.run(["git", "-C", root, "status", "--porcelain"],
                                       capture_output=True, text=True).stdout
            self.assertEqual(porcelain.strip(), "")             # the tree is CLEAN — porcelain alone would miss it
            self.assertTrue(checkout_health._op_in_progress(root))        # the sentinel probe catches it
            self.assertIn("op-in-progress", checkout_health._is_lossless(root)[1])

    def test_unstrand_refuses_during_a_paused_rebase_with_no_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _repo(tmp, "rb")
            self._pause_rebase(root)
            before = _head(root)
            r = checkout_health.unstrand(cwd=root, apply=True)
            self.assertEqual(r["status"], "needs-manual")
            self.assertEqual(r["reason"], "op-in-progress")
            self.assertEqual(_head(root), before)               # HEAD never moved — nothing disturbed


class TestPersistedDefaultBranch(unittest.TestCase):
    """#342: `_default_branch` reads the persisted manifest name FIRST, but only when it is a real
    local branch (a stale/wrong name must never redirect the detached-HEAD re-attach mutation); else it falls
    back to the live origin/HEAD → main/master → sole-branch resolution."""

    def _repo(self, root, *, branch, persisted):
        os.makedirs(os.path.join(root, ".engine"))
        _git(root, "init", "-q", "-b", branch)
        with open(os.path.join(root, "f.txt"), "w") as fh:
            fh.write("x")
        _git(root, "add", "-A")
        _git(root, "-c", "user.email=e@x", "-c", "user.name=n", "commit", "-q", "-m", "seed")
        manifest = {"engine_release": "0.0.0-dev", "packages": {"core": "0.0.0-dev"}, "identity": "solo"}
        if persisted is not None:
            manifest["default_branch"] = persisted
        with open(os.path.join(root, ".engine", "engine.json"), "w") as fh:
            json.dump(manifest, fh)

    def test_persisted_name_wins_over_the_fallback_guess(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = os.path.join(tmp, "r")
            self._repo(root, branch="main", persisted="trunk")
            _git(root, "branch", "trunk")           # 'trunk' is a real local branch, distinct from main
            # the live fallback would pick 'main'; the validated persisted name wins
            self.assertEqual(checkout_health._default_branch(root), "trunk")

    def test_falls_back_when_persisted_name_is_not_a_local_branch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = os.path.join(tmp, "r")
            self._repo(root, branch="main", persisted="renamed-away")   # stale: no such local branch
            self.assertEqual(checkout_health._default_branch(root), "main")   # ignored -> live fallback

    def test_falls_back_when_no_persisted_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = os.path.join(tmp, "r")
            self._repo(root, branch="main", persisted=None)             # construction / pre-persistence repo
            self.assertEqual(checkout_health._default_branch(root), "main")


class TestProductBuildTarget(unittest.TestCase):
    """The engine-mechanic executable build target readers (eADR-0026): the manifest reader and the two-state
    per-machine path resolver. The fail-closed origin-match belt itself moved to mechanic_build.py (the guarded
    gate); its tests live in test_mechanic_build.py. These readers stay fail-soft-quiet."""

    def _write_manifest(self, root: str, obj: dict) -> None:
        with open(os.path.join(root, ".engine", "engine.json"), "w", encoding="utf-8") as fh:
            json.dump(obj, fh)

    @contextlib.contextmanager
    def _env(self, **kw):
        saved = {k: os.environ.get(k) for k in kw}
        try:
            for k, v in kw.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
            yield
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def test_recorded_target_reads_manifest_and_absent_is_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _repo(tmp, "co")
            self._write_manifest(root, {"product_build_target": "StarshipSuperjam/engine-template"})
            self.assertEqual(checkout_health.recorded_product_build_target(root),
                             "StarshipSuperjam/engine-template")
            self._write_manifest(root, {"engine_release": "1.0.0"})   # no target -> self-building default
            self.assertIsNone(checkout_health.recorded_product_build_target(root))

    def test_resolve_path_silent_when_no_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _repo(tmp, "co")
            self._write_manifest(root, {"engine_release": "1.0.0"})
            with self._env(ENGINE_PRODUCT_CHECKOUT="/anything"):   # even with env set, no target -> silent
                self.assertEqual(checkout_health.resolve_product_checkout(root), (None, None))

    def test_resolve_path_env_then_file_then_loud(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _repo(tmp, "co")
            self._write_manifest(root, {"product_build_target": "o/r"})
            # env wins first
            with self._env(ENGINE_PRODUCT_CHECKOUT="/home/me/et"):
                self.assertEqual(checkout_health.resolve_product_checkout(root), ("/home/me/et", None))
            # no env -> gitignored fallback file
            os.makedirs(os.path.join(root, ".engine", "mechanic"))
            with open(os.path.join(root, ".engine", "mechanic", "product-checkout-path"), "w",
                      encoding="utf-8") as fh:
                fh.write("/home/me/from-file\n")
            with self._env(ENGINE_PRODUCT_CHECKOUT=None):
                self.assertEqual(checkout_health.resolve_product_checkout(root), ("/home/me/from-file", None))
            # neither -> LOUD (the fork case: slug travelled, local path never set)
            os.remove(os.path.join(root, ".engine", "mechanic", "product-checkout-path"))
            with self._env(ENGINE_PRODUCT_CHECKOUT=None):
                self.assertEqual(checkout_health.resolve_product_checkout(root), (None, "path-unset"))

    def test_mechanic_orientation_is_the_one_dict_boot_relays(self):
        # The single boot-facing reader (Slice 3): None when not a mechanic; else one dict carrying the product
        # slug and the resolved-or-unset path state. Manifest read once here; the path reuses the same env-then-file
        # seam as resolve_product_checkout (proven both ways below).
        with tempfile.TemporaryDirectory() as tmp:
            root = _repo(tmp, "co")
            # not a mechanic -> None (no product_build_target), even with a stray env var set
            self._write_manifest(root, {"engine_release": "1.0.0"})
            with self._env(ENGINE_PRODUCT_CHECKOUT="/anything"):
                self.assertIsNone(checkout_health.mechanic_orientation(root))
            # a mechanic pointed at a REAL directory -> resolved, carrying product + checkout
            self._write_manifest(root, {"product_build_target": "o/r"})
            real = os.path.join(tmp, "product-clone")
            os.makedirs(real)
            with self._env(ENGINE_PRODUCT_CHECKOUT=real):
                self.assertEqual(checkout_health.mechanic_orientation(root),
                                 {"product": "o/r", "checkout": real, "state": "resolved"})
            # the gitignored fallback file resolves it too (no env)
            os.makedirs(os.path.join(root, ".engine", "mechanic"))
            with open(os.path.join(root, ".engine", "mechanic", "product-checkout-path"), "w",
                      encoding="utf-8") as fh:
                fh.write(real + "\n")
            with self._env(ENGINE_PRODUCT_CHECKOUT=None):
                self.assertEqual(checkout_health.mechanic_orientation(root),
                                 {"product": "o/r", "checkout": real, "state": "resolved"})
            # a mechanic whose local path is unset -> path-unset, checkout None (the fork case)
            os.remove(os.path.join(root, ".engine", "mechanic", "product-checkout-path"))
            with self._env(ENGINE_PRODUCT_CHECKOUT=None):
                self.assertEqual(checkout_health.mechanic_orientation(root),
                                 {"product": "o/r", "checkout": None, "state": "path-unset"})

    def test_mechanic_orientation_separates_a_recorded_path_from_a_real_one(self):
        # A typo'd / moved / deleted folder must NOT read as ready to build in: it is its own state, so boot can
        # keep offering (and echo the bad value) instead of affirming a readiness it never checked.
        with tempfile.TemporaryDirectory() as tmp:
            root = _repo(tmp, "co")
            self._write_manifest(root, {"product_build_target": "o/r"})
            missing = os.path.join(tmp, "not-cloned-here")
            with self._env(ENGINE_PRODUCT_CHECKOUT=missing):
                self.assertEqual(checkout_health.mechanic_orientation(root),
                                 {"product": "o/r", "checkout": missing, "state": "path-unreachable"})

    def test_mechanic_orientation_tolerates_a_padded_env_value(self):
        # An env var pasted with stray whitespace must still resolve; without the strip it would read as a
        # folder that isn't there and nag for setup that is already correct. (The file seam strips separately.)
        with tempfile.TemporaryDirectory() as tmp:
            root = _repo(tmp, "co")
            self._write_manifest(root, {"product_build_target": "o/r"})
            real = os.path.join(tmp, "product-clone")
            os.makedirs(real)
            with self._env(ENGINE_PRODUCT_CHECKOUT=f"  {real}\n"):
                self.assertEqual(checkout_health.mechanic_orientation(root),
                                 {"product": "o/r", "checkout": real, "state": "resolved"})

    def test_mechanic_orientation_expands_a_home_relative_path(self):
        # `~/clone` is the most natural thing an operator writes, and `git -C` does not expand it — so the reader
        # must, or a correct path is reported unreachable and then refused at build time.
        with tempfile.TemporaryDirectory() as tmp:
            root = _repo(tmp, "co")
            self._write_manifest(root, {"product_build_target": "o/r"})
            with mock.patch.dict(os.environ, {"HOME": tmp}):
                os.makedirs(os.path.join(tmp, "clone"))
                with self._env(ENGINE_PRODUCT_CHECKOUT="~/clone"):
                    got = checkout_health.mechanic_orientation(root)
            self.assertEqual(got["state"], "resolved")
            self.assertEqual(got["checkout"], os.path.join(tmp, "clone"))
            self.assertNotIn("~", got["checkout"])


def _origin_and_dirty_subsumed(tmp: str, *, extra_upstream: bool = False,
                               work_creates_setup: bool = False) -> tuple:
    """#810 reconcile fixture. origin is seeded with a template LICENSE + shared.txt, then a `work` clone is
    taken at that seed. origin then advances by ONE commit — the setup a reviewed PR landed: REMOVE LICENSE, set
    shared.txt to the setup value, add setup.txt (and, with extra_upstream, an unrelated later `extra.txt` so the
    target is AHEAD of the transformation on paths work never touched). `work` then applies the SAME setup to its
    WORKING TREE, UNCOMMITTED — remove LICENSE, set shared.txt (and, with work_creates_setup, also create the
    untracked setup.txt). So `work` is behind + dirty, and its dirty changes are SUBSUMED by the verified target.
    Returns (work, origin). Hermetic; no network."""
    origin = os.path.join(tmp, "origin")
    os.makedirs(os.path.join(origin, ".claude"))
    os.makedirs(os.path.join(origin, ".engine"))
    with open(os.path.join(origin, ".claude", "settings.json"), "w") as fh:
        fh.write("{}")
    with open(os.path.join(origin, ".engine", "marker"), "w") as fh:
        fh.write("e")
    with open(os.path.join(origin, ".engine", "engine.json"), "w") as fh:
        # record a home_repository that is NOT this fixture's (local) origin, so the foreign-LICENSE carve-out
        # (repo_identity.is_home_repo) treats this as a deployed repo and the detector fires on the leftover seed.
        json.dump({"home_repository": "StarshipSuperjam/engine-template"}, fh)
    with open(os.path.join(origin, "LICENSE"), "w") as fh:
        fh.write(license_seeds.CURRENT_SEED)   # a REAL engine seed, so the foreign-LICENSE detector recognises it
    with open(os.path.join(origin, "shared.txt"), "w") as fh:
        fh.write("base\n")
    _git(origin, "init", "-q", "-b", "main")
    _gcommit(origin, "2026-06-01", "add", "-A")
    _gcommit(origin, "2026-06-01", "commit", "-q", "-m", "seed")
    work = os.path.join(tmp, "work")
    subprocess.run(["git", "clone", "-q", origin, work], capture_output=True, text=True, check=False)
    # origin advances: the equivalent setup landed through a reviewed PR (LICENSE removed, shared.txt set, setup
    # file added) and, optionally, a later unrelated PR — so the target supersedes the local transformation.
    os.remove(os.path.join(origin, "LICENSE"))
    with open(os.path.join(origin, "shared.txt"), "w") as fh:
        fh.write("setup\n")
    with open(os.path.join(origin, "setup.txt"), "w") as fh:
        fh.write("setup value\n")
    if extra_upstream:
        with open(os.path.join(origin, "extra.txt"), "w") as fh:
            fh.write("later unrelated PR\n")
    _gcommit(origin, "2026-06-02", "add", "-A")
    _gcommit(origin, "2026-06-02", "commit", "-q", "-m", "land the equivalent setup")
    # work applies the SAME setup to its working tree, UNCOMMITTED (the first-run-strand shape).
    os.remove(os.path.join(work, "LICENSE"))
    with open(os.path.join(work, "shared.txt"), "w") as fh:
        fh.write("setup\n")
    if work_creates_setup:
        with open(os.path.join(work, "setup.txt"), "w") as fh:
            fh.write("setup value\n")
    return work, origin


class TestReconcileSubsumed(unittest.TestCase):
    """#810: a behind checkout whose UNCOMMITTED changes are already SUBSUMED by the verified target (a first-run
    transformation the reviewed upstream absorbed) is reconciled LOSSLESSLY on consent — rescue-first, then
    brought current — instead of the plain lossless gate refusing it forever. Genuine unrelated work still blocks
    as a true no-op; losslessness never rests on the subsumption judgment."""

    def _rescue_branch(self, work: str) -> str:
        out = checkout_health._run(["git", "-C", work, "branch", "--list", "engine-rescue/*",
                                    "--format=%(refname:short)"]) or ""
        names = [n for n in out.splitlines() if n.strip()]
        self.assertEqual(len(names), 1, f"exactly one rescue branch expected, got {names}")
        return names[0]

    def test_superseding_target_is_rescued_then_brought_current(self):
        with tempfile.TemporaryDirectory() as tmp:
            work, _ = _origin_and_dirty_subsumed(tmp, extra_upstream=True)
            target = _consent_target(work)
            r = checkout_health.catch_up(cwd=work, apply=True, do_fetch=True, expected_target=target)
            self.assertEqual(r["status"], "fixed")
            self.assertTrue(r["reconciled"])
            self.assertTrue(r["applied"])
            self.assertEqual(_head(work).strip(), target)                     # local main == verified target
            self.assertEqual((checkout_health._run(["git", "-C", work, "symbolic-ref", "--short", "HEAD"])
                              or "").strip(), "main")
            self.assertFalse((checkout_health._run(["git", "-C", work, "status", "--porcelain"])
                              or "").strip(), "the working tree must be clean after reconcile")
            self.assertFalse(os.path.exists(os.path.join(work, "LICENSE")))   # target dropped it
            self.assertTrue(os.path.exists(os.path.join(work, "setup.txt")))  # target's setup file materialized
            self.assertTrue(os.path.exists(os.path.join(work, "extra.txt")))  # the superseding later PR too
            # the dirty state is preserved on the rescue branch (LICENSE removed + shared.txt set, but NOT the
            # target-only files) — nothing was lost.
            rescue = r["rescue"]
            self.assertIsNone(checkout_health._run(["git", "-C", work, "cat-file", "-e", f"{rescue}:LICENSE"]))
            self.assertEqual(checkout_health._run(["git", "-C", work, "show", f"{rescue}:shared.txt"]), "setup\n")
            self.assertIsNone(checkout_health._run(["git", "-C", work, "cat-file", "-e", f"{rescue}:extra.txt"]))

    def test_exact_match_is_rescued_then_brought_current(self):
        with tempfile.TemporaryDirectory() as tmp:
            work, _ = _origin_and_dirty_subsumed(tmp, extra_upstream=False, work_creates_setup=True)
            target = _consent_target(work)
            r = checkout_health.catch_up(cwd=work, apply=True, do_fetch=True, expected_target=target)
            self.assertEqual(r["status"], "fixed")
            self.assertTrue(r["reconciled"])
            self.assertEqual(_head(work).strip(), target)
            self.assertEqual(checkout_health._run(["git", "-C", work, "show", "HEAD:setup.txt"]), "setup value\n")

    def test_genuine_unrelated_work_still_blocks_as_a_true_no_op(self):
        with tempfile.TemporaryDirectory() as tmp:
            work, _ = _origin_and_dirty_subsumed(tmp, extra_upstream=True)
            # an unrelated edit to shared.txt that does NOT match the target -> not subsumed -> must NOT reconcile
            with open(os.path.join(work, "shared.txt"), "w") as fh:
                fh.write("my own unrelated edit\n")
            before = _head(work)
            target = _consent_target(work)
            r = checkout_health.catch_up(cwd=work, apply=True, do_fetch=True, expected_target=target)
            self.assertEqual(r["status"], "blocked")
            self.assertEqual(r["reason"], "local-work")
            self.assertNotIn("rescue", r, "an unsubsumed dirty tree must NOT be rescued (true no-op)")
            self.assertEqual(_head(work), before)                              # HEAD never moved
            with open(os.path.join(work, "shared.txt")) as fh:
                self.assertEqual(fh.read(), "my own unrelated edit\n")         # the edit is intact
            self.assertFalse(os.path.exists(os.path.join(work, "setup.txt")))  # nothing pulled in
            branches = checkout_health._run(["git", "-C", work, "branch", "--list", "engine-rescue/*"]) or ""
            self.assertFalse(branches.strip(), "no rescue branch is created on the no-op path")

    def test_dry_run_never_reconciles_or_mutates(self):
        with tempfile.TemporaryDirectory() as tmp:
            work, _ = _origin_and_dirty_subsumed(tmp, extra_upstream=True)
            before = _head(work)
            r = checkout_health.catch_up(cwd=work, apply=False, do_fetch=True)
            self.assertEqual(r["status"], "behind")
            self.assertFalse(r["applied"])
            self.assertEqual(_head(work), before)
            self.assertFalse((checkout_health._run(["git", "-C", work, "branch", "--list", "engine-rescue/*"])
                              or "").strip())

    def test_a_wrong_subsumed_judgment_loses_nothing(self):
        # The losslessness proof: force BOTH the read-only pre-check AND the authoritative gate to (wrongly) say
        # "subsumed" on a tree that is NOT subsumed. Reconcile then adopts the target, but the discarded dirty
        # content is fully recoverable on the retained rescue branch — losslessness never rested on the judgment.
        with tempfile.TemporaryDirectory() as tmp:
            work, _ = _origin_and_dirty_subsumed(tmp, extra_upstream=True)
            with open(os.path.join(work, "shared.txt"), "w") as fh:
                fh.write("genuinely divergent content the target does NOT have\n")
            target = _consent_target(work)
            with mock.patch.object(checkout_health, "_dirty_subsumed", return_value=True), \
                 mock.patch.object(checkout_health, "_commit_subsumed", return_value=True):
                r = checkout_health.catch_up(cwd=work, apply=True, do_fetch=True, expected_target=target)
            self.assertEqual(r["status"], "fixed")
            self.assertEqual(_head(work).strip(), target)
            rescue = r["rescue"]
            self.assertEqual(
                checkout_health._run(["git", "-C", work, "show", f"{rescue}:shared.txt"]),
                "genuinely divergent content the target does NOT have\n",
                "the discarded working-tree content must be fully recoverable on the rescue branch")

    def test_authoritative_gate_catches_a_wrong_pre_check_and_keeps_work_safe(self):
        # If only the CHEAP pre-check is wrong (approximation optimistic) the authoritative post-rescue gate still
        # declines: nothing is adopted, HEAD returns to the default, and the work is safe on the rescue branch.
        with tempfile.TemporaryDirectory() as tmp:
            work, _ = _origin_and_dirty_subsumed(tmp, extra_upstream=True)
            with open(os.path.join(work, "shared.txt"), "w") as fh:
                fh.write("not actually upstream\n")
            before = _head(work)
            target = _consent_target(work)
            with mock.patch.object(checkout_health, "_dirty_subsumed", return_value=True):
                r = checkout_health.catch_up(cwd=work, apply=True, do_fetch=True, expected_target=target)
            self.assertEqual(r["status"], "blocked")
            self.assertEqual(r["reason"], "local-work")
            self.assertFalse(r["reconciled"])
            self.assertEqual(_head(work).strip(), before.strip())             # target NOT adopted
            self.assertEqual((checkout_health._run(["git", "-C", work, "symbolic-ref", "--short", "HEAD"])
                              or "").strip(), "main")                          # HEAD back on the default
            self.assertEqual(checkout_health._run(["git", "-C", work, "show", f"{r['rescue']}:shared.txt"]),
                             "not actually upstream\n")                        # work safe on rescue

    def test_rescue_failure_refuses_and_keeps_work(self):
        with tempfile.TemporaryDirectory() as tmp:
            work, _ = _origin_and_dirty_subsumed(tmp, extra_upstream=True)
            before = _head(work)
            target = _consent_target(work)
            with mock.patch.object(checkout_health, "save_recovery_point", return_value=None):
                r = checkout_health.catch_up(cwd=work, apply=True, do_fetch=True, expected_target=target)
            self.assertEqual(r["status"], "blocked")
            self.assertEqual(r["reason"], "rescue-failed")
            self.assertFalse(r["applied"])
            self.assertEqual(_head(work), before)
            with open(os.path.join(work, "shared.txt")) as fh:
                self.assertEqual(fh.read(), "setup\n")                        # the dirty tree is untouched

    def test_a_stash_alongside_dirty_still_blocks_and_never_reconciles(self):
        with tempfile.TemporaryDirectory() as tmp:
            work, _ = _origin_and_dirty_subsumed(tmp, extra_upstream=True)
            # a stash makes _is_lossless reasons != ["uncommitted"], so reconcile must NOT engage
            _git(work, "stash", "push", "-u", "-m", "some other work")
            os.remove(os.path.join(work, "LICENSE"))
            with open(os.path.join(work, "shared.txt"), "w") as fh:
                fh.write("setup\n")
            target = _consent_target(work)
            r = checkout_health.catch_up(cwd=work, apply=True, do_fetch=True, expected_target=target)
            self.assertEqual(r["status"], "blocked")
            self.assertEqual(r["reason"], "local-work")
            self.assertIn("stash", r["reasons"])
            self.assertNotIn("rescue", r)
            stash = checkout_health._run(["git", "-C", work, "stash", "list"]) or ""
            self.assertIn("some other work", stash)                           # the stash is byte-intact

    def test_off_default_dirty_subsumed_reconciles_via_return_to_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            work, _ = _origin_and_dirty_subsumed(tmp, extra_upstream=True)
            _git(work, "stash", "push", "-u")            # park the dirty tree so we can make a side branch...
            _git(work, "checkout", "-q", "-b", "my-feature")
            _git(work, "stash", "pop")                   # ...then restore the dirty subsumed tree on the side branch
            target = _consent_target(work)
            r = checkout_health.return_to_default(cwd=work, apply=True, do_fetch=True, expected_target=target)
            self.assertEqual(r["status"], "fixed")
            self.assertTrue(r["reconciled"])
            self.assertEqual(_head(work).strip(), target)
            self.assertEqual((checkout_health._run(["git", "-C", work, "symbolic-ref", "--short", "HEAD"])
                              or "").strip(), "main")
            # the side branch still exists (its own ref is untouched) and the dirty work is on the rescue branch
            self.assertTrue((checkout_health._run(["git", "-C", work, "branch", "--list", "my-feature"])
                             or "").strip())
            self.assertEqual(checkout_health._run(["git", "-C", work, "show", f"{r['rescue']}:shared.txt"]),
                             "setup\n")

    def test_catch_up_off_default_dirty_still_declines_off_main(self):
        # catch_up never touches a side branch: parked off-default it declines BEFORE the reconcile arm, no mutation.
        with tempfile.TemporaryDirectory() as tmp:
            work, _ = _origin_and_dirty_subsumed(tmp, extra_upstream=True)
            _git(work, "stash", "push", "-u")
            _git(work, "checkout", "-q", "-b", "my-feature")
            _git(work, "stash", "pop")
            before = _head(work)
            r = checkout_health.catch_up(cwd=work, apply=True, do_fetch=True, expected_target=_consent_target(work))
            self.assertEqual(r["status"], "off-main")
            self.assertFalse(r["applied"])
            self.assertEqual(_head(work), before)
            self.assertFalse((checkout_health._run(["git", "-C", work, "branch", "--list", "engine-rescue/*"])
                              or "").strip())


class TestReconcileFailSafe(unittest.TestCase):
    """#810 review fixes: the subsumption predicates fail CLOSED on an unreadable git read (never treat a failed
    diff as 'nothing changed -> subsumed'); a rescue that fails AFTER its branch switch returns HEAD to the
    original branch (no false 'untouched', no stray branch); and the new operator-facing CLI messages render."""

    def _blocking_run(self, predicate):
        """A _run that returns None (a git read failure) for commands matching `predicate`, else the real read."""
        real = checkout_health._run

        def flaky(cmd, **kw):
            return None if predicate(cmd) else real(cmd, **kw)
        return flaky

    def test_commit_subsumed_is_conservative_on_an_unreadable_diff(self):
        with tempfile.TemporaryDirectory() as tmp:
            work, _ = _origin_and_dirty_subsumed(tmp)
            with mock.patch.object(checkout_health, "_run",
                                   side_effect=self._blocking_run(lambda c: "--name-only" in c)):
                # the name-diff read fails -> must return False (NOT subsumed), never a false True from empty paths
                self.assertFalse(checkout_health._commit_subsumed(work, "HEAD", "HEAD", "HEAD"))

    def test_dirty_subsumed_conservative_when_the_tracked_diff_read_fails(self):
        # DISCRIMINATING: the tree is genuinely NOT subsumed (a divergent tracked edit) but ALSO has an untracked
        # file that matches the target. Pre-fix, a None tracked-diff coalesced to [] and the untracked loop then
        # (wrongly) returned True; the fix must return False because the tracked read was unreadable.
        with tempfile.TemporaryDirectory() as tmp:
            work, _ = _origin_and_dirty_subsumed(tmp, extra_upstream=True, work_creates_setup=True)
            with open(os.path.join(work, "shared.txt"), "w", encoding="utf-8") as fh:
                fh.write("divergent content the target does NOT have\n")   # genuinely unsubsumed tracked change
            target = _consent_target(work)
            with mock.patch.object(checkout_health, "_run",
                                   side_effect=self._blocking_run(lambda c: "diff" in c and "--name-only" in c)):
                self.assertFalse(checkout_health._dirty_subsumed(work, target))

    def test_dirty_subsumed_conservative_when_the_untracked_list_read_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            work, _ = _origin_and_dirty_subsumed(tmp, extra_upstream=True, work_creates_setup=True)
            target = _consent_target(work)
            with mock.patch.object(checkout_health, "_run",
                                   side_effect=self._blocking_run(lambda c: "ls-files" in c)):
                self.assertFalse(checkout_health._dirty_subsumed(work, target))

    def test_rescue_commit_failure_restores_head_and_reports_honestly(self):
        # A REAL partial failure: the rescue's `git commit` fails (a rejecting pre-commit hook) AFTER
        # save_recovery_point has switched onto a new engine-rescue branch. The arm must return HEAD to the
        # original branch, leave no stray branch, keep the dirty work, and report honestly — not "untouched"
        # while stranded on a rescue branch (the exact incoherence #810 set out to cure).
        with tempfile.TemporaryDirectory() as tmp:
            work, _ = _origin_and_dirty_subsumed(tmp, extra_upstream=True)
            hook = os.path.join(work, ".git", "hooks", "pre-commit")
            with open(hook, "w", encoding="utf-8") as fh:
                fh.write("#!/bin/sh\nexit 1\n")
            os.chmod(hook, 0o755)
            target = _consent_target(work)
            r = checkout_health.catch_up(cwd=work, apply=True, do_fetch=True, expected_target=target)
            self.assertEqual(r["status"], "blocked")
            self.assertEqual(r["reason"], "rescue-failed")
            self.assertTrue(r["restored"], "HEAD must be returned to the original branch")
            self.assertFalse(r["applied"])
            self.assertEqual((checkout_health._run(["git", "-C", work, "symbolic-ref", "--short", "HEAD"])
                              or "").strip(), "main")
            self.assertFalse((checkout_health._run(["git", "-C", work, "branch", "--list", "engine-rescue/*"])
                              or "").strip(), "no stray rescue branch is left behind")
            self.assertFalse(os.path.exists(os.path.join(work, "LICENSE")))     # the dirty deletion is intact
            with open(os.path.join(work, "shared.txt")) as fh:
                self.assertEqual(fh.read(), "setup\n")                          # the dirty edit is intact

    def test_rescue_commit_succeeds_but_hook_redirties_reports_incomplete(self):
        # The subtle sub-case: the rescue COMMIT lands, but a post-commit hook writes a file so the tree is dirty
        # again -> save_recovery_point returns None even though the work WAS saved. The arm must NOT claim
        # "couldn't save"; it detects the surviving rescue commit (branch -d refuses it) and names it honestly.
        with tempfile.TemporaryDirectory() as tmp:
            work, _ = _origin_and_dirty_subsumed(tmp, extra_upstream=True)
            hook = os.path.join(work, ".git", "hooks", "post-commit")
            with open(hook, "w", encoding="utf-8") as fh:
                fh.write("#!/bin/sh\necho artifact > hook-artifact.txt\n")
            os.chmod(hook, 0o755)
            target = _consent_target(work)
            r = checkout_health.catch_up(cwd=work, apply=True, do_fetch=True, expected_target=target)
            self.assertEqual(r["status"], "blocked")
            self.assertEqual(r["reason"], "rescue-incomplete")
            self.assertTrue(r["rescue"].startswith("engine-rescue/"))
            # the work is genuinely on the named rescue branch (LICENSE removed, shared.txt set)
            self.assertEqual(checkout_health._run(["git", "-C", work, "show", f"{r['rescue']}:shared.txt"]),
                             "setup\n")
            self.assertIsNone(checkout_health._run(["git", "-C", work, "cat-file", "-e", f"{r['rescue']}:LICENSE"]))
            self.assertEqual((checkout_health._run(["git", "-C", work, "symbolic-ref", "--short", "HEAD"])
                              or "").strip(), "main")                            # HEAD back on the default

    def _render(self, fn, result: dict) -> str:
        # exercise the real plain-language renderer against a crafted result dict (never touches a real checkout)
        target = "catch_up" if fn is checkout_health._plain_catch_up else "return_to_default"
        buf = io.StringIO()
        with mock.patch.object(checkout_health, target, return_value=result), \
                contextlib.redirect_stdout(buf):
            fn(apply=True)
        return buf.getvalue()

    def test_cli_renders_reconciled_success_with_the_rescue_branch(self):
        out = self._render(checkout_health._plain_catch_up,
                           {"status": "fixed", "reconciled": True, "rescue": "engine-rescue/abc123",
                            "applied": True})
        self.assertIn("engine-rescue/abc123", out)
        self.assertIn("nothing was lost", out.lower())

    def test_cli_renders_rescue_failed_honestly_by_restored(self):
        restored = self._render(checkout_health._plain_catch_up,
                                {"status": "blocked", "reason": "rescue-failed", "restored": True, "applied": False})
        self.assertIn("back exactly as it was", restored.lower())
        not_restored = self._render(checkout_health._plain_catch_up,
                                    {"status": "blocked", "reason": "rescue-failed", "restored": False,
                                     "applied": True})
        self.assertIn("check the folder", not_restored.lower())

    def test_cli_renders_not_subsumed_block_with_rescue(self):
        out = self._render(checkout_health._plain_catch_up,
                           {"status": "blocked", "reason": "local-work", "reconciled": False,
                            "rescue": "engine-rescue/def456", "restored": True, "applied": True})
        self.assertIn("engine-rescue/def456", out)
        self.assertIn("not to be part of the shared project", out.lower())

    def test_cli_postcondition_failed_keeps_its_specific_message_over_the_generic_rescue_line(self):
        out = self._render(checkout_health._plain_catch_up,
                           {"status": "blocked", "reason": "postcondition-failed",
                            "rescue": "engine-rescue/ghi789", "applied": True})
        self.assertIn("raced the final update check", out.lower())
        self.assertIn("engine-rescue/ghi789", out)

    def test_cli_renders_rescue_incomplete_naming_the_branch(self):
        for fn in (checkout_health._plain_catch_up, checkout_health._plain_return_to_default):
            out = self._render(fn, {"status": "blocked", "reason": "rescue-incomplete",
                                    "rescue": "engine-rescue/jkl012", "restored": True, "applied": True})
            self.assertIn("engine-rescue/jkl012", out)
            self.assertIn("safe on that branch", out.lower())
            self.assertNotIn("couldn't save", out.lower())   # never the false "nothing saved" wording

    def test_cli_return_to_default_postcondition_failed_names_the_rescue_branch(self):
        # DH nit: the reconcile arm's postcondition-failed (rescue set, no `restored`) must still point at the
        # rescue branch, not leave the operator without the safe copy's location.
        out = self._render(checkout_health._plain_return_to_default,
                           {"status": "blocked", "reason": "postcondition-failed",
                            "rescue": "engine-rescue/mno345", "applied": True})
        self.assertIn("engine-rescue/mno345", out)

    def test_cli_return_to_default_renders_reconciled_success(self):
        out = self._render(checkout_health._plain_return_to_default,
                           {"status": "fixed", "reconciled": True, "rescue": "engine-rescue/xyz",
                            "applied": True})
        self.assertIn("engine-rescue/xyz", out)
        self.assertIn("nothing was lost", out.lower())


class TestFirstRunStrandRegression(unittest.TestCase):
    """#810 end-to-end regression: template checkout -> first-run leaves the transformation dirty -> the
    equivalent setup lands upstream (target advances and drops LICENSE) -> the operator checkout stays old and
    dirty. The lossless reconcile brings it current with the dirty state preserved on a rescue branch, AND the
    now-current HEAD no longer trips the foreign-LICENSE detector — the two misleading notices both clear."""

    def test_reconcile_clears_the_stranded_checkout_and_the_license_notice(self):
        import license_health
        with tempfile.TemporaryDirectory() as tmp:
            work, _ = _origin_and_dirty_subsumed(tmp, extra_upstream=True)
            target = _consent_target(work)
            # Before: the checkout is behind + dirty, and its committed HEAD still carries the template seed —
            # exactly the stale-HEAD input the incident's second (misleading) LICENSE notice was read from...
            self.assertIsNotNone(
                license_seeds.matched_seed_id(license_health._committed(work, "LICENSE") or ""),
                "the stranded checkout's committed HEAD still carries the leftover template LICENSE seed")
            # ...while the VERIFIED TARGET has already dropped it, so the correlation predicate would suppress the
            # redundant offer even before catch-up (the Part 3 coherence fix).
            self.assertTrue(license_health.license_absent_upstream(work, target),
                            "the verified target already removed LICENSE -> the redundant offer is suppressed")
            r = checkout_health.catch_up(cwd=work, apply=True, do_fetch=True, expected_target=target)
            self.assertEqual(r["status"], "fixed")
            self.assertTrue(r["reconciled"])
            # After: local main == verified target, clean, and the committed HEAD carries no LICENSE at all — the
            # detector's fire condition is gone for good; the dirty transformation is preserved on the rescue branch.
            self.assertEqual(_head(work).strip(), target)
            self.assertFalse((checkout_health._run(["git", "-C", work, "status", "--porcelain"]) or "").strip())
            self.assertIsNone(license_health._committed(work, "LICENSE"),
                              "once current, the committed HEAD carries no LICENSE -> the notice cannot re-fire")
            self.assertEqual(checkout_health._run(["git", "-C", work, "show", f"{r['rescue']}:shared.txt"]),
                             "setup\n")


def _mechanic_with_target(tmp: str, name: str = "mechanic", target: str | None = "acme/product") -> str:
    """A mechanic checkout whose manifest records (or omits) a product_build_target."""
    root = _repo(tmp, name)
    manifest = {"product_build_target": target} if target else {"engine_release": "1.0.0"}
    with open(os.path.join(root, ".engine", "engine.json"), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh)
    return root


def _product_with_origin(tmp: str, name: str = "product",
                         origin: str = "git@github.com:acme/product.git") -> str:
    root = _repo(tmp, name)
    _git(root, "remote", "add", "origin", origin)
    return root


class TestProductBuildSprawl(unittest.TestCase):
    """The negative control (engine-template#902): stray product worktrees and sibling clones are surfaced so a
    regression to the old sprawl is CAUGHT, while the sanctioned .engine/mechanic/worktrees/ home reads clean."""

    def test_clean_mechanic_reports_no_sprawl(self):
        with tempfile.TemporaryDirectory() as tmp:
            m = _mechanic_with_target(tmp)
            p = _product_with_origin(tmp)
            with mock.patch.dict(os.environ, {"ENGINE_PRODUCT_CHECKOUT": p}):
                self.assertIsNone(checkout_health.detect_product_build_sprawl(cwd=m))

    def test_not_a_mechanic_reports_no_sprawl(self):
        with tempfile.TemporaryDirectory() as tmp:
            m = _mechanic_with_target(tmp, target=None)
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("ENGINE_PRODUCT_CHECKOUT", None)
                self.assertIsNone(checkout_health.detect_product_build_sprawl(cwd=m))

    def test_stray_worktree_outside_the_sanctioned_home_is_flagged(self):
        with tempfile.TemporaryDirectory() as tmp:
            m = _mechanic_with_target(tmp)
            p = _product_with_origin(tmp)
            stray = os.path.join(tmp, "loose-wt")            # NOT under m/.engine/mechanic/worktrees
            _git(p, "worktree", "add", "-q", "--detach", stray)
            with mock.patch.dict(os.environ, {"ENGINE_PRODUCT_CHECKOUT": p}):
                got = checkout_health.detect_product_build_sprawl(cwd=m)
            self.assertIsNotNone(got)
            self.assertIn(os.path.realpath(stray), got["stray_worktrees"])
            self.assertEqual(got["sibling_clones"], [])

    def test_sanctioned_worktree_is_not_flagged(self):
        with tempfile.TemporaryDirectory() as tmp:
            m = _mechanic_with_target(tmp)
            p = _product_with_origin(tmp)
            ok = os.path.join(m, ".engine", "mechanic", "worktrees", "902-x")
            _git(p, "worktree", "add", "-q", "--detach", ok)   # the sanctioned home — must read clean
            with mock.patch.dict(os.environ, {"ENGINE_PRODUCT_CHECKOUT": p}):
                self.assertIsNone(checkout_health.detect_product_build_sprawl(cwd=m))

    def test_sibling_clone_with_matching_origin_is_flagged(self):
        with tempfile.TemporaryDirectory() as tmp:
            m = _mechanic_with_target(tmp)
            p = _product_with_origin(tmp)                      # basename "product"
            sib = _product_with_origin(tmp, name="product-656-labels")   # same origin, sibling folder
            with mock.patch.dict(os.environ, {"ENGINE_PRODUCT_CHECKOUT": p}):
                got = checkout_health.detect_product_build_sprawl(cwd=m)
            self.assertIsNotNone(got)
            self.assertIn(os.path.realpath(sib), got["sibling_clones"])

    def test_sibling_folder_with_a_different_origin_is_not_flagged(self):
        with tempfile.TemporaryDirectory() as tmp:
            m = _mechanic_with_target(tmp)
            p = _product_with_origin(tmp)
            _product_with_origin(tmp, name="product-unrelated", origin="git@github.com:acme/other.git")
            with mock.patch.dict(os.environ, {"ENGINE_PRODUCT_CHECKOUT": p}):
                self.assertIsNone(checkout_health.detect_product_build_sprawl(cwd=m))


class TestEngineRootAndDefaultSeams(unittest.TestCase):
    """The two public seams the mechanic build entry rides: the durable engine root (even from a linked
    worktree) and the confident default branch (never a guess)."""

    def test_engine_common_checkout_resolves_the_main_from_a_linked_worktree(self):
        with tempfile.TemporaryDirectory() as tmp:
            main = _repo(tmp, "eng")
            wt = os.path.join(tmp, "wt")
            _git(main, "worktree", "add", "-q", "--detach", wt)
            self.assertEqual(os.path.realpath(checkout_health.engine_common_checkout(cwd=wt)),
                             os.path.realpath(main))

    def test_engine_common_checkout_is_none_outside_a_repo(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(checkout_health.engine_common_checkout(cwd=tmp))

    def test_confident_default_branch_reads_a_clone_origin_head(self):
        with tempfile.TemporaryDirectory() as tmp:
            seed = _repo(tmp, "seed")
            bare = os.path.join(tmp, "remote.git")
            subprocess.run(["git", "clone", "--quiet", "--bare", seed, bare], capture_output=True, text=True)
            clone = os.path.join(tmp, "clone")
            subprocess.run(["git", "clone", "--quiet", bare, clone], capture_output=True, text=True)
            got = checkout_health.confident_default_branch(clone)
            head = subprocess.run(["git", "-C", clone, "symbolic-ref", "--short", "refs/remotes/origin/HEAD"],
                                  capture_output=True, text=True).stdout.strip()
            self.assertEqual(got, head.split("origin/", 1)[1])       # the default, without the origin/ prefix

    def test_confident_default_branch_is_none_without_a_confident_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            solo = _repo(tmp, "solo")                                # no origin/HEAD, no persisted default
            self.assertIsNone(checkout_health.confident_default_branch(solo))


if __name__ == "__main__":
    unittest.main()
