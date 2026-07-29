"""Unit tests for compact.py — ledger compaction, the crash-safe rebuild-and-swap.

Compaction is Layer-1: reversible, mechanical, autonomous. It folds a record's reinforcement markers into a
carried frecency snapshot (so demotion survives the fold — the recurrence property), prunes those markers, and
swaps a fresh ledger in atomically under the single-writer lock. These tests exercise the REAL fold + swap +
generation gate + lock through a throwaway `ENGINE_MEMORY_DIR` cabinet, with an injected power-cut at each swap
point, and pin the load-bearing invariants: a crash leaves exactly one intact ledger (old or new); recall
content is NEVER dropped (only the non-recall markers are); the content-free id is preserved; the score is identical
before and after; the generation gate routes a crash-staled index to the scan; a leftover temp is reaped and
never promoted.
"""

from __future__ import annotations

import inspect
import json
import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from memory import capture, compact, forget, index, ledger, legacy_shapes as legacy, records  # noqa: E402

_DAY = 86400


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

    def _episodic(self, text, *, age_days=0, role="decision", batchless=True):
        rec = legacy.episodic("S", role, text, "b")
        if batchless:
            rec.pop(records.BATCH_KEY, None)
        rec["ts"] = int(time.time()) - age_days * _DAY
        ledger.append(rec)
        return rec

    def _content_ids(self):
        return {r.get(records.RECORD_ID_KEY) for r in ledger.iter_records()
                if isinstance(r, dict) and r.get("kind") in (records.EPISODIC_KIND, capture.RECORD_KIND)}

    def _kinds(self):
        return [r.get("kind") for r in ledger.iter_records() if isinstance(r, dict)]

    def _reinforcements(self):
        return sum(1 for k in self._kinds() if k == records.REINFORCEMENT_KIND)

    def _scratch(self):
        return sum(1 for n in os.listdir(self._tmp.name)
                   if n.startswith(compact._TEMP_PREFIX) and n.endswith(compact._TEMP_SUFFIX))

    def _by_id(self, rid):
        return [r for r in ledger.iter_records() if isinstance(r, dict) and r.get(records.RECORD_ID_KEY) == rid]


class CrashSafeSwapTests(_Base):
    def test_a_crash_before_the_swap_leaves_the_old_ledger_intact(self):
        e = self._episodic("alpha bridge note")
        rid = e[records.RECORD_ID_KEY]
        for _ in range(3):
            ledger.append(legacy.reinforcement(rid))
        ids = self._content_ids()
        book = self._reinforcements()
        gen0 = ledger.generation()
        with self.assertRaises(compact._InjectedCrash):
            compact.compact(_crash_after="write")
        # OLD ledger intact: content preserved, markers NOT pruned (the tidy didn't take), gen unbumped, complete.
        self.assertEqual(self._content_ids(), ids)
        self.assertEqual(self._reinforcements(), book)
        self.assertEqual(ledger.generation(), gen0)
        self.assertFalse(ledger.read().torn_trailing)
        self.assertGreaterEqual(self._scratch(), 1)            # the half-finished temp is left
        # Recovery: a clean pass reaps the leftover and completes.
        self.assertEqual(compact.compact()["status"], "ok")
        self.assertEqual(self._scratch(), 0)
        self.assertEqual(self._content_ids(), ids)
        self.assertEqual(self._reinforcements(), 0)

    def test_a_crash_after_the_swap_leaves_the_new_ledger_intact(self):
        e = self._episodic("equinox parade note")
        rid = e[records.RECORD_ID_KEY]
        for _ in range(3):
            ledger.append(legacy.reinforcement(rid))
        ids = self._content_ids()
        index.rebuild()                                        # an index at generation 0
        with self.assertRaises(compact._InjectedCrash):
            compact.compact(_crash_after="swap")
        # NEW ledger in place: content preserved, markers folded away, gen bumped, complete, no leftover temp.
        self.assertEqual(self._content_ids(), ids)
        self.assertEqual(self._reinforcements(), 0)
        self.assertEqual(ledger.generation(), 1)
        self.assertFalse(ledger.read().torn_trailing)
        self.assertEqual(self._scratch(), 0)
        # The index is now generation-stale (built at 0, ledger at 1) -> query falls back to the scan, still finds it.
        q = index.query("equinox")
        self.assertTrue(q.degraded)                            # the gen-gate routed it to the scan
        self.assertEqual(len(q.records), 1)
        # A clean pass rebuilds the index -> the fast path returns.
        compact.compact()
        self.assertFalse(index.query("equinox").degraded)

    def test_exactly_one_intact_ledger_after_either_crash(self):
        for crash in ("write", "swap"):
            with self.subTest(crash=crash):
                self._tmp.cleanup(); self._tmp = tempfile.TemporaryDirectory()
                os.environ[ledger.ENV_DIR] = self._tmp.name
                e = self._episodic("solstice note")
                ledger.append(legacy.reinforcement(e[records.RECORD_ID_KEY]))
                with self.assertRaises(compact._InjectedCrash):
                    compact.compact(_crash_after=crash)
                read = ledger.read()
                self.assertFalse(read.torn_trailing)           # the canonical ledger is whole, never half-written
                self.assertIn(e[records.RECORD_ID_KEY],
                              [r.get(records.RECORD_ID_KEY) for r in read.records])


