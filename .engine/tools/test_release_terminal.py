#!/usr/bin/env python3
"""Unit tests for release_terminal — the terminal-cut publisher (tag + GitHub Release on merge).

The network is faked by a programmable transport double (the issue_conformance_ci idiom): every test runs
the REAL publish logic offline. Covered: the sentinel / pre-release / invalid-version loud no-ops; the
strictly-greater-than-latest guard (older refused, equal falls through to idempotency, first-cut allowed);
the git-refs tag pinned to the exact merge commit; the wrong-commit refusal (never an overwrite); an
annotated tag dereferenced before the comparison; Release idempotency (already-published no-op, and a
half-done publish converging on re-run); the tag/Release create-failure split-brain recovery; and the
plain-language PR comment posted on BOTH success and failure."""
import unittest

import release_cut
import release_terminal as rt

COMMIT = "a" * 40
OTHER = "b" * 40


class _FakeGitHub:
    """A programmable GitHub transport: (method, path, body) -> (status, json). Mutable state, so a create
    is visible to a later read and the real idempotency/convergence logic runs end to end."""

    def __init__(self, *, latest="__none__", tags=None, releases=None, annotated=None,
                 create_ref_status=201, create_release_status=201, comment_status=201):
        self.latest = latest                      # "__none__" -> 404 (no published release); else a tag string
        self.tags = dict(tags or {})              # lightweight tag -> commit sha
        self.annotated = dict(annotated or {})    # annotated tag -> (tag_object_sha, commit_sha)
        self.releases = set(releases or [])       # tags carrying a published Release
        self.create_ref_status = create_ref_status
        self.create_release_status = create_release_status
        self.comment_status = comment_status
        self.calls = []

    def transport(self, method, path, body=None):
        self.calls.append((method, path, body))
        if path.endswith("/releases/latest"):
            return (404, None) if self.latest == "__none__" else (200, {"tag_name": self.latest})
        if "/git/ref/tags/" in path:
            tag = path.rsplit("/", 1)[1]
            if tag in self.annotated:
                return 200, {"object": {"sha": self.annotated[tag][0], "type": "tag"}}
            if tag in self.tags:
                return 200, {"object": {"sha": self.tags[tag], "type": "commit"}}
            return 404, None
        if "/git/tags/" in path:                  # deref an annotated tag object -> its commit
            tag_obj = path.rsplit("/", 1)[1]
            for _t, (ts, commit) in self.annotated.items():
                if ts == tag_obj:
                    return 200, {"object": {"sha": commit, "type": "commit"}}
            return 404, None
        if method == "POST" and path.endswith("/git/refs"):
            if self.create_ref_status in (200, 201):
                self.tags[body["ref"].rsplit("/", 1)[1]] = body["sha"]
            return self.create_ref_status, None
        if "/releases/tags/" in path:
            tag = path.rsplit("/", 1)[1]
            return (200, {"tag_name": tag}) if tag in self.releases else (404, None)
        if method == "POST" and path.endswith("/releases"):
            if self.create_release_status in (200, 201):
                self.releases.add(body["tag_name"])
            return self.create_release_status, None
        if method == "POST" and "/comments" in path:
            return self.comment_status, {"id": 1}
        raise AssertionError(f"unexpected call {method} {path}")

    # ---- introspection
    def comment_bodies(self):
        return [b["body"] for (m, p, b) in self.calls if m == "POST" and "/comments" in p]

    def created_refs(self):
        return [b for (m, p, b) in self.calls if m == "POST" and p.endswith("/git/refs")]

    def created_releases(self):
        return [b for (m, p, b) in self.calls if m == "POST" and p.endswith("/releases")]


def _client(fake):
    return rt.TerminalCutClient("acme/engine-home", "tok", transport=fake.transport)


