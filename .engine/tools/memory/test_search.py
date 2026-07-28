"""test_search.py — unit tests for ranked, filtered recall: index.search (memory substrate).

Run via the engine's CI command:
    uv run --directory .engine --frozen -- python -m unittest discover -s tools -p 'test_*.py' -b

Covers the `search` laws (the search.json contract): results come back BEST-FIRST by lexical relevance (bm25 on
both paths) with usage (frecency) breaking near-ties but NEVER overriding a clearly-stronger match ("BM25
leads"); a never-accessed match is deprioritized, never dropped (ranking, not retention); the role/tag filters
narrow; the fast and slow paths return the same SET (the availability law; the slow path ranks the FULL matched
set before slicing, not an early ledger-order truncation); and `search` is side-effect-free (it never reinforces
— that is the MCP server's job — and never writes the ledger). `query` stays UNRANKED.

The two paths now agree on ORDER as well as membership — `test_index.RankingParityTests` is where that is
pinned; the set-level assertions here are the weaker floor, kept because they are what the contract promises.
"""

import inspect
import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from memory import forget, index, ledger, records  # noqa: E402

_ID = records.RECORD_ID_KEY


class SearchTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="engine-memory-search-")
        self.ledger = os.path.join(self.tmp, "ledger.ndjson")
        self.index = os.path.join(self.tmp, "index.sqlite3")
        self.now = int(time.time())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def add(self, text, *, role="observation", tags=(), ts=None):
        rid = records.new_record_id()
        ledger.append({_ID: rid, "ts": self.now if ts is None else ts, "role": role,
                       "tags": list(tags), "text": text}, path=self.ledger)
        return rid

    def rebuild(self):
        return index.rebuild(ledger_file=self.ledger, index_file=self.index)

    def search(self, text, **kw):
        return index.search(text, ledger_file=self.ledger, index_file=self.index, **kw)

    def reinforce(self, rid, times=1):
        for _ in range(times):
            forget.record_access(rid, path=self.ledger)

    def ids(self, result):
        return [r.get(_ID) for r in result.records]


class RankingTests(SearchTestCase):
    def _discriminative_corpus(self):
        # "export" is RARE (two records) so bm25's IDF is high and tf separates the strong from the weak match.
        strong = self.add("the export format and export schedule and export owner were decided", role="decision")
        weak = self.add("a passing note that export came up once")
        for t in ("onboarding copy stays short", "the nightly job rebuilds the cache",
                  "prefer dark mode everywhere", "the meeting moved to friday",
                  "snake_case for config names", "retries capped at three"):
            self.add(t)
        self.rebuild()
        return strong, weak

    def test_bm25_orders_best_match_first(self):
        strong, weak = self._discriminative_corpus()
        order = self.ids(self.search("export"))
        self.assertEqual(order[0], strong)
        self.assertIn(weak, order)

    def test_a_clearly_stronger_match_leads(self):
        strong, weak = self._discriminative_corpus()
        self.rebuild()
        self.assertEqual(self.ids(self.search("export"))[0], strong)

    def test_equal_relevance_orders_newest_first(self):
        # Two identical texts score identically, so the tiebreak alone decides. It used to be how often each
        # had been recalled; with that gone the surviving order was ledger position ASCENDING — oldest first,
        # on a store that is almost entirely conversation, and worst on exactly the broad query where every
        # match ties. Newest-first is the deliberate replacement, and this is what pins it.
        older = self.add("the field almanac lists the frost dates")
        newer = self.add("the field almanac lists the frost dates")
        self.rebuild()
        self.assertEqual(self.ids(self.search("almanac")), [newer, older])

    def test_neither_of_two_equal_matches_is_dropped(self):
        a = self.add("the field almanac lists the frost dates")
        b = self.add("the field almanac lists the frost dates")
        self.rebuild()
        self.assertEqual(set(self.ids(self.search("almanac"))), {a, b})

    def test_limit_caps_the_top_k_by_ranking(self):
        strong, weak = self._discriminative_corpus()
        result = self.search("export", limit=1)
        self.assertEqual(len(result.records), 1)
        self.assertEqual(result.records[0].get(_ID), strong)   # the top-k, not an arbitrary slice

    def test_score_field_is_the_lexical_relevance(self):
        self._discriminative_corpus()
        result = self.search("export")
        for r in result.records:
            self.assertIn(records.SCORE_KEY, r)
            self.assertIsInstance(r[records.SCORE_KEY], float)
            self.assertGreaterEqual(r[records.SCORE_KEY], 0.0)
        # The ordering is by relevance, best first — no rounding, no second term.
        rels = [r[records.SCORE_KEY] for r in result.records]
        self.assertEqual(rels, sorted(rels, reverse=True))

    def test_empty_query_returns_empty_not_degraded(self):
        self.add("anything at all")
        self.rebuild()
        for q in ("", "   ", "!!!"):
            res = self.search(q)
            self.assertEqual(res.records, [])
            self.assertFalse(res.degraded)