class IdAndPruneTests(_Base):
    def test_the_4b_id_is_preserved_on_re_append(self):
        e = self._episodic("the quokka decision")
        rid = e[records.RECORD_ID_KEY]
        ledger.append(legacy.reinforcement(rid))
        compact.compact()
        survivors = self._by_id(rid)
        self.assertEqual(len(survivors), 1)                    # exactly one content record, same id
        self.assertEqual(survivors[0].get("kind"), records.EPISODIC_KIND)

    def test_reinforcement_markers_are_pruned(self):
        e = self._episodic("pelican note")
        for _ in range(4):
            ledger.append(legacy.reinforcement(e[records.RECORD_ID_KEY]))
        compact.compact()
        self.assertNotIn(records.REINFORCEMENT_KIND, self._kinds())

    def test_an_un_reinforced_record_is_rewritten_verbatim(self):
        # The degenerate-live shape: no markers -> compaction folds nothing onto the record (no snapshot fields).
        e = self._episodic("the verbatim note")
        before = {k: v for k, v in e.items()}
        compact.compact()
        after = self._by_id(e[records.RECORD_ID_KEY])[0]
        self.assertEqual(after, before)                        # byte-for-byte the same record (id preserved)
        self.assertNotIn(records.FRECENCY_SNAPSHOT_KEY, after)


