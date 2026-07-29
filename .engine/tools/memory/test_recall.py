"""test_recall.py — unit tests for the transcript-window reader.

Run: uv run --directory .engine --frozen -- python tools/selftest.py

Four properties carry the weight, because a window is presented to a model (and an operator) as "what was
actually said": (1) CONVERSATION FIDELITY — turns come back in the order they happened, a >4KB message split
across records is rejoined whole, and one session's words never leak into another's; (2) THE GENUINE-TURN
FILTER — a harness-injected pseudo-turn is never shown as the operator's own words; (3) READ-ONLY — a window
appends nothing, so reading memory cannot change it; (4) THE LEAK GUARD — a throwaway path that resolves to
the live store is refused loudly, because this reader's output is verbatim conversation and a demo's stdout
can be a public log. Legacy-record tolerance is pinned too: real ledgers hold turn-deltas missing envelope
fields, and a window must skip them rather than crash.
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # .engine/tools on path
import quiet_call  # noqa: E402  (capture a demo walkthrough's stdout so it can't bury the suite summary)
from memory import ledger, recall, records  # noqa: E402


def _rec(session_id, seq, speaker, text, *, injected=False, kind=None, **extra):
    tags = ["transcript", "stop"] + ([records.INJECTED_TAG] if injected else [])
    out = {"v": 1, "kind": kind or records.AMBIENT_CAPTURE_KIND,
           records.RECORD_ID_KEY: records.new_record_id(), "session_id": session_id,
           "ts": 1, "seq": seq, "speaker": speaker, "text": text, "tags": tags}
    out.update(extra)
    return out


class _CabinetBase(unittest.TestCase):
    """Every test writes to a THROWAWAY cabinet — never the real ledger."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="engine-recall-test-")
        self.cabinet = os.path.join(self._tmp.name, "ledger.ndjson")
        self.addCleanup(self._tmp.cleanup)

    def _write(self, *recs):
        for r in recs:
            ledger.append(r, path=self.cabinet)


class WindowFidelityTests(_CabinetBase):
    def test_turns_come_back_in_conversation_order(self):
        # Written out of order on purpose: `seq` is the authority, not append position.
        self._write(_rec("s1", 2, "user", "third"),
                    _rec("s1", 0, "user", "first"),
                    _rec("s1", 1, "assistant", "second"))
        texts = [t["text"] for t in recall.window("s1", path=self.cabinet)["turns"]]
        self.assertEqual(texts, ["first", "second", "third"])

    def test_a_split_message_is_rejoined_whole(self):
        # Capture splits a >4KB message into records sharing ONE seq; they must come back as one turn.
        self._write(_rec("s1", 0, "user", "part-one "),
                    _rec("s1", 0, "user", "part-two "),
                    _rec("s1", 0, "user", "part-three"))
        turns = recall.window("s1", path=self.cabinet)["turns"]
        self.assertEqual(len(turns), 1, "the chunks of one message must present as ONE turn")
        self.assertEqual(turns[0]["text"], "part-one part-two part-three")
        self.assertEqual(turns[0]["chunks"], 3)

    def test_same_seq_different_speaker_is_not_merged(self):
        # A defensive boundary: only chunks of the SAME message (same seq AND speaker) concatenate.
        self._write(_rec("s1", 0, "user", "asked"), _rec("s1", 0, "assistant", "answered"))
        turns = recall.window("s1", path=self.cabinet)["turns"]
        self.assertEqual([t["speaker"] for t in turns], ["user", "assistant"])

    def test_another_session_never_leaks_in(self):
        self._write(_rec("s1", 0, "user", "mine"), _rec("s2", 0, "user", "theirs"))
        texts = [t["text"] for t in recall.window("s1", path=self.cabinet)["turns"]]
        self.assertEqual(texts, ["mine"])

    def test_unknown_session_explains_why_it_is_empty(self):
        # An empty window must never read as bare silence: a caller has to be able to tell "wrong id" from
        # "this session holds nothing readable", instead of concluding memory does not hold the answer.
        self._write(_rec("s1", 0, "user", "mine"))
        result = recall.window("nope", path=self.cabinet)
        self.assertEqual(result["turns"], [])
        self.assertIn("No stored conversation", result["note"])
        self.assertNotIn("Reconstructed", result["note"], "no completeness caveat when nothing was returned")

    def test_blank_session_id_returns_nothing(self):
        self._write(_rec("s1", 0, "user", "mine"))
        self.assertEqual(recall.session_turns("", path=self.cabinet), [])
        self.assertEqual(recall.session_turns(None, path=self.cabinet), [])


