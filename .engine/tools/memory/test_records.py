"""Unit tests for the stable, content-free record id minted at capture.

The id (`records.RECORD_ID_KEY`, a `uuid4().hex`) is stamped in every record factory — turn-deltas
(`capture._make_record`), and the legacy summary shapes (`legacy_shapes.episodic` / `_make_marker`). It is a
durable, content-free NAME for a record: unique per record, never derived from content, kept OUT of the search
body (`index._NON_BODY_KEYS`), and stable across an index rebuild and a ledger re-append (the compaction
precursor). These tests exercise the real minter, the real factories, and the real recall paths through them,
plus the back-compat tolerance of legacy records that carry no id, and the folded-in `SESSION_ENV` fix.
"""

from __future__ import annotations

import os
import string
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from memory import capture, index, ledger, legacy_shapes as legacy, records  # noqa: E402

_HEX = set(string.hexdigits.lower())


class InjectedPseudoTurnTests(unittest.TestCase):
    """The harness-injected pseudo-turn predicates (issue #274). `is_injected_pseudo_turn_text` is start-anchored
    on the two ground-truthed standalone sentinels; `is_injected_record` adds the durable `INJECTED_TAG` path and
    keeps a back-compat text fallback for records captured before tagging existed."""

    def test_text_matches_the_two_standalone_sentinels(self):
        self.assertTrue(records.is_injected_pseudo_turn_text("<task-notification>\n<task-id>x</task-id>"))
        self.assertTrue(records.is_injected_pseudo_turn_text(
            "This session is being continued from a previous conversation that ran out of context."))

    def test_a_mid_sentence_mention_is_kept(self):
        # start-anchored: a real turn that merely talks ABOUT a marker is never matched
        self.assertFalse(records.is_injected_pseudo_turn_text(
            "what does <task-notification> mean in my transcript?"))
        self.assertFalse(records.is_injected_pseudo_turn_text(
            "remind me: this session is being continued from a previous conversation, right?"))

    def test_system_reminder_is_deliberately_not_matched(self):
        # <system-reminder> fuses with a real human prompt in the same turn, so dropping it would lose content
        self.assertFalse(records.is_injected_pseudo_turn_text(
            "<system-reminder>...</system-reminder>\n\nRemember this for me: ship on Friday."))

    def test_non_string_text_is_not_injected(self):
        self.assertFalse(records.is_injected_pseudo_turn_text(None))
        self.assertFalse(records.is_injected_pseudo_turn_text(123))

    def test_record_is_injected_by_tag(self):
        rec = {"kind": "turn-delta", "text": "any chunk text at all", "tags": ["transcript", "stop", records.INJECTED_TAG]}
        self.assertTrue(records.is_injected_record(rec))

    def test_record_is_injected_by_text_back_compat(self):
        # a record captured BEFORE tagging existed: no INJECTED_TAG, but its text begins with a marker
        rec = {"kind": "turn-delta", "text": "<task-notification>\n...", "tags": ["transcript", "stop"]}
        self.assertTrue(records.is_injected_record(rec))

    def test_a_normal_record_is_not_injected(self):
        rec = {"kind": "turn-delta", "text": "redesign the export", "tags": ["transcript", "stop"]}
        self.assertFalse(records.is_injected_record(rec))
        self.assertFalse(records.is_injected_record(None))
        self.assertFalse(records.is_injected_record({"text": "see the <task-notification> block for details"}))


class _Base(unittest.TestCase):
    """A throwaway temp cabinet via ENGINE_MEMORY_DIR, mirroring test_forget._Base."""

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


class MinterTests(unittest.TestCase):
    def test_new_record_id_is_32_char_lowercase_hex(self):
        rid = records.new_record_id()
        self.assertEqual(len(rid), 32)
        self.assertTrue(set(rid) <= _HEX, rid)              # content-free: just hex

    def test_two_mints_differ(self):
        self.assertNotEqual(records.new_record_id(), records.new_record_id())


class FactoryTests(unittest.TestCase):
    def test_every_factory_stamps_a_unique_nonempty_id(self):
        td = capture._make_record("S", 0, "user", "a turn note")
        ep = legacy.episodic("S", "decision", "an episode", "batch-x")
        mk = legacy.marker("S", "batch-x")           # markers carry an id too (uniform invariant)
        ids = [r[records.RECORD_ID_KEY] for r in (td, ep, mk)]
        for rid in ids:
            self.assertIsInstance(rid, str)
            self.assertEqual(len(rid), 32)
        self.assertEqual(len(set(ids)), 3)                     # all three distinct

    def test_identical_text_gets_different_ids(self):
        a = capture._make_record("S", 0, "user", "exactly the same words")
        b = capture._make_record("S", 1, "user", "exactly the same words")
        self.assertNotEqual(a[records.RECORD_ID_KEY], b[records.RECORD_ID_KEY])  # not derived from content


