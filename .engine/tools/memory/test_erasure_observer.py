"""Unit tests for erasure_observer.py — the cross-session Layer-2 erasure OBSERVER.

The observer turns a MERGED single-purpose erasure PR into the enactment `operator-adjudicated-erasure` marker. These
tests pin the load-bearing behavior with the GitHub network stubbed at the injectable `_transport` seam (no live
GitHub): it enacts ONLY a genuine merge (never a mere close), binds the target to the IMMUTABLE merge tree (never the
editable PR body), fails SAFE on every read doubt, dedups on the target id alone (so a re-merge never re-fires), and
the SessionStart handler is fail-OPEN and relays a single plain-language heads-up. Throwaway ENGINE_MEMORY_DIR
cabinet throughout.
"""

from __future__ import annotations

import base64
import io
import json
import os
import re
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import hooks  # noqa: E402
import quiet_call  # noqa: E402  (capture a demo walkthrough's stdout so it can't bury the suite summary)
from memory import compact, erasure_observer as obs, ledger, records  # noqa: E402

_GOOD_SHA = "a1b2c3d4e5f600000000000000000000deadbeef"


def _rid() -> str:
    return records.new_record_id()


def _contents_for(target: str) -> dict:
    """A base64 contents response carrying a LEGACY single-target proposal `{"target": …}` (the earlier on-disk
    shape; the observer reads it as a one-note batch — this is the back-compat fixture reused across the suite)."""
    raw = json.dumps({"target": target, "cost": "a plain-language paraphrase"}).encode("utf-8")
    return {"content": base64.b64encode(raw).decode("ascii"), "encoding": "base64"}


def _contents_batch(targets: list) -> dict:
    """A base64 contents response carrying a batch proposal `{"targets": […], "costs": […]}`."""
    raw = json.dumps({"targets": list(targets), "costs": ["a paraphrase"] * len(targets)}).encode("utf-8")
    return {"content": base64.b64encode(raw).decode("ascii"), "encoding": "base64"}


def _contents_raw(payload: dict) -> dict:
    """A base64 contents response carrying an ARBITRARY proposal payload (for the malformed-grammar cases)."""
    return {"content": base64.b64encode(json.dumps(payload).encode("utf-8")).decode("ascii"), "encoding": "base64"}


def _merged_pr(number: int, sha: str = _GOOD_SHA, body: str = "an editable body") -> dict:
    return {"number": number, "merged_at": "2026-06-21T00:00:00Z", "merge_commit_sha": sha, "body": body}


def _closed_pr(number: int, body: str = "closed unmerged") -> dict:
    return {"number": number, "merged_at": None, "merge_commit_sha": None, "body": body}


def _gh(*, prs, contents=None, discovery=None, raise_exc=False, error_status=None, repo="o/r"):
    """A stub GitHub reader. `prs`: {number -> pr object} for /pulls/{n}. `contents`: {sha -> contents-response}
    for the proposal read. `discovery`: the /issues? list (defaults to one PR-item per `prs` key). `raise_exc`
    makes every read raise (a transport fault); `error_status` makes every read an HTTP error."""
    contents = contents or {}
    items = discovery if discovery is not None else [{"number": n, "pull_request": {}} for n in prs]

    def transport(method, path, body):
        if raise_exc:
            raise RuntimeError("github unreachable")
        if error_status is not None:
            return error_status, None
        if "/issues?" in path:
            return 200, items
        m = re.search(r"/pulls/(\d+)", path)
        if m:
            n = int(m.group(1))
            return (200, prs[n]) if n in prs else (404, None)
        cm = re.search(r"/contents/.*\?ref=([^&]+)", path)
        if cm:
            resp = contents.get(cm.group(1))
            return (200, resp) if resp is not None else (404, None)
        return 404, None

    return obs._FakeGH(transport, repo=repo)


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

    def _targets(self):
        return [r.get(records.TARGET_KEY) for r in ledger.iter_records()
                if isinstance(r, dict) and r.get("kind") == records.ERASURE_KIND]