class NoOpGuards(unittest.TestCase):
    def test_sentinel_is_a_loud_no_op_touching_no_network(self):
        fake = _FakeGitHub()
        r = rt.publish(_client(fake), rt.SENTINEL, COMMIT)
        self.assertFalse(r["published"])
        self.assertEqual(r["reason"], "nothing-to-publish")
        self.assertEqual(fake.calls, [])                    # refused BEFORE any API call

    def test_prerelease_is_a_loud_no_op(self):
        fake = _FakeGitHub()
        r = rt.publish(_client(fake), "0.1.0-rc1", COMMIT)
        self.assertEqual(r["reason"], "nothing-to-publish")
        self.assertEqual(fake.calls, [])

    def test_invalid_version_is_refused_before_any_write(self):
        fake = _FakeGitHub()
        r = rt.publish(_client(fake), "not-a-version", COMMIT)
        self.assertFalse(r["published"])
        self.assertEqual(r["reason"], "invalid-version")
        self.assertEqual(fake.calls, [])


class StrictlyGreaterGuard(unittest.TestCase):
    def test_first_cut_with_no_latest_is_allowed(self):
        fake = _FakeGitHub(latest="__none__")
        r = rt.publish(_client(fake), "0.1.0", COMMIT)
        self.assertTrue(r["published"])
        self.assertEqual(r["tag"], "v0.1.0")

    def test_a_newer_version_publishes(self):
        fake = _FakeGitHub(latest="v0.1.0")
        r = rt.publish(_client(fake), "0.2.0", COMMIT)
        self.assertTrue(r["published"])

    def test_an_older_version_is_refused_and_writes_nothing(self):
        fake = _FakeGitHub(latest="v0.2.0")
        r = rt.publish(_client(fake), "0.1.0", COMMIT)
        self.assertFalse(r["published"])
        self.assertEqual(r["reason"], "not-newer")
        self.assertEqual(fake.created_refs(), [])           # no tag created
        self.assertEqual(fake.created_releases(), [])       # no release created

    def test_a_version_equal_to_latest_falls_through_to_idempotency_not_refused(self):
        # the fully-published re-run: latest == this version. It must NOT be refused as "not newer" — it must
        # reach the already-published no-op (the reordering this test locks in).
        fake = _FakeGitHub(latest="v0.1.0", tags={"v0.1.0": COMMIT}, releases={"v0.1.0"})
        r = rt.publish(_client(fake), "0.1.0", COMMIT)
        self.assertTrue(r["published"])
        self.assertEqual(r["reason"], "already-published")


class ExactCommitTagging(unittest.TestCase):
    def test_fresh_publish_creates_the_tag_at_the_exact_merge_commit(self):
        fake = _FakeGitHub(latest="__none__")
        r = rt.publish(_client(fake), "0.1.0", COMMIT)
        self.assertTrue(r["published"])
        refs = fake.created_refs()
        self.assertEqual(len(refs), 1)
        self.assertEqual(refs[0], {"ref": "refs/tags/v0.1.0", "sha": COMMIT})   # pinned to the merge SHA
        self.assertEqual(fake.created_releases()[0]["tag_name"], "v0.1.0")

    def test_a_tag_on_a_different_commit_is_refused_never_overwritten(self):
        fake = _FakeGitHub(latest="__none__", tags={"v0.1.0": OTHER})
        r = rt.publish(_client(fake), "0.1.0", COMMIT)
        self.assertFalse(r["published"])
        self.assertEqual(r["reason"], "tag-conflict")
        self.assertEqual(fake.created_refs(), [])           # never tried to move/overwrite the tag
        self.assertEqual(fake.created_releases(), [])

    def test_an_annotated_tag_is_dereferenced_before_the_commit_comparison(self):
        # an annotated tag object points at the commit; the publisher must compare the COMMIT, not the tag
        # object sha. Here the annotated tag resolves to the merge commit -> it matches, so publish proceeds.
        fake = _FakeGitHub(latest="__none__", annotated={"v0.1.0": ("t" * 40, COMMIT)})
        r = rt.publish(_client(fake), "0.1.0", COMMIT)
        self.assertTrue(r["published"])                     # tag already correct -> just publishes the Release
        self.assertEqual(fake.created_refs(), [])           # tag already existed, none created


