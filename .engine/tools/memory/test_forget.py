"""Unit tests for forget.py — Layer-1 logical retirement of crash-duplicate consolidations.

The retirement is REVERSIBLE and recall-only: an orphaned crash-pass episodic is excluded from recall but
stays resident in the ledger, fully recoverable. These tests exercise the real filter (`live_records`), the
real recall paths (fast index + slow scan) through it, the read-only `duplicates` inspector, and the
build-conformance invariant that this Layer-1 module reaches NO physical-erasure path.
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from memory import capture, compact, forget, index, ledger, legacy_shapes as legacy, records  # noqa: E402


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

    def _episodic(self, session, text, batch, role="decision"):
        """Append an episodic carrying `batch` (a crashed pass leaves this with no closing marker)."""
        ledger.append(legacy.episodic(session, role, text, batch))

    def _marker(self, session, batch):
        ledger.append(legacy.marker(session, batch))

    def _episodics(self):
        return [r for r in ledger.iter_records() if r.get("kind") == records.EPISODIC_KIND]

    def _live_episodics(self):
        return [r for r in forget.live_records() if r.get("kind") == records.EPISODIC_KIND]


class LiveRecordsTests(_Base):
    def test_an_orphan_episodic_is_retired_from_recall(self):
        self._episodic("S", "orphan note", "batch-x")          # batch-x never gets a marker
        self.assertEqual(list(forget.live_records()), [])      # the orphan is not surfaced

    def test_a_marked_pass_is_kept(self):
        self._episodic("S", "good note", "batch-x")
        self._marker("S", "batch-x")
        kinds = [r.get("kind") for r in forget.live_records()]
        self.assertIn(records.EPISODIC_KIND, kinds)            # the completed pass's episodic is live
        self.assertIn(records.MARKER_KIND, kinds)              # markers always pass through

    def test_two_consolidated_markers_per_session_keep_both_passes_live(self):
        # #446: a re-swept session carries more than one `consolidated` marker (one per pass). Both passes' batches
        # are closed, so neither pass's episodic is orphaned — the recall-closure keying tolerates multiple markers.
        legacy.store_episodic("S", [{"role": "decision", "text": "first pass summary"}])
        ledger.append(capture._make_record("S", 1, "user", "a later turn"))
        legacy.store_episodic("S", [{"role": "lesson", "text": "second pass summary"}])
        self.assertEqual(len(self._records_of(records.MARKER_KIND)), 2)
        live = {r["text"] for r in self._live_episodics()}
        self.assertEqual(live, {"first pass summary", "second pass summary"})   # both stay live, neither orphaned

    def _records_of(self, kind):
        return [r for r in ledger.iter_records() if r.get("kind") == kind]

    def test_a_batchless_episodic_is_always_live(self):
        # a pre-batch episodic with no batch field — nothing to resolve, so never retired (back-compat)
        ledger.append({"v": 1, "kind": records.EPISODIC_KIND, "session_id": "S", "text": "old note", "tags": []})
        self.assertEqual(len(self._live_episodics()), 1)

    def test_an_empty_string_batch_is_treated_as_batchless_and_stays_live(self):
        # Defensive: the real write path mints a uuid, never "" — but a hand-edited / corrupt record with an
        # empty batch must be treated as batchless (always live), never mistaken for an unmarked orphan.
        ledger.append({"v": 1, "kind": records.EPISODIC_KIND, "session_id": "S", "text": "edge note",
                       "tags": [], records.BATCH_KEY: ""})
        self.assertEqual(len(self._live_episodics()), 1)

    def test_genuine_turns_are_recall_content_and_markers_pass_through(self):
        # The conversation IS the canonical record (eADR-0038), so a genuine turn-delta is recall content. This
        # DELIBERATELY inverts the earlier verdict, which excluded the whole kind because verbatim raw crowded
        # paraphrased summaries out of recall; the answer now is that the summaries are the disposable layer.
        # The structural `consolidated` marker still passes through (it carries no recall text, so it never
        # surfaces as a hit, but it is not bookkeeping the reader drops).
        ledger.append(capture._make_record("S", 0, "user", "a turn note"))   # turn-delta, no batch
        self._marker("S", "batch-x")                                          # a lone marker
        kinds = sorted({r.get("kind") for r in forget.live_records()})
        self.assertIn(capture.RECORD_KIND, kinds)       # the genuine turn IS recall content now
        self.assertIn(records.MARKER_KIND, kinds)

    def test_a_harness_injected_pseudo_turn_is_still_excluded(self):
        # The one thing that stays out: text the operator never said. Presenting a `/compact` continuation
        # summary or a task notification as their own words is a correctness bug, not a cosmetic one — the same
        # rule the consolidation sweep and the transcript-window reader already apply.
        genuine = capture._make_record("S", 0, "user", "a genuine turn note")
        injected = capture._make_record("S", 1, "user", "<task-notification> ignore me </task-notification>")
        injected.setdefault("tags", []).append(records.INJECTED_TAG)
        ledger.append(genuine)
        ledger.append(injected)
        texts = [r.get("text") for r in forget.live_records()]
        self.assertIn("a genuine turn note", texts)
        self.assertNotIn("<task-notification> ignore me </task-notification>", texts)

    def test_an_excluded_pseudo_turn_stays_in_the_raw_ledger_recoverable(self):
        # exclusion is recall-only — nothing is ever deleted by it (recall-exclusion, not erasure)
        injected = capture._make_record("S", 0, "user", "This session is being continued from a previous conversation")
        ledger.append(injected)
        self.assertEqual([r.get("kind") for r in ledger.iter_records()], [capture.RECORD_KIND])  # still resident
        self.assertEqual(list(forget.live_records()), [])                                         # just not surfaced

    def test_a_turn_is_reachable_on_both_recall_paths_and_the_sweep_still_sees_it(self):
        # Membership holds identically on the fast FTS5 path AND the degraded forced scan (the parity law),
        # while the consolidation sweep reads the raw ledger UNFILTERED, so the delta is still its input too.
        ledger.append(capture._make_record("S", 0, "user", "a quokka turn note"))
        self._episodic("S", "the quokka decision", "batch-x")
        self._marker("S", "batch-x")                                  # close the batch -> the episodic is live
        index.rebuild()
        for hits in (index.query("quokka").records, index.query("quokka", force_scan=True).records):
            kinds = {r.get("kind") for r in hits}
            self.assertIn(records.EPISODIC_KIND, kinds)               # the curated summary surfaces...
            self.assertIn(capture.RECORD_KIND, kinds)                 # ...and so does the conversation itself
        raw_turns = [r for r in ledger.iter_records()
                     if r.get("kind") == capture.RECORD_KIND and r.get("session_id") == "S"]
        self.assertEqual([r.get("text") for r in raw_turns],
                         ["a quokka turn note"])                      # the conversation itself is untouched

    def test_every_chunk_of_a_legacy_injected_message_is_excluded_not_just_the_first(self):
        # Capture splits a long message into several records sharing one `seq`. For a message captured BEFORE
        # injected-tagging existed, the only recogniser is a start-anchored text match — which by construction
        # matches the FIRST chunk alone. That was inert while the whole kind was excluded from recall; now the
        # tail chunks would be surfaced as ordinary conversation, and a `/compact` summary contains a section
        # headed "All user messages" — so recall could return a machine's paraphrase of what was asked for, as
        # if the operator had said it. Measured on the real store when this was found: 442 such chunks.
        head = capture._make_record("S", 4, "user", "This session is being continued from a previous conversation")
        tail = capture._make_record("S", 4, "user", "6. All user messages:\n   - deploy the thing")
        for r in (head, tail):
            r.pop("tags", None)                       # legacy shape: captured before tagging existed
        ledger.append(head)
        ledger.append(tail)
        texts = [r.get("text") for r in forget.live_records()]
        self.assertEqual(texts, [], "a later chunk of an injected message must travel with its head")

    def test_nothing_of_any_kind_is_ever_aged_out_of_recall(self):
        # The tier ratchet used to archive a never-reinforced record at 26.7 days (`dead-end`) to 32.9
        # (`decision`), and a captured turn could never earn its way out (nothing reinforces what nothing could
        # recall). Exempting only the turn would have left the summaries carrying the decisions aging out from
        # underneath the conversation, so the ratchet is gone for every kind — which is what this asserts, one
        # record per role plus a role-less turn, all far past every boundary the ratchet ever had. The sealed
        # benchmark CANNOT catch a regression here (its corpus is stamped relative to run time), so this must.
        ancient = int(time.time()) - 400 * 86400
        turn = capture._make_record("S", 0, "user", "an ancient quokka turn note")
        turn["ts"] = ancient
        ledger.append(turn)
        for role in sorted(legacy.ROLE_VOCABULARY):
            rec = legacy.episodic("S", role, f"an ancient {role} note", "b")
            rec.pop(records.BATCH_KEY, None)              # batchless: always live, never a crash orphan
            rec["ts"] = ancient
            ledger.append(rec)
        texts = [r.get("text") for r in forget.live_records()]
        self.assertIn("an ancient quokka turn note", texts)
        for role in legacy.ROLE_VOCABULARY:
            self.assertIn(f"an ancient {role} note", texts, f"a {role} record aged out of recall")

    def test_the_orphan_stays_in_the_raw_ledger_recoverable(self):
        self._episodic("S", "orphan note", "batch-x")          # retired from recall...
        self.assertEqual(len(self._episodics()), 1)            # ...but STILL in the ledger (recoverable)
        self.assertEqual(self._live_episodics(), [])

    def test_multiple_orphan_batches_all_retired_only_the_marked_one_surfaces(self):
        self._episodic("S", "crash one", "batch-a")            # crashed pass A (no marker)
        self._episodic("S", "crash two", "batch-b")            # crashed pass B (no marker)
        self._episodic("S", "the good one", "batch-c")         # completed pass C...
        self._marker("S", "batch-c")                           # ...with its marker
        self.assertEqual([r["text"] for r in self._live_episodics()], ["the good one"])

class RecallRetirementTests(_Base):
    def test_a_crash_duplicate_surfaces_once_in_recall(self):
        self._episodic("S", "the sourdough decision", "the-pass-that-crashed")   # orphan
        legacy.store_episodic("S", [{"role": "decision", "text": "the sourdough decision retried"}])
        hits = [r for r in index.query("sourdough").records if r.get("kind") == records.EPISODIC_KIND]
        self.assertEqual(len(hits), 1)
        self.assertIn("retried", hits[0]["text"])              # the completed retry, not the orphan

    def test_fast_and_slow_recall_agree_after_retirement(self):
        self._episodic("S", "the quokka migration", "the-pass-that-crashed")
        legacy.store_episodic("S", [{"role": "decision", "text": "the quokka migration retried"}])
        fast = sorted(r["text"] for r in index.query("quokka").records
                      if r.get("kind") == records.EPISODIC_KIND)
        slow = sorted(r["text"] for r in index.query("quokka", force_scan=True).records
                      if r.get("kind") == records.EPISODIC_KIND)
        self.assertEqual(fast, slow)                           # parity holds through the retirement filter
        self.assertEqual(len(fast), 1)

    def test_the_batch_uuid_is_not_a_search_term(self):
        legacy.store_episodic("S", [{"role": "decision", "text": "a plain note"}])
        ep = next(r for r in ledger.iter_records() if r.get("kind") == records.EPISODIC_KIND)
        self.assertEqual(index.query(ep[records.BATCH_KEY]).records, [])   # the uuid is provenance, not content


class DuplicatesInspectorTests(_Base):
    def test_lists_retired_passes_by_session_not_the_kept_ones(self):
        self._episodic("S1", "crashed note one", "batch-a")
        self._episodic("S2", "crashed note two", "batch-b")
        self._episodic("S2", "kept note", "batch-c")
        self._marker("S2", "batch-c")
        groups = forget.duplicates()
        self.assertEqual(set(groups), {"S1", "S2"})
        self.assertEqual([r["text"] for r in groups["S1"]], ["crashed note one"])
        self.assertEqual([r["text"] for r in groups["S2"]], ["crashed note two"])  # the kept note is NOT listed

    def test_empty_when_nothing_is_retired(self):
        self._episodic("S", "good", "batch-x")
        self._marker("S", "batch-x")
        self.assertEqual(forget.duplicates(), {})


class BuildConformanceTests(unittest.TestCase):
    def test_forget_reaches_no_physical_erasure_path(self):
        # Layer-1 logical retirement NEVER erases (the two-layer law): physical removal is reachable
        # only through Layer 2's merge-gated path. forget.py must carry no ledger-delete / erase call — a
        # build-conformance invariant pinned by source scan.
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "forget.py")
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
        for token in ("os.remove", "os.unlink", "os.truncate", "truncate(", "rmtree", "os.replace"):
            self.assertNotIn(token, src, f"forget.py must not reach physical erasure: found {token!r}")

    def test_no_layer1_file_mints_an_erasure_marker(self):
        # Layer-2 physical erasure is reachable ONLY through compact's gated removal, which fires only on
        # an `operator-adjudicated-erasure` marker. Exactly TWO Layer-2 files may touch the minter call:
        # `compact.py` (the chokepoint OWNER — it DEFINES `enact_erasure` and performs the removal) and
        # `erasure_observer.py` (the SANCTIONED cross-session ENACTOR — it calls the minter, but ONLY with a
        # merge SHA read from a genuinely-merged single-purpose erasure PR, never from evidence or argv). Every OTHER
        # (Layer-1) memory file must NOT call the minter — a Layer-1 routine that minted a marker could route the
        # autonomous fold into erasure. The ban targets the minter CALL (`enact_erasure(`), NOT the kind constant
        # (forget legitimately references `records.ERASURE_KIND` to drop the marker from recall) and NOT `compact()`
        # (the deleted writers legitimately called it — calling compact is not minting). A glob-walk over the whole
        # memory package (not a fixed file list) so a Layer-1 tool added LATER is covered too. The package marker
        # `__init__.py` is NOT exempted — it is scanned like any other file (it must never mint either).
        sanctioned = ("compact.py", "erasure_observer.py")
        mem_dir = os.path.dirname(os.path.abspath(__file__))
        for name in sorted(os.listdir(mem_dir)):
            if not name.endswith(".py") or name.startswith("test_") or name in sanctioned:
                continue
            with open(os.path.join(mem_dir, name), encoding="utf-8") as fh:
                src = fh.read()
            self.assertNotIn("enact_erasure(", src,
                             f"{name} must not mint an erasure marker — the only sanctioned callers are "
                             f"compact.enact_erasure (the owner) and erasure_observer (the cross-session enactor)")


_DAY = 86400


class SetAsideReportTests(_Base):
    """The set-aside report the boot readout relays: the ONE class recall drops that the operator has a handle
    on (summarised), never the crash-orphan class. There used to be a second, reversible class — a note the
    archived-tier ratchet aged out — and these tests pinned it; the ratchet is gone for every kind, so what they
    pin now is that age alone sets nothing aside."""

    def _aged(self, text, *, age_days=400, session="D"):
        """A never-reinforced episodic far older than any threshold the retired ratchet used. Batchless, so it
        is never a crash orphan either — it is simply an old note, and it must stay in recall."""
        rec = legacy.episodic(session, "decision", text, "b")
        rec.pop(records.BATCH_KEY, None)
        rec["ts"] = int(time.time()) - age_days * _DAY
        ledger.append(rec)
        return rec[records.RECORD_ID_KEY]

    def _raws(self, n, *, age_days=25, session="S"):
        out = []
        for i in range(n):
            rec = legacy.episodic(session, "decision", f"raw note {i} word{i}", "b")
            rec.pop(records.BATCH_KEY, None)
            rec["ts"] = int(time.time()) - age_days * _DAY
            ledger.append(rec)
            out.append(rec[records.RECORD_ID_KEY])
        return out

    def _summarise(self, raw_ids, *, session="S"):
        """A COMPLETED roll-up: fold the raws into one gist so each raw is superseded out of recall."""
        legacy.store_gist(session, "rolled-up summary of the older notes", list(raw_ids))

    def test_an_old_unused_note_is_not_set_aside_at_all(self):
        # The inversion of what this class used to assert. A note nobody has come back to in over a year is
        # still searchable, so it is not "set aside" and the readout must not name it — there is nothing to
        # offer the operator a handle on.
        rid = self._aged("an old decision nobody revisits")
        report = forget.set_aside()
        self.assertEqual(report["rows"], [])
        self.assertEqual(report["totals"], {"summarised": 0, "withheld_notes": 0, "withheld_sessions": 0})
        self.assertIn(rid, {r.get(records.RECORD_ID_KEY) for r in forget.live_records()})

    def test_a_summarised_raw_is_set_aside_and_not_reversible(self):
        raws = self._raws(3)
        self._summarise(raws)
        report = forget.set_aside()
        rows = {r["id"]: r for r in report["rows"]}
        for rid in raws:
            self.assertIn(rid, rows)
            self.assertEqual(rows[rid]["reason"], forget.SET_ASIDE_SUMMARISED)
            self.assertFalse(rows[rid]["reversible"])   # a folded raw CANNOT be brought back — only shown
            self.assertTrue(rows[rid]["stands_in"])     # it names the summary that stands in for it
        self.assertEqual(report["totals"]["summarised"], 3)

    def test_crash_orphans_are_excluded_from_the_readout(self):
        # A consolidation orphan (unclosed batch) and a roll-up gist orphan are duplicates the good copy replaces,
        # not losses — deliberately NOT in the operator readout (an "undo" would re-admit a duplicate).
        ledger.append(legacy.episodic("S", "decision", "orphaned episodic", "batch-x"))
        legacy.store_gist("S", "orphan gist", ["nope"], close=False)   # the closing marker never landed
        report = forget.set_aside()
        texts = {r["text"] for r in report["rows"]}
        self.assertNotIn("orphaned episodic", texts)
        self.assertNotIn("orphan gist", texts)
        self.assertEqual(report["identity"], [])       # neither class is set-aside for the operator

    def test_markers_and_turn_deltas_never_appear(self):
        self._summarise(self._raws(1))                  # one real set-aside row to prove the report is non-empty
        ledger.append(capture._make_record("S", 0, "user", "a raw turn note"))   # ambient turn-delta
        ledger.append(legacy.reinforcement("whatever"))   # a reinforcement marker an older engine left
        report = forget.set_aside()
        self.assertTrue(report["rows"])
        self.assertEqual({r["reason"] for r in report["rows"]}, {forget.SET_ASIDE_SUMMARISED})
        self.assertTrue(all(r["text"] not in ("a raw turn note",) for r in report["rows"]))

    def test_set_aside_and_live_records_partition_the_recall_eligible_population(self):
        # The honest invariant: every content record (episodic/gist) is EITHER surfaced by recall, OR named in the
        # set-aside report, OR a crash-orphan (excluded from both by design) — an EXACT, disjoint partition. The
        # orphan set is derived INDEPENDENTLY from live_records' own predicates (never as a residual), so a
        # set_aside miss — a record recall hides that the readout fails to classify — cannot hide in the leftover;
        # it breaks the partition. This is what makes the test a real net for a future live_records exclusion.
        # Compact partway so the check runs against the folded (matured-store) form, not just fresh markers.
        live = self._aged("a fresh live note", age_days=0, session="L")
        old_but_live = self._aged("an old note nobody revisits", session="L2")
        raws = self._raws(2, session="R")
        self._summarise(raws, session="R")
        ledger.append(legacy.episodic("O", "decision", "orphan", "orphan-batch"))
        compact.compact()                                        # fold supersessions + prune markers

        src = ledger.ledger_path()
        closed = forget._closed_batches(src)
        closed_rollup = forget._closed_rollup_batches(src)
        content = [r for r in ledger.iter_records()
                   if isinstance(r, dict) and r.get("kind") in (records.EPISODIC_KIND, records.GIST_KIND)]
        all_content = {r.get(records.RECORD_ID_KEY) for r in content}
        live_ids = {r.get(records.RECORD_ID_KEY) for r in forget.live_records()
                    if r.get("kind") in (records.EPISODIC_KIND, records.GIST_KIND)}
        aside_ids = set(forget.set_aside()["identity"])
        # the crash-orphan set, derived from the SAME predicates live_records excludes by — not a residual
        orphan_ids = {r.get(records.RECORD_ID_KEY) for r in content
                      if forget._is_retired(r, closed) or forget._is_gist_orphan(r, closed_rollup)}

        self.assertEqual(live_ids & aside_ids, set())            # disjoint: nothing is both live and set aside
        self.assertEqual(aside_ids & orphan_ids, set())          # a crash orphan is never offered in the readout
        self.assertEqual(live_ids & orphan_ids, set())           # an orphan is not live either
        self.assertEqual(live_ids | aside_ids | orphan_ids, all_content)   # exact, complete partition
        self.assertIn(live, live_ids)
        self.assertIn(old_but_live, live_ids)                    # age alone never moves a note to the aside set
        self.assertLessEqual(set(raws), aside_ids)               # summarised raws set aside even after compaction
        self.assertTrue(orphan_ids)                              # the orphan is in the excluded set, not lost

    def test_summarised_survives_compaction_which_prunes_the_marker(self):
        # The matured-store case: compaction folds each supersession into the raw's carried `superseded_by`
        # field and PRUNES the `superseded` marker. The readout must still classify the raw as summarised (from
        # the carried field, exactly as live_records does) — never go silent, and never fall through to a false
        # "demoted + reversible". Regression guard for the marker-only-classification defect.
        raws = self._raws(2)
        self._summarise(raws)
        compact.compact()                                        # folds + prunes the markers
        report = forget.set_aside()
        rows = {r["id"]: r for r in report["rows"]}
        for rid in raws:
            self.assertIn(rid, rows, "a summarised raw vanished from the readout after compaction")
            self.assertEqual(rows[rid]["reason"], forget.SET_ASIDE_SUMMARISED)
            self.assertFalse(rows[rid]["reversible"])            # never a false bring-back offer post-compaction
        self.assertEqual(report["totals"], {"summarised": 2, "withheld_notes": 0, "withheld_sessions": 0})

    def test_no_row_is_ever_offered_as_reversible(self):
        # The honesty contract for the SUMMARISED class: a summary was written over the note, and there is no
        # un-fold. Nothing in the readout may offer to bring one of those back.
        #
        # There IS a reversible class — what the operator withheld, which `forget.restore` genuinely reverses —
        # and it is deliberately reported as a count rather than as rows (`set_aside`'s docstring says why:
        # a row would quote withheld wording back into the briefing at every session start). So the invariant
        # this test pins survives that feature intact: every ROW is a fold, and no row is ever reversible.
        self._aged("an old note nobody revisits")
        raws = self._raws(2, age_days=40)
        self._summarise(raws)
        ledger.append(legacy.episodic("O", "decision", "aged orphan", "orphan-b"))
        compact.compact()
        rows = forget.set_aside()["rows"]
        self.assertTrue(rows)
        # The load-bearing half: the restore that once backed `reversible=True` is GONE, so nothing could
        # honour such a row even if one appeared. (The field itself is a literal `False` in `set_aside`, so
        # asserting on it alone would be a tautology — it is checked here only to keep the readout's contract
        # explicit.)
        self.assertFalse(hasattr(forget, "restore_to_recall"))
        self.assertEqual([r["reversible"] for r in rows], [False] * len(rows))

    def test_totals_count_the_full_population_while_rows_respect_the_limit(self):
        self._summarise(self._raws(5))
        report = forget.set_aside(limit=2)
        self.assertEqual(len(report["rows"]), 2)                 # the sample is bounded
        self.assertEqual(report["totals"]["summarised"], 5)      # the total is the whole population
        self.assertEqual(len(report["identity"]), 5)             # identity is the whole population, not the sample

    def test_ordering_survives_a_damaged_timestamp(self):
        # The summarised class is set aside for a reason independent of its ts (a completed supersession), so a
        # raw carrying a damaged ts still belongs in the report — and the sort key must tolerate it, sorting it
        # last rather than raising mid-sort (the index.recent_decisions total-key guarantee). Hand-build a closed
        # supersession over a raw whose ts is a string, alongside a well-formed one.
        good = legacy.episodic("S", "decision", "well-formed folded raw word1", "b")
        good.pop(records.BATCH_KEY, None)
        good["ts"] = int(time.time()) - 25 * _DAY
        bad = legacy.episodic("S", "decision", "folded raw with a broken ts word2", "b")
        bad.pop(records.BATCH_KEY, None)
        bad["ts"] = "not-a-number"
        gist = legacy.gist("S", "the summary",
                           [good[records.RECORD_ID_KEY], bad[records.RECORD_ID_KEY]], "rb")
        for rec in (good, bad, gist):
            ledger.append(rec)
        gid = gist[records.RECORD_ID_KEY]
        ledger.append(legacy.superseded(good[records.RECORD_ID_KEY], gid, "rb"))
        ledger.append(legacy.superseded(bad[records.RECORD_ID_KEY], gid, "rb"))
        ledger.append(legacy.rollup_marker("S", "rb"))     # closes batch rb -> both supersessions live
        report = forget.set_aside()                              # no exception despite the string ts
        ids = [r["id"] for r in report["rows"]]
        self.assertIn(good[records.RECORD_ID_KEY], ids)
        self.assertIn(bad[records.RECORD_ID_KEY], ids)


class SetAsideHandleTests(_Base):
    def test_a_folded_raw_stays_out_of_recall_and_its_wording_stays_readable(self):
        # The only handle a set-aside note has, now that the reversible class is gone: the summary stands in for
        # it in search, and its original wording is still there to be read word-for-word.
        rec = legacy.episodic("S", "decision", "raw folded away word1", "b")
        rec.pop(records.BATCH_KEY, None)
        rec["ts"] = int(time.time()) - 25 * _DAY
        ledger.append(rec)
        raw_id = rec[records.RECORD_ID_KEY]
        legacy.store_gist("S", "the summary", [raw_id])
        self.assertNotIn(raw_id, {r.get(records.RECORD_ID_KEY) for r in forget.live_records()})
        self.assertEqual(forget.recorded_text(raw_id)["text"], "raw folded away word1")

    def test_recorded_text_returns_the_exact_wording_and_does_not_reinforce(self):
        rec = legacy.episodic("S", "decision", "the exact original wording", "b")
        rec.pop(records.BATCH_KEY, None)
        ledger.append(rec)
        rid = rec[records.RECORD_ID_KEY]
        before = sum(1 for r in ledger.iter_records() if r.get("kind") == records.REINFORCEMENT_KIND)
        got = forget.recorded_text(rid)
        self.assertEqual(got["text"], "the exact original wording")
        after = sum(1 for r in ledger.iter_records() if r.get("kind") == records.REINFORCEMENT_KIND)
        self.assertEqual(before, after)                          # merely looking never re-ranks recall
        self.assertIsNone(forget.recorded_text("no-such-id"))
        self.assertIsNone(forget.recorded_text(""))


class WithholdTests(_Base):
    """The operator's own reversible control: withhold takes a note or a whole conversation out of everything
    recall surfaces, and restore puts it back. Nothing is deleted at any point."""

    WORD = "marzipan"

    def _turns(self, session: str, count: int = 4, ts: "int | None" = None) -> list:
        """Append `count` genuine captured turns and return their record ids."""
        base = int(time.time()) if ts is None else ts
        ids = []
        for n in range(count):
            rid = records.new_record_id()
            ids.append(rid)
            ledger.append({"v": capture.RECORD_VERSION, "kind": records.AMBIENT_CAPTURE_KIND,
                           records.RECORD_ID_KEY: rid, "session_id": session, "seq": n,
                           "speaker": "user" if n % 2 == 0 else "assistant", "ts": base + n,
                           "text": f"turn {n} about {self.WORD} and pastry"})
        return ids

    def _hits(self, *, force_scan: bool) -> int:
        return len(index.search(self.WORD, force_scan=force_scan).records)

    def test_withhold_reaches_the_fast_index_with_no_rebuild_in_between(self):
        # THE load-bearing test, and the shape matters as much as the assertion. `ledger.append` does not move
        # the generation stamp and `index.extend` accepts captured turns only, so an implementation that merely
        # appended the marker would leave the fast index stamped current while holding the withheld record —
        # and it would answer with `degraded=False`, i.e. claiming to be authoritative. Every ordinary cabinet
        # test rebuilds before querying and would sail past that. This one must NOT rebuild after the withhold.
        ids = self._turns("s-withhold")
        index.rebuild()
        self.assertEqual((self._hits(force_scan=False), self._hits(force_scan=True)), (4, 4))

        forget.withhold(record_id=ids[1])
        self.assertEqual(self._hits(force_scan=True), 3)          # the scan reads membership directly
        self.assertEqual(self._hits(force_scan=False), 3)         # and the fast path must agree, unprompted

    def test_restore_brings_it_back_on_both_paths(self):
        ids = self._turns("s-restore")
        index.rebuild()
        forget.withhold(record_id=ids[1])
        self.assertEqual(self._hits(force_scan=False), 3)
        forget.restore(record_id=ids[1])
        self.assertEqual((self._hits(force_scan=False), self._hits(force_scan=True)), (4, 4))

    def test_withholding_a_session_reaches_the_window_reader_and_the_briefing_too(self):
        # A predicate applied only inside `live_records` would take the session out of search while the window
        # reader read it back verbatim and the cold-start briefing kept quoting its opening line every session.
        from memory import recall

        self._turns("s-whole")
        index.rebuild()
        self.assertTrue(recall.session_turns("s-whole"))
        self.assertTrue(recall.session_cards())

        forget.withhold(session_id="s-whole")
        self.assertEqual(self._hits(force_scan=False), 0)
        self.assertEqual(self._hits(force_scan=True), 0)
        self.assertEqual(recall.session_turns("s-whole"), [])
        self.assertEqual(recall.session_cards(), [])

        forget.restore(session_id="s-whole")
        self.assertEqual(len(recall.session_turns("s-whole")), 4)
        self.assertEqual(self._hits(force_scan=False), 4)

    def test_a_withheld_conversation_stays_withheld_as_it_continues(self):
        # Withholding a conversation the operator is still IN is the most natural way to use the control, and
        # the incremental index update is the path that breaks it: it inserts a freshly captured turn without
        # consulting the ledger at all, so every turn after the withhold went straight back into the fast
        # index — found there, absent from the scan, and answered as authoritative. The index carries the
        # withholds it was built under precisely so this cannot happen.
        self._turns("s-live")
        index.rebuild()
        forget.withhold(session_id="s-live")
        self.assertEqual((self._hits(force_scan=False), self._hits(force_scan=True)), (0, 0))

        later = self._turns("s-live", count=2, ts=int(time.time()) + 500)
        fresh = [r for r in ledger.iter_records() if r.get(records.RECORD_ID_KEY) in set(later)]
        index.extend(fresh)
        self.assertEqual((self._hits(force_scan=False), self._hits(force_scan=True)), (0, 0))

        # And restoring brings back everything, including what was said while it was withheld — nothing was
        # dropped on the way in, only kept out of what recall surfaces.
        forget.restore(session_id="s-live")
        self.assertEqual((self._hits(force_scan=False), self._hits(force_scan=True)), (6, 6))

    def test_a_pin_goes_with_the_conversation_it_was_asked_for_in(self):
        # A pin records its origin under its OWN key, not `session_id`, so a session withhold missed it
        # entirely — the operator watched a conversation vanish from search, from the reader and from the
        # briefing while the one fragment still read into every future session was the one place they would
        # never think to look.
        from memory import pins as _pins

        self._turns("s-pinned")
        _pins.add("a standing note asked for in that conversation", session_id="s-pinned")
        self.assertEqual(len(_pins.list_pins()), 1)
        forget.withhold(session_id="s-pinned")
        self.assertEqual(_pins.list_pins(), [])
        forget.restore(session_id="s-pinned")
        self.assertEqual(len(_pins.list_pins()), 1)

    def test_what_is_withheld_can_be_named_again_after_the_session_that_hid_it(self):
        # Reversibility is promised on every surface, and `restore` needs the exact identifier. Nothing else
        # could supply one: the readout reports counts, search excludes these by construction, and the pin
        # list shows only live pins. Without this the promise held only while the session that performed the
        # withhold still had the id in context.
        ids = self._turns("s-named")
        self._turns("s-whole-named")
        forget.withhold(record_id=ids[0])
        forget.withhold(session_id="s-whole-named")
        report = forget.withheld_report()
        self.assertEqual([n["id"] for n in report["notes"]], [ids[0]])
        self.assertEqual([r["session_id"] for r in report["sessions"]], ["s-whole-named"])
        self.assertTrue(all(isinstance(n["withheld_at"], int) for n in report["notes"]))
        self.assertNotIn("text", report["notes"][0])       # identifiers and when — never the wording
        forget.restore(record_id=report["notes"][0]["id"])
        self.assertEqual(forget.withheld_report()["notes"], [])

    def test_ledger_order_decides_a_tie_that_timestamps_cannot(self):
        # Capture stamps whole seconds, so withhold-then-restore inside one second shares a `ts`. Ordering by
        # time would leave the operator's most recent instruction decided by a coin toss.
        ids = self._turns("s-tie")
        index.rebuild()
        moment = int(time.time())
        forget.withhold(record_id=ids[2], now=moment)
        forget.restore(record_id=ids[2], now=moment)
        self.assertEqual(self._hits(force_scan=False), 4)         # the LAST marker wins: restored
        forget.withhold(record_id=ids[2], now=moment)
        self.assertEqual(self._hits(force_scan=False), 3)         # and again, in the other direction

    def test_nothing_is_deleted_by_either_verb(self):
        ids = self._turns("s-intact")
        before = sum(1 for _ in ledger.iter_records())
        forget.withhold(session_id="s-intact")
        forget.restore(record_id=ids[0])
        after = [r for r in ledger.iter_records()]
        self.assertEqual(len(after), before + 2)                  # two markers appended, nothing removed
        self.assertEqual(sum(1 for r in after if r.get(records.RECORD_ID_KEY) in set(ids)), len(ids))

    def test_a_marker_naming_both_or_neither_target_is_refused(self):
        # One field carrying either a record id or a session id would be indistinguishable to every reader,
        # so the shape is checked where it is written rather than guessed at where it is read.
        for kwargs in ({}, {"record_id": "r", "session_id": "s"}, {"record_id": ""}, {"session_id": ""}):
            with self.assertRaises(forget.ControlNotRecorded):
                forget.withhold(**kwargs)

    def test_an_identifier_that_names_nothing_is_refused_not_confirmed(self):
        # Appending a marker always succeeds — it names a target and says nothing about whether the target is
        # real. So a stale or mistyped id produced a confident "that note is out of recall now" over a note
        # still fully searchable, and this is the one class of report where being wrong is silent: the
        # operator has no way to notice that the thing they made private is not.
        self._turns("s-exists")
        for kwargs in ({"record_id": "deadbeefdeadbeefdeadbeefdeadbeef"}, {"session_id": "s-nope"}):
            with self.assertRaises(forget.ControlNotRecorded) as caught:
                forget.withhold(**kwargs)
            self.assertIn("no", str(caught.exception).lower())
            with self.assertRaises(forget.ControlNotRecorded):
                forget.restore(**kwargs)
        self.assertEqual([r for r in ledger.iter_records()
                          if r.get("kind") in (records.WITHHOLD_KIND, records.RESTORE_KIND)], [])

    def test_withholding_something_already_withheld_says_so_rather_than_stacking(self):
        ids = self._turns("s-twice")
        forget.withhold(record_id=ids[0])
        with self.assertRaises(forget.ControlNotRecorded) as caught:
            forget.withhold(record_id=ids[0])
        self.assertIn("already out of recall", str(caught.exception))

    def test_a_corrupt_marker_surfaces_rather_than_hides(self):
        # Failure direction: a marker that cannot be read must never take conversation out of recall on its own.
        self._turns("s-corrupt")
        index.rebuild()
        for bad in ({records.TARGET_KEY: 7}, {records.TARGET_SESSION_KEY: None}, {}):
            marker = {"v": capture.RECORD_VERSION, "kind": records.WITHHOLD_KIND,
                      records.RECORD_ID_KEY: records.new_record_id(), "ts": int(time.time())}
            marker.update(bad)
            ledger.append(marker)
        self.assertEqual(self._hits(force_scan=True), 4)

    def test_the_markers_are_never_themselves_a_recall_result(self):
        ids = self._turns("s-markers")
        forget.withhold(record_id=ids[0])
        forget.restore(record_id=ids[0])
        kinds = {r.get("kind") for r in forget.live_records()}
        self.assertNotIn(records.WITHHOLD_KIND, kinds)
        self.assertNotIn(records.RESTORE_KIND, kinds)

    def test_the_readout_counts_what_is_withheld_without_quoting_it(self):
        ids = self._turns("s-readout")
        self._turns("s-other")
        forget.withhold(record_id=ids[0])
        forget.withhold(session_id="s-other")
        totals = forget.set_aside()["totals"]
        self.assertEqual(totals["withheld_notes"], 1)
        self.assertEqual(totals["withheld_sessions"], 1)
        # Counted, never quoted: no row carries the withheld record, so the briefing cannot print it back.
        self.assertNotIn(ids[0], {r["id"] for r in forget.set_aside()["rows"]})

    def test_withholding_a_session_takes_its_summaries_with_it(self):
        # The curated layer written over a withheld conversation is written FROM it, so leaving it surfaced
        # would defeat the control by paraphrase.
        rec = legacy.episodic("s-summary", "decision", f"decided about {self.WORD}", "b-1")
        rec.pop(records.BATCH_KEY, None)
        ledger.append(rec)
        self._turns("s-summary")
        index.rebuild()
        self.assertEqual(self._hits(force_scan=True), 5)
        forget.withhold(session_id="s-summary")
        self.assertEqual(self._hits(force_scan=True), 0)


if __name__ == "__main__":
    unittest.main()