class GenuineTurnFilterTests(_CabinetBase):
    def test_injected_pseudo_turn_is_never_shown_as_the_operators_words(self):
        # The load-bearing filter: a /compact continuation summary or task-notification is machine
        # scaffolding. Showing it as conversation would misattribute it to the operator.
        self._write(_rec("s1", 0, "user", "real words"),
                    _rec("s1", 1, "user", "scaffolding", injected=True))
        texts = [t["text"] for t in recall.window("s1", path=self.cabinet)["turns"]]
        self.assertEqual(texts, ["real words"])

    def test_non_turn_delta_records_are_ignored(self):
        # The ledger is shared: episodics, gists and markers live beside raw turns and are not conversation.
        self._write(_rec("s1", 0, "user", "real words"),
                    _rec("s1", 1, "user", "a summary", kind="episodic"))
        texts = [t["text"] for t in recall.window("s1", path=self.cabinet)["turns"]]
        self.assertEqual(texts, ["real words"])

    def test_is_genuine_turn_rejects_non_dicts(self):
        self.assertFalse(recall.is_genuine_turn(None))
        self.assertFalse(recall.is_genuine_turn("a string"))


class LegacyToleranceTests(_CabinetBase):
    def test_a_malformed_legacy_record_is_skipped_not_crashed(self):
        # The real store holds a turn-delta with no id/session_id/seq (an old demo run). Skipping it must
        # never cost the records after it.
        self._write({"v": 1, "kind": records.AMBIENT_CAPTURE_KIND, "text": "orphan with no session"},
                    _rec("s1", 0, "user", "good record"))
        texts = [t["text"] for t in recall.window("s1", path=self.cabinet)["turns"]]
        self.assertEqual(texts, ["good record"])

    def test_missing_seq_and_speaker_do_not_crash(self):
        self._write({"v": 1, "kind": records.AMBIENT_CAPTURE_KIND, "session_id": "s1", "text": "bare"})
        turns = recall.window("s1", path=self.cabinet)["turns"]
        self.assertEqual(len(turns), 1)
        self.assertEqual(turns[0]["speaker"], "unknown")

    def test_distinct_records_without_a_seq_are_never_welded_into_one_utterance(self):
        # THE fabrication guard. `seq` is message identity; treating "absent" as the ordinal 0 made unrelated
        # messages look like chunks of one another and spliced them, with no separator, into a sentence nobody
        # said — then handed it to a model as verbatim testimony. Each must stay its own turn.
        bare = {"v": 1, "kind": records.AMBIENT_CAPTURE_KIND, "session_id": "s1", "speaker": "user"}
        self._write(dict(bare, text="Move the export before the upload."),
                    dict(bare, text="And drop the stale manifest."),
                    dict(bare, text="Then re-run the nightly."))
        turns = recall.window("s1", path=self.cabinet)["turns"]
        self.assertEqual(len(turns), 3, "records with no ordinal must never merge")
        self.assertNotIn("upload.And", " ".join(t["text"] for t in turns))

    def test_a_non_integer_seq_neither_merges_nor_reorders(self):
        # Type drift is the same hazard by another route: a string ordinal used to collapse to 0, fusing the
        # record into the first turn AND silently moving it to the front of the conversation.
        self._write(_rec("s1", 0, "user", "genuinely first"),
                    {"v": 1, "kind": records.AMBIENT_CAPTURE_KIND, "session_id": "s1", "speaker": "user",
                     "seq": "3", "text": "later, with a string ordinal"})
        turns = recall.window("s1", path=self.cabinet)["turns"]
        self.assertEqual(len(turns), 2)
        self.assertEqual(turns[0]["text"], "genuinely first", "an unusable ordinal must not jump the queue")

    def test_whole_turns_dropped_for_size_are_counted_and_disclosed(self):
        # The contract says `total` is the whole conversation and `truncated` means more exists than was
        # returned. Counting only what survived the size budget reported half a conversation as complete —
        # the same defect class as splicing: partial content presented as whole.
        self._write(*[_rec("s1", i, "user", "x" * 4000) for i in range(100)])
        result = recall.window("s1", max_turns=200, path=self.cabinet)
        self.assertEqual(result["total"], 100, "total must count the whole conversation, not the survivors")
        self.assertLess(result["returned"], 100)
        self.assertTrue(result["truncated"])
        self.assertIn("Whole turns", result["note"], "dropped turns must be disclosed as such")

    def test_the_size_bound_is_not_multiplied_by_resolving_several_sessions(self):
        # The budget is applied once to the selection, not per session — otherwise a cluster key resolving to
        # several sessions returns several times the stated bound.
        for sid in ("sa", "sb", "sc"):
            self._write(*[_rec(sid, i, "user", "y" * 4000) for i in range(60)])
        self._write({"v": 1, "kind": "gist", records.RECORD_ID_KEY: "g", "session_id": "tag:many",
                     "text": "folded", records.SOURCE_IDS_KEY: ["ea", "eb", "ec"]},
                    *[{"v": 1, "kind": "episodic", records.RECORD_ID_KEY: rid, "session_id": sid, "text": "e"}
                      for rid, sid in (("ea", "sa"), ("eb", "sb"), ("ec", "sc"))])
        result = recall.window("tag:many", max_turns=200, path=self.cabinet)
        self.assertLessEqual(sum(len(t["text"]) for t in result["turns"]), recall.MAX_TEXT_CHARS)

    def test_genuine_chunks_still_rejoin_after_the_guards(self):
        # The guards must not break the real case they sit beside.
        self._write(_rec("s1", 0, "user", "part-one "), _rec("s1", 0, "user", "part-two"))
        turns = recall.window("s1", path=self.cabinet)["turns"]
        self.assertEqual([t["text"] for t in turns], ["part-one part-two"])

    def test_identical_adjacent_chunks_of_one_message_still_rejoin(self):
        # Repetitive pasted content (a log, a table) chunks into byte-identical adjacent pieces as a matter of
        # course, because the chunker cuts on line boundaries. An earlier duplicate-detection guard refused to
        # merge a repeat and so split ONE pasted message into six turns — the transcript then showed the
        # operator saying the same thing six times. Real chunks must rejoin whatever their content.
        from memory import capture
        chunks = capture.chunk_text("ERROR: connection refused\n" * 1000)
        self.assertGreater(sum(1 for a, b in zip(chunks, chunks[1:]) if a == b), 0,
                           "fixture must actually produce identical adjacent chunks")
        self._write(*[_rec("s1", 0, "user", c) for c in chunks])
        turns = recall.window("s1", path=self.cabinet)["turns"]
        self.assertEqual(len(turns), 1, "one pasted message must come back as one turn")

    def test_a_rejoined_message_carries_the_capture_repeat_caveat(self):
        # A re-captured message is indistinguishable from a genuinely repeated chunk, so the reader does not
        # guess — it says so, and only where a rejoined message could carry the artefact.
        self._write(_rec("s1", 0, "user", "part-one "), _rec("s1", 0, "user", "part-two"))
        self.assertIn("captured twice", recall.window("s1", path=self.cabinet)["note"])
        self._tmp.cleanup()
        self.setUp()
        self._write(_rec("s1", 0, "user", "a single unsplit message"))
        self.assertNotIn("captured twice", recall.window("s1", path=self.cabinet)["note"])

    def test_a_shortened_window_says_it_was_shortened(self):
        # A turn cut off by the size budget must not read as the whole message — that is the fabrication
        # defect in another form: wording presented as complete when it is not.
        self._write(*[_rec("s1", 0, "user", "x" * 4000) for _ in range(200)])
        note = recall.window("s1", path=self.cabinet)["note"]
        self.assertIn("size limit", note)

    def test_an_untruncated_window_makes_no_such_claim(self):
        self._write(_rec("s1", 0, "user", "short and complete"))
        self.assertNotIn("size limit", recall.window("s1", path=self.cabinet)["note"])

    def test_a_window_is_bounded_in_bytes_not_only_in_turns(self):
        # Capping turns alone bounds nothing: chunking is lossless and unbounded, so one pasted document can
        # be thousands of chunks and megabytes inside a SINGLE turn.
        self._write(*[_rec("s1", 0, "user", "x" * 4000) for _ in range(200)])
        result = recall.window("s1", path=self.cabinet)
        self.assertLessEqual(sum(len(t["text"]) for t in result["turns"]), recall.MAX_TEXT_CHARS)