class NeverDropsRecallContentTests(_Base):
    """The Layer-1 guarantee: an UNMARKED compaction (no operator-adjudicated-erasure marker present) drops NO
    recall content — every turn-delta / episodic / gist survives the fold, even when archived or crash-retired.
    This is the no-marker floor too: when no operator-adjudicated-erasure marker is present (as in these tests,
    which mint none), the removal set is empty, so compaction erases nothing. (The MARKED case is
    Layer2ErasureTests, below.)"""

    def test_every_content_record_survives_compaction(self):
        a = self._episodic("the manifest note")
        b = self._episodic("the migration note", role="decision")
        ledger.append(capture._make_record("S", 0, "user", "a raw turn note about turnips"))
        ledger.append(legacy.reinforcement(a[records.RECORD_ID_KEY]))
        ids = self._content_ids()
        compact.compact()
        self.assertEqual(self._content_ids(), ids)             # superset (==) before -> after; nothing dropped

    def test_a_crash_duplicate_orphan_survives_and_stays_retired(self):
        # An orphan episodic (batch never closed) + a completed pass; the orphan is retired but NOT erased.
        orphan = legacy.episodic("S", "decision", "orphaned summary", "batch-x")
        ledger.append(orphan)
        legacy.store_episodic("S", [{"role": "decision", "text": "the completed summary"}])
        orphan_id = orphan[records.RECORD_ID_KEY]
        self.assertNotIn(orphan_id, [r.get(records.RECORD_ID_KEY) for r in forget.live_records()])
        compact.compact()
        self.assertEqual(len(self._by_id(orphan_id)), 1)                                  # survived the rewrite
        self.assertNotIn(orphan_id, [r.get(records.RECORD_ID_KEY) for r in forget.live_records()])  # still retired
        self.assertIn(records.MARKER_KIND, self._kinds())                                 # the marker survived too

    def test_a_long_unused_record_survives_the_rewrite_and_stays_in_recall(self):
        # This used to assert the opposite half: a record scored into the archived tier was excluded from recall,
        # and the point was that the fold reads the RAW ledger so it kept the row anyway. The age-out is gone, so
        # what has to hold now is stronger — the coldest-scoring record is BOTH still recalled and still resident
        # after the rewrite, and compaction must not quietly reintroduce an age-based drop of its own.
        old = self._episodic("the buried gantry note", age_days=40, role="lesson")
        aid = old[records.RECORD_ID_KEY]
        self.assertIn(aid, [r.get(records.RECORD_ID_KEY) for r in forget.live_records()])
        compact.compact()
        self.assertEqual(len(self._by_id(aid)), 1)             # still resident in the ledger
        self.assertIn(aid, [r.get(records.RECORD_ID_KEY) for r in forget.live_records()])


class MarkerSurvivalTests(_Base):
    def test_an_erasure_pass_never_drops_a_consolidated_marker(self):
        ledger.append(capture._make_record("S", 0, "user", "the note"))
        legacy.store_episodic("S", [{"role": "decision", "text": "the summary"}])
        gone = self._episodic("erase this unrelated note")
        compact.enact_erasure(gone[records.RECORD_ID_KEY], "merge-sha-abc")
        report = compact.compact()
        self.assertEqual(report["erased"], 1)                                  # the erasure did happen...
        self.assertIn(records.MARKER_KIND, self._kinds())                      # ...but the marker survived


