"""Unit tests for pins.py — durable operator intent as a record-type in the one substrate.

These exercise the real ledger and the real recall paths: a pin is only useful if `search` finds it, and the
properties that matter most (scrubbed on the way in, findable with no rebuild, removable and restorable) are
the ones a plausible-but-wrong implementation would quietly fail.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from memory import forget, index, ledger, pins, records  # noqa: E402


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


class PinTests(_Base):
    def test_a_pin_is_findable_by_search_with_no_rebuild_in_between(self):
        # The same trap the withhold verbs face: `ledger.append` leaves the generation stamp alone and
        # `index.extend` takes captured turns only, so an implementation that merely appended would leave the
        # operator's brand-new pin missing from a search that reports itself authoritative.
        index.rebuild()
        pins.add("Always ask before filing an issue.")
        self.assertEqual(len(index.search("filing").records), 1)
        self.assertEqual(len(index.search("filing", force_scan=True).records), 1)

    def test_secret_shaped_text_is_scrubbed_before_it_is_stored(self):
        # A pin does not travel through capture, so capture's scrub never sees it — and a pinned credential
        # would be read into the briefing of every future session. There must be no unscrubbed copy anywhere.
        record = pins.add("the key is sk-ant-api03-" + "A" * 32)
        self.assertNotIn("sk-ant-api03", record["text"])
        stored = [r for r in ledger.iter_records() if r.get("kind") == records.PIN_KIND]
        self.assertEqual(len(stored), 1)
        self.assertNotIn("sk-ant-api03", stored[0]["text"])

    def test_every_pin_records_the_route_it_arrived_by(self):
        # Never an authority claim — the field exists so no reader can present a pin as verified wording.
        self.assertEqual(pins.add("via the tool")[records.PIN_VIA_KEY], records.PIN_VIA_ASSISTANT)
        self.assertEqual(pins.add("typed", via=records.PIN_VIA_CLI)[records.PIN_VIA_KEY], records.PIN_VIA_CLI)
        self.assertEqual(pins.add("nonsense", via="operator-swears-it")[records.PIN_VIA_KEY],
                         records.PIN_VIA_ASSISTANT)      # an unknown route is never trusted upward

    def test_the_route_and_the_source_session_are_not_searchable_words(self):
        # "assistant" and "cli" are ordinary words: indexed, they would make every pin match a search for
        # either, and the source session is uuid hex whose fragments are real words.
        pins.add("keep the onboarding copy short", session_id="deadbeefcafe")
        index.rebuild()
        self.assertEqual(index.search("assistant").records, [])
        self.assertEqual(index.search("deadbeefcafe").records, [])
        self.assertEqual(len(index.search("onboarding").records), 1)

    def test_removing_a_pin_withholds_it_rather_than_deleting_it(self):
        record = pins.add("a preference that changed")
        rid = record[records.RECORD_ID_KEY]
        index.rebuild()
        pins.remove(rid)
        self.assertEqual(index.search("preference").records, [])
        self.assertEqual(pins.list_pins(), [])
        # Still in the ledger, byte for byte — removal is a withhold, so restore is the operator's undo.
        self.assertIn(rid, {r.get(records.RECORD_ID_KEY) for r in ledger.iter_records()})
        forget.restore(record_id=rid)
        self.assertEqual(len(pins.list_pins()), 1)
        self.assertEqual(len(index.search("preference").records), 1)

    def test_list_reads_through_the_same_liveness_as_recall(self):
        # One definition of "live". A second one here could disagree with search and leave the operator told
        # something is pinned that no recall would ever surface.
        kept = pins.add("kept")
        gone = pins.add("gone")
        pins.remove(gone[records.RECORD_ID_KEY])
        self.assertEqual([r[records.RECORD_ID_KEY] for r in pins.list_pins()],
                         [kept[records.RECORD_ID_KEY]])

    def test_list_is_newest_first_and_honours_a_limit(self):
        first = pins.add("older", now=1000)
        second = pins.add("newer", now=2000)
        self.assertEqual([r[records.RECORD_ID_KEY] for r in pins.list_pins()],
                         [second[records.RECORD_ID_KEY], first[records.RECORD_ID_KEY]])
        self.assertEqual([r[records.RECORD_ID_KEY] for r in pins.list_pins(limit=1)],
                         [second[records.RECORD_ID_KEY]])

    def test_nothing_is_saved_when_a_pin_is_refused(self):
        # A refusal must leave no half-record behind, and must not be silent: the operator asked for something
        # to be remembered, so a quiet decline leaves them believing it was.
        for bad in ("", "   ", None, "x" * (pins.MAX_PIN_CHARS + 1)):
            with self.assertRaises(pins.PinRefused):
                pins.add(bad)
        self.assertEqual([r for r in ledger.iter_records() if r.get("kind") == records.PIN_KIND], [])

    def test_an_over_long_pin_is_refused_rather_than_truncated(self):
        with self.assertRaises(pins.PinRefused) as caught:
            pins.add("y" * (pins.MAX_PIN_CHARS + 1))
        self.assertIn("Nothing was saved", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
