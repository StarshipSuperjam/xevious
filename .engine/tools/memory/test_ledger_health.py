"""Unit tests for ledger_health.py — memory's boot-surfaced health detector (#396 U07b).

`detect_ledger_malformed` reports a rotting ledger (unreadable lines) so boot can disclose it. It reports
ONLY genuine malformed lines — never a torn trailing line, which is the normal self-healing post-crash state.
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from memory import index, ledger, ledger_health  # noqa: E402


class DetectLedgerMalformedTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._prev = os.environ.get(ledger.ENV_DIR)
        os.environ[ledger.ENV_DIR] = self._tmp.name

    def tearDown(self):
        if self._prev is None:
            os.environ.pop(ledger.ENV_DIR, None)
        else:
            os.environ[ledger.ENV_DIR] = self._prev
        self._tmp.cleanup()

    def _raw(self, text: str) -> None:
        with open(ledger.ledger_path(), "a", encoding="utf-8") as fh:
            fh.write(text)

    def test_a_clean_ledger_reports_zero(self):
        ledger.append({"kind": "episodic", "text": "clean"})
        self.assertEqual(ledger_health.detect_ledger_malformed(), 0)

    def test_a_missing_ledger_reports_zero(self):
        self.assertEqual(ledger_health.detect_ledger_malformed(), 0)   # the substrate ships empty

    def test_malformed_lines_are_counted(self):
        ledger.append({"kind": "episodic", "text": "before"})
        self._raw("not json at all\n")
        self._raw("{also not valid\n")
        ledger.append({"kind": "episodic", "text": "after"})
        self.assertEqual(ledger_health.detect_ledger_malformed(), 2)

    def test_a_torn_trailing_line_is_not_reported(self):
        """A torn trailing line is the normal, self-healing post-crash state — never surfaced as rot."""
        ledger.append({"kind": "episodic", "text": "kept"})
        self._raw('{"kind":"episodic","text":"torn, no newline"')  # torn trailing, no terminator
        self.assertEqual(ledger_health.detect_ledger_malformed(), 0)


class DetectFastSearchUnavailableTests(unittest.TestCase):
    """detect_fast_search_unavailable reports the LATENCY axis its sibling excludes: recall still answers, but
    every search reads the whole store. This disclosure used to ride the per-prompt seam, which no longer
    queries anything — cold start is now the only place that can state it unconditionally."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._prev = os.environ.get(ledger.ENV_DIR)
        os.environ[ledger.ENV_DIR] = self._tmp.name

    def tearDown(self):
        if self._prev is None:
            os.environ.pop(ledger.ENV_DIR, None)
        else:
            os.environ[ledger.ENV_DIR] = self._prev
        self._tmp.cleanup()

    def test_an_empty_store_draws_no_signal_even_without_fast_search(self):
        # "No memories yet" is not a fault, and a full scan of nothing is instant — the same rule the sibling
        # detectors keep. Without this gate every fresh install on such a machine would open with a warning
        # about a slowness it cannot yet experience.
        with mock.patch.object(index, "fts5_available", return_value=False):
            self.assertFalse(ledger_health.detect_fast_search_unavailable())

    def test_a_populated_store_without_fast_search_draws_the_signal(self):
        ledger.append({"kind": "episodic", "text": "something to search"})
        with mock.patch.object(index, "fts5_available", return_value=False):
            self.assertTrue(ledger_health.detect_fast_search_unavailable())

    def test_a_populated_store_WITH_fast_search_draws_no_signal(self):
        ledger.append({"kind": "episodic", "text": "something to search"})
        with mock.patch.object(index, "fts5_available", return_value=True):
            self.assertFalse(ledger_health.detect_fast_search_unavailable())

    def test_it_reads_the_real_probe_not_a_cached_flag(self):
        # Non-vacuity for the mocked tests above: unmocked, the detector must reach index.fts5_available and
        # agree with it on this machine.
        ledger.append({"kind": "episodic", "text": "something to search"})
        self.assertEqual(ledger_health.detect_fast_search_unavailable(), not index.fts5_available())

    def test_a_fault_degrades_to_no_signal_rather_than_a_false_alarm(self):
        # Best-effort readout, never a gate: a broken probe must not open the session with a warning.
        ledger.append({"kind": "episodic", "text": "something to search"})
        with mock.patch.object(index, "fts5_available", side_effect=RuntimeError("probe blew up")):
            self.assertFalse(ledger_health.detect_fast_search_unavailable())