class EnactTests(_Base):
    def test_enacts_a_genuinely_merged_labelled_pr(self):
        target = _rid()
        gh = _gh(prs={7: _merged_pr(7)}, contents={_GOOD_SHA: _contents_for(target)})
        self.assertEqual(obs.enact_from_merged_prs(gh), [7])
        markers = [r for r in ledger.iter_records()
                   if isinstance(r, dict) and r.get("kind") == records.ERASURE_KIND]
        self.assertEqual(len(markers), 1)
        self.assertEqual(markers[0][records.TARGET_KEY], target)
        self.assertEqual(markers[0][records.MERGE_SHA_KEY], _GOOD_SHA)   # the authorising merge is recorded

    def test_a_closed_unmerged_pr_is_never_enacted(self):
        # The locked "merge event only": a PR merely closed (merged_at null) never erases.
        target = _rid()
        gh = _gh(prs={8: _closed_pr(8)}, contents={_GOOD_SHA: _contents_for(target)})
        self.assertEqual(obs.enact_from_merged_prs(gh), [])
        self.assertEqual(self._targets(), [])

    def test_a_merged_pr_without_a_merge_sha_is_inert(self):
        target = _rid()
        pr = _merged_pr(7)
        pr["merge_commit_sha"] = ""                                       # merged_at set, but no real merge commit
        gh = _gh(prs={7: pr}, contents={_GOOD_SHA: _contents_for(target)})
        self.assertEqual(obs.enact_from_merged_prs(gh), [])
        self.assertEqual(self._targets(), [])

    def test_a_closed_pr_with_a_speculative_merge_sha_is_inert(self):
        # `merged_at` is the AUTHORITATIVE "was actually merged" signal; `merge_commit_sha` ALONE is not a merge.
        # GitHub computes a SPECULATIVE test-merge `merge_commit_sha` for an open/mergeable PR, and a closed-but-
        # unmerged PR can RETAIN it — whose tree could even contain a committed proposal. The merge gate must still
        # reject it (this pins the `merged_at` half AT THE INTEGRATION LEVEL: an AI could open a PR, commit a valid
        # proposal, take the speculative SHA, then CLOSE without merging — never touching protected main — and it
        # must remain inert). Mutation: gating only on a non-empty `merge_commit_sha` would erase here; this reds it.
        target = _rid()
        pr = {"number": 7, "merged_at": None, "merge_commit_sha": _GOOD_SHA, "body": "closed, never merged"}
        gh = _gh(prs={7: pr}, contents={_GOOD_SHA: _contents_for(target)})
        self.assertEqual(obs.enact_from_merged_prs(gh), [])
        self.assertEqual(self._targets(), [])

    def test_a_missing_proposal_is_inert(self):
        gh = _gh(prs={7: _merged_pr(7)}, contents={})                    # no proposal at the merge tree (404)
        self.assertEqual(obs.enact_from_merged_prs(gh), [])
        self.assertEqual(self._targets(), [])

    def test_a_non_base64_proposal_is_inert(self):
        # The >1 MB case: GitHub returns encoding "none" + an empty content; treat as unreadable -> skip.
        gh = _gh(prs={7: _merged_pr(7)}, contents={_GOOD_SHA: {"content": "", "encoding": "none"}})
        self.assertEqual(obs.enact_from_merged_prs(gh), [])
        self.assertEqual(self._targets(), [])

    def test_a_corrupt_proposal_is_inert(self):
        for bad in ({"content": "!!! not base64 !!!", "encoding": "base64"},          # base64 decode fails
                    {"content": base64.b64encode(b"not json").decode(), "encoding": "base64"}):  # json fails
            with self.subTest(bad=bad):
                gh = _gh(prs={7: _merged_pr(7)}, contents={_GOOD_SHA: bad})
                self.assertEqual(obs.enact_from_merged_prs(gh), [])
        self.assertEqual(self._targets(), [])

    def test_a_malformed_target_is_inert(self):
        # A target that is not the exact 32-char uuid4-hex shape fails SAFE (no erasure).
        raw = json.dumps({"target": "not-a-real-id", "cost": "x"}).encode("utf-8")
        bad = {"content": base64.b64encode(raw).decode("ascii"), "encoding": "base64"}
        gh = _gh(prs={7: _merged_pr(7)}, contents={_GOOD_SHA: bad})
        self.assertEqual(obs.enact_from_merged_prs(gh), [])
        self.assertEqual(self._targets(), [])

    def test_binds_to_the_committed_tree_not_the_editable_body(self):
        # THE core property: the PR's BODY names a DIFFERENT valid id, but the committed proposal names the
        # real target. The observer enacts the PROPOSAL's target and never the body's — the body is reachable-but-
        # ignored (it is never read).
        target, body_id = _rid(), _rid()
        pr = _merged_pr(7, body=f"please erase {body_id} instead")     # a misleading, editable body
        gh = _gh(prs={7: pr}, contents={_GOOD_SHA: _contents_for(target)})
        self.assertEqual(obs.enact_from_merged_prs(gh), [7])
        self.assertEqual(self._targets(), [target])                     # the proposal's target, not the body's
        self.assertNotIn(body_id, self._targets())

    def test_two_prs_naming_one_target_mint_once(self):
        target = _rid()
        gh = _gh(prs={7: _merged_pr(7, sha="sha7" + "0" * 36), 9: _merged_pr(9, sha="sha9" + "0" * 36)},
                 contents={"sha7" + "0" * 36: _contents_for(target), "sha9" + "0" * 36: _contents_for(target)})
        enacted = obs.enact_from_merged_prs(gh)
        self.assertEqual(enacted, [7])                                  # the first; the second dedups in-run
        self.assertEqual(self._targets(), [target])


