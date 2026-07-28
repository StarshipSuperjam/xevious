"""test_index.py — unit tests for the derived memory lookup (SQLite + FTS5).

Run via the engine's CI command:
    uv run --directory .engine --frozen -- python -m unittest discover -s tools -p 'test_*.py' -b

These tests cover the derived-index laws: the fast lookup and the slow backup return the SAME set of records (the
unicode61-mirror), the FTS5-absent condition is detected and degrades to the scan, the rebuild is atomic
(a crash leaves the prior index intact), and reads stay line-resilient. FTS5 is present in CI's SQLite, so the
scan path is exercised both by `force_scan=True` and by monkeypatching `fts5_available` to False.
"""

import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import unicodedata
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from memory import forget, index, ledger, records  # noqa: E402


def _bodies(result):
    return sorted(r["body"] for r in result.records)


class IndexTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="engine-memory-test-")
        self.ledger = os.path.join(self.tmp, "ledger.ndjson")
        self.index = os.path.join(self.tmp, "index.sqlite3")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def file(self, *records):
        for record in records:
            ledger.append(record, path=self.ledger)

    def q(self, text, **kw):
        return index.query(text, ledger_file=self.ledger, index_file=self.index, **kw)

    def rebuild(self):
        return index.rebuild(ledger_file=self.ledger, index_file=self.index)


class Fts5DetectionTests(IndexTestCase):
    def test_fts5_available_true_on_this_runtime(self):
        # CI's SQLite has FTS5; the whole fast path depends on it.
        self.assertTrue(index.fts5_available())

    def test_detection_does_not_leak_a_probe_table_on_a_passed_connection(self):
        conn = sqlite3.connect(":memory:")
        try:
            self.assertTrue(index.fts5_available(conn))
            temp_names = {r[0] for r in conn.execute("SELECT name FROM temp.sqlite_master").fetchall()}
            main_names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master").fetchall()}
            self.assertNotIn(index._FTS_PROBE_TABLE, temp_names | main_names)
        finally:
            conn.close()


class RoundTripTests(IndexTestCase):
    def test_rebuild_then_query_finds_records(self):
        self.file({"body": "we shipped the export feature"}, {"body": "we paused the import feature"})
        report = self.rebuild()
        self.assertTrue(report.fts5)
        self.assertEqual(report.indexed, 2)
        self.assertEqual(report.with_text, 2)
        self.assertEqual(_bodies(self.q("export")), ["we shipped the export feature"])

    def test_implicit_and_every_word_must_appear(self):
        self.file({"body": "alpha beta gamma"}, {"body": "alpha delta"})
        self.rebuild()
        self.assertEqual(_bodies(self.q("alpha")), ["alpha beta gamma", "alpha delta"])
        self.assertEqual(_bodies(self.q("alpha beta")), ["alpha beta gamma"])
        self.assertEqual(_bodies(self.q("alpha zeta")), [])

    def test_fast_path_is_not_degraded_forced_scan_is(self):
        self.file({"body": "hello world"})
        self.rebuild()
        self.assertFalse(self.q("hello").degraded)
        self.assertTrue(self.q("hello", force_scan=True).degraded)

    def test_limit_caps_results_on_both_paths(self):
        self.file(*({"body": f"repeated token item number {n}"} for n in range(5)))
        self.rebuild()
        self.assertEqual(len(self.q("repeated", limit=2).records), 2)
        self.assertEqual(len(self.q("repeated", limit=2, force_scan=True).records), 2)