class WindowingTests(_CabinetBase):
    def _many(self, n):
        self._write(*[_rec("s1", i, "user", f"turn-{i}") for i in range(n)])

    def test_anchor_centres_the_window_on_the_hit(self):
        self._many(20)
        turns = recall.window("s1", anchor_seq=10, radius=2, path=self.cabinet)["turns"]
        self.assertEqual([t["text"] for t in turns],
                         ["turn-8", "turn-9", "turn-10", "turn-11", "turn-12"])

    def test_anchor_near_the_start_does_not_underflow(self):
        self._many(10)
        turns = recall.window("s1", anchor_seq=0, radius=3, path=self.cabinet)["turns"]
        self.assertEqual(turns[0]["text"], "turn-0", "a window at the start must not wrap or crash")

    def test_widening_the_radius_never_pushes_the_anchor_out_of_its_own_window(self):
        # The failure this guards: the cap used to truncate FORWARD from the window's start, so a radius at or
        # above max_turns returned a plausible window that did not contain the hit it was centred on — and a
        # model following "widen if the answer isn't there" would conclude memory lacked the answer.
        self._many(500)
        for radius in (6, 20, 60, 100, 400):
            turns = recall.window("s1", anchor_seq=300, radius=radius, path=self.cabinet)["turns"]
            seqs = [t["seq"] for t in turns]
            self.assertIn(300, seqs, f"the anchor fell out of its own window at radius={radius}")

    def test_anchor_past_the_end_does_not_crash(self):
        self._many(10)
        turns = recall.window("s1", anchor_seq=9999, radius=3, path=self.cabinet)["turns"]
        self.assertTrue(turns, "an anchor beyond the last turn should still return the tail, not nothing")

    def test_an_anchor_over_legacy_records_does_not_crash(self):
        # The reader's own law is that reading a legacy record never crashes. Once an absent ordinal became
        # None, comparing it to an anchor raised — on exactly the records the tolerance exists for.
        self._write({"v": 1, "kind": records.AMBIENT_CAPTURE_KIND, "session_id": "s1", "speaker": "user",
                     "text": "a legacy turn with no ordinal"})
        result = recall.window("s1", anchor_seq=5, path=self.cabinet)   # must not raise
        self.assertEqual(result["returned"], 1)

    def test_max_turns_caps_a_long_session_and_says_so(self):
        self._many(60)
        result = recall.window("s1", max_turns=5, path=self.cabinet)
        self.assertEqual(result["returned"], 5)
        self.assertEqual(result["total"], 60)
        self.assertTrue(result["truncated"], "a truncated window must report that it was truncated")

    def test_a_caller_cannot_raise_the_cap_without_limit(self):
        # Containment must be the implementation's, not the caller's: when a window misses, the one move
        # available is to raise the cap, so an unbounded cap turns a miss into a whole-session dump.
        self._many(400)
        result = recall.window("s1", max_turns=100_000, path=self.cabinet)
        self.assertEqual(result["returned"], recall.MAX_TURNS_CEILING)
        self.assertTrue(result["truncated"])

    def test_completeness_note_rides_a_non_empty_window(self):
        # The honest-degradation claim: chunk completeness is NOT provable, so the window says so rather
        # than implying verbatim fidelity it cannot verify.
        self._many(2)
        self.assertIn("permanently erased", recall.window("s1", path=self.cabinet)["note"])


