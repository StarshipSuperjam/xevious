#!/usr/bin/env python3
"""Tests for boot_alarm_ledger — the standing-alarm presentation ledger.

These lock the behaviours a non-engineer cannot read code to verify: an UNCHANGED standing alarm collapses
(decide -> "collapse") only after a true full relay; a NEW/CHANGED one renders full with the prior value
exposed for worsening labels; a VANISHED alarm is dropped so a recurrence relays full again; the ledger is
FAIL-TOWARD-FULL on a missing/corrupt/unwritable store; shown-in-full is stamped only on a true full relay
(no suppression-by-drift); the path resolves to a stable per-instance root (not an ephemeral worktree); and
the module imports nothing from boot (a one-way boot -> ledger dependency, sweep-isolation)."""
from __future__ import annotations

import ast
import json
import os
import tempfile
import unittest
from unittest import mock

import boot_alarm_ledger as bal


def _decide(alarms, path):
    return bal.decide(alarms, path=path)


class TestCollapseDecision(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "standing-alarms.json")

    def test_first_ever_session_renders_full_then_seeds(self):
        # No ledger yet -> full (neutral), ok False; and the ledger is seeded so the NEXT session can collapse.
        out = _decide([{"key": "findings", "value": 20}], self.path)
        self.assertFalse(out["ok"])  # missing ledger -> neutral full, never a misleading "still"/"worse"
        self.assertEqual(out["results"]["findings"]["outcome"], "full")
        self.assertTrue(os.path.isfile(self.path))

    def test_unchanged_condition_collapses_after_a_full_relay(self):
        _decide([{"key": "findings", "value": 20}], self.path)             # full (seed)
        out = _decide([{"key": "findings", "value": 20}], self.path)       # same -> collapse
        self.assertTrue(out["ok"])
        self.assertEqual(out["results"]["findings"]["outcome"], "collapse")

    def test_changed_condition_relays_full_with_prior(self):
        _decide([{"key": "findings", "value": 20}], self.path)
        _decide([{"key": "findings", "value": 20}], self.path)             # collapse
        out = _decide([{"key": "findings", "value": 25}], self.path)       # changed -> full, prior exposed
        self.assertEqual(out["results"]["findings"]["outcome"], "full")
        self.assertEqual(out["results"]["findings"]["prior"], 20)          # so the renderer can say "got worse"

    def test_vanished_alarm_is_dropped_so_a_recurrence_relays_full(self):
        _decide([{"key": "findings", "value": 20}], self.path)            # full (seed)
        _decide([], self.path)                                            # the alarm is gone -> dropped
        out = _decide([{"key": "findings", "value": 20}], self.path)      # recurs -> full again (NOT collapse)
        self.assertEqual(out["results"]["findings"]["outcome"], "full")
        self.assertIsNone(out["results"]["findings"]["prior"])            # no stale baseline survived

    def test_shown_in_full_is_only_stamped_on_a_true_full_relay(self):
        # The suppression-by-drift guard: drive full -> collapse -> collapse; the baseline stays the full value,
        # so a later genuine change still relays full (a terse render never becomes the collapse baseline).
        _decide([{"key": "gate", "value": ["off", "a"]}], self.path)     # full (seed)
        c1 = _decide([{"key": "gate", "value": ["off", "a"]}], self.path)  # collapse
        c2 = _decide([{"key": "gate", "value": ["off", "a"]}], self.path)  # collapse again
        self.assertEqual(c1["results"]["gate"]["outcome"], "collapse")
        self.assertEqual(c2["results"]["gate"]["outcome"], "collapse")
        changed = _decide([{"key": "gate", "value": ["off", "b"]}], self.path)
        self.assertEqual(changed["results"]["gate"]["outcome"], "full")
        self.assertEqual(changed["results"]["gate"]["prior"], ["off", "a"])

    def test_two_independent_alarms_collapse_independently(self):
        _decide([{"key": "gate", "value": ["off", "a"]}, {"key": "findings", "value": 3}], self.path)
        out = _decide([{"key": "gate", "value": ["off", "a"]}, {"key": "findings", "value": 4}], self.path)
        self.assertEqual(out["results"]["gate"]["outcome"], "collapse")   # gate unchanged
        self.assertEqual(out["results"]["findings"]["outcome"], "full")   # findings changed
        # and the gate baseline persisted across a session that wrote a findings change
        again = _decide([{"key": "gate", "value": ["off", "a"]}, {"key": "findings", "value": 4}], self.path)
        self.assertEqual(again["results"]["gate"]["outcome"], "collapse")
        self.assertEqual(again["results"]["findings"]["outcome"], "collapse")