class MirrorParityTests(IndexTestCase):
    """The load-bearing property: the fast lookup and the slow backup return the same records, including
    the inputs a naive backup (a [A-Za-z0-9_] split) would get wrong — underscores and diacritics."""

    CORPUS = [
        {"body": "we chose the snake_case_config naming convention"},
        {"body": "the café meeting approved the naïve cache plan"},
        {"body": "Müller reviewed the e=mc2 derivation"},
        {"body": "the QUICK Fox jumped"},
        {"body": "ёжик решение про кэш"},  # Cyrillic: FTS5 folds "ё" differently from Python — the regressed class
        {"body": "δοκιμή τέλος της συνεδρίασης"},  # Greek with tonos accents — also regressed before the fix
        {"body": "unrelated decoy about timeouts and retries"},
    ]
    # Each query exercises a divergence class FTS5's own folder and a naive split disagree on: underscore-split,
    # diacritic-fold, case-fold, Cyrillic, Greek, plus a plain word and a miss.
    QUERIES = ["config", "snake", "cafe", "naive", "muller", "mc2", "quick", "fox",
               "ёжик", "решение", "δοκιμη", "τελος", "timeouts", "absent"]

    def test_fast_and_scan_agree_across_the_divergence_corpus(self):
        self.file(*self.CORPUS)
        self.rebuild()
        for query_text in self.QUERIES:
            fast = _bodies(self.q(query_text))
            scan = _bodies(self.q(query_text, force_scan=True))
            self.assertEqual(fast, scan, f"fast vs slow disagree on {query_text!r}")

    def test_divergence_class_queries_actually_match(self):
        # Guard against a vacuous parity pass: these are exactly the queries a naive split (or FTS5's own
        # folder, for the Cyrillic/Greek cases) would get wrong.
        self.file(*self.CORPUS)
        self.rebuild()
        self.assertEqual(_bodies(self.q("config")), ["we chose the snake_case_config naming convention"])
        self.assertEqual(_bodies(self.q("cafe")), ["the café meeting approved the naïve cache plan"])
        self.assertEqual(_bodies(self.q("naive")), ["the café meeting approved the naïve cache plan"])
        self.assertEqual(_bodies(self.q("ёжик")), ["ёжик решение про кэш"])  # fast path must find it, not just scan
        self.assertEqual(_bodies(self.q("δοκιμη")), ["δοκιμή τέλος της συνεδρίασης"])

    def test_tokenize_folds_words_the_expected_way(self):
        # _tokenize is the single folding authority for both paths. Pin its rules directly — each line is a
        # mutation tripwire (a naive [A-Za-z0-9_] split, .casefold(), or NFKD would change one of these).
        cases = {
            "snake_case_config": ["snake", "case", "config"],  # underscore is a separator
            "café": ["cafe"],  # NFD + drop combining marks → diacritic fold
            "naïve": ["naive"],
            "Müller": ["muller"],  # case fold
            "straße": ["straße"],  # .lower(), NOT casefold (ß stays — no "ss")
            "ёжик": ["ежик"],  # Cyrillic yo → e (diacritic strip)
            "Ⅳ": ["ⅳ"],  # NFD canonical, NOT NFKD (stays — not "iv")
            "a.b-c2": ["a", "b", "c2"],  # punctuation separates; digits are word chars
        }
        for text, expected in cases.items():
            self.assertEqual(index._tokenize(text), expected, f"_tokenize({text!r})")

    def test_every_indexed_token_is_retrievable_via_the_fast_path(self):
        # Proves FTS5 indexed exactly the tokens _tokenize produced (the derived-index architecture): each token of a
        # record's text, queried through the FAST lookup, returns that record.
        records = [{"body": "the snake_case_config café decision"}, {"body": "ёжик решение"}]
        self.file(*records)
        self.rebuild()
        for record in records:
            for token in set(index._tokenize(record["body"])):
                result = self.q(token)
                self.assertFalse(result.degraded, f"token {token!r} should use the fast path")
                self.assertIn(record["body"], [r["body"] for r in result.records], f"token {token!r}")


class Fts5AbsentDispatchTests(IndexTestCase):
    """Cover the genuine FTS5-absent branch (CI has FTS5, so monkeypatch the detector)."""

    def test_query_falls_back_to_scan_when_fts5_absent(self):
        self.file({"body": "decision about the rollout"})
        self.rebuild()
        original = index.fts5_available
        index.fts5_available = lambda conn=None: False
        try:
            result = self.q("rollout")  # not force_scan — the dispatch must choose scan because FTS5 is "absent"
            self.assertTrue(result.degraded)
            self.assertEqual(_bodies(result), ["decision about the rollout"])
        finally:
            index.fts5_available = original

    def test_rebuild_is_a_noop_when_fts5_absent(self):
        self.file({"body": "nothing to index without the fast feature"})
        original = index.fts5_available
        index.fts5_available = lambda conn=None: False
        try:
            report = self.rebuild()
            self.assertFalse(report.fts5)
            self.assertEqual(report.indexed, 0)
            self.assertFalse(os.path.exists(self.index))  # no index file written
        finally:
            index.fts5_available = original


class AtomicRebuildTests(IndexTestCase):
    def test_failed_rebuild_leaves_prior_index_intact_and_no_temp(self):
        self.file({"body": "the original indexed decision"})
        self.rebuild()
        self.assertEqual(_bodies(self.q("original")), ["the original indexed decision"])
        # A second rebuild from a changed cabinet that fails at the atomic swap must NOT corrupt the prior index.
        self.file({"body": "a brand new decision that should not land"})
        original_replace = index.os.replace
        index.os.replace = lambda *a, **k: (_ for _ in ()).throw(OSError("simulated crash at swap"))
        try:
            with self.assertRaises(OSError):
                self.rebuild()
        finally:
            index.os.replace = original_replace
        # The prior index still answers the old way; the new record never landed in it.
        self.assertEqual(_bodies(self.q("original")), ["the original indexed decision"])
        self.assertEqual(self.q("brand").records, [])
        # No half-built temp left behind in the data dir.
        leftovers = [n for n in os.listdir(self.tmp) if n.startswith(".index-build-")]
        self.assertEqual(leftovers, [])

    def test_rebuild_overwrites_a_stale_index(self):
        self.file({"body": "first"})
        self.rebuild()
        self.file({"body": "second"})
        self.rebuild()
        self.assertEqual(_bodies(self.q("second")), ["second"])
        self.assertEqual(_bodies(self.q("first")), ["first"])