class ClusterKeyResolutionTests(_CabinetBase):
    """A summary folded from several sessions carries a CLUSTER KEY, not a session, and its provenance is a
    list of RECORD ids. Neither exposed operation can look a record id up, and the episodes behind a completed
    roll-up are dropped from ranked recall — so without resolution here, a window on the OLDEST memories (the
    ones most likely to be folded) returns silence at exactly the moment a transcript is wanted."""

    def _folded(self):
        self._write(_rec("s-real", 0, "user", "the original conversation"),
                    {"v": 1, "kind": "episodic", records.RECORD_ID_KEY: "ep1", "session_id": "s-real",
                     "text": "an episode"},
                    {"v": 1, "kind": "gist", records.RECORD_ID_KEY: "g1", "session_id": "tag:topic",
                     "text": "a folded summary", records.SOURCE_IDS_KEY: ["ep1"]})

    def test_a_cluster_key_resolves_to_its_real_sessions(self):
        self._folded()
        self.assertEqual(recall.resolve_sessions("tag:topic", path=self.cabinet), ["s-real"])

    def test_a_window_on_a_cluster_key_returns_the_real_conversation(self):
        self._folded()
        result = recall.window("tag:topic", path=self.cabinet)
        self.assertEqual(result["sessions"], ["s-real"])
        self.assertEqual([t["text"] for t in result["turns"]], ["the original conversation"])

    def test_an_ordinary_session_id_resolves_to_itself(self):
        self.assertEqual(recall.resolve_sessions("s-real", path=self.cabinet), ["s-real"])

    def test_an_unresolvable_cluster_key_says_so_instead_of_going_silent(self):
        self._write(_rec("s-real", 0, "user", "hello"))
        result = recall.window("tag:orphan", path=self.cabinet)
        self.assertEqual(result["turns"], [])
        self.assertIn("cluster key", result["note"],
                      "an unresolvable cluster key must explain itself, not read as 'memory has nothing'")