class StabilityTests(_Base):
    def test_the_id_survives_an_index_rebuild_byte_for_byte(self):
        # The recall vehicle is a CURATED episodic (closed batch → live): ambient turn-deltas are not recall
        # content (#332). The id-stability property is record-kind-agnostic.
        rec = legacy.episodic("S", "decision", "the quokka decision", "batch-x")
        ledger.append(rec)
        ledger.append(legacy.marker("S", "batch-x"))   # close the batch so the episodic is live
        index.rebuild()
        hits = [r for r in index.query("quokka").records if r.get("kind") == records.EPISODIC_KIND]
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0][records.RECORD_ID_KEY], rec[records.RECORD_ID_KEY])  # index read it, not re-mint

    def test_re_appending_a_record_preserves_its_id(self):
        rec = capture._make_record("S", 0, "user", "a re-filed note")
        ledger.append(rec)
        ledger.append(rec)                                     # the move the future compaction will make
        ids = [r[records.RECORD_ID_KEY] for r in ledger.iter_records()
               if r.get("kind") == capture.RECORD_KIND]
        self.assertEqual(ids, [rec[records.RECORD_ID_KEY], rec[records.RECORD_ID_KEY]])  # preserved, not lost


class NonSearchableTests(_Base):
    def test_the_id_is_not_a_search_term(self):
        # vehicle is a curated episodic (closed batch → live); ambient turn-deltas are not recall content (#332)
        rec = legacy.episodic("S", "decision", "findable by its words", "batch-x")
        ledger.append(rec)
        ledger.append(legacy.marker("S", "batch-x"))
        index.rebuild()
        self.assertTrue(index.query("findable").records)              # the words ARE findable
        self.assertEqual(index.query(rec[records.RECORD_ID_KEY]).records, [])  # the id is NOT


class BackCompatTests(_Base):
    def test_a_record_with_no_id_still_appends_indexes_and_queries(self):
        # a legacy CURATED record — NO "id" field — must read/index/query fine (no reader requires an id). A
        # batchless episodic is always live; ambient turn-deltas are no longer recall content (#332),
        # so the recall vehicle is an episodic. `ts` is current (not a sentinel): scored demotion sets
        # an ancient record aside from recall, which would mask the id tolerance this test is about.
        ledger.append({"v": 1, "kind": records.EPISODIC_KIND, "session_id": "S", "ts": int(time.time()),
                       "role": "observation", "text": "an old note about pelicans", "tags": ["episodic"]})
        index.rebuild()
        hits = index.query("pelicans").records
        self.assertEqual(len(hits), 1)
        self.assertIsNone(hits[0].get(records.RECORD_ID_KEY))         # absent id tolerated, no crash

    def test_no_id_episodics_recall_and_retire_correctly(self):
        # both episodics are id-free (legacy): the no-batch one is live; the orphan-batch one still retires
        ledger.append({"v": 1, "kind": records.EPISODIC_KIND, "session_id": "S",
                       "text": "kept summary", "tags": ["episodic"]})
        ledger.append({"v": 1, "kind": records.EPISODIC_KIND, "session_id": "S",
                       "text": "orphan summary", "tags": ["episodic"], records.BATCH_KEY: "never-closed"})
        index.rebuild()
        texts = sorted(r["text"] for r in index.query("summary").records
                       if r.get("kind") == records.EPISODIC_KIND)
        self.assertEqual(texts, ["kept summary"])                    # orphan retired; no-batch kept — both id-free


class SessionEnvFixTests(unittest.TestCase):
    def test_capture_session_env_is_the_live_platform_var(self):
        # the folded-in fix: capture's env fallback must name the live var (the shared convention)
        self.assertEqual(capture.SESSION_ENV, "CLAUDE_CODE_SESSION_ID")


class HarnessSpanTests(unittest.TestCase):
    """A harness block fused into a real turn. It cannot be excluded record-wise without losing the operator's
    own words in the same message, so it is marked out wherever the text is indexed or shown."""

    def test_the_block_goes_and_the_operators_own_words_stay(self):
        text = "<system-reminder>\nThe engine says do X.\n</system-reminder>\n\nplease look at the export job"
        out = records.mark_harness_spans(text)
        self.assertNotIn("do X", out)
        self.assertIn("please look at the export job", out)
        self.assertIn(records.HARNESS_SPAN_MARKER, out)

    def test_it_is_idempotent_and_leaves_ordinary_text_alone(self):
        self.assertEqual(records.mark_harness_spans("just a normal turn"), "just a normal turn")
        once = records.mark_harness_spans("<system-reminder>a</system-reminder> hi")
        self.assertEqual(records.mark_harness_spans(once), once)

    def test_it_never_raises_on_a_non_string_or_an_unclosed_block(self):
        self.assertIsNone(records.mark_harness_spans(None))
        self.assertEqual(records.mark_harness_spans(42), 42)
        unclosed = "<system-reminder> never closed, so nothing to bound"
        self.assertEqual(records.mark_harness_spans(unclosed), unclosed)


if __name__ == "__main__":
    unittest.main()