class ThrowawayTests(IndexTestCase):
    def test_missing_index_degrades_to_scan(self):
        self.file({"body": "recoverable memory"})
        # never built — no index file
        self.assertFalse(os.path.exists(self.index))
        result = self.q("recoverable")
        self.assertTrue(result.degraded)
        self.assertEqual(_bodies(result), ["recoverable memory"])

    def test_delete_and_rebuild_is_identical(self):
        self.file({"body": "DO NOT LOSE THIS"})
        self.rebuild()
        before = _bodies(self.q("lose"))
        os.remove(self.index)
        self.rebuild()
        self.assertEqual(_bodies(self.q("lose")), before)

    def test_corrupt_or_empty_index_degrades_to_scan(self):
        # A present-but-unreadable fast lookup (0-byte, or non-database bytes from a truncated copy / disk
        # error) must fall back to the slow backup, not crash — the availability law.
        self.file({"body": "recoverable decision"})

        def zero_byte(p):
            open(p, "wb").close()

        def garbage(p):
            with open(p, "wb") as fh:
                fh.write(b"this is not a database")

        for make_broken in (zero_byte, garbage):
            make_broken(self.index)
            result = self.q("recoverable")
            self.assertTrue(result.degraded)
            self.assertEqual(_bodies(result), ["recoverable decision"])


class ResilienceTests(IndexTestCase):
    def test_empty_ledger_rebuilds_to_empty_index(self):
        report = self.rebuild()  # no ledger file at all
        self.assertEqual(report.indexed, 0)
        self.assertTrue(os.path.exists(self.index))
        self.assertEqual(self.q("anything").records, [])

    def test_malformed_interior_line_does_not_cost_the_rest(self):
        self.file({"body": "memory before the corruption"})
        with open(self.ledger, "a", encoding="utf-8") as fh:
            fh.write("@@@ not json @@@\n")
        self.file({"body": "memory after the corruption"})
        self.rebuild()
        self.assertEqual(
            _bodies(self.q("memory")),
            ["memory after the corruption", "memory before the corruption"],
        )

    def test_torn_trailing_line_is_dropped(self):
        self.file({"body": "intact memory"})
        with open(self.ledger, "a", encoding="utf-8") as fh:
            fh.write('{"body":"half written when the power went ou')  # no newline
        self.rebuild()
        self.assertEqual(_bodies(self.q("intact")), ["intact memory"])
        self.assertEqual(self.q("half").records, [])


class ProjectionTests(IndexTestCase):
    def test_tags_field_is_excluded_from_the_searchable_text(self):
        # The locked law: tags are NOT indexed into the full-text body.
        self.file({"body": "the visible narrative", "tags": ["secretxyztag", "eADR-0007"]})
        self.rebuild()
        self.assertEqual(_bodies(self.q("visible")), ["the visible narrative"])
        self.assertEqual(self.q("secretxyztag").records, [])  # fast path
        self.assertEqual(self.q("secretxyztag", force_scan=True).records, [])  # slow path agrees
        self.assertNotIn("secretxyztag", index._record_text({"body": "x", "tags": ["secretxyztag"]}))

    def test_string_free_record_is_indexed_but_unsearchable(self):
        self.file({"count": 7, "ok": True}, {"body": "has words"})
        report = self.rebuild()
        self.assertEqual(report.indexed, 2)
        self.assertEqual(report.with_text, 1)  # only the record with string content is searchable
        self.assertEqual(_bodies(self.q("words")), ["has words"])

    def test_indexed_body_equals_the_shared_tokenization(self):
        # The fast path indexes exactly the tokens of _record_text(record) — the same tokens the scan path
        # matches against — so the two paths cannot silently desync on the projection or the tokenizer.
        # Asserted through BEHAVIOUR rather than by reading the stored body back: the FTS table is contentless
        # (`content=''`), so it keeps the inverted index and no second copy of the text. Behaviour is the better
        # assertion anyway — it holds whatever the storage shape is, and it is what the parity law actually
        # claims. Each record's own projected tokens must MATCH it on the fast path, and only it.
        #
        # The fixture carries the non-ASCII divergence classes on purpose. A desync between the projection and
        # the tokenizer shows up first where FTS5's own folder and Python's disagree — diacritics, Cyrillic,
        # Greek tonos — so an ASCII-only fixture would pass while exactly the interesting case was broken.
        records = [{"body": "first narrative", "title": "a title"}, {"note": "nested", "extra": ["deep", "words"]},
                   {"body": "the café meeting approved the naïve plan"},
                   {"body": "ёжик решение про кэш", "note": "δοκιμή τέλος"},
                   {"body": "snake_case_config and Müller's e=mc2"}]
        self.file(*records)
        self.rebuild()
        for record in records:
            for token in index._tokenize(index._record_text(record)):
                fast = index.query(token, index_file=self.index, ledger_file=self.ledger).records
                scan = index.query(token, force_scan=True, index_file=self.index, ledger_file=self.ledger).records
                self.assertIn(record, fast, "a projected token must retrieve its own record on the fast path")
                self.assertEqual([json.dumps(r, sort_keys=True) for r in fast],
                                 [json.dumps(r, sort_keys=True) for r in scan],
                                 "the fast and slow paths must return the same set for token %r" % token)

    def test_non_dict_records_index_and_agree_across_paths(self):
        # The ledger is record-agnostic: a top-level string or list record must index and match on both paths.
        self.file("a bare string about caches", ["a", "list", "about", "caches"], {"body": "a dict about caches"})
        self.rebuild()
        fast = index.query("caches", ledger_file=self.ledger, index_file=self.index).records
        scan = index.query("caches", force_scan=True, ledger_file=self.ledger, index_file=self.index).records
        self.assertEqual(fast, scan)
        self.assertEqual(len(fast), 3)

    def test_limit_returns_the_same_records_not_just_the_same_count(self):
        # More matches than the limit: the fast path (ORDER BY ord LIMIT) and the scan (iter order, break at
        # limit) must pick the SAME records, in the same order — not merely the same count.
        self.file(*({"body": f"shared token entry {n}"} for n in range(6)))
        self.rebuild()
        fast = index.query("shared", limit=3, ledger_file=self.ledger, index_file=self.index).records
        scan = index.query("shared", limit=3, force_scan=True, ledger_file=self.ledger, index_file=self.index).records
        self.assertEqual(fast, scan)
        self.assertEqual(len(fast), 3)