class DedupTests(_Base):
    def test_a_second_sequential_run_mints_no_new_marker(self):
        target = _rid()
        gh = _gh(prs={7: _merged_pr(7)}, contents={_GOOD_SHA: _contents_for(target)})
        self.assertEqual(obs.enact_from_merged_prs(gh), [7])
        self.assertEqual(obs.enact_from_merged_prs(gh), [])            # the retained marker dedups it
        self.assertEqual(self._targets(), [target])

    def test_dedup_holds_after_the_proposal_is_removed(self):
        # The never-nag guarantee rests on the RETAINED LEDGER MARKER, not the committed proposal file: once enacted,
        # a later run whose proposal is GONE (404) still dedups (no re-mint, no re-fire) — so a later cleanup of
        # the proposal can never re-nag.
        target = _rid()
        gh1 = _gh(prs={7: _merged_pr(7)}, contents={_GOOD_SHA: _contents_for(target)})
        self.assertEqual(obs.enact_from_merged_prs(gh1), [7])
        gh2 = _gh(prs={7: _merged_pr(7)}, contents={})                 # proposal removed at the tree
        self.assertEqual(obs.enact_from_merged_prs(gh2), [])
        self.assertEqual(self._targets(), [target])

    def test_a_preexisting_marker_blocks_a_remint(self):
        target = _rid()
        compact.enact_erasure(target, "some-earlier-merge")           # a marker already on file
        gh = _gh(prs={7: _merged_pr(7)}, contents={_GOOD_SHA: _contents_for(target)})
        self.assertEqual(obs.enact_from_merged_prs(gh), [])
        self.assertEqual(self._targets(), [target])                    # still exactly one


class DiscoveryTests(_Base):
    def test_a_labelled_issue_that_is_not_a_pr_is_skipped(self):
        # The /issues? endpoint lists Issues AND PRs; only items with a `pull_request` key are PRs. A labelled
        # plain Issue (no pull_request key) is not a candidate.
        target = _rid()
        gh = _gh(prs={5: _merged_pr(5)},
                 discovery=[{"number": 5, "pull_request": {}}, {"number": 6}],   # #6 is a plain Issue
                 contents={_GOOD_SHA: _contents_for(target)})
        self.assertEqual(obs.enact_from_merged_prs(gh), [5])
        self.assertEqual(self._targets(), [target])

    def test_no_labelled_prs_is_a_clean_noop(self):
        gh = _gh(prs={}, discovery=[])
        self.assertEqual(obs.enact_from_merged_prs(gh), [])
        self.assertEqual(self._targets(), [])


class FailOpenTests(_Base):
    def test_a_transport_fault_yields_no_enact_and_never_raises(self):
        gh = _gh(prs={7: _merged_pr(7)}, raise_exc=True)
        self.assertEqual(obs.enact_from_merged_prs(gh), [])            # swallowed, not raised
        self.assertEqual(self._targets(), [])

    def test_an_http_error_yields_no_enact(self):
        gh = _gh(prs={7: _merged_pr(7)}, error_status=503)
        self.assertEqual(obs.enact_from_merged_prs(gh), [])
        self.assertEqual(self._targets(), [])