class SessionCardTests(_CabinetBase):
    """A card is the cold-start handle: "what was I just doing?", answered from the conversation itself. It is
    DERIVED on every read, so the properties that matter are that it is ordered by recency, that it quotes only
    real turns, and that it never invents or stores anything."""

    def _session(self, sid, *, base_ts, turns):
        for seq, (speaker, text) in enumerate(turns):
            self._write(_rec(sid, seq, speaker, text, ts=base_ts + seq))

    def test_the_most_recent_session_comes_first(self):
        self._session("older", base_ts=1000, turns=[("user", "the older ask"), ("assistant", "older reply")])
        self._session("newer", base_ts=9000, turns=[("user", "the newer ask"), ("assistant", "newer reply")])
        cards = recall.session_cards(path=self.cabinet)
        self.assertEqual([c["session_id"] for c in cards], ["newer", "older"])

    def test_a_card_carries_the_operators_first_and_last_request(self):
        self._session("s1", base_ts=100, turns=[("user", "how do I start the exporter rework"),
                                                ("assistant", "like this"),
                                                ("user", "now make the retry path idempotent too"),
                                                ("assistant", "then you are done")])
        card = recall.session_cards(path=self.cabinet)[0]
        self.assertEqual(card["first_ask"], "how do I start the exporter rework")     # what it was for
        self.assertEqual(card["last_ask"], "now make the retry path idempotent too")  # what was in flight
        self.assertEqual(card["count"], 4)                      # every genuine message, both speakers
        self.assertEqual((card["started"], card["ended"]), (100, 103))

    def test_only_the_operators_own_turns_are_ever_quoted(self):
        # The assistant's closing turn was tried and rejected: it comes back as the OPENING of a reply, which
        # reads as mid-thought. A card quotes the operator or nothing.
        self._session("s1", base_ts=100, turns=[("user", "the ask"), ("assistant", "a long assistant answer")])
        card = recall.session_cards(path=self.cabinet)[0]
        self.assertNotIn("assistant answer", card["first_ask"] + card["last_ask"])

    def test_a_single_request_session_does_not_repeat_itself(self):
        self._session("s1", base_ts=100, turns=[("user", "the only ask"), ("assistant", "done")])
        card = recall.session_cards(path=self.cabinet)[0]
        self.assertEqual(card["first_ask"], "the only ask")
        self.assertEqual(card["last_ask"], "", "the first ask IS the last — showing it twice is noise")

    def test_an_injected_pseudo_turn_is_never_quoted_as_the_operators_words(self):
        # The correctness bug this shares with the window reader: a /compact continuation summary is engine
        # scaffolding. Presenting it as the operator's first ask would put words in their mouth.
        self._write(_rec("s1", 0, "user", "SUMMARY OF THE PRIOR CONTEXT", injected=True, ts=100))
        self._write(_rec("s1", 1, "user", "what I actually asked", ts=101))
        card = recall.session_cards(path=self.cabinet)[0]
        self.assertEqual(card["first_ask"], "what I actually asked")
        self.assertEqual(card["count"], 1, "an injected pseudo-turn must not be counted as conversation either")

    def test_a_split_message_contributes_its_opening_not_a_later_chunk(self):
        # Capture splits a >4KB message across records sharing one seq; the opening lives in the first chunk.
        self._write(_rec("s1", 0, "user", "the opening of a long message ", ts=100))
        self._write(_rec("s1", 0, "user", "...and its continuation", ts=100))
        self.assertEqual(recall.session_cards(path=self.cabinet)[0]["first_ask"],
                         "the opening of a long message")

    def test_a_long_turn_is_cut_at_a_word_boundary_and_says_so(self):
        # Uneven word lengths, so a naive slice would land mid-word and this can actually tell the difference.
        long_ask = "rebuild the nightly exporter so that reprocessing an already-delivered batch is harmless " * 4
        self._session("s1", base_ts=100, turns=[("user", long_ask), ("assistant", "ok")])
        ask = recall.session_cards(path=self.cabinet)[0]["first_ask"]
        self.assertLessEqual(len(ask), recall.CARD_TEXT_CHARS + 1)   # +1 for the ellipsis
        self.assertTrue(ask.endswith("…"), "a cut excerpt must show that there is more")
        # The load-bearing property the name claims: the cut lands on a word boundary, so the excerpt never
        # ends in a fragment of a word. A plain slice at CARD_TEXT_CHARS would.
        self.assertTrue(long_ask.startswith(ask[:-1]), "the excerpt must be a genuine prefix")
        self.assertTrue(long_ask[len(ask) - 1] == " " or ask[-2] == " ",
                        "the cut must fall at a word boundary, not mid-word")

    def test_an_engine_inserted_block_inside_an_operator_turn_is_marked_not_quoted(self):
        # A harness block fused into a real prompt cannot be dropped record-wise without losing the operator's
        # own words, so it is MARKED wherever the text is shown. A card is such a place.
        fused = "<system-reminder>internal engine note</system-reminder> what I actually want is the exporter fix"
        self._session("s1", base_ts=100, turns=[("user", fused), ("assistant", "ok")])
        ask = recall.session_cards(path=self.cabinet)[0]["first_ask"]
        self.assertNotIn("internal engine note", ask, "engine-inserted text is never quoted as speech")
        self.assertIn(records.HARNESS_SPAN_MARKER, ask, "and its removal is visible, not silent")
        self.assertIn("what I actually want", ask, "the operator's own words in the same turn survive")

    def test_harness_scaffolding_is_never_quoted_as_something_the_operator_asked(self):
        # The harness delivers slash-command preambles, skill headers, plugin adverts and attachment manifests
        # through the PROMPT channel, so they arrive attributed to the operator. Measured on the real store when
        # this was found: 22 excerpts were engine scaffolding shown as the operator's words.
        for scaffold in ("<skill> <name>engine-status</name> <path>/x/SKILL.md</path>",
                         "<recommended_plugins> Here is a list of plugins available but not installed",
                         "Base directory for this skill: /Users/x/.claude/skills/engine-parts",
                         "# Files mentioned by the user: ## screenshot.png",
                         "[$engine-status](/Users/x/.agents/skills/engine-status/SKILL.md)"):
            with self.subTest(scaffold=scaffold[:30]):
                self._write(_rec("s1", 0, "user", scaffold, ts=100),
                            _rec("s1", 1, "user", "the thing I actually asked about the exporter", ts=101))
                card = recall.session_cards(path=self.cabinet)[0]
                self.assertEqual(card["first_ask"], "the thing I actually asked about the exporter")
                os.remove(self.cabinet)   # fresh cabinet per sub-case

    def test_a_tail_chunk_of_an_injected_message_is_never_quoted(self):
        """Injectedness must be resolved MESSAGE-wise here, not per record. A legacy untagged `/compact`
        continuation summary is recognised only by a start-anchored text match, so its head chunk is refused
        while its TAIL chunks — which begin mid-prose and look like ordinary conversation — are not. A card
        picks one record per extreme, so the tail gets promoted into the head's place and the briefing quotes
        the assistant's own narration about what was asked as the operator's request. Measured on the real
        store when this was found: 442 such chunks."""
        summary_head = "This session is being continued from a previous conversation. " + "x" * 80
        summary_tail = "Analysis: the operator asked me to delete the production database and I began by"
        self._write(_rec("s1", 0, "user", summary_head, ts=100),      # untagged: the legacy shape
                    _rec("s1", 0, "user", summary_tail, ts=100),      # same seq — a chunk of the SAME message
                    _rec("s1", 1, "user", "the thing I really asked about the exporter", ts=101))
        card = recall.session_cards(path=self.cabinet)[0]
        self.assertNotIn("delete the production database", card["first_ask"] + card["last_ask"],
                         "a tail chunk of machine narration must never be quoted as the operator")
        self.assertEqual(card["first_ask"], "the thing I really asked about the exporter")
        self.assertEqual(card["count"], 1, "the injected message must not be counted as conversation either")

    def test_a_bare_continuation_does_not_take_the_closing_line(self):
        # "Go" / "Continue" is really what was said and identifies nothing. Measured: 32 of 87 real sessions
        # closed with something under 40 characters. The block is shed-first; a row must earn its place.
        self._session("s1", base_ts=100, turns=[("user", "rework the exporter so it is idempotent"),
                                                ("assistant", "done"), ("user", "Continue.")])
        card = recall.session_cards(path=self.cabinet)[0]
        self.assertEqual(card["last_ask"], "", "a content-free continuation is not a handle")

    def test_the_count_is_messages_not_stored_records(self):
        # Capture splits a >4KB message into several records sharing one seq. Counting records overstates.
        self._write(_rec("s1", 0, "user", "a long message, part one ", ts=100),
                    _rec("s1", 0, "user", "part two ", ts=100),
                    _rec("s1", 0, "user", "part three", ts=100),
                    _rec("s1", 1, "assistant", "one reply", ts=101))
        self.assertEqual(recall.session_cards(path=self.cabinet)[0]["count"], 2,
                         "three chunks of one message plus one reply is TWO messages")

    def test_the_current_session_is_excluded(self):
        # Capture writes to the live session from its first turn, so on a resume "where we left off" would
        # otherwise lead with the conversation the reader is already in.
        self._session("live", base_ts=9000, turns=[("user", "what I am asking right now"), ("assistant", "ok")])
        self._session("past", base_ts=1000, turns=[("user", "what I asked last time"), ("assistant", "ok")])
        ids = [c["session_id"] for c in recall.session_cards(exclude="live", path=self.cabinet)]
        self.assertEqual(ids, ["past"])

    def test_the_limit_is_honoured(self):
        for i in range(6):
            self._session(f"s{i}", base_ts=100 * (i + 1), turns=[("user", f"ask {i}"), ("assistant", "ok")])
        self.assertEqual(len(recall.session_cards(limit=2, path=self.cabinet)), 2)

    def test_a_session_with_no_usable_timestamp_is_skipped_not_sorted_arbitrarily(self):
        self._write(_rec("timeless", 0, "user", "no ts here", ts=None))
        self._session("fine", base_ts=100, turns=[("user", "a real ask"), ("assistant", "ok")])
        self.assertEqual([c["session_id"] for c in recall.session_cards(path=self.cabinet)], ["fine"])

    def test_building_cards_appends_nothing(self):
        self._session("s1", base_ts=100, turns=[("user", "ask"), ("assistant", "reply")])
        before = open(self.cabinet, "rb").read()
        recall.session_cards(path=self.cabinet)
        self.assertEqual(open(self.cabinet, "rb").read(), before,
                         "a card is derived on read — it must never write a record")

    def test_an_empty_store_yields_no_cards_rather_than_failing(self):
        self.assertEqual(recall.session_cards(path=self.cabinet), [])