class RankingParityTests(IndexTestCase):
    """The degraded path scores with the SAME bm25 the fast path reads out of FTS5, computed in plain Python.
    So the availability law now covers the ANSWER, not merely the fact that one comes back: a machine whose
    SQLite lacks FTS5 gets the same records in the same order, just slower.

    It used to score `log1p(term occurrences)` — no length normalisation and no inverse document frequency. That
    was close enough while the store held only short curated summaries, which are all about the same size. It
    stopped being close enough when the conversation became recall content: a 4 KB transcript fragment and a
    one-line summary that mention a word the same number of times scored IDENTICALLY, leaving ledger position to
    decide between them, and a bounded query (the recall workflow caps every expansion) then returned a
    materially different set on the two paths."""

    def _corpus(self):
        # Lengths and repetition both vary, and the terms differ in how many records carry them, so length
        # normalisation and inverse document frequency each have something to bite on. A ranking that ignored
        # either would order this differently.
        out = [{"body": "cache " * 12 + "a long fragment that says cache many times and little else " * 6},
               {"body": "the cache decision: write through, not write back"},
               {"body": "cache " + " ".join(f"filler{i}" for i in range(300))},
               {"body": "a short note about the cache"},
               {"body": "retries and timeouts, plus one mention of cache"}]
        out += [{"body": f"unrelated note {i} about retries"} for i in range(60)]
        return out

    def _search(self, text, **kw):
        return index.search(text, ledger_file=self.ledger, index_file=self.index, **kw)

    def _ranked_bodies(self, text, **kw):
        return [r["body"] for r in self._search(text, **kw).records]

    def test_the_two_paths_rank_identically(self):
        self.file(*self._corpus())
        self.rebuild()
        for query_text in ("cache", "retries", "cache retries"):
            fast = self._ranked_bodies(query_text)
            scan = self._ranked_bodies(query_text, force_scan=True)
            self.assertTrue(fast, query_text)
            self.assertEqual(fast, scan, f"the two paths ordered {query_text!r} differently")

    def test_a_repeated_query_word_counts_once_on_both_paths(self):
        # Every other test here uses distinct query words, which is exactly how this got past the first round.
        # The fast path hands its terms to fts5 as a MATCH expression and fts5 sums a repeated term's
        # contribution once per occurrence, so "cache cache" scored a record at double the plain scan's figure —
        # the same record, two different relevance numbers, and a wide enough gap moved records across
        # relevance buckets and reordered the answer.
        self.file(*self._corpus())
        self.rebuild()
        single = self._search("cache", limit=3)
        for repeated in ("cache cache", "cache cache cache"):
            for kw in ({}, {"force_scan": True}):
                got = self._search(repeated, limit=3, **kw)
                self.assertEqual([r["body"] for r in got.records], [r["body"] for r in single.records],
                                 f"{repeated!r} answered differently from {'cache'!r} ({kw})")
                self.assertAlmostEqual(got.records[0][records.SCORE_KEY],
                                       single.records[0][records.SCORE_KEY], places=9,
                                       msg=f"a repeated word changed the relevance ({kw})")

    def test_a_bounded_query_returns_the_same_records_on_both_paths(self):
        # The case that actually reaches the model: the recall workflow caps every expansion, so a divergence in
        # ORDER is a divergence in the SET of records the session ever sees.
        self.file(*self._corpus())
        self.rebuild()
        self.assertEqual(self._ranked_bodies("cache", limit=3),
                         self._ranked_bodies("cache", limit=3, force_scan=True))

    def test_a_padded_fragment_loses_to_a_tight_note_that_says_it_as_often(self):
        # The product consequence, and the exact thing the old scan-path relevance could not see: these two
        # mention the term the same number of times, so `log1p(occurrences)` scored them EQUAL and ledger
        # position broke the tie. Length normalisation puts the tight note first — on both paths.
        tight = {"body": "cache cache cache — write through, not write back"}
        padded = {"body": "cache cache cache " + " ".join(f"filler{i}" for i in range(400))}
        self.file(padded, tight)                              # padded first, so ledger order favours the wrong one
        self.file(*({"body": f"unrelated note {i}"} for i in range(30)))
        self.rebuild()
        for kw in ({}, {"force_scan": True}):
            self.assertEqual(self._ranked_bodies("cache", **kw)[0], tight["body"],
                             f"a padded fragment outranked an equally-specific tight note ({kw})")

    def test_an_unlimited_query_over_many_matches_stays_on_the_fast_path(self):
        # The re-read of the surviving records is CHUNKED because an unlimited query keeps every match, and one
        # SQL placeholder per match runs into SQLite's per-statement parameter cap. Over the cap the driver
        # raises an error that `_ranked`'s broken-index guard swallows — so a perfectly healthy index would have
        # dropped through to the full plain-Python scan, silently and many times slower.
        cap = sqlite3.connect(":memory:").getlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER)
        self.file(*({records.RECORD_ID_KEY: f"r{i}", "body": "quokka note"} for i in range(cap + 200)))
        self.rebuild()
        got = self._search("quokka")                          # no limit: every match survives to the re-read
        self.assertEqual(len(got.records), cap + 200)
        self.assertFalse(got.degraded, "a healthy index fell through to the scan — the re-read hit the cap")

    def test_the_scores_match_fts5s_own_bm25(self):
        # Not "close enough" — the scan reproduces fts5's bm25 exactly, epsilon-floored idf included, which is
        # what makes the order identical rather than merely similar.
        self.file(*self._corpus())
        self.rebuild()
        fast = index.search("cache", ledger_file=self.ledger, index_file=self.index).records
        scan = index.search("cache", ledger_file=self.ledger, index_file=self.index, force_scan=True).records
        self.assertEqual(len(fast), len(scan))            # zip() would otherwise pass on a matching prefix
        self.assertTrue(fast)
        for a, b in zip(fast, scan):
            self.assertAlmostEqual(a[records.SCORE_KEY], b[records.SCORE_KEY], places=9)

    def test_a_word_in_every_record_scores_the_floor_not_a_penalty(self):
        # fts5 floors the inverse document frequency at a positive sliver rather than letting it go negative, so
        # a term more than half the corpus carries never ranks a record DOWN for containing it. Reproducing that
        # floor is load-bearing: without it the two paths would disagree on exactly the commonest queries.
        self.file(*({"body": f"ubiquitous note {i}"} for i in range(20)))
        self.rebuild()
        for kw in ({}, {"force_scan": True}):
            scores = [r[records.SCORE_KEY] for r in
                      index.search("ubiquitous", ledger_file=self.ledger, index_file=self.index, **kw).records]
            self.assertEqual(len(scores), 20)
            self.assertTrue(all(s > 0 for s in scores), f"a floored idf went non-positive ({kw})")


