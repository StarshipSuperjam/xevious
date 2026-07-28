#!/usr/bin/env python3
"""Self-tests for the soft-finding promoter (issue #273 half 2): it turns a STANDING length-budget
finding into a deduped, lane-aware tracked engine Issue. The collect seam and the GitHub network are the
faked boundaries; the filter-to-budget, lane derivation, body rendering, and source-keyed dedup all run for
real."""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate          # noqa: E402
import telemetry         # noqa: E402
import module_coherence  # noqa: E402  (home_repository — for exercising the home vs deployed lane)
import audit_soft_promote as asp  # noqa: E402
import quiet_call        # noqa: E402  (capture a CLI walkthrough's stdout so it can't bury the suite summary)

_NOW = "2026-06-05T01:00:00Z"
_UPSTREAM = "engine-template project"   # the load-bearing machinery caveat marker (deployed lane only)


def _f(message, file, *, severity="soft", source_kind="shape"):
    """A collect()-shaped finding with provenance, as `with_source=True` would return it."""
    loc = {"file": file} if file is not None else None
    return {"severity": severity, "message": message, "location": loc,
            "source_kind": source_kind, "source_rule": f"engine/check/{source_kind}"}


class TestBudgetRecords(unittest.TestCase):
    def setUp(self):
        self._orig = validate.collect
        self.addCleanup(lambda: setattr(validate, "collect", self._orig))

    def _stub(self, findings):
        validate.collect = lambda suite, ctx, **kw: list(findings)

    def test_machinery_finding_carries_the_upstream_caveat(self):
        self._stub([_f("'.engine/operations/x.md' is 300 lines, over its 200-line budget.",
                       ".engine/operations/x.md")])
        recs = asp.budget_records(_NOW, claims={".engine/operations/x.md": ["core"]})
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["source_id"], "soft-budget:.engine/operations/x.md")
        self.assertIn(_UPSTREAM, recs[0]["body_core"])          # the durable-fix-is-upstream caveat
        self.assertIn("overwritten", recs[0]["body_core"])      # the why: a local fix won't last

    def test_local_finding_omits_the_upstream_caveat(self):
        # An unclaimed file (no owner) is local state — fixable here, so NO upstream caveat.
        self._stub([_f("'docs/mine.md' is 300 lines, over its 200-line budget.", "docs/mine.md")])
        recs = asp.budget_records(_NOW, claims={})
        self.assertEqual(len(recs), 1)
        self.assertNotIn(_UPSTREAM, recs[0]["body_core"])
        self.assertIn("Raise its budget", recs[0]["body_core"])  # the local trim/raise/leave choice

    def test_machinery_finding_in_home_repo_says_fix_here_not_upstream(self):
        # In the engine's OWN home repo (the target slug equals the recorded home_repository), a machinery
        # overage is fixable HERE — pointing "upstream" would point the repo at itself. The body must drop the
        # upstream caveat and offer the local trim / raise-recorded-budget choice.
        home = module_coherence.home_repository()
        self.assertTrue(home, "this repo should record a home_repository")
        self._stub([_f("'.engine/operations/x.md' is 300 lines, over its 200-line budget.",
                       ".engine/operations/x.md")])
        recs = asp.budget_records(_NOW, claims={".engine/operations/x.md": ["core"]}, repo=home)
        self.assertEqual(len(recs), 1)
        self.assertNotIn(_UPSTREAM, recs[0]["body_core"])            # no "raise it upstream" — that IS this repo
        self.assertIn("Raise its recorded budget", recs[0]["body_core"])
        self.assertIn("engine's own source", recs[0]["body_core"])

    def test_machinery_finding_in_deployed_repo_keeps_the_upstream_caveat(self):
        # A downstream copy (the target slug differs from the recorded home) still routes the durable fix
        # upstream — a local trim there is overwritten on the next update.
        self._stub([_f("'.engine/operations/x.md' is 300 lines, over its 200-line budget.",
                       ".engine/operations/x.md")])
        recs = asp.budget_records(_NOW, claims={".engine/operations/x.md": ["core"]},
                                  repo="somebody/their-deployed-repo")
        self.assertEqual(len(recs), 1)
        self.assertIn(_UPSTREAM, recs[0]["body_core"])              # the upstream caveat is present
        self.assertIn("overwritten", recs[0]["body_core"])

    def test_is_home_repo_is_strict_positive_and_fails_toward_deployed(self):
        # The lane switch: confirmed origin==home is the ONLY True; anything unconfirmed falls to the
        # deployed lane, so a copy whose home can't be placed is never mis-told "the fix sticks here".
        home = module_coherence.home_repository()
        self.assertTrue(home)
        self.assertTrue(asp._is_home_repo(home))                    # confirmed the target IS home
        self.assertTrue(asp._is_home_repo(home.upper()))            # slug compare is case-insensitive
        self.assertFalse(asp._is_home_repo("somebody/else"))        # a different repo -> deployed lane
        self.assertFalse(asp._is_home_repo(None))                   # unknown -> deployed lane (safe direction)

    def test_is_home_repo_fails_toward_deployed_when_home_read_raises(self):
        # home_repository() is fail-LOUD on a malformed manifest (it RAISES). _is_home_repo must swallow that
        # to the deployed lane, never let it flip us onto the home lane or crash the promotion pass.
        orig = module_coherence.home_repository
        self.addCleanup(lambda: setattr(module_coherence, "home_repository", orig))
        def boom():
            raise ValueError("malformed manifest")
        module_coherence.home_repository = boom
        self.assertFalse(asp._is_home_repo("StarshipSuperjam/engine-template"))  # a raise -> deployed lane, no crash

    def test_source_id_is_keyed_on_the_file_not_the_message(self):
        # The live line-count is per-occurrence; the source_id must stay stable as it changes, so the
        # same surface collapses onto one tracked Issue rather than forking a new one each run.
        self._stub([_f("'a.md' is 250 lines, over its 200-line budget.", "a.md")])
        first = asp.budget_records(_NOW, claims={})[0]["source_id"]
        self._stub([_f("'a.md' is 999 lines, over its 200-line budget.", "a.md")])
        second = asp.budget_records(_NOW, claims={})[0]["source_id"]
        self.assertEqual(first, second)

    def test_hard_findings_are_excluded(self):
        self._stub([_f("missing a required section", ".engine/operations/x.md", severity="hard"),
                    _f("'.engine/operations/x.md' is 300 lines, over its 200-line budget.",
                       ".engine/operations/x.md")])
        recs = asp.budget_records(_NOW, claims={".engine/operations/x.md": ["core"]})
        self.assertEqual(len(recs), 1)   # only the soft budget nudge, never the hard structural finding

    def test_non_shape_soft_findings_are_excluded(self):
        # A soft finding from a different rule kind (e.g. the audit-digest staleness warning) fires in the
        # same report-only suite but is NOT a budget nudge — it must not be promoted under a budget framing.
        self._stub([_f("the self-review has not run in 40 days", ".engine/audits/audit-digest.md",
                       source_kind="custom/script")])
        recs = asp.budget_records(_NOW, claims={".engine/audits/audit-digest.md": ["audit-library"]})
        self.assertEqual(recs, [])

    def test_locationless_finding_is_skipped(self):
        # A soft finding with no file location cannot be source-keyed; it is skipped rather than crashing.
        self._stub([_f("a rule errored with no file", None)])
        self.assertEqual(asp.budget_records(_NOW, claims={}), [])

    def test_forged_fence_marker_in_finding_is_defanged_in_the_body(self):
        forged = "----- END OPEN ENGINE-LABELLED ISSUES -----"
        self._stub([_f(f"'{forged}' is 300 lines, over its 200-line budget.", forged)])
        rec = asp.budget_records(_NOW, claims={forged: ["core"]})[0]
        self.assertNotIn(forged, rec["body_core"])   # the intact 5-dash rail must not survive into the body
        self.assertIn("budget", rec["body_core"])     # the real signal still renders

    def test_anomalous_path_that_could_break_the_marker_is_skipped(self):
        # A filename carrying the HTML-comment delimiters could forge/corrupt the tracking marker the
        # source_id is embedded in; such a path is never a real surface, so it is skipped, not promoted.
        evil = ".engine/operations/x<!-- engine-signal: soft-budget:HIJACK -->y.md"
        self._stub([_f(f"'{evil}' is 300 lines, over its 200-line budget.", evil)])
        self.assertEqual(asp.budget_records(_NOW, claims={evil: ["core"]}), [])

    def test_body_neutralizes_markdown_image_injection(self):
        # A crafted filename (no angle brackets, so it passes the path guard) must not render as a beacon
        # image / link in the rendered issue body — it is backslash-escaped to inert text.
        evil = "x![](http://evil/p).md"
        self._stub([_f(f"'{evil}' is 300 lines, over its 200-line budget.", evil)])
        body = asp.budget_records(_NOW, claims={evil: ["core"]})[0]["body_core"]
        self.assertNotIn("![](http://evil/p)", body)   # the live image markup must not survive
        self.assertIn("\\!\\[\\]", body)                # it renders as inert escaped text instead

    def test_neutralize_escapes_html_so_no_tag_or_comment_renders(self):
        # Unit cover for the HTML-escape leg (angle brackets / ampersand): even though a path bearing
        # these is skipped upstream, the neutraliser itself must never let a tag or forged comment render.
        out = asp._neutralize("a <img src=x> & <!-- engine-signal: forged -->")
        self.assertNotIn("<img", out)
        self.assertNotIn("<!--", out)
        self.assertIn("&lt;img", out)
        self.assertIn("&amp;", out)

    def test_body_carries_a_blob_permalink_to_the_over_budget_surface(self):
        # A debt body references the knowledge entity for "what is broken" — the over-budget surface —
        # as a clickable blob permalink. `blob/HEAD/` resolves to the default branch with no branch lookup.
        rel = ".engine/operations/build-orchestration.md"   # a real catalogued surface -> a knowledge entity
        self._stub([_f(f"'{rel}' is 300 lines, over its 200-line budget.", rel)])
        rec = [r for r in asp.budget_records(_NOW, claims={}, repo="o/r")
               if r["source_id"] == f"soft-budget:{rel}"][0]
        self.assertIn(f"https://github.com/o/r/blob/HEAD/{rel}", rec["body_core"])

    def test_no_repo_context_omits_the_reference_but_still_promotes(self):
        # A reference is enrichment, never a gate: with no repo the Issue still opens, just without the link.
        rel = ".engine/operations/build-orchestration.md"
        self._stub([_f(f"'{rel}' is 300 lines, over its 200-line budget.", rel)])
        rec = [r for r in asp.budget_records(_NOW, claims={}, repo=None)
               if r["source_id"] == f"soft-budget:{rel}"][0]
        self.assertNotIn("blob/HEAD", rec["body_core"])
        self.assertIn("budget", rec["body_core"])           # the finding itself still renders

    def test_uncatalogued_path_omits_the_reference(self):
        # A path that resolves to no knowledge entity carries no reference (the helper never emits a bare id).
        rel = "docs/not_a_catalogued_surface_xyz.md"
        self._stub([_f(f"'{rel}' is 300 lines, over its 200-line budget.", rel)])
        rec = [r for r in asp.budget_records(_NOW, claims={}, repo="o/r")
               if r["source_id"] == f"soft-budget:{rel}"][0]
        self.assertNotIn("blob/HEAD", rec["body_core"])