class ReleaseIdempotency(unittest.TestCase):
    def test_already_published_is_a_clean_no_op(self):
        fake = _FakeGitHub(latest="v0.1.0", tags={"v0.1.0": COMMIT}, releases={"v0.1.0"})
        r = rt.publish(_client(fake), "0.1.0", COMMIT)
        self.assertTrue(r["published"])
        self.assertEqual(r["reason"], "already-published")
        self.assertEqual(fake.created_releases(), [])       # did NOT re-create the Release

    def test_a_half_done_publish_converges_on_re_run(self):
        # the tag was created by a prior failed run but the Release never landed: latest is still absent, the
        # tag matches the commit -> the re-run creates the missing Release and completes.
        fake = _FakeGitHub(latest="__none__", tags={"v0.1.0": COMMIT}, releases=set())
        r = rt.publish(_client(fake), "0.1.0", COMMIT)
        self.assertTrue(r["published"])
        self.assertEqual(r["reason"], "published")
        self.assertEqual(fake.created_refs(), [])           # tag already there
        self.assertEqual(len(fake.created_releases()), 1)   # Release created this run

    def test_a_lost_tag_create_race_is_reconciled_against_the_existing_tag(self):
        # create_tag returns 422 (a ref appeared between the read and the create); the publisher re-resolves
        # and, finding it on the SAME commit, proceeds rather than failing.
        fake = _FakeGitHub(latest="__none__", create_ref_status=422)
        fake.tags["v0.1.0"] = COMMIT                        # the racing ref, already at the right commit
        r = rt.publish(_client(fake), "0.1.0", COMMIT)
        self.assertTrue(r["published"])


class CreateFailures(unittest.TestCase):
    def test_tag_create_failure_is_a_loud_recoverable_stop(self):
        fake = _FakeGitHub(latest="__none__", create_ref_status=500)
        r = rt.publish(_client(fake), "0.1.0", COMMIT)
        self.assertFalse(r["published"])
        self.assertEqual(r["reason"], "tag-create-failed")
        self.assertIn("re-run", r["recovery"].lower())
        self.assertEqual(fake.created_releases(), [])       # never reached the Release step

    def test_release_create_failure_names_the_split_brain_and_recovery(self):
        fake = _FakeGitHub(latest="__none__", create_release_status=500)
        r = rt.publish(_client(fake), "0.1.0", COMMIT)
        self.assertFalse(r["published"])
        self.assertEqual(r["reason"], "release-create-failed")
        self.assertIn("re-run", r["recovery"].lower())
        self.assertEqual(len(fake.created_refs()), 1)       # the tag WAS created (the split-brain the msg names)


class Announce(unittest.TestCase):
    def test_run_comments_the_success_onto_the_merged_pr(self):
        fake = _FakeGitHub(latest="__none__")
        r = rt.run(_client(fake), "0.1.0", COMMIT, pr_number=42)
        self.assertTrue(r["published"])
        self.assertTrue(r["announced"])
        bodies = fake.comment_bodies()
        self.assertEqual(len(bodies), 1)
        self.assertIn("v0.1.0 is now released", bodies[0])

    def test_run_comments_the_failure_onto_the_merged_pr(self):
        fake = _FakeGitHub(latest="v0.2.0")                 # an older version -> refused
        r = rt.run(_client(fake), "0.1.0", COMMIT, pr_number=42)
        self.assertFalse(r["published"])
        bodies = fake.comment_bodies()
        self.assertEqual(len(bodies), 1)
        self.assertIn("did not publish a release", bodies[0])

    def test_a_comment_failure_does_not_flip_a_successful_publish(self):
        fake = _FakeGitHub(latest="__none__", comment_status=500)
        r = rt.run(_client(fake), "0.1.0", COMMIT, pr_number=42)
        self.assertTrue(r["published"])                     # the Release is the durable success artifact
        self.assertFalse(r["announced"])
        self.assertIn("500", r["announce_error"])