class BoundedFastPathTests(IndexTestCase):
    """The fast path walks its matches in bm25 order and stops once no unread row could reach the top `limit`.
    The bound is on WORK, never on the answer. It matters because the index MATCHES a common word by the tens of
    thousands once the conversation is recall content, and hydrating every match to return ten records cost a
    measured 134 MB resident spike inside the long-lived recall server."""

    def s(self, text, **kw):
        # `search`, not `query` — the RANKED entry point is the one that hydrates to rank.
        return index.search(text, ledger_file=self.ledger, index_file=self.index, **kw)

    _MATCHES = 200

    def _varied(self):
        # Relevance genuinely varies: the token repeats a different number of times against a different amount of
        # filler, AND it is SELECTIVE — present in a tenth of the store, so bm25's inverse-document-frequency
        # term is well away from zero. Both are needed. A token present in half the records or more scores an
        # identical ~0 on every one of them (fts5's idf is log((N-n+0.5)/(n+0.5)), which is 0 at n = N/2), and
        # then every match ties in one bucket — the separate case the flat-bucket test below covers.
        matching = [{"body": ("quokka " * (1 + i % 5)) + " ".join(f"filler{i}x{j}" for j in range(i % 11))}
                    for i in range(self._MATCHES)]
        return matching + [{"body": f"unrelated entry {i}"} for i in range(9 * self._MATCHES)]

    def test_a_bounded_query_returns_exactly_what_full_hydration_would(self):
        self.file(*self._varied())
        self.rebuild()
        bounded = self.s("quokka", limit=5).records
        everything = self.s("quokka").records        # unlimited: the whole matched set ranked, then sliced
        self.assertEqual(len(bounded), 5)
        self.assertEqual(bounded, everything[:5])

    def test_a_selective_word_stops_reading_long_before_the_end_of_its_matches(self):
        self.file(*self._varied())
        self.rebuild()
        read = []
        real = index._passes_filters                 # called exactly once per record actually parsed
        index._passes_filters = (lambda r, tags, session=None:
                                 (read.append(r), real(r, tags, session))[1])
        try:
            self.assertEqual(len(self.s("quokka", limit=5).records), 5)
        finally:
            index._passes_filters = real
        self.assertLess(len(read), self._MATCHES // 3,
                        f"parsed {len(read)} of {self._MATCHES} matches to answer a limit of 5 — no early stop")

    def test_a_common_word_scores_every_match_but_keeps_none_of_them(self):
        # A word in EVERY record collapses bm25's IDF to zero, so every match ties and the boundary rule can
        # skip nothing — the whole run of equals has to be read for the newest-first tiebreak to be honoured.
        # That is the query shape that produced the 134 MB spike, and the bound that has to hold for it is
        # RETENTION: parse each record and let it go, keeping only the sort key, then re-read the few that won.
        self.file(*({"body": f"quokka entry {n}"} for n in range(300)))
        self.rebuild()
        seen = {}
        real = index._hydrate_winners

        def spy(conn, keys, limit):
            seen["keys"] = list(keys)
            return real(conn, keys, limit)

        index._hydrate_winners = spy
        try:
            self.assertEqual(len(self.s("quokka", limit=5).records), 5)
        finally:
            index._hydrate_winners = real
        self.assertEqual(len(seen["keys"]), 300, "a flat run of equals has to be read whole")
        self.assertTrue(all(len(k) == 2 and not any(isinstance(f, (dict, str)) for f in k)
                            for k in seen["keys"]),
                        "the walk held on to record bodies — the retention bound is what this shape is for")

    def test_a_filter_that_rejects_most_matches_still_returns_a_full_page(self):
        # The boundary is set from the limit-th record that PASSED the filter, so a query whose filter rejects
        # nearly everything keeps reading rather than quietly returning a short page.
        self.file(*({"body": f"quokka entry {n}", "session_id": "s-keep" if n % 50 == 0 else "s-drop"}
                    for n in range(300)))
        self.rebuild()
        self.assertEqual(len(self.s("quokka", session="s-keep", limit=5).records), 5)

    def test_the_newest_of_equally_relevant_matches_wins_from_deep_in_the_run(self):
        # Equal-relevance matches all tie, and the tiebreak — newest first — can only be honoured if the walk
        # reads the WHOLE run of equals rather than stopping at the limit-th. The winner here is the last
        # record filed, so a walk that stopped early would return the first in ledger order and never see it.
        # This is the property the boundary rule exists to keep, and it did not go away with the usage term.
        self.file(*({records.RECORD_ID_KEY: f"r{n}", "body": "quokka sighting"} for n in range(120)))
        self.rebuild()
        top = self.s("quokka", limit=1).records
        self.assertEqual([r[records.RECORD_ID_KEY] for r in top], ["r119"])