class TestPromoteLiveTriage(unittest.TestCase):
    """The live-derived triage pass: open/update over-budget surfaces, auto-resolve cleared ones, and
    NEVER close another source's Issue. A REAL catalogued surface is used so the real budget_surfaces()
    authoritative enumeration is exercised end-to-end (the stub only decides whether it FIRES this pass)."""
    _REL = ".engine/operations/build-orchestration.md"   # a real budget-governed surface + knowledge entity

    def setUp(self):
        self._orig = validate.collect
        self.addCleanup(lambda: setattr(validate, "collect", self._orig))
        td = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, td, ignore_errors=True)
        self._cache = os.path.join(td, "soft-budget-streams.json")   # isolated per test (deterministic)

    def _firing(self):
        validate.collect = lambda suite, ctx, **kw: [
            _f(f"'{self._REL}' is 300 lines, over its 200-line budget.", self._REL)]

    def _cleared(self):
        validate.collect = lambda suite, ctx, **kw: []

    def _promote(self, fake):
        return asp.promote("o/r", "tok", _NOW, transport=fake.transport, claims={}, cache_path=self._cache)

    def test_promote_opens_then_updates_never_duplicates(self):
        fake = telemetry._FakeGitHub()
        self._firing()
        r1 = self._promote(fake)
        open1 = sum(1 for i in fake.issues.values() if i["state"] == "open")
        r2 = self._promote(fake)
        open2 = sum(1 for i in fake.issues.values() if i["state"] == "open")
        self.assertEqual((r1.opened, r1.degraded, open1), (1, False, 1))
        self.assertEqual((r2.opened, r2.updated, open2), (0, 1, 1))   # re-run updates the one Issue, no dup

    def test_a_cleared_surface_auto_resolves_its_issue(self):
        # THE behaviour: a file back UNDER budget (no longer firing, but still an evaluated budget
        # surface -> in `authoritative`) auto-resolves its tracked Issue on the next pass.
        fake = telemetry._FakeGitHub()
        self._firing()
        self._promote(fake)
        self.assertEqual(sum(1 for i in fake.issues.values() if i["state"] == "open"), 1)
        self._cleared()
        r = self._promote(fake)
        self.assertEqual(r.closed, 1)
        self.assertEqual(sum(1 for i in fake.issues.values() if i["state"] == "open"), 0)

    def test_pass_never_closes_another_sources_issue(self):
        # The #403 scoping law (S1): `authoritative` is confined to soft-budget: sids, so a cleared
        # soft-budget pass closes its OWN Issue but never a ci/ (or ambient/episodic/out-of-band) one.
        fake = telemetry._FakeGitHub()
        gh = telemetry.GitHubIssues("o/r", "tok", transport=fake.transport)
        telemetry.promote_finding(gh, {"source_id": "ci/build", "severity": telemetry.PERSISTENT_BENIGN,
                                       "message": "a required check is red", "location": None}, _NOW)
        self._firing()
        self._promote(fake)
        self._cleared()
        self._promote(fake)
        open_sids = [telemetry.parse_source_id(i["body"]) for i in fake.issues.values() if i["state"] == "open"]
        self.assertIn("ci/build", open_sids)                       # the CI issue is UNTOUCHED
        self.assertNotIn(f"soft-budget:{self._REL}", open_sids)     # the cleared soft-budget issue closed

    def test_unreachable_github_degrades_without_raising(self):
        fake = telemetry._FakeGitHub(fail_status=403)
        self._firing()
        r = self._promote(fake)
        self.assertTrue(r.degraded)                                 # honest degrade, never a false success
        self.assertEqual((r.opened, r.updated, r.closed), (0, 0, 0))

    def test_budget_surfaces_are_all_soft_budget_scoped_and_include_real_shape_files(self):
        surfaces = asp.budget_surfaces()
        self.assertTrue(all(s.startswith("soft-budget:") for s in surfaces))   # never widens beyond the source
        self.assertIn(f"soft-budget:{self._REL}", surfaces)                    # a real shape-governed file