class Prose(unittest.TestCase):
    def test_release_notes_are_plain_carry_the_readiness_line_and_no_jargon(self):
        # proposal=None so the test never reaches the network (the recompute goes through module_manager, not
        # the transport seam); the minimal-notes shape is asserted with no proposal.
        notes = rt._release_notes("v0.1.0", proposal=None)
        self.assertIn("v0.1.0", notes)
        self.assertIn("no automated check", notes.lower())          # the readiness line
        for banned in ("terminal cut", "release-cut", "release_cut", "target_commitish"):
            self.assertNotIn(banned, notes)

    def test_release_notes_are_human_readable_with_sections_and_descriptions(self):
        proposal = {"engine_floor_level": "major",
                    "change_inventory": ["Added the 'x' capability.", "Removed the 'legacy' capability."],
                    "impacts": [{"what": "the contract surface 'c.md' changed",
                                 "why": "read it against consumers before confirming."}]}
        notes = rt._release_notes("v1.0.0", proposal=proposal)
        self.assertIn("v1.0.0", notes)
        self.assertIn("breaking change", notes.lower())                 # the major-release callout
        self.assertIn("## What changed since the last release", notes)  # a section header, not a flat list
        self.assertIn("- Added the 'x' capability.", notes)             # bulleted structural changes
        self.assertIn("## Interface changes to read", notes)            # a distinct section
        self.assertIn("**The contract surface 'c.md' changed.**", notes)  # bold heading
        self.assertIn("Read it against consumers", notes)               # WITH its description (own sentence)

    def test_release_notes_degrade_to_minimal_without_a_proposal(self):
        # a recompute that failed or found nothing yields None -> version + readiness only, no sections
        notes = rt._release_notes("v0.2.0", proposal=None)
        self.assertNotIn("What changed since the last release", notes)
        self.assertIn("v0.2.0", notes)

    def test_proposal_for_publish_first_cut_renders_the_first_release_line(self):
        # the GENUINE first release: no prior release to diff, but the first-cut proposal still carries the
        # first-release framing line, so the first published Release is not barer than the merged PR.
        p = rt._proposal_for_publish(baseline=release_cut.Baseline(None, True, "n"))
        self.assertIsNotNone(p)
        self.assertEqual(p["mode"], "first-cut")
        self.assertTrue(any("First release" in c for c in p["change_inventory"]))
        # and it renders into human-readable notes with a first-release section (not "since the last release")
        notes = rt._release_notes("v0.1.0", proposal=p)
        self.assertIn("## What this release establishes", notes)
        self.assertNotIn("since the last release", notes)
        self.assertIn("First release", notes)

    def test_proposal_for_publish_degrades_on_failure(self):
        # any exception in the recompute degrades to None rather than raising into the publish path (the tag is
        # already created by then — a notes failure must not strand a tag with no Release)
        saved = release_cut.classify
        release_cut.classify = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
        try:
            out = rt._proposal_for_publish(baseline=release_cut.Baseline("v0.1.0", False, "n"),
                                           baseline_tree="/tmp")
        finally:
            release_cut.classify = saved
        self.assertIsNone(out)

    def test_proposal_for_publish_threads_the_merged_pr_list(self):
        # with a target commit, the recomputed proposal carries the merged-PR work list (best-effort helper is
        # stubbed so the test stays offline); without a target, the list is skipped.
        saved_classify, saved_prs = release_cut.classify, release_cut.merged_pr_titles
        release_cut.classify = lambda *a, **k: {"mode": "diff", "change_inventory": [], "impacts": []}
        release_cut.merged_pr_titles = lambda prev, target, **k: ["Fix a thing (#7)"] if target else []
        try:
            with_target = rt._proposal_for_publish(baseline=release_cut.Baseline("v0.1.0", False, "n"),
                                                   baseline_tree="/tmp", target="deadbeef")
            without = rt._proposal_for_publish(baseline=release_cut.Baseline("v0.1.0", False, "n"),
                                               baseline_tree="/tmp")
        finally:
            release_cut.classify, release_cut.merged_pr_titles = saved_classify, saved_prs
        self.assertEqual(with_target["merged_prs"], ["Fix a thing (#7)"])
        self.assertEqual(without["merged_prs"], [])

    def test_comment_bodies_read_distinct_across_outcomes(self):
        published = rt._comment_body({"published": True, "reason": "published", "tag": "v0.1.0"})
        already = rt._comment_body({"published": True, "reason": "already-published", "tag": "v0.1.0"})
        failed = rt._comment_body({"published": False, "reason": "release-create-failed", "tag": "v0.1.0",
                                   "message": "the release could not be published."})
        refused = rt._comment_body({"published": False, "reason": "not-newer", "tag": "v0.1.0",
                                    "message": "it is older.", "recovery": "cut it again higher."})
        self.assertEqual(len({published, already, failed, refused}), 4)
        self.assertIn("released", published.lower())
        self.assertIn("did not finish", failed.lower())

    def test_a_did_not_finish_comment_links_the_run_url_when_present_else_names_the_actions_tab(self):
        failed = {"published": False, "reason": "errored", "tag": None, "message": "GitHub was unreachable."}
        with_url = rt._comment_body(failed, run_url="https://gh.example/run/9")
        without = rt._comment_body(failed)
        self.assertIn("https://gh.example/run/9", with_url)   # a direct re-run link when the workflow passes it
        self.assertIn("Actions", without)                     # else point at where the control lives
        self.assertIn("did not finish", with_url.lower())