class TestFailTowardFull(unittest.TestCase):
    def test_corrupt_ledger_fails_to_full(self):
        d = tempfile.mkdtemp()
        path = os.path.join(d, "standing-alarms.json")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("}{ not json")
        out = _decide([{"key": "findings", "value": 20}], path)
        self.assertFalse(out["ok"])
        self.assertEqual(out["results"]["findings"]["outcome"], "full")
        # and it self-heals: the garbage is overwritten with a valid seed, so next session can collapse
        nxt = _decide([{"key": "findings", "value": 20}], path)
        self.assertEqual(nxt["results"]["findings"]["outcome"], "collapse")

    def test_non_dict_ledger_fails_to_full(self):
        d = tempfile.mkdtemp()
        path = os.path.join(d, "standing-alarms.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(["not", "a", "dict"], fh)
        out = _decide([{"key": "findings", "value": 20}], path)
        self.assertFalse(out["ok"])
        self.assertEqual(out["results"]["findings"]["outcome"], "full")

    def test_unwritable_location_fails_to_full_and_never_raises(self):
        # A path whose parent is a FILE (not a dir): makedirs fails -> fail-toward-full, no exception.
        d = tempfile.mkdtemp()
        blocker = os.path.join(d, "afile")
        with open(blocker, "w", encoding="utf-8") as fh:
            fh.write("x")
        path = os.path.join(blocker, "standing-alarms.json")  # parent is a file
        out = _decide([{"key": "findings", "value": 20}], path)
        self.assertFalse(out["ok"])
        self.assertEqual(out["results"]["findings"]["outcome"], "full")

    def test_malformed_alarm_entry_fails_to_full_and_never_raises(self):
        # A malformed alarm (missing "key") must degrade to fail-toward-full, never raise into the hook.
        out = bal.decide([{"value": 5}], path=os.path.join(tempfile.mkdtemp(), "l.json"))
        self.assertFalse(out["ok"])
        self.assertEqual(out["results"], {})  # empty -> the renderer defaults every alarm to full

    def test_no_eligible_alarms_yields_no_results_and_never_crashes(self):
        out = _decide([], os.path.join(tempfile.mkdtemp(), "l.json"))
        self.assertEqual(out["results"], {})

    def test_empty_alarm_set_drops_a_prior_entry(self):
        # The vanish path at the decide level: a populated ledger, then a session with no eligible alarms,
        # clears the ledger so a later recurrence relays full (no stale baseline survives).
        d = tempfile.mkdtemp()
        path = os.path.join(d, "l.json")
        _decide([{"key": "findings", "value": 5}], path)   # seed
        _decide([], path)                                  # all alarms gone -> drop
        out = _decide([{"key": "findings", "value": 5}], path)
        self.assertEqual(out["results"]["findings"]["outcome"], "full")


class TestPathResolution(unittest.TestCase):
    def test_env_override_wins(self):
        d = tempfile.mkdtemp()
        with mock.patch.dict(os.environ, {bal.ENV_DIR: d}):
            self.assertEqual(bal.ledger_dir(), os.path.abspath(d))
            self.assertEqual(bal.ledger_path(), os.path.join(os.path.abspath(d), bal.LEDGER_FILENAME))

    def test_explicit_path_arg_wins_over_everything(self):
        self.assertEqual(bal.ledger_path(path="/tmp/x.json"), "/tmp/x.json")

    def test_default_dir_is_the_boot_cache_under_a_clone_root(self):
        # No env override: resolves under <root>/.engine/boot/.cache (the gitignored, .cache-pruned home).
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(bal.ENV_DIR, None)
            d = bal.ledger_dir()
        self.assertTrue(d.endswith(os.path.join(".engine", "boot", ".cache")), d)


class TestSweepIsolation(unittest.TestCase):
    def test_module_does_not_import_boot_or_memory(self):
        # The ledger shares NO code path with memory's sweep; the dependency is one-way boot -> ledger.
        with open(bal.__file__, encoding="utf-8") as fh:
            src = fh.read()
        tree = ast.parse(src)
        imported: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported += [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                imported.append(node.module or "")
        for forbidden in ("boot", "memory"):
            self.assertNotIn(forbidden, imported,
                             f"boot_alarm_ledger must not import {forbidden} (one-way dependency / sweep isolation)")
            self.assertFalse(any(m.startswith(forbidden + ".") for m in imported),
                             f"boot_alarm_ledger must not import a {forbidden} submodule")


class TestRetireAndSection15(unittest.TestCase):
    """The kept-on-purpose intent-exit (#471): a retired marker stops a retire-eligible finding, survives
    the hook's decide() rewrites, and — the weakening-guard guarantee — can NEVER silence a governance alarm."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "standing-alarms.json")

    def test_retire_then_is_retired_for_an_eligible_class(self):
        self.assertFalse(bal.is_retired("fp1", "foreign_license", path=self.path))
        self.assertTrue(bal.retire("fp1", "foreign_license", path=self.path)["ok"])
        self.assertTrue(bal.is_retired("fp1", "foreign_license", path=self.path))

    def test_a_governance_class_marker_is_never_honored(self):
        # Weakening-guard falsification: even with a marker recorded at a fingerprint, a NON-eligible (governance) class is
        # never honored — a mis-written / injection-planted marker cannot silence a governance alarm.
        bal.retire("fp-gov", "foreign_license", path=self.path)   # a marker now exists at this fingerprint
        self.assertFalse(bal.is_retired("fp-gov", "gate", path=self.path))
        self.assertFalse(bal.is_retired("fp-gov", "findings", path=self.path))

    def test_retire_greenfield_cli_derives_the_fingerprint_from_the_detector(self):
        # The self-deriving dismiss: the CLI reads the LIVE detector's fingerprint (never a hand-supplied value
        # that could silently mismatch), so the marker it writes is exactly the one is_retired later checks.
        import greenfield_intake
        with mock.patch.dict(os.environ, {bal.ENV_DIR: self.dir}):
            with mock.patch.object(greenfield_intake, "detect_greenfield",
                                   return_value={"greenfield": True, "fingerprint": "greenfield"}):
                self.assertEqual(bal.main(["retire-greenfield"]), 0)
            # the marker the detector's real fingerprint would resolve to is now honored
            self.assertTrue(bal.is_retired("greenfield", "greenfield_intake"))

    def test_retire_greenfield_cli_reports_nothing_to_retire_when_not_greenfield(self):
        import greenfield_intake
        with mock.patch.dict(os.environ, {bal.ENV_DIR: self.dir}):
            with mock.patch.object(greenfield_intake, "detect_greenfield", return_value=None):
                self.assertEqual(bal.main(["retire-greenfield"]), 1)

    def test_retire_refuses_a_non_eligible_class_at_write_time(self):
        r = bal.retire("x", "gate", path=self.path)
        self.assertFalse(r["ok"])
        self.assertEqual(r["reason"], "not-retire-eligible")
        self.assertFalse(os.path.isfile(self.path))   # and nothing was written

    def test_retired_marker_survives_a_decide_pass_over_other_keys(self):
        # B1 durability: the SessionStart hook's decide() rebuilds the collapse baselines every boot; it must NOT
        # erase a retire marker, or the intent-exit would last exactly one session.
        bal.retire("fp1", "foreign_license", path=self.path)
        bal.decide([{"key": "gate", "value": ["off", "x"]}, {"key": "findings", "value": 3}], path=self.path)
        self.assertTrue(bal.is_retired("fp1", "foreign_license", path=self.path))

    def test_retired_marker_survives_decide_even_when_no_keys_are_live(self):
        bal.retire("fp1", "foreign_license", path=self.path)
        bal.decide([], path=self.path)   # every alarm vanished -> a full rewrite
        self.assertTrue(bal.is_retired("fp1", "foreign_license", path=self.path))

    def test_retire_preserves_an_existing_collapse_baseline(self):
        # The reverse-direction clobber: writing a retire marker must not wipe a collapse baseline decide() set.
        bal.decide([{"key": "findings", "value": 7}], path=self.path)          # seed a full baseline
        bal.retire("fp1", "foreign_license", path=self.path)
        out = bal.decide([{"key": "findings", "value": 7}], path=self.path)    # unchanged -> still collapses
        self.assertEqual(out["results"]["findings"]["outcome"], "collapse")
        self.assertTrue(bal.is_retired("fp1", "foreign_license", path=self.path))

    def test_is_retired_fails_toward_showing_on_an_unreadable_ledger(self):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write("not json{{")
        self.assertFalse(bal.is_retired("fp1", "foreign_license", path=self.path))

    def test_the_eligible_class_set_is_locked_to_exactly_the_two_offers(self):
        # The weakening-guard drift guard: no future alarm may become retire-eligible (silenceable) without a deliberate edit
        # to RETIRE_ELIGIBLE_CLASSES AND this assertion. Keeping this exact set is what keeps a governance alarm
        # un-retireable. The two eligible classes are the operator-dismissable OFFERS — the leftover-license
        # cleanup (#471) and the first-engagement greenfield-intake nudge (#553); a governance/strand alarm is
        # never here.
        self.assertEqual(bal.RETIRE_ELIGIBLE_CLASSES, frozenset({"foreign_license", "greenfield_intake"}))

    @unittest.skipUnless(bal._HAVE_FCNTL, "cross-process lock needs fcntl")
    def test_retire_reports_lock_contention_honestly(self):
        fd = bal._acquire(self.path + ".lock")
        self.assertIsNotNone(fd)
        try:
            r = bal.retire("fp1", "foreign_license", path=self.path)
        finally:
            bal._release(fd)
        self.assertFalse(r["ok"])
        self.assertEqual(r["reason"], "contended")


if __name__ == "__main__":
    unittest.main()