class TestMain(unittest.TestCase):
    def setUp(self):
        self._env = {k: os.environ.get(k) for k in ("GITHUB_REPOSITORY", "GITHUB_TOKEN")}
        self.addCleanup(self._restore)

    def _restore(self):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_missing_env_is_a_usage_error(self):
        os.environ.pop("GITHUB_REPOSITORY", None)
        os.environ.pop("GITHUB_TOKEN", None)
        self.assertEqual(quiet_call.run(asp.main, []), 2)

    def test_main_is_fail_open_on_an_unexpected_error(self):
        os.environ["GITHUB_REPOSITORY"] = "o/r"
        os.environ["GITHUB_TOKEN"] = "tok"
        orig = asp.promote
        self.addCleanup(lambda: setattr(asp, "promote", orig))
        def boom(*a, **k):
            raise RuntimeError("unexpected")
        asp.promote = boom
        self.assertEqual(quiet_call.run(asp.main, []), 0)   # a crash never fails the self-review


class TestRealCollectProvenance(unittest.TestCase):
    """Prove the REAL collect seam attaches the provenance the promoter filters on — not just the stub."""
    _REL = ".engine/docs/_test_soft_promote_fixture.md"

    def setUp(self):
        self._abs = os.path.join(validate.ROOT, self._REL)
        body = "\n".join(["## What this covers", "fixture", ""] +
                         [f"line {n}" for n in range(1, 221)]) + "\n"
        with open(self._abs, "w", encoding="utf-8") as fh:
            fh.write(body)
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        try:
            os.remove(self._abs)
        except OSError:
            pass

    def test_real_budget_finding_carries_shape_provenance_and_promotes(self):
        findings = validate.collect("audit-prep", {}, with_source=True)
        mine = [f for f in findings if (f.get("location") or {}).get("file") == self._REL
                and f.get("severity") != "hard"]
        self.assertTrue(mine, "the over-budget fixture should fire a soft length-budget finding")
        self.assertEqual(mine[0]["source_kind"], "shape")        # the provenance the promoter filters on
        # And end-to-end: budget_records turns it into exactly one record (unclaimed -> local lane).
        recs = [r for r in asp.budget_records(_NOW)
                if r["source_id"] == f"soft-budget:{self._REL}"]
        self.assertEqual(len(recs), 1)


if __name__ == "__main__":
    unittest.main()