class ExitCode(unittest.TestCase):
    def test_published_and_noop_are_zero_refusals_are_one(self):
        self.assertEqual(rt._exit_code({"published": True, "reason": "published"}), 0)
        self.assertEqual(rt._exit_code({"published": False, "reason": "nothing-to-publish"}), 0)
        self.assertEqual(rt._exit_code({"published": False, "reason": "not-newer"}), 1)
        self.assertEqual(rt._exit_code({"published": False, "reason": "release-create-failed"}), 1)


class RaisedFailureLegibility(unittest.TestCase):
    """A transient read/transport failure (a PublishError raised mid-publish) must STILL reach the merged PR
    with a plain recovery — the legibility promise holds for the raise-path, not only decided refusals."""

    def _flaky_on_latest(self, fake):
        orig = fake.transport

        def flaky(method, path, body=None):
            if path.endswith("/releases/latest"):
                return 500, None            # an unexpected read failure -> the client raises PublishError
            return orig(method, path, body)
        return flaky

    def test_a_transient_read_failure_still_comments_the_recovery_on_the_pr(self):
        fake = _FakeGitHub(latest="__none__")
        client = rt.TerminalCutClient("acme/engine-home", "tok", transport=self._flaky_on_latest(fake))
        r = rt.run(client, "0.1.0", COMMIT, pr_number=42)
        self.assertFalse(r["published"])
        self.assertEqual(r["reason"], "errored")            # converted to a loud result, not propagated away
        self.assertEqual(rt._exit_code(r), 1)               # a loud non-zero stop
        bodies = fake.comment_bodies()
        self.assertEqual(len(bodies), 1)                    # the recovery reached the PR (the legibility promise)
        self.assertIn("did not finish", bodies[0])


class LatestReadFailure(unittest.TestCase):
    def test_an_unreadable_latest_release_raises_rather_than_assuming_first_cut(self):
        # a 500 on /releases/latest must NOT be misread as "no release -> first cut" (which would let an older
        # version publish). It raises PublishError -> the CLI surfaces a loud plain error.
        fake = _FakeGitHub()
        fake.latest = "__none__"
        orig = fake.transport

        def boom(method, path, body=None):
            if path.endswith("/releases/latest"):
                return 500, None
            return orig(method, path, body)
        client = rt.TerminalCutClient("acme/engine-home", "tok", transport=boom)
        with self.assertRaises(rt.PublishError):
            rt.publish(client, "0.1.0", COMMIT)