class Layer2ErasureTests(_Base):
    """The gated Layer-2 physical erasure (the single irreversible act). Compaction removes a recall
    record IFF a VALID operator-adjudicated-erasure marker targets it; an UNMARKED record is never removed; the
    marker is RETAINED (the idempotency tombstone); a re-compaction is a clean no-op; the generation bumps; and a
    crash just after the swap still leaves the target erased.

    Mutation-kills (the contract for the cold lens) — the suite goes RED if:
      * the `_is_erased` continue is dropped from `_write_compacted_temp` -> the marked record survives (test 1);
      * `_is_erased` is inverted to match UNMARKED records -> the kept record vanishes (test 1);
      * `_is_erased` ignores the target id (erase all / none) -> test 1 fails either way;
      * the marker is pruned (or the `kind != ERASURE_KIND` guard removed) -> retention / no-op fail (tests 2, 4);
      * the read-side SHA floor is removed -> a SHA-less marker erases (test 3)."""

    def _slips(self):
        return sum(1 for r in ledger.iter_records()
                   if isinstance(r, dict) and r.get("kind") == records.ERASURE_KIND)

    def test_1_a_marked_record_is_removed_and_an_unmarked_one_survives(self):
        gone = self._episodic("erase this floodplain note")
        keep = self._episodic("keep this fireworks note")
        index.rebuild()
        before = self._content_ids()
        compact.enact_erasure(gone[records.RECORD_ID_KEY], "merge-sha-abc")
        report = compact.compact()
        after = self._content_ids()
        self.assertEqual(report["erased"], 1)
        self.assertNotIn(gone[records.RECORD_ID_KEY], after)            # the WITH-marker record IS removed
        self.assertIn(keep[records.RECORD_ID_KEY], after)              # the WITHOUT-marker record is NEVER removed
        self.assertEqual(after, before - {gone[records.RECORD_ID_KEY]})   # and ONLY the marked one

    def test_2_the_marker_is_retained_and_generation_bumps(self):
        gone = self._episodic("erase this note")
        compact.enact_erasure(gone[records.RECORD_ID_KEY], "merge-sha-abc")
        gen0 = ledger.generation()
        report = compact.compact()
        self.assertEqual(report["erased"], 1)
        self.assertEqual(self._slips(), 1)                             # the marker is RETAINED (the tombstone)
        self.assertGreater(ledger.generation(), gen0)                  # generation bumped across the erasing pass

    def test_3_a_sha_less_marker_is_inert(self):
        keep = self._episodic("keep this note")
        self.assertIsNone(compact.enact_erasure(keep[records.RECORD_ID_KEY], ""))   # blank sha -> no marker minted
        ledger.append({"v": capture.RECORD_VERSION, "kind": records.ERASURE_KIND,   # hand-inject a SHA-less marker
                       records.RECORD_ID_KEY: records.new_record_id(),
                       records.TARGET_KEY: keep[records.RECORD_ID_KEY],
                       "ts": int(time.time()), "tags": [records.ERASURE_TAG]})       # NOTE: no merge_sha
        report = compact.compact()
        self.assertEqual(report["erased"], 0)                          # the read-side consent floor holds
        self.assertIn(keep[records.RECORD_ID_KEY], self._content_ids())  # the target survives (no consent provenance)

    def test_4_re_running_is_a_clean_no_op(self):
        gone = self._episodic("erase this note")
        keep = self._episodic("keep this note")
        compact.enact_erasure(gone[records.RECORD_ID_KEY], "merge-sha-abc")
        compact.compact()
        after1 = self._content_ids()
        report2 = compact.compact()                                    # re-run, target already gone
        self.assertEqual(report2["erased"], 0)                         # idempotent no-op
        self.assertEqual(self._content_ids(), after1)                 # the kept note unchanged
        self.assertIn(keep[records.RECORD_ID_KEY], after1)
        self.assertEqual(self._slips(), 1)                            # the marker still retained

    def test_5_the_marker_is_never_pruned_by_a_colliding_target_id(self):
        # If a marker targeted another marker's id, a naive predicate would prune the marker, breaking idempotency.
        # _is_erased excludes ERASURE_KIND, so EVERY erasure marker is retained regardless of what targets it.
        keep = self._episodic("keep this note")
        marker = compact.enact_erasure(keep[records.RECORD_ID_KEY], "merge-sha-abc")
        ledger.append({"v": capture.RECORD_VERSION, "kind": records.ERASURE_KIND,    # a 2nd marker targeting the 1st
                       records.RECORD_ID_KEY: records.new_record_id(),
                       records.TARGET_KEY: marker[records.RECORD_ID_KEY], records.MERGE_SHA_KEY: "sha2",
                       "ts": int(time.time()), "tags": [records.ERASURE_TAG]})
        compact.compact()
        self.assertEqual(self._slips(), 2)                            # BOTH markers retained (neither pruned)
        self.assertNotIn(keep[records.RECORD_ID_KEY], self._content_ids())   # the real target still erased

    def test_6_a_crash_after_the_swap_leaves_the_target_erased(self):
        # The first point where the swap's durability backs an IRREVERSIBLE (not merely recoverable) guarantee:
        # a power-cut just AFTER the erasing swap must leave the target GONE (the new ledger is already in place).
        gone = self._episodic("erase this note")
        keep = self._episodic("keep this note")
        compact.enact_erasure(gone[records.RECORD_ID_KEY], "merge-sha-abc")
        with self.assertRaises(compact._InjectedCrash):
            compact.compact(_crash_after="swap")
        ids = self._content_ids()                                     # read the on-disk ledger after the 'crash'
        self.assertNotIn(gone[records.RECORD_ID_KEY], ids)           # the erased target stays gone
        self.assertIn(keep[records.RECORD_ID_KEY], ids)