class SafetyTests(IndexTestCase):
    def test_empty_and_punctuation_only_queries_return_nothing(self):
        self.file({"body": "some memory"})
        self.rebuild()
        for text in ["", "   ", "!!!", "...", "@#$%"]:
            result = self.q(text)
            self.assertEqual(result.records, [])
            self.assertFalse(result.degraded)

    def test_fts5_operators_in_a_query_are_neutralized(self):
        # Raw FTS5 syntax in user input must never reach the MATCH parser as syntax.
        self.file({"body": "alpha bravo charlie"})
        self.rebuild()
        for hostile in ['alpha" OR "bravo', "alpha NEAR bravo", "alpha*", "alpha AND bravo", 'alpha" --'] :
            fast = _bodies(self.q(hostile))
            scan = _bodies(self.q(hostile, force_scan=True))
            self.assertEqual(fast, scan, f"fast vs slow disagree on hostile input {hostile!r}")

    def test_module_import_is_side_effect_free_for_close_seam(self):
        # close.py does `import memory`; that must not touch the filesystem or build anything (capture
        # is now exposed, but binding it does no filesystem work — all reads/writes are inside calls).
        self.assertTrue(hasattr(index, "query"))
        import memory
        self.assertTrue(hasattr(memory, "capture_turn_delta"))  # the capture path lit this up


