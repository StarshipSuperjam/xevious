"""Unit tests for the Layer-2 erasure WRITER — compact.enact_erasure, the sole minter of the merge-gated
`operator-adjudicated-erasure` marker.

The REMOVAL (compact's gated Layer-2 filter) is pinned in test_compact.py::Layer2ErasureTests, and the LIVE
auto-trigger path in test_compact_trigger.py. THIS file pins the MINTER: it appends a well-formed marker under the
single-writer lock, names the target by its content-free id + the merge SHA, carries no recall content, never names
itself, and is a no-op on a blank target OR a blank merge SHA (the consent-provenance floor). It also runs the
operator demo clean. Throwaway ENGINE_MEMORY_DIR cabinet throughout.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import quiet_call  # noqa: E402  (capture a demo walkthrough's stdout so it can't bury the suite summary)
from memory import compact, ledger, records  # noqa: E402


class _Base(unittest.TestCase):
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

    def _markers(self):
        return [r for r in ledger.iter_records()
                if isinstance(r, dict) and r.get("kind") == records.ERASURE_KIND]


class EnactErasureTests(_Base):
    def test_mints_a_well_formed_content_free_marker(self):
        m = compact.enact_erasure("target-id-abc", "merge-sha-xyz")
        self.assertIsNotNone(m)
        self.assertEqual(m["kind"], records.ERASURE_KIND)
        self.assertEqual(m[records.TARGET_KEY], "target-id-abc")
        self.assertEqual(m[records.MERGE_SHA_KEY], "merge-sha-xyz")
        self.assertEqual(m["tags"], [records.ERASURE_TAG])
        self.assertNotIn("text", m)                          # pure provenance: no recall content
        self.assertNotIn("session_id", m)
        self.assertNotEqual(m[records.RECORD_ID_KEY], m[records.TARGET_KEY])   # a marker never names itself
        on_file = self._markers()
        self.assertEqual(len(on_file), 1)                    # appended once, under the lock
        self.assertEqual(on_file[0][records.RECORD_ID_KEY], m[records.RECORD_ID_KEY])

    def test_a_blank_target_is_a_no_op(self):
        self.assertIsNone(compact.enact_erasure("", "merge-sha-xyz"))
        self.assertIsNone(compact.enact_erasure(None, "merge-sha-xyz"))
        self.assertEqual(self._markers(), [])

    def test_a_blank_merge_sha_is_a_no_op(self):
        # The consent-provenance floor: an erasure marker without its merge identity is never minted.
        self.assertIsNone(compact.enact_erasure("target-id-abc", ""))
        self.assertIsNone(compact.enact_erasure("target-id-abc", None))
        self.assertEqual(self._markers(), [])

    def test_the_writer_is_append_only_dedup_is_the_observers_job(self):
        compact.enact_erasure("t", "sha-1")
        compact.enact_erasure("t", "sha-2")
        self.assertEqual(len(self._markers()), 2)            # append-only; idempotent dedup is the observer's


class DemoTests(_Base):
    def test_demo_erase_runs_clean(self):
        self.assertEqual(quiet_call.run(compact._demo_erase), 0)


if __name__ == "__main__":
    unittest.main()