class SearchBodyTests(_Base):
    def test_compaction_carries_no_score_onto_a_record(self):
        # Compaction used to fold a frecency snapshot onto every reinforced record before pruning its markers,
        # so the scorer could resume from it. There is no scorer, so there is nothing to resume and nothing to
        # carry — the markers are simply reclaimed. A record must come out of a compaction byte-identical.
        e = self._episodic("the riverside survey note")
        rid = e[records.RECORD_ID_KEY]
        for _ in range(3):
            ledger.append(legacy.reinforcement(rid))
        compact.compact()
        comp = self._by_id(rid)[0]
        for carried in (records.FRECENCY_SNAPSHOT_KEY, records.SNAPSHOT_TS_KEY,
                        records.LAST_ACCESS_TS_KEY, records.TIER_KEY):
            self.assertNotIn(carried, comp, f"compaction carried {carried!r} onto a record")
        self.assertEqual(comp, e)                                     # byte-identical: only the markers went

    def test_a_tier_carried_by_an_older_engine_is_not_searchable(self):
        # A deployed repo that upgrades brings records an older engine compacted, and those DO carry a `tier`
        # string ("hot"/"cold"/"archived"). It must stay out of the search body, else a query for one of those
        # words would surface every such record. The note's own text contains no tier word, so a hit could only
        # be the leaked field. This is the reason `records.TIER_KEY` still exists at all.
        old = legacy.episodic("S", "decision", "the riverside survey note", "b")
        old.pop(records.BATCH_KEY, None)                              # batchless: always live
        old[records.TIER_KEY] = "archived"
        ledger.append(old)
        index.rebuild()
        for word in (records.TIER_KEY, "hot", "warm", "cold", "archived"):
            self.assertEqual(index.query(word).records, [], f"the carried {word!r} leaked into search")
        self.assertEqual(len(index.query("riverside").records), 1)    # its real words are still findable


class GenerationTests(_Base):
    def test_generation_increments_per_compaction(self):
        self._episodic("a note")
        self.assertEqual(ledger.generation(), 0)
        compact.compact()
        self.assertEqual(ledger.generation(), 1)
        compact.compact()
        self.assertEqual(ledger.generation(), 2)

    def test_a_gen_stale_index_falls_back_to_the_scan(self):
        e = self._episodic("findable note")
        index.rebuild()                                        # index built at generation 0
        self.assertFalse(index.query("findable").degraded)     # fast path: index gen == ledger gen
        ledger.bump_generation()                               # ledger -> 1, index still 0 (stale)
        q = index.query("findable")
        self.assertTrue(q.degraded)                            # gen mismatch -> scan, never a stale fast answer
        self.assertEqual(len(q.records), 1)
        index.rebuild()                                        # rebuild stamps gen 1 -> fast again
        self.assertFalse(index.query("findable").degraded)

    def test_the_gen_gate_reads_the_queried_ledgers_own_sidecar_not_the_env_default(self):
        # The plan-gate's SERIOUS finding: an explicit ledger_file/index_file must compare against THAT store's
        # generation sidecar, never the ENGINE_MEMORY_DIR default. A SECOND store (not the env dir) proves it.
        other = tempfile.mkdtemp()
        try:
            led = os.path.join(other, ledger.LEDGER_FILENAME)
            idx = os.path.join(other, index.INDEX_FILENAME)
            ledger.append({"v": 1, "kind": records.EPISODIC_KIND, "session_id": "S",
                           records.RECORD_ID_KEY: records.new_record_id(), "ts": int(time.time()),
                           "text": "an offsite note", "tags": ["episodic"]}, path=led)
            index.rebuild(ledger_file=led, index_file=idx)     # stamps generation 0 from `other`'s sidecar (absent -> 0)
            self.assertFalse(index.query("offsite", ledger_file=led, index_file=idx).degraded)
            ledger.bump_generation(for_path=led)               # writes other/ledger-meta.json -> 1 (NOT the env dir)
            q = index.query("offsite", ledger_file=led, index_file=idx)
            self.assertTrue(q.degraded)                        # reads `other`'s gen (1) != index gen (0) -> scan
            self.assertEqual(len(q.records), 1)
        finally:
            shutil.rmtree(other, ignore_errors=True)