class HandlerTests(_Base):
    def _run_hook(self, payload=None):
        out, err = io.StringIO(), io.StringIO()
        code = hooks.run_hook("SessionStart", obs._session_start_handler,
                              stdin=io.StringIO(json.dumps(payload or {})), stdout=out, stderr=err)
        return code, out.getvalue()

    def test_handler_injects_a_one_time_notice_on_a_new_enactment(self):
        target = _rid()
        gh = _gh(prs={7: _merged_pr(7)}, contents={_GOOD_SHA: _contents_for(target)})
        with mock.patch.object(obs, "_reader", return_value=gh):
            decision = obs._session_start_handler({})
        self.assertEqual(decision.get("action"), "inject")
        self.assertIn("#7", decision.get("context", ""))
        self.assertIn(target, self._targets())

    def test_handler_is_silent_when_nothing_is_enacted(self):
        gh = _gh(prs={8: _closed_pr(8)})                               # closed-unmerged only
        with mock.patch.object(obs, "_reader", return_value=gh):
            decision = obs._session_start_handler({})
        self.assertEqual(decision, hooks.proceed())
        self.assertEqual(self._targets(), [])

    def test_handler_is_silent_on_the_dedup_session(self):
        target = _rid()
        gh = _gh(prs={7: _merged_pr(7)}, contents={_GOOD_SHA: _contents_for(target)})
        with mock.patch.object(obs, "_reader", return_value=gh):
            first = obs._session_start_handler({})
            second = obs._session_start_handler({})                    # same merged PR, a later session
        self.assertEqual(first.get("action"), "inject")               # told once...
        self.assertEqual(second, hooks.proceed())                      # ...never again

    def test_handler_proceeds_when_no_reader(self):
        with mock.patch.object(obs, "_reader", return_value=None):
            self.assertEqual(obs._session_start_handler({}), hooks.proceed())

    def test_handler_fails_open_on_a_reader_fault(self):
        with mock.patch.object(obs, "_reader", side_effect=RuntimeError("boom")):
            self.assertEqual(obs._session_start_handler({}), hooks.proceed())

    def test_session_start_via_run_hook_proceeds_and_enacts(self):
        target = _rid()
        gh = _gh(prs={7: _merged_pr(7)}, contents={_GOOD_SHA: _contents_for(target)})
        with mock.patch.object(obs, "_reader", return_value=gh):
            code, _out = self._run_hook({"session_id": "S"})
        self.assertEqual(code, hooks.EXIT_PROCEED)                      # SessionStart is never blocked
        self.assertIn(target, self._targets())


class UnitTests(unittest.TestCase):
    def test_is_record_id_accepts_only_the_uuid4_hex_shape(self):
        self.assertTrue(obs._is_record_id(records.new_record_id()))
        for bad in ("", "short", "g" * 32, "A" * 32, records.new_record_id() + "0", None, 123):
            self.assertFalse(obs._is_record_id(bad))

    def test_is_genuinely_merged_requires_both_merged_at_and_sha(self):
        self.assertTrue(obs._is_genuinely_merged(_merged_pr(7)))
        self.assertFalse(obs._is_genuinely_merged(_closed_pr(8)))
        self.assertFalse(obs._is_genuinely_merged({"merged_at": "x", "merge_commit_sha": ""}))
        self.assertFalse(obs._is_genuinely_merged({"merged_at": None, "merge_commit_sha": "abc"}))
        self.assertFalse(obs._is_genuinely_merged(None))


class ReadTargetsTests(_Base):
    """`_read_targets` reads the batch grammar, keeps back-compat with the legacy single, and WHOLE-BATCH
    REJECTS on any doubt — the faithful generalisation of the single-target fail-SAFE."""

    def test_reads_a_batch_of_ids(self):
        a, b = _rid(), _rid()
        gh = _gh(prs={7: _merged_pr(7)}, contents={_GOOD_SHA: _contents_batch([a, b])})
        self.assertEqual(obs._read_targets(gh, _GOOD_SHA), [a, b])

    def test_reads_a_legacy_single_target_as_a_one_note_batch(self):
        a = _rid()
        gh = _gh(prs={7: _merged_pr(7)}, contents={_GOOD_SHA: _contents_for(a)})
        self.assertEqual(obs._read_targets(gh, _GOOD_SHA), [a])

    def test_whole_batch_rejects_when_any_element_is_malformed(self):
        # The operator consented to the committed list; a single corrupt/foreign element voids the ENTIRE batch (a
        # malformed element implies corruption/tampering, since the proposer only ever writes valid ids) -> erase none.
        good = _rid()
        gh = _gh(prs={7: _merged_pr(7)},
                 contents={_GOOD_SHA: _contents_raw({"targets": [good, "not-an-id"], "costs": ["x", "y"]})})
        self.assertEqual(obs._read_targets(gh, _GOOD_SHA), [])

    def test_rejects_a_batch_whose_costs_do_not_pin_one_to_one_with_targets(self):
        # Consumer-side consent-integrity: the operator consents on one cost line per target. A targets/costs length
        # mismatch (or absent costs) means the committed list and the enumerated body diverged -> erase none. (The
        # producer structurally cannot emit this; the guard defends against a hand-crafted / corrupted proposal.)
        a, b = _rid(), _rid()
        for bad in ({"targets": [a, b], "costs": ["only one"]},          # fewer costs than targets
                    {"targets": [a, b]}):                                 # costs absent entirely
            with self.subTest(bad=bad):
                gh = _gh(prs={7: _merged_pr(7)}, contents={_GOOD_SHA: _contents_raw(bad)})
                self.assertEqual(obs._read_targets(gh, _GOOD_SHA), [])

    def test_rejects_an_empty_batch(self):
        gh = _gh(prs={7: _merged_pr(7)}, contents={_GOOD_SHA: _contents_raw({"targets": [], "costs": []})})
        self.assertEqual(obs._read_targets(gh, _GOOD_SHA), [])

    def test_rejects_a_proposal_with_neither_key(self):
        gh = _gh(prs={7: _merged_pr(7)}, contents={_GOOD_SHA: _contents_raw({"cost": "x"})})
        self.assertEqual(obs._read_targets(gh, _GOOD_SHA), [])