class DetectRecallOfflineTests(unittest.TestCase):
    """detect_recall_offline reports the AVAILABILITY floor: a present-but-unreadable ledger (#397)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._prev = os.environ.get(ledger.ENV_DIR)
        os.environ[ledger.ENV_DIR] = self._tmp.name

    def tearDown(self):
        if self._prev is None:
            os.environ.pop(ledger.ENV_DIR, None)
        else:
            os.environ[ledger.ENV_DIR] = self._prev
        self._tmp.cleanup()

    def test_a_missing_ledger_is_not_offline(self):
        # The substrate ships empty — "no memories yet" is the normal state, never "offline".
        self.assertFalse(ledger_health.detect_recall_offline())

    def test_a_readable_ledger_is_not_offline(self):
        ledger.append({"kind": "episodic", "text": "readable"})
        self.assertFalse(ledger_health.detect_recall_offline())

    def test_a_malformed_but_readable_ledger_is_not_offline(self):
        # A corrupt LINE the read skips is rot (detect_ledger_malformed's job), not offline — the file still OPENS.
        ledger.append({"kind": "episodic", "text": "ok"})
        with open(ledger.ledger_path(), "a", encoding="utf-8") as fh:
            fh.write("not json at all\n")
        self.assertFalse(ledger_health.detect_recall_offline())

    def test_a_present_but_unreadable_ledger_is_offline(self):
        # A present ledger the read cannot OPEN (here: a directory where the file should be) => recall can't answer.
        path = ledger.ledger_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        os.mkdir(path)                                   # IsADirectoryError on open -> the OSError family -> offline
        try:
            self.assertTrue(ledger_health.detect_recall_offline())
        finally:
            os.rmdir(path)

    def test_offline_is_mutually_exclusive_with_malformed_on_an_unopenable_ledger(self):
        # On the same unopenable ledger, offline fires True while malformed degrades to None (no line count) — so the
        # two boot signals can never co-fire (the render precedence rests on this).
        path = ledger.ledger_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        os.mkdir(path)
        try:
            self.assertTrue(ledger_health.detect_recall_offline())
            self.assertIsNone(ledger_health.detect_ledger_malformed())
        finally:
            os.rmdir(path)


class DetectStalledMigrationTests(unittest.TestCase):
    """detect_stalled_migration reports an ORPHANED in-flight marker (tidying paused) — #396."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._prev = os.environ.get(ledger.ENV_DIR)
        os.environ[ledger.ENV_DIR] = self._tmp.name

    def tearDown(self):
        if self._prev is None:
            os.environ.pop(ledger.ENV_DIR, None)
        else:
            os.environ[ledger.ENV_DIR] = self._prev
        self._tmp.cleanup()

    def _write_marker(self, marker):
        import json
        from memory import capture
        with open(os.path.join(self._tmp.name, capture.MIGRATION_MARKER_FILENAME), "w", encoding="utf-8") as fh:
            fh.write(json.dumps(marker))

    def test_no_marker_reports_false(self):
        self.assertFalse(ledger_health.detect_stalled_migration())

    def test_a_live_migration_reports_false(self):
        self._write_marker({"pid": os.getpid(), "started_at": time.time()})   # live => normal, not a stall
        self.assertFalse(ledger_health.detect_stalled_migration())

    def test_an_orphaned_marker_reports_true(self):
        from memory import capture
        self._write_marker({"pid": os.getpid(), "started_at": time.time() - capture.MIGRATION_ORPHAN_CEILING_S - 1})
        self.assertTrue(ledger_health.detect_stalled_migration())


if __name__ == "__main__":
    unittest.main()