class FilterTests(SearchTestCase):
    def test_a_role_is_not_something_you_can_filter_on(self):
        # The role filter is gone, and this is the shape of its absence: a caller that passes one gets a
        # TypeError rather than a silently-ignored argument or an empty answer that reads as "not held".
        # Nothing writes a role any more, so the filter could only ever have matched records an older engine
        # left behind — while looking, to any caller, exactly like the project having no history on a subject.
        self.add("we decided to ship export", role="decision")
        self.rebuild()
        with self.assertRaises(TypeError):
            index.search("export", roles=["decision"])
        self.assertEqual(len(self.search("export").records), 1)     # the record itself is still found

    def test_tag_any_match(self):
        a = self.add("export plans", tags=["eADR-0007", "release"])
        self.add("export musings", tags=["scratch"])
        self.add("export with no tags")
        self.rebuild()
        got = set(self.ids(self.search("export", tags=["eADR-0007"])))
        self.assertEqual(got, {a})
        # any-match: a record sharing ANY requested tag passes
        self.assertIn(a, self.ids(self.search("export", tags=["release", "nope"])))


class ParityAndDegradeTests(SearchTestCase):
    def test_fast_and_slow_agree_on_set(self):
        self.add("export one export two", role="decision")
        self.add("export three")
        self.add("unrelated note")
        self.rebuild()
        fast = self.search("export")
        slow = self.search("export", force_scan=True)
        self.assertFalse(fast.degraded)
        self.assertTrue(slow.degraded)
        self.assertEqual(set(self.ids(fast)), set(self.ids(slow)))   # same SET (order may differ)

    def test_slow_path_ranks_the_full_set_not_a_ledger_truncation(self):
        # The S1 guard: the weak match is FIRST in the ledger, the strong match LAST. With limit=1 the slow path
        # must rank the FULL set and return the STRONG match — a first-k ledger truncation would return the weak.
        weak = self.add("alpha mentioned once")
        for t in ("beta gamma", "delta epsilon", "zeta eta", "theta iota"):
            self.add(t)
        strong = self.add("alpha alpha alpha core")
        self.rebuild()
        result = self.search("alpha", limit=1, force_scan=True)
        self.assertTrue(result.degraded)
        self.assertEqual(self.ids(result), [strong])
        self.assertNotEqual(self.ids(result), [weak])

    def test_degraded_flag(self):
        self.add("export note")
        self.rebuild()
        self.assertFalse(self.search("export").degraded)
        self.assertTrue(self.search("export", force_scan=True).degraded)

    def test_fts5_absent_falls_back_and_still_ranks(self):
        strong = self.add("export export export decided", role="decision")
        self.add("export once")
        for t in ("alpha", "beta", "gamma", "delta"):
            self.add(t)
        self.rebuild()
        original = index.fts5_available
        index.fts5_available = lambda *a, **k: False
        try:
            result = self.search("export")
            self.assertTrue(result.degraded)
            self.assertEqual(self.ids(result)[0], strong)
        finally:
            index.fts5_available = original

    def test_a_corrupt_index_is_repaired_rather_than_left_slow_forever(self):
        # An unusable index used to mean every later query took the full-ledger scan, correctly but far more
        # slowly, with nothing bringing it back. Reading now repairs it, so the cost is paid once.
        a = self.add("export plans here")
        self.add("nothing relevant")
        self.rebuild()
        with open(self.index, "wb") as fh:
            fh.write(b"this is not a sqlite database at all")
        result = self.search("export")
        self.assertIn(a, self.ids(result))
        self.assertFalse(result.degraded, "an unusable index should have been rebuilt, not merely tolerated")

    def test_when_the_repair_cannot_run_the_answer_still_comes_back(self):
        # The availability law is what the repair rests on, not what it replaces: if rebuilding fails for any
        # reason, recall must still answer from the scan rather than raise or return nothing.
        a = self.add("export plans here")
        self.add("nothing relevant")
        self.rebuild()
        with open(self.index, "wb") as fh:
            fh.write(b"this is not a sqlite database at all")

        def refuse(*args, **kwargs):
            raise OSError("disk full")

        original = index.rebuild
        index.rebuild = refuse
        try:
            result = self.search("export")
        finally:
            index.rebuild = original
        self.assertTrue(result.degraded)
        self.assertIn(a, self.ids(result))


class NoSideEffectTests(SearchTestCase):
    def test_search_writes_no_ledger_bytes(self):
        self.add("export decision", role="decision")
        self.rebuild()
        before = os.stat(self.ledger)
        self.search("export")
        after = os.stat(self.ledger)
        self.assertEqual((before.st_size, before.st_mtime_ns), (after.st_size, after.st_mtime_ns))

    def test_search_source_has_no_write_calls(self):
        # A source-scan: the ranked recall path must not reach the reinforcement appender or a ledger write.
        src = "".join(inspect.getsource(fn) for fn in
                       (index.search, index._ranked, index._rank_slice_score, index._fast_candidates))
        self.assertNotIn("record_access", src)
        self.assertNotIn("ledger.append", src)
        # And the stronger property this slice adds: the fast path reads no ledger at all, so what a search
        # costs tracks what it matched rather than what is stored.
        self.assertNotIn("live_records", inspect.getsource(index._fast_candidates))


class QueryUnchangedTests(SearchTestCase):
    def test_query_stays_ledger_order_and_carries_no_score(self):
        # `query` must not gain ranking or the score field — guards against accidental coupling.
        first = self.add("export alpha")
        second = self.add("export export export beta")
        self.rebuild()
        result = index.query("export", ledger_file=self.ledger, index_file=self.index)
        self.assertEqual([r.get(_ID) for r in result.records], [first, second])   # ledger order, not ranked
        for r in result.records:
            self.assertNotIn(records.SCORE_KEY, r)


if __name__ == "__main__":
    unittest.main()