class LockTests(_Base):
    def test_compaction_reports_busy_when_the_single_writer_lock_is_held(self):
        self._episodic("a note")
        held = capture._acquire_lock(os.path.join(ledger.ledger_dir(), capture.LOCK_FILENAME))
        self.assertIsNotNone(held)
        try:
            self.assertEqual(compact.compact()["status"], "busy")   # never writes lock-free
        finally:
            capture._release_lock(held)

    def test_a_bookkeeping_marker_appended_under_contention_is_skipped_not_written_lock_free(self):
        # The single-writer law, exercised on the one marker kind a real store is full of. Compaction holds
        # this lock across its whole read-fold-swap, so anything that wrote without it could be renamed away
        # mid-flight. Nothing appends a reinforcement marker any more, but the law it proved still binds every
        # writer, and a legacy store is the honest fixture for it.
        e = self._episodic("a note")
        rid = e[records.RECORD_ID_KEY]
        held = capture._acquire_lock(os.path.join(ledger.ledger_dir(), capture.LOCK_FILENAME))
        try:
            self.assertIsNone(capture._acquire_lock(               # a second writer cannot take it
                os.path.join(ledger.ledger_dir(), capture.LOCK_FILENAME)))
        finally:
            capture._release_lock(held)
        ledger.append(legacy.reinforcement(rid))                   # lock free now -> the marker lands
        self.assertEqual(self._reinforcements(), 1)


class ProductionSafetyTests(_Base):
    def test_compact_never_injects_a_crash_by_default(self):
        # The fault injector defaults OFF, so no production caller can reach it.
        self.assertIsNone(inspect.signature(compact.compact).parameters["_crash_after"].default)
        self._episodic("a note")
        self.assertEqual(compact.compact()["status"], "ok")    # a real pass completes, never raises

    def test_a_leftover_temp_is_reaped_and_never_promoted(self):
        # Recovery binds to the fixed canonical name: a complete same-schema leftover temp is ignored-and-reaped,
        # never mistaken for the canonical ledger.
        e = self._episodic("the canonical note")
        bogus = os.path.join(self._tmp.name, compact._TEMP_PREFIX + "deadbeef" + compact._TEMP_SUFFIX)
        with open(bogus, "w", encoding="utf-8") as fh:
            fh.write('{"kind":"episodic","text":"a stray leftover that must never become canonical"}\n')
        compact.compact()
        self.assertEqual(self._scratch(), 0)                   # the leftover was reaped
        self.assertEqual(len(self._by_id(e[records.RECORD_ID_KEY])), 1)   # the canonical note is the survivor
        self.assertEqual(index.query("stray").records, [])     # the leftover was never promoted into recall