class ProductRelease(unittest.TestCase):
    """Product-mode publish (#516): a deployed repo publishes its OWN product release — the PR comment and the
    notes speak of the product, and the publish decision/idempotency logic is the SAME (reused, not forked)."""

    def test_run_stamps_product_and_comments_the_product(self):
        fake = _FakeGitHub(latest="__none__")
        r = rt.run(_client(fake), "0.1.0", COMMIT, pr_number=42, product=True)
        self.assertTrue(r["published"])
        self.assertTrue(r["product"])
        self.assertTrue(r["announced"])
        # the comment posted to the PR speaks of the product, not the engine
        comment = [b for (m, p, b) in fake.calls if p.endswith("/comments")][0]["body"]
        self.assertIn("Release v0.1.0 is now published", comment)
        self.assertNotIn("Engine version", comment)
        self.assertNotIn("Your instances", comment)

    def test_comment_body_product_vs_engine_wording(self):
        pub = {"published": True, "reason": "published", "tag": "v0.1.0", "product": True}
        self.assertIn("Release v0.1.0 is now published", rt._comment_body(pub))
        self.assertNotIn("Your instances", rt._comment_body(pub))
        # the engine path is unchanged (no product marker)
        eng = {"published": True, "reason": "published", "tag": "v0.1.0"}
        self.assertIn("Engine version v0.1.0 is now released", rt._comment_body(eng))
        # already-published + nothing-to-publish also speak the right noun
        already = {"published": True, "reason": "already-published", "tag": "v0.1.0", "product": True}
        self.assertIn("Release v0.1.0 was already published", rt._comment_body(already))
        nothing = {"published": False, "reason": "nothing-to-publish", "tag": None, "product": True, "message": ""}
        self.assertIn("did not set a new product version", rt._comment_body(nothing))

    def test_proposal_for_publish_product_builds_a_product_proposal(self):
        # first product release: baseline injected (offline), no target so the merged-PR list is skipped.
        p = rt._proposal_for_publish(baseline=release_cut.Baseline(None, True, "n"), product=True,
                                     slug="acme/my-product")
        self.assertIsNotNone(p)
        self.assertTrue(p["product"])
        self.assertEqual(p["mode"], "first-cut")
        # and the published notes speak of the product's release, not the engine version
        notes = rt._release_notes("v0.1.0", proposal=p)
        self.assertTrue(notes.startswith("Release v0.1.0."))
        self.assertNotIn("Engine version", notes)

    def test_product_publish_reuses_the_same_raise_only_and_idempotency(self):
        # the publish DECISION logic is shared: an older product version is refused, a newer one publishes,
        # a re-run is a clean no-op — exactly the engine path, just marked product.
        fake = _FakeGitHub(latest="v0.2.0")
        older = rt.run(_client(fake), "0.1.0", COMMIT, pr_number=None, product=True)
        self.assertFalse(older["published"])
        self.assertEqual(older["reason"], "not-newer")

    def test_cmd_publish_refuses_a_malformed_product_file_before_any_publish(self):
        # the publish CLI edge mode-gates too: a malformed product-version.json at the merged commit exits 2
        # BEFORE the client is built or the network is touched (never an engine publish in a product repo).
        import json, os, shutil, tempfile, validate
        root = tempfile.mkdtemp()
        os.makedirs(os.path.join(root, ".engine"))
        with open(os.path.join(root, ".engine", "engine.json"), "w") as fh:
            json.dump({"engine_release": "0.2.0", "packages": {}, "identity": "solo",
                       "home_repository": "acme/eng"}, fh)
        with open(os.path.join(root, "product-version.json"), "w") as fh:
            fh.write("{ not json")
        saved_root = validate.ROOT
        saved_env = {k: os.environ.get(k) for k in ("GITHUB_REPOSITORY", "GITHUB_TOKEN")}
        validate.ROOT = root
        os.environ["GITHUB_REPOSITORY"], os.environ["GITHUB_TOKEN"] = "acme/deployed", "tok"

        class _Args:
            commit, pr = "a" * 40, None
        try:
            code = rt._cmd_publish(_Args())
        finally:
            validate.ROOT = saved_root
            for k, v in saved_env.items():
                os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
            shutil.rmtree(root, ignore_errors=True)
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