class IndexFreshnessAndExtendTests(IndexTestCase):
    """The index's own shape version, and the narrow contract `extend` keeps."""

    def _turn(self, rid, text, *, seq=0, injected=False):
        rec = {records.RECORD_ID_KEY: rid, "kind": records.AMBIENT_CAPTURE_KIND, "session_id": "s1",
               "seq": seq, "speaker": "user", "tags": ["transcript", "stop"], "ts": int(time.time()),
               "text": text}
        if injected:
            rec["tags"].append(records.INJECTED_TAG)
        return rec

    def test_an_index_built_under_the_old_rules_is_treated_as_stale(self):
        # The failure this exists to stop: generation moves only on compaction, so a change to what the index
        # is allowed to CONTAIN leaves an existing index stamped current while holding the wrong set. Before
        # the version leg, an operator's pre-upgrade index answered on the fast path with degraded=False and
        # none of their conversation in it — silently, until some unrelated event forced a rebuild.
        if not index.fts5_available():
            self.skipTest("no FTS5 on this machine — there is no fast path to go stale")
        self.file(self._turn("t1", "a quokka turn"))
        self.rebuild()
        self.assertTrue(index.query("quokka", ledger_file=self.ledger, index_file=self.index).records)
        conn = sqlite3.connect(self.index)                       # forge an older shape, generation untouched
        try:
            conn.execute("UPDATE meta SET schema_version = ? WHERE rowid = 1", (index.INDEX_SCHEMA_VERSION - 1,))
            conn.commit()
        finally:
            conn.close()
        stale = index.query("quokka", ledger_file=self.ledger, index_file=self.index)
        self.assertTrue(stale.records, "recall must still ANSWER — availability holds, latency does not")
        self.assertTrue(stale.degraded, "an old-shape index must degrade to the scan, never answer confidently")

    # One record of every kind and every exclusion case, with the set recall should surface pinned beside it.
    # The point is the COUPLING, not the coverage: the version leg above only protects an operator's existing
    # index if somebody remembers to bump the version when membership moves, and twice now in this subsystem
    # somebody did not. (The first time was caught in review; the second shipped as far as a cold audit — a
    # change removed the archived-tier age-out, which is squarely a membership change, and left the version
    # alone, so an index built by the previous release answered `degraded=False` with the wrong set.) This
    # fixture turns "remember to bump it" into a failing test, and it is anchored on BEHAVIOUR rather than on
    # the source of the predicates, so editing a comment cannot trip it.
    _MEMBERSHIP_FIXTURE = [
        {records.RECORD_ID_KEY: "turn", "kind": records.AMBIENT_CAPTURE_KIND, "session_id": "S", "seq": 0,
         "speaker": "user", "text": "a genuine turn", "tags": ["transcript"]},
        {records.RECORD_ID_KEY: "injected", "kind": records.AMBIENT_CAPTURE_KIND, "session_id": "S", "seq": 1,
         "speaker": "user", "text": "<task-notification>x</task-notification>",
         "tags": ["transcript", records.INJECTED_TAG]},
        {records.RECORD_ID_KEY: "ancient", "kind": records.EPISODIC_KIND, "session_id": "S", "role": "dead-end",
         "ts": 1, "text": "an episodic older than any threshold the retired age-out used", "tags": ["episodic"]},
        {records.RECORD_ID_KEY: "batchless", "kind": records.EPISODIC_KIND, "session_id": "S",
         "role": "decision", "text": "a batchless episodic", "tags": ["episodic"]},
        {records.RECORD_ID_KEY: "orphan", "kind": records.EPISODIC_KIND, "session_id": "S", "role": "decision",
         "text": "a crashed pass's orphan", "tags": ["episodic"], records.BATCH_KEY: "never-closed"},
        {records.RECORD_ID_KEY: "closed", "kind": records.EPISODIC_KIND, "session_id": "S", "role": "decision",
         "text": "a completed pass's episodic", "tags": ["episodic"], records.BATCH_KEY: "b-done"},
        {records.RECORD_ID_KEY: "marker", "kind": records.MARKER_KIND, "session_id": "S",
         records.BATCH_KEY: "b-done"},
        {records.RECORD_ID_KEY: "folded", "kind": records.EPISODIC_KIND, "session_id": "S", "role": "decision",
         "text": "a raw a summary was written over", "tags": ["episodic"],
         records.SUPERSEDED_BY_KEY: "thegist"},
        {records.RECORD_ID_KEY: "thegist", "kind": records.GIST_KIND, "session_id": "tag:x", "role": "lesson",
         "text": "the summary standing in for it", "tags": ["gist"]},
        {records.RECORD_ID_KEY: "reinforce", "kind": records.REINFORCEMENT_KIND, records.TARGET_KEY: "closed"},
        {records.RECORD_ID_KEY: "supersede", "kind": records.SUPERSEDED_KIND, records.TARGET_KEY: "folded",
         records.SUPERSEDED_BY_KEY: "thegist", records.BATCH_KEY: "r-open"},
        {records.RECORD_ID_KEY: "rolledup", "kind": records.ROLLUP_KIND, records.BATCH_KEY: "r-other"},
        {records.RECORD_ID_KEY: "erasure", "kind": records.ERASURE_KIND, records.TARGET_KEY: "gone"},
    ]
    _MEMBERSHIP_EXPECTED = {"turn", "ancient", "batchless", "closed", "marker", "thegist"}

    def test_a_change_to_membership_must_bump_the_index_schema_version(self):
        self.file(*self._MEMBERSHIP_FIXTURE)
        surfaced = {r.get(records.RECORD_ID_KEY) for r in forget.live_records(self.ledger)}
        self.assertEqual(
            surfaced, self._MEMBERSHIP_EXPECTED,
            "what recall surfaces has changed. That is a membership change, so an index an operator's previous "
            "release already built now holds the wrong set while still stamping as current — bump "
            "index.INDEX_SCHEMA_VERSION (and add its line to the history above it) in the SAME change, then "
            "update this fixture.")

    def test_extend_admits_a_genuine_turn_and_refuses_everything_else(self):
        # extend is the only thing keeping the fast path current between rebuilds, and it is public. Its
        # contract is narrow ON PURPOSE: a full rebuild applies five exclusions and extend can only apply one,
        # so anything but a freshly captured turn is refused rather than let into the fast path alone.
        if not index.fts5_available():
            self.skipTest("no FTS5 on this machine — there is no fast index to extend")
        self.file(self._turn("t0", "an earlier turn"))
        self.rebuild()
        orphan = {records.RECORD_ID_KEY: "e1", "kind": records.EPISODIC_KIND, records.BATCH_KEY: "never-closed",
                  "role": "decision", "ts": int(time.time()), "text": "zebrafish decision"}
        added = index.extend(
            [self._turn("t1", "a genuine wombat turn", seq=1),
             self._turn("t2", "<task-notification> wombat done </task-notification>", seq=2, injected=True),
             orphan],
            ledger_file=self.ledger, index_file=self.index)
        self.assertEqual(added, 1, "only the genuine turn belongs in the index")
        self.assertTrue(index.query("wombat", ledger_file=self.ledger, index_file=self.index).records)
        for text in ("task-notification", "zebrafish"):
            self.assertEqual(index.query(text, ledger_file=self.ledger, index_file=self.index).records, [],
                             "extend must not admit what a rebuild would drop: %r" % text)



