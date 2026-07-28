#!/usr/bin/env python3
"""Self-tests for the standing product-spec-conformance sweep (#449) — the core tool that reads
product-design's committed spec-obligation matrix BY PRESENCE, prepares the audit persona's conditional
hunt-set, and promotes the persona's per-criterion divergence verdicts as deduped engine issues.

The faked boundaries are ONLY the network (the matrix's git history, the GitHub issue writes); the conditional
logic, the position-free dedup identity, the block parse+strip, the author-text neutralisation, the coverage
disclosure, and the producer/consumer matrix contract all run for real. Plan-gate resolutions are each
pinned by a named test below."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate            # noqa: E402
import telemetry           # noqa: E402
import conformance_sweep as cs  # noqa: E402
import quiet_call          # noqa: E402  (capture the demo's stdout so it cannot bury the suite summary)
from product_design import obligation_matrix  # noqa: E402  (the producer, imported HERE in the test only)

_DIGEST = "sha256:" + "a" * 64
_DIGEST2 = "sha256:" + "b" * 64

# The leak-guard backstage slugs — none may reach any operator-facing string (feed / issue title+body).
# Broadened past the six floor slugs to the audit's full backstage set (spec-conformance lens nit #3).
_BACKSTAGE = ["spec-obligation matrix", "coverage denominator", "divergence-hunt", "divergence-hunter",
              "conformance-enforcement floor", "over-build", "function-probe", "generic sweep",
              "fingerprint-gated", "retire-candidate", "concern-list"]


def _cap(status="locked", criterion="It works end to end") -> str:
    return (f"---\nstatus: {status}\n---\n\n# A capability\n\n## Acceptance criteria\n\n"
            f"| Criterion | How verified | Who checks it |\n| --- | --- | --- |\n"
            f"| {criterion} | a behavioral demo | operator |\n")


def _seed(files: dict) -> str:
    d = tempfile.mkdtemp(prefix="engine-conformance-test-")
    for rel, body in files.items():
        path = os.path.join(d, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(body)
    return d


def _matrix(rows) -> dict:
    return {"schema_version": 1, "source": "docs/spec", "rows": rows}


def _row(doc="docs/spec/a.md", position=0, digest=_DIGEST, criterion="It works end to end"):
    return {"doc": doc, "position": position, "digest": digest, "criterion": criterion,
            "how_verified": "a demo", "who": "operator"}


def _item(doc="docs/spec/a.md", digest=_DIGEST, verdict="diverges", note="the code stops early", criterion="It works"):
    return {"doc": doc, "digest": digest, "verdict": verdict, "note": note, "criterion": criterion}


def _block(items, kind="product-conformance") -> str:
    return "prose\n\n<!-- conformance-verdicts.v1\n" + json.dumps({"kind": kind, "items": items}) + "\n-->\n"


class TestLockedDocsAndState(unittest.TestCase):
    def test_locked_docs_enumerates_only_locked(self):
        root = _seed({"docs/spec/a.md": _cap("locked"), "docs/spec/b.md": _cap("draft"),
                      "docs/spec/nested/c.md": _cap("locked"), "docs/spec/notmd.txt": "x"})
        self.addCleanup(__import__("shutil").rmtree, root, True)
        self.assertEqual(cs.locked_docs(root), ["docs/spec/a.md", "docs/spec/nested/c.md"])

    def test_no_spec_dir_is_silent(self):
        root = _seed({"README.md": "hi"})
        self.addCleanup(__import__("shutil").rmtree, root, True)
        self.assertEqual(cs.conditional_state(root, None, _matrix_loaded=True), "silent")

    def test_locked_spec_without_matrix_is_degraded(self):
        root = _seed({"docs/spec/a.md": _cap("locked")})
        self.addCleanup(__import__("shutil").rmtree, root, True)
        self.assertEqual(cs.conditional_state(root, None, _matrix_loaded=True), "degraded")
        self.assertEqual(cs.conditional_state(root, _matrix([]), _matrix_loaded=True), "degraded")

    def test_locked_spec_with_rows_is_active(self):
        root = _seed({"docs/spec/a.md": _cap("locked")})
        self.addCleanup(__import__("shutil").rmtree, root, True)
        self.assertEqual(cs.conditional_state(root, _matrix([_row()]), _matrix_loaded=True), "active")


class TestLoadMatrixContract(unittest.TestCase):
    def _write(self, obj_or_text):
        d = tempfile.mkdtemp(prefix="engine-conformance-matrix-")
        self.addCleanup(__import__("shutil").rmtree, d, True)
        p = os.path.join(d, "m.json")
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(obj_or_text if isinstance(obj_or_text, str) else json.dumps(obj_or_text))
        return p

    def test_valid_matrix_loads(self):
        self.assertIsNotNone(cs.load_matrix(self._write(_matrix([_row()]))))

    def test_absent_matrix_is_none(self):
        self.assertIsNone(cs.load_matrix(os.path.join(tempfile.gettempdir(), "does-not-exist-xyz.json")))

    def test_malformed_json_is_none(self):
        self.assertIsNone(cs.load_matrix(self._write("{not json")))

    def test_contract_violation_is_none(self):
        # A row missing the pinned `digest` field violates the product-spec-matrix.v1 contract -> None
        # (treated as missing -> the disclosed degradation path, never a silent wrong read or a crash).
        bad = _matrix([{"doc": "docs/spec/a.md", "position": 0, "criterion": "x", "how_verified": "y", "who": "z"}])
        self.assertIsNone(cs.load_matrix(self._write(bad)))


class TestPrioritisation(unittest.TestCase):
    def test_history_unreadable_flags_nothing(self):
        # prior_pairs None (history could not be read) -> empty stale set (disclosed separately), never
        # a silent "nothing moved".
        self.assertEqual(cs.stale_rows([_row()], None), [])

    def test_no_prior_flags_all_first_appearance(self):
        # no prior version (frozenset()) -> every current row is new (a freshly-locked criterion is
        # caught, not left to sampling).
        rows = [_row(), _row(doc="docs/spec/b.md", digest=_DIGEST2)]
        self.assertEqual(cs.stale_rows(rows, frozenset()), rows)

    def test_moved_digest_flagged_stable_not(self):
        rows = [_row(), _row(doc="docs/spec/b.md", digest=_DIGEST2)]
        prior = frozenset({("docs/spec/a.md", _DIGEST)})   # b is new/moved, a is unchanged
        self.assertEqual(cs.stale_rows(rows, prior), [rows[1]])

    def test_sample_stable_rotates_with_offset(self):
        stable = [_row(position=i, digest="sha256:" + str(i) * 64) for i in range(5)]
        self.assertEqual([r["position"] for r in cs.sample_stable(stable, 0, 2)], [0, 1])
        self.assertEqual([r["position"] for r in cs.sample_stable(stable, 2, 2)], [2, 3])
        self.assertEqual(cs.sample_stable([], 0, 3), [])


class TestFeed(unittest.TestCase):
    def test_silent_feed(self):
        root = _seed({"README.md": "hi"})
        self.addCleanup(__import__("shutil").rmtree, root, True)
        feed = cs.build_feed(root, matrix=None, baseline_pairs=None)
        self.assertEqual(feed, cs._FEED_SILENT)
        self.assertIn("not applicable", feed.lower())   # never nags toward a spec — just skip it
        self.assertIn("skip", feed.lower())

    def test_degraded_feed_names_the_one_fix(self):
        root = _seed({"docs/spec/a.md": _cap("locked")})
        self.addCleanup(__import__("shutil").rmtree, root, True)
        feed = cs.build_feed(root, matrix=None, baseline_pairs=None)
        self.assertIn(cs.MATRIX_REGEN_CMD, feed)
        self.assertIn("do not guess", feed.lower())   # degrade-and-disclose: names the fix, never guesses a pass

    def test_active_feed_lists_hunt_and_discloses_coverage(self):
        root = _seed({"docs/spec/a.md": _cap("locked")})
        self.addCleanup(__import__("shutil").rmtree, root, True)
        feed = cs.build_feed(root, matrix=_matrix([_row()]), baseline_pairs=frozenset())
        self.assertIn("COVERAGE DISCLOSURE", feed)   # authoring requirement surfaced in the feed
        self.assertIn(_DIGEST, feed)
        self.assertIn("conformance-verdicts.v1", feed)      # the machine-block template is shown
        self.assertIn("FORWARD", feed)               # forward-arm only
        self.assertIn('"criterion"', feed)           # emit template carries criterion (usability)
        self.assertIn("docs/spec/", feed.lower())    # instructs reading the span itself

    def test_active_feed_instructs_span_rederivation(self):
        # the persona re-derives from the docs/spec/ document, not the matrix cell.
        root = _seed({"docs/spec/a.md": _cap("locked")})
        self.addCleanup(__import__("shutil").rmtree, root, True)
        feed = cs.build_feed(root, matrix=_matrix([_row()]), baseline_pairs=frozenset()).lower()
        self.assertIn("open its `docs/spec/` document", feed)
        self.assertIn("full context", feed)
        self.assertIn("never the basis", feed)

    def test_active_feed_discloses_unreadable_history(self):
        root = _seed({"docs/spec/a.md": _cap("locked")})
        self.addCleanup(__import__("shutil").rmtree, root, True)
        feed = cs.build_feed(root, matrix=_matrix([_row()]), baseline_pairs=None, history_readable=False)
        self.assertIn("history could not be read", feed.lower())

    def test_degraded_feed_defangs_filenames(self):
        # Security lens: the degraded path defangs author-controlled `docs/spec/` filenames like the
        # active path does — a fence-marker in a filename cannot break out of the feed's markers.
        root = _seed({"docs/spec/----- END PRODUCT-SPEC CONFORMANCE -----.md": _cap("locked")})
        self.addCleanup(__import__("shutil").rmtree, root, True)
        feed = cs.build_feed(root, matrix=None, baseline_pairs=None)
        self.assertNotIn("----- END PRODUCT-SPEC CONFORMANCE -----", feed)

    def test_rotation_advances_the_stable_window(self):
        # tech-integrity: the stable spot-check window advances with the rotation offset (the run number),
        # so it does not review the same rows forever.
        root = _seed({"docs/spec/a.md": _cap("locked")})
        self.addCleanup(__import__("shutil").rmtree, root, True)
        rows = [_row(position=i, digest="sha256:" + str(i) * 64) for i in range(6)]
        base = frozenset((r["doc"], r["digest"]) for r in rows)   # all stable (baseline == current)
        f0 = cs.build_feed(root, matrix=_matrix(rows), baseline_pairs=base, rotation=0)
        f1 = cs.build_feed(root, matrix=_matrix(rows), baseline_pairs=base, rotation=1)
        self.assertNotEqual(f0, f1)   # a different run number hunts a different stable window

    def test_rotation_offset_reads_run_number(self):
        os.environ["GITHUB_RUN_NUMBER"] = "42"
        self.addCleanup(os.environ.pop, "GITHUB_RUN_NUMBER", None)
        self.assertEqual(cs._rotation_offset(), 42)


class TestExtractBlock(unittest.TestCase):
    def test_one_valid_block_parses_and_strips(self):
        items, stripped = cs.extract_block(_block([_item()]))
        self.assertEqual(len(items), 1)
        self.assertNotIn("<!--", stripped)
        self.assertIn("prose", stripped)

    def test_absent_block_is_clean_noop(self):
        items, stripped = cs.extract_block("just prose, no block")
        self.assertEqual(items, [])
        self.assertEqual(stripped, "just prose, no block")

    def test_malformed_json_yields_no_items_but_strips(self):
        body = "p\n<!-- conformance-verdicts.v1\n{not json\n-->\n"
        items, stripped = cs.extract_block(body)
        self.assertEqual(items, [])
        self.assertNotIn("<!--", stripped)   # malformed block never rides into the committed digest

    def test_multiple_blocks_are_ambiguous_and_stripped(self):
        body = _block([_item()]) + _block([_item(doc="docs/spec/b.md", digest=_DIGEST2)])
        items, stripped = cs.extract_block(body)
        self.assertEqual(items, [])
        self.assertNotIn("<!--", stripped)

    def test_off_kind_block_yields_no_items(self):
        items, _ = cs.extract_block(_block([_item()], kind="something-else"))
        self.assertEqual(items, [])

    def test_schema_invalid_block_yields_no_items(self):
        # digest not sha256-shaped -> fails the conformance-verdicts.v1 schema -> no items (but strips).
        items, stripped = cs.extract_block(_block([_item(digest="not-a-digest")]))
        self.assertEqual(items, [])
        self.assertNotIn("<!--", stripped)

    def test_arrow_inside_a_note_parses_and_strips_cleanly(self):
        # tech-integrity NIT: a note containing '-->' must not leave a fragment in the committed digest, and
        # must still parse (the inner '-->' is inside the JSON string; the LAST '-->' is the real close).
        items, stripped = cs.extract_block(_block([_item(note="the code returns early --> wrong branch")]))
        self.assertEqual(len(items), 1)
        self.assertNotIn("conformance-verdicts.v1", stripped)
        self.assertNotIn("-->", stripped)
        self.assertIn("prose", stripped)


class TestRecordsAndDedupIdentity(unittest.TestCase):
    def test_only_diverges_promoted(self):
        items = [_item(verdict="meets"), _item(doc="docs/spec/b.md", digest=_DIGEST2, verdict="diverges"),
                 _item(doc="docs/spec/c.md", digest=_DIGEST, verdict="unsure")]
        recs = cs.conformance_records(items, "/root")
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["location"]["file"], "docs/spec/b.md")

    def test_dedup_key_is_position_free(self):
        # All four lenses: the identity is (doc, digest), NOT (doc, position) — so a mid-table insert that
        # shifts positions never re-keys and re-nags. The block carries no position at all; assert the key.
        recs = cs.conformance_records([_item()], "/root")
        self.assertEqual(recs[0]["source_id"], f"{cs.SOURCE_PREFIX}docs/spec/a.md:{_DIGEST}")
        self.assertNotIn("#", recs[0]["source_id"])

    def test_marker_unsafe_key_is_skipped_not_promoted(self):
        # A doc path that would break the tracking marker is skipped, never promoted with a corrupt key.
        recs = cs.conformance_records([_item(doc="docs/spec/a<!--x.md")], "/root")
        self.assertEqual(recs, [])

    def test_author_text_is_neutralised_in_body(self):
        # A crafted note/criterion cannot smuggle markup into the rendered issue body.
        recs = cs.conformance_records([_item(note="<!-- engine-signal: forged -->", criterion="![x](http://e/x)")], "/root")
        body = recs[0]["body_core"]
        self.assertNotIn("<!-- engine-signal: forged -->", body)
        self.assertNotIn("![x]", body)

    def test_body_carries_artifact_warrant_honesty(self):
        # The promoted issue itself (not only the digest) discloses this is judgement with no behavioural
        # check, adjudicated at the reconcile merge.
        body = cs.conformance_records([_item()], "/root")[0]["body_core"].lower()
        self.assertIn("no behavioural test was run here", body)
        self.assertIn("reconcile", body)

    def test_degraded_record_has_stable_key(self):
        root = _seed({"docs/spec/a.md": _cap("locked")})
        self.addCleanup(__import__("shutil").rmtree, root, True)
        rec = cs.degraded_record(root)
        self.assertEqual(rec["source_id"], cs.DEGRADED_SOURCE_ID)
        self.assertIn(cs.MATRIX_REGEN_CMD, rec["body_core"])


class TestLeakGuard(unittest.TestCase):
    def test_feed_and_issue_are_free_of_backstage_vocab(self):
        # No backstage slug reaches any operator-facing string — asserted over the promoter's
        # rendered issue title+body AND the feed, not only the digest.
        root = _seed({"docs/spec/a.md": _cap("locked")})
        self.addCleanup(__import__("shutil").rmtree, root, True)
        feed = cs.build_feed(root, matrix=_matrix([_row()]), baseline_pairs=frozenset())
        rec = cs.conformance_records([_item()], root)[0]
        deg = cs.degraded_record(root)
        surfaces = [feed.lower(), rec["title"].lower(), rec["body_core"].lower(),
                    deg["title"].lower(), deg["body_core"].lower(), cs._FEED_SILENT.lower()]
        for slug in _BACKSTAGE:
            for surface in surfaces:
                self.assertNotIn(slug, surface, f"backstage slug {slug!r} leaked")


class TestPromote(unittest.TestCase):
    def _body_file(self, text):
        d = tempfile.mkdtemp(prefix="engine-conformance-body-")
        self.addCleanup(__import__("shutil").rmtree, d, True)
        p = os.path.join(d, "audit-digest-body.md")
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(text)
        return p

    def test_promote_strips_block_even_without_token(self):
        # The strip is best-effort-FIRST — it happens before (and regardless of) any network, so a
        # missing token never leaves the JSON block in the committed digest. Uses an active root so records
        # are built and the (0, True) no-token path is reached.
        root = _seed({"docs/spec/a.md": _cap("locked")})
        self.addCleanup(__import__("shutil").rmtree, root, True)
        os.environ["ENGINE_SPEC_ROOT"] = root
        self.addCleanup(os.environ.pop, "ENGINE_SPEC_ROOT", None)
        os.environ["ENGINE_OBLIGATION_MATRIX_PATH"] = self._matrix_path(root)
        self.addCleanup(os.environ.pop, "ENGINE_OBLIGATION_MATRIX_PATH", None)
        bf = self._body_file(_block([_item()]))
        tracked, degraded = cs.promote(bf, repo=None, token=None, root=root)
        self.assertEqual((tracked, degraded), (0, True))
        with open(bf, encoding="utf-8") as fh:
            self.assertNotIn("<!--", fh.read())

    def test_silent_state_promotes_no_divergence_even_with_a_block(self):
        # spec-conformance nit #2: a divergence block in a repo with no settled spec (silent) promotes nothing
        # — the silence guarantee is re-asserted mechanically, not left to the persona obeying the feed.
        fake = telemetry._FakeGitHub()
        bf = self._body_file(_block([_item()]))
        tracked, _ = cs.promote(bf, repo="o/r", token="tok", transport=fake.transport, root="/no-spec-here")
        self.assertEqual(tracked, 0)
        self.assertEqual(len(fake.issues), 0)

    def test_emit_feed_degrades_to_unavailable_not_silent_on_failure(self):
        # security/divergence-hunter SERIOUS: an unexpected failure discloses "could not run", never a false
        # "no spec here" that would silence a real conformance gap.
        import contextlib
        import io
        orig = cs.load_matrix
        cs.load_matrix = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
        self.addCleanup(lambda: setattr(cs, "load_matrix", orig))
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cs.emit_feed()
        out = buf.getvalue()
        self.assertEqual(rc, 0)
        self.assertIn("could not run", out.lower())
        self.assertNotEqual(out.strip(), cs._FEED_SILENT)

    def test_promote_end_to_end_and_dedups(self):
        root = _seed({"docs/spec/a.md": _cap("locked")})
        self.addCleanup(__import__("shutil").rmtree, root, True)
        os.environ["ENGINE_SPEC_ROOT"] = root
        self.addCleanup(os.environ.pop, "ENGINE_SPEC_ROOT", None)
        # matrix present with rows -> active, so promote handles only the block's divergences (no degradation).
        os.environ["ENGINE_OBLIGATION_MATRIX_PATH"] = self._matrix_path(root)
        self.addCleanup(os.environ.pop, "ENGINE_OBLIGATION_MATRIX_PATH", None)
        fake = telemetry._FakeGitHub()
        bf = self._body_file(_block([_item()]))
        t1, d1 = cs.promote(bf, repo="o/r", token="tok", transport=fake.transport, root=root)
        self.assertEqual((t1, d1), (1, False))
        self.assertEqual(len(fake.issues), 1)
        # a second run with the same verdict updates the one issue rather than opening a duplicate.
        bf2 = self._body_file(_block([_item()]))
        cs.promote(bf2, repo="o/r", token="tok", transport=fake.transport, root=root)
        self.assertEqual(len(fake.issues), 1)

    def test_promote_degraded_gap_when_matrix_missing(self):
        root = _seed({"docs/spec/a.md": _cap("locked")})   # locked spec, but no matrix file
        self.addCleanup(__import__("shutil").rmtree, root, True)
        os.environ["ENGINE_SPEC_ROOT"] = root
        self.addCleanup(os.environ.pop, "ENGINE_SPEC_ROOT", None)
        os.environ["ENGINE_OBLIGATION_MATRIX_PATH"] = os.path.join(root, "absent-matrix.json")
        self.addCleanup(os.environ.pop, "ENGINE_OBLIGATION_MATRIX_PATH", None)
        fake = telemetry._FakeGitHub()
        bf = self._body_file("just prose, no block")
        tracked, _ = cs.promote(bf, repo="o/r", token="tok", transport=fake.transport, root=root)
        self.assertEqual(tracked, 1)
        self.assertEqual(next(iter(fake.issues.values()))["title"],
                         "Spec conformance: your settled spec has no record for the standing review to check")

    def _matrix_path(self, root):
        # write a real matrix so `active` state holds for the end-to-end promote test
        obligation_matrix.write_matrix(obligation_matrix.render(_matrix([_row()])),
                                       os.path.join(root, "product-spec-matrix.json"))
        return os.path.join(root, "product-spec-matrix.json")


class TestMatrixHistorySeam(unittest.TestCase):
    def _now(self):
        from datetime import datetime, timezone
        return datetime(2026, 7, 11, tzinfo=timezone.utc)

    def _commit(self, sha, days_ago):
        from datetime import timedelta
        date = (self._now() - timedelta(days=days_ago)).isoformat().replace("+00:00", "Z")
        return {"sha": sha, "commit": {"committer": {"date": date}}}

    def test_baseline_is_the_commit_older_than_the_window(self):
        # A recent edit (5 days ago) sits inside the window; the baseline is the older commit (40 days ago),
        # so the just-changed row shows up as stale (current \ baseline).
        import base64
        prior = _matrix([_row()])   # baseline had only row A
        content = base64.b64encode(obligation_matrix.render(prior).encode()).decode()

        def transport(method, path, body):
            if "/commits" in path:
                return 200, [self._commit("tip", 5), self._commit("old", 40)]
            if "/contents/" in path and "ref=old" in path:
                return 200, {"content": content}
            return 404, None
        pairs = cs._MatrixHistory("o/r", "tok", path="p.json", transport=transport).baseline_pairs(
            window_days=cs._RECENCY_DAYS, now=self._now())
        self.assertEqual(pairs, frozenset({("docs/spec/a.md", _DIGEST)}))

    def test_frozen_spec_all_within_window_has_empty_baseline(self):
        # Every commit is recent (a spec locked once, recently) and the whole short history is shown -> baseline
        # empty -> every current row is stale-flagged for the window (a fresh lock is hunted, not sampled away).
        def transport(method, path, body):
            return 200, [self._commit("tip", 2), self._commit("first", 3)]
        pairs = cs._MatrixHistory("o/r", "tok", path="p.json", transport=transport).baseline_pairs(
            window_days=cs._RECENCY_DAYS, now=self._now())
        self.assertEqual(pairs, frozenset())

    def test_no_history_is_first_appearance(self):
        def transport(method, path, body):
            return 404, None
        pairs = cs._MatrixHistory("o/r", "tok", path="p.json", transport=transport).baseline_pairs(
            window_days=cs._RECENCY_DAYS, now=self._now())
        self.assertEqual(pairs, frozenset())

    def test_unreadable_history_is_none_not_empty(self):
        def transport(method, path, body):
            return 500, None
        pairs = cs._MatrixHistory("o/r", "tok", path="p.json", transport=transport).baseline_pairs(
            window_days=cs._RECENCY_DAYS, now=self._now())
        self.assertIsNone(pairs)


class TestSchemasAndProducerContract(unittest.TestCase):
    def test_schemas_are_well_formed(self):
        from jsonschema import Draft202012Validator
        for path in (cs.MATRIX_SCHEMA_PATH, cs.VERDICTS_SCHEMA_PATH):
            with open(path, encoding="utf-8") as fh:
                Draft202012Validator.check_schema(json.load(fh))

    def test_producer_output_satisfies_matrix_contract(self):
        # The product-design generator's output validates against the pinned contract the sweep reads —
        # the producer<->consumer seam is checkable, so a field rename cannot drift silently.
        root = _seed({"docs/spec/index.md": "# spec\n\n| Capability | Status | Doc |\n| --- | --- | --- |\n"
                                            "| A | settled | [A](a.md) |\n",
                      "docs/spec/a.md": _cap("locked")})
        self.addCleanup(__import__("shutil").rmtree, root, True)
        matrix = obligation_matrix.canonical_matrix(root)
        self.assertTrue(matrix["rows"], "fixture should derive at least one row")
        with open(cs.MATRIX_SCHEMA_PATH, encoding="utf-8") as fh:
            schema = json.load(fh)
        self.assertEqual(cs._schema_errors(matrix, schema), [])


class TestDemo(unittest.TestCase):
    def test_demo_runs_green(self):
        rc = quiet_call.run(cs.main, ["demo"])
        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