class BatchEnactTests(_Base):
    """One merged batch PR mints ONE singular marker per target under the SHARED merge SHA, is resumable
    per target under partial failure, and erases the whole batch in one compaction."""

    def test_a_merged_batch_mints_one_marker_per_target_under_one_sha(self):
        a, b, c = _rid(), _rid(), _rid()
        gh = _gh(prs={7: _merged_pr(7)}, contents={_GOOD_SHA: _contents_batch([a, b, c])})
        self.assertEqual(obs.enact_from_merged_prs(gh), [7])             # ONE PR reported, whatever the batch size
        markers = [r for r in ledger.iter_records()
                   if isinstance(r, dict) and r.get("kind") == records.ERASURE_KIND]
        self.assertEqual({m[records.TARGET_KEY] for m in markers}, {a, b, c})   # one marker per target
        self.assertTrue(all(m[records.MERGE_SHA_KEY] == _GOOD_SHA for m in markers))  # all under the one merge

    def test_a_whole_batch_reject_enacts_nothing(self):
        good = _rid()
        gh = _gh(prs={7: _merged_pr(7)},
                 contents={_GOOD_SHA: _contents_raw({"targets": [good, "bad"], "costs": ["x", "y"]})})
        self.assertEqual(obs.enact_from_merged_prs(gh), [])
        self.assertEqual(self._targets(), [])                            # not even the valid member is enacted

    def test_partial_enactment_is_resumable_per_target(self):
        # Mint 2 of 3, "crash", re-run: only the missing 3rd mints (the `seen` set is re-read from the ledger).
        a, b, c = _rid(), _rid(), _rid()
        compact.enact_erasure(a, _GOOD_SHA)                             # pretend `a` was minted before the crash
        compact.enact_erasure(b, _GOOD_SHA)                             # ...and `b` too
        gh = _gh(prs={7: _merged_pr(7)}, contents={_GOOD_SHA: _contents_batch([a, b, c])})
        self.assertEqual(obs.enact_from_merged_prs(gh), [7])            # the PR is reported (c was newly minted)
        self.assertEqual(sorted(self._targets()), sorted([a, b, c]))    # exactly one marker each, no double-mint
        self.assertEqual(len(self._targets()), 3)
        self.assertEqual(obs.enact_from_merged_prs(gh), [])            # a full re-run mints nothing (all seen)
        self.assertEqual(len(self._targets()), 3)

    def test_a_merged_batch_erases_the_whole_batch_in_one_compaction(self):
        # End-to-end: two authorised targets are removed in a single compaction swap; a third, un-authorised note
        # survives. (Records planted directly with content-free ids; compaction removes exactly the marked pair.)
        from memory import legacy_shapes as legacy
        keep = legacy.episodic("S", "decision", "keep me", "bk")
        gone1 = legacy.episodic("S", "lesson", "erase one", "b1")
        gone2 = legacy.episodic("S", "lesson", "erase two", "b2")
        for rec in (keep, gone1, gone2):
            ledger.append(rec)
        gid1, gid2 = gone1[records.RECORD_ID_KEY], gone2[records.RECORD_ID_KEY]
        gh = _gh(prs={7: _merged_pr(7)}, contents={_GOOD_SHA: _contents_batch([gid1, gid2])})
        obs.enact_from_merged_prs(gh)
        report = compact.compact()
        self.assertEqual(report.get("erased"), 2)                       # both erased in ONE swap
        remaining = {r.get(records.RECORD_ID_KEY) for r in ledger.iter_records() if isinstance(r, dict)}
        self.assertIn(keep[records.RECORD_ID_KEY], remaining)           # the un-authorised note survived
        self.assertNotIn(gid1, remaining)
        self.assertNotIn(gid2, remaining)


class DemoTests(_Base):
    def test_demo_runs_clean(self):
        self.assertEqual(quiet_call.run(obs._demo), 0)


if __name__ == "__main__":
    unittest.main()