class ExtendFaultHealsTests(IndexTestCase):
    """A failed incremental update must leave the index STALE, not silently short a turn.

    `extend` is best-effort because it runs at end-of-turn capture, which must never be gated on it. That was
    survivable while consolidation, roll-up and compaction all rebuilt routinely — they are gone, so a fault
    here would otherwise leave the turn in the ledger, absent from an index still stamped current, answered
    authoritatively without it.
    """

    def test_a_faulting_extend_marks_the_index_stale_so_the_next_search_repairs_it(self):
        import sqlite3
        from unittest import mock
        self.file({"body": "an existing memory"})
        self.rebuild()
        epoch_before = ledger.index_epoch(for_path=self.ledger)
        turn = {"v": 1, "kind": records.AMBIENT_CAPTURE_KIND, records.RECORD_ID_KEY: records.new_record_id(),
                "session_id": "s-1", "seq": 0, "speaker": "user", "ts": 1785000000,
                "text": "the quokka turn that must not vanish"}
        ledger.append(turn, path=self.ledger)
        # The fault is injected INSIDE the guarded block, where a real one lands (a locked database, a
        # truncated file). Breaking the FTS5 probe instead would exercise the no-fast-search path, which is a
        # different case and correctly does not bump.
        with mock.patch.object(index, "_stamped_withholds", side_effect=sqlite3.Error("disk hiccup")):
            self.assertEqual(index.extend([turn], ledger_file=self.ledger, index_file=self.index), 0)
        self.assertGreater(ledger.index_epoch(for_path=self.ledger), epoch_before,
                           "a faulting extend left the index stamped current — the turn is now invisible")
        # And the repair is real: the next search heals and finds it.
        found = index.search("quokka", ledger_file=self.ledger, index_file=self.index)
        self.assertEqual([r.get("text") for r in found.records], [turn["text"]])
        self.assertFalse(found.degraded)

    def test_an_already_stale_index_is_not_bumped_again(self):
        # Declining because the index is ALREADY stale is not a fault — every reader knows, and a bump there
        # would just churn a rebuild the next search was going to do anyway.
        self.file({"body": "an existing memory"})
        self.rebuild()
        ledger.bump_index_epoch(for_path=self.ledger)          # something else already invalidated it
        epoch = ledger.index_epoch(for_path=self.ledger)
        turn = {"v": 1, "kind": records.AMBIENT_CAPTURE_KIND, records.RECORD_ID_KEY: records.new_record_id(),
                "session_id": "s-1", "seq": 0, "speaker": "user", "ts": 1785000000, "text": "a turn"}
        self.assertEqual(index.extend([turn], ledger_file=self.ledger, index_file=self.index), 0)
        self.assertEqual(ledger.index_epoch(for_path=self.ledger), epoch)


class LedgerFreeFastPathTests(IndexTestCase):
    """The proof the plan called load-bearing: answer a search with the ledger gone.

    This is the whole read-path bound stated as something that can fail. Recall used to make a full pass over
    the ledger on every query just to collect the usage tiebreak — measured as the ENTIRE cost of a search,
    80.8 ms of 80.8 ms on a 30 MB store. Nothing weaker proves the pass is gone: a timing is a measurement, a
    source scan is a proxy, and only removing the file distinguishes "reads it quickly" from "does not read
    it". With the file unreadable, a fast path that still touched it returns nothing or raises.
    """

    def test_a_search_answers_with_the_ledger_removed(self):
        self.file(*({"body": f"the quokka sighting number {n}"} for n in range(20)))
        self.rebuild()
        os.replace(self.ledger, self.ledger + ".gone")          # the one source of truth, taken away
        try:
            result = index.search("quokka", limit=5, ledger_file=self.ledger, index_file=self.index)
        finally:
            os.replace(self.ledger + ".gone", self.ledger)
        self.assertEqual(len(result.records), 5)
        self.assertFalse(result.degraded, "it fell back to the scan, which means it wanted the ledger")

    def test_the_slow_path_does_need_the_ledger_which_is_what_makes_the_above_meaningful(self):
        # The control. If the scan also answered without the file, the test above would prove nothing about
        # where the records came from.
        self.file(*({"body": f"the quokka sighting number {n}"} for n in range(20)))
        self.rebuild()
        os.replace(self.ledger, self.ledger + ".gone")
        try:
            scanned = index.search("quokka", limit=5, force_scan=True,
                                   ledger_file=self.ledger, index_file=self.index)
        finally:
            os.replace(self.ledger + ".gone", self.ledger)
        self.assertEqual(scanned.records, [])


if __name__ == "__main__":
    unittest.main()