class LedgerIntegrityCompactionTests(_Base):
    """#396: compaction is bound by the ledger read law — it preserves a torn trailing fragment and
    reports a skipped malformed line, never silently erasing recoverable recall with erased:0."""

    def test_a_torn_trailing_fragment_survives_compaction_and_still_heals(self):
        self._episodic("kept content")
        # A crash mid-append: a COMPLETE JSON record missing only its terminating newline.
        torn = b'{"kind":"episodic","text":"torn but complete","id":"x1"}'
        with open(ledger.ledger_path(), "ab") as fh:
            fh.write(torn)
        self.assertTrue(ledger.read().torn_trailing)
        report = compact.compact()
        self.assertEqual(report["status"], "ok")
        self.assertTrue(report["torn_preserved"])
        # The torn tail survives the whole-ledger swap, byte-for-byte, still un-terminated.
        after = ledger.read()
        self.assertTrue(after.torn_trailing)
        self.assertEqual(after.torn_raw, torn)
        # ...and a later append still heals it into a real record — it was preserved, not erased.
        ledger.append({"kind": "episodic", "text": "next after heal"})
        healed = ledger.read()
        self.assertFalse(healed.torn_trailing)
        texts = [r.get("text") for r in healed.records]
        self.assertIn("torn but complete", texts)   # the once-torn fragment, now recovered
        self.assertIn("kept content", texts)         # the real content, never at risk

    def test_a_malformed_line_is_reported_by_compaction_not_silently_erased(self):
        self._episodic("real content")
        with open(ledger.ledger_path(), "a", encoding="utf-8") as fh:
            fh.write("this is not json at all\n")
        report = compact.compact()
        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["malformed"], 1)     # skipped-and-REPORTED, never a silent erased:0

    def test_a_clean_compaction_reports_no_corruption(self):
        self._episodic("clean note")
        report = compact.compact()
        self.assertEqual(report["malformed"], 0)
        self.assertFalse(report["torn_preserved"])


class MigrationWindowRefusalTests(_Base):
    """#396: compaction refuses within a migration window, and self-heals an orphaned marker."""

    def _marker_path(self):
        return os.path.join(self._tmp.name, capture.MIGRATION_MARKER_FILENAME)

    def _write_marker(self, marker):
        with open(self._marker_path(), "w", encoding="utf-8") as fh:
            fh.write(json.dumps(marker))

    def test_compaction_refuses_within_a_live_migration_window(self):
        self._episodic("a decision")
        self.assertTrue(capture.open_migration_window(self._tmp.name))    # live marker (this PID, now)
        report = compact.compact()
        self.assertEqual(report["status"], "busy")
        self.assertEqual(report["folded"], 0)
        self.assertEqual(report["pruned"], 0)
        self.assertTrue(os.path.exists(self._marker_path()))             # a live migration's marker is untouched

    def test_compaction_self_heals_an_orphaned_marker_and_proceeds(self):
        self._episodic("a decision")
        # Orphaned by the wall-clock ceiling (deterministic; no reliance on a specific dead PID).
        self._write_marker({"pid": os.getpid(), "started_at": time.time() - capture.MIGRATION_ORPHAN_CEILING_S - 1})
        report = compact.compact()
        self.assertEqual(report["status"], "ok")
        self.assertFalse(os.path.exists(self._marker_path()))            # the stale marker was cleared under the lock

    def test_maybe_compact_clears_an_orphan_even_below_the_waste_threshold(self):
        # The reachability fix (deliverable gate): the orphan recovery must NOT ride the waste gate, or the boot
        # heads-up would linger forever on a quiet ledger. Below-threshold => the fold is skipped, but the orphan
        # is still reaped so recovery (and the heads-up clearing) rides EVERY maybe_compact.
        self.assertLess(compact.reclaimable_waste(), compact._COMPACT_WASTE_THRESHOLD)   # a clean/quiet ledger
        self._write_marker({"pid": os.getpid(), "started_at": time.time() - capture.MIGRATION_ORPHAN_CEILING_S - 1})
        report = compact.maybe_compact()
        self.assertEqual(report["status"], "skipped")                   # nothing to fold — the gate holds
        self.assertFalse(os.path.exists(self._marker_path()))           # ...but the orphan was reaped anyway

    def test_maybe_compact_leaves_a_live_marker_in_place(self):
        self._write_marker({"pid": os.getpid(), "started_at": time.time()})   # a genuinely in-progress migration
        compact.maybe_compact()
        self.assertTrue(os.path.exists(self._marker_path()))            # never reaped while live


if __name__ == "__main__":
    unittest.main()