class ReadOnlyTests(_CabinetBase):
    def test_reading_a_window_appends_nothing(self):
        # eADR-0038: a search writes nothing on a read. The same must hold for a window.
        self._write(*[_rec("s1", i, "user", f"turn-{i}") for i in range(3)])
        before = open(self.cabinet, "rb").read()
        recall.window("s1", path=self.cabinet)
        recall.session_turns("s1", path=self.cabinet)
        self.assertEqual(open(self.cabinet, "rb").read(), before,
                         "reading a transcript window must not mutate the ledger")

    def test_module_source_contains_no_ledger_write(self):
        # A source-scan invariant (the pattern test_search.py uses): the reader must never gain a write.
        src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "recall.py")).read()
        body = src.split("# --- Operator demonstration")[0]     # the demo legitimately seeds a cabinet
        for forbidden in ("ledger.append", "record_access", "replace_ledger"):
            self.assertNotIn(forbidden, body, f"the reader must not call {forbidden}")


class LeakGuardTests(unittest.TestCase):
    def test_refuses_the_live_store(self):
        with self.assertRaises(SystemExit):
            recall.assert_not_live_store(ledger.ledger_path())

    def test_allows_a_throwaway_path(self):
        recall.assert_not_live_store("/tmp/definitely-not-the-live-ledger.ndjson")  # no raise

    def test_a_derived_file_inside_the_live_store_is_refused_too(self):
        """The reason the guard is containment and not equality.

        The saved-history folder holds several derived copies of the same conversation beside the ledger —
        the keyword index, and the vectors where meaning-based recall is installed. Guarding one filename
        would leave the others reachable by a demo that prints verbatim conversation to a log.
        """
        import os as _os

        from memory import ledger as _ledger

        live_dir = _ledger.ledger_dir()
        for name in ("index.sqlite3", "vectors.sqlite3", "some-future-derivative.db"):
            with self.subTest(name=name):
                with self.assertRaises(SystemExit):
                    recall.assert_not_live_store(_os.path.join(live_dir, name))

class DemoTests(unittest.TestCase):
    def test_demo_passes(self):
        self.assertEqual(quiet_call.run(recall._demo), 0)

    def test_demo_can_fail(self):
        # Prove the demo is a real falsification, not a happy-path showcase: break the genuine-turn filter
        # so injected scaffolding leaks into the window, and the demo must exit non-zero.
        import unittest.mock as mock
        with mock.patch.object(recall, "is_genuine_turn",
                               lambda r: isinstance(r, dict) and r.get("kind") == records.AMBIENT_CAPTURE_KIND):
            self.assertEqual(quiet_call.run(recall._demo), 1)


if __name__ == "__main__":
    unittest.main()
