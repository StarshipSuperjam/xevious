"""Tests for overlay_disclosure — the non-blocking, merge-time upgrade-overwrite notice.

These lock the behaviours a non-engineer cannot read code to verify:
  - the deployed-only gate is correct: it compares the checkout's ON-DISK git origin against the update home
    that checkout's own manifest records, slug-normalized — so the engine's own home stays silent, a case- or
    `.git`-skewed recorded home cannot make it comment on its own pull requests, `GITHUB_REPOSITORY` cannot
    flip it either way, and a corrupt manifest fails LOUD rather than as a reassuring silence;
  - an engine-authored lifecycle pull request (update/removal/arrival branch) is exempt (the notice would
    read backwards there);
  - the overwrite set is the overlay's OWN membership (present provides + module MANIFESTS + FOUNDATION_CODE),
    proven by running the real overlay and comparing — so the notice cannot drift from what the update does,
    and it includes the module manifests a bare provides∪FOUNDATION_CODE would miss;
  - a crafted rename target cannot inject markup — every rendered path is whitelist-sanitized (a code span,
    not backslash-escaping, is the boundary);
  - a preserved file and a pure add are never warned about;
  - the single marker comment is idempotent AND only ever a BOT-authored one is edited (a user comment that
    quotes the marker is never overwritten); it is posted once, updated in place, retracted when empty;
  - the workflow is an engine-owned traveler (FOUNDATION_INFRA → FOUNDATION_CODE overlay), like the others;
  - a broken run is VISIBLE (exit 1), not a green that would falsely read as "nothing to overwrite".
"""
import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate                       # noqa: E402
import module_coherence               # noqa: E402
import module_manager                 # noqa: E402
import overlay_disclosure as od       # noqa: E402
import repo_identity                  # noqa: E402
import test_repo_identity as tri      # noqa: E402  (reuse its REAL throwaway-git-checkout fixtures — the
                                      #  on-disk origin read IS what the deployed gate is about)
import quiet_call                     # noqa: E402  (capture the demo walkthrough so it can't bury the summary)
import demo_overlay_disclosure        # noqa: E402

WORKFLOW_REL = ".github/workflows/engine-overlay-disclosure.yml"
BOT = {"type": "Bot"}
HOME = "StarshipSuperjam/engine-template"


class _Recorder:
    """A scripted GitHub transport: records (method, path, body) and answers from an in-memory comment store.
    A POSTed comment is stored as bot-authored (that is what the Actions token produces), so a later
    reconcile recognizes its own comment."""

    def __init__(self, comments=None):
        self.comments = list(comments or [])
        self.calls = []
        self._id = 5000

    def __call__(self, method, path, body=None):
        self.calls.append((method, path, body))
        if method == "GET" and "/comments" in path:
            return 200, list(self.comments)
        if method == "POST" and path.endswith("/comments"):
            self._id += 1
            self.comments.append({"id": self._id, "body": body["body"], "user": BOT})
            return 201, {"id": self._id}
        if method == "PATCH" and "/comments/" in path:
            cid = int(path.rsplit("/", 1)[-1])
            for c in self.comments:
                if c["id"] == cid:
                    c["body"] = body["body"]
            return 200, {"id": cid}
        return 200, None

    def counts(self):
        return {m: sum(1 for c in self.calls if c[0] == m) for m in ("GET", "POST", "PATCH")}


class TestDeployedGate(unittest.TestCase):
    """The gate: deployed iff the checkout's ON-DISK git origin differs, slug-normalized, from the update home
    its OWN manifest records.

    Driven against REAL throwaway git checkouts, never mocked seams. What this change is ABOUT is which read
    the gate performs, so a test that patched two function names would assert only that they are called — it
    would stay green against an implementation that consulted `GITHUB_REPOSITORY` alongside them, and the
    env-immunity case below could not be written at all."""

    def setUp(self):
        self.tmp = tri._mkdtemp(self)

    def _repo(self, name, **kw):
        return tri._repo(self.tmp, name, **kw)

    def test_home_repo_is_silent(self):
        self.assertFalse(od.is_deployed(self._repo("home", origin=f"https://github.com/{HOME}.git")))

    def test_downstream_copy_is_active(self):
        self.assertTrue(od.is_deployed(self._repo("copy", origin="https://github.com/acme/product.git")))

    def test_absent_home_is_silent(self):
        self.assertFalse(od.is_deployed(
            self._repo("nohome", origin="https://github.com/acme/product.git", home=None)))

    def test_unreadable_origin_is_silent(self):
        self.assertFalse(od.is_deployed(self._repo("noorigin", origin=None)))

    def test_case_and_git_suffix_skew_in_the_recorded_home_is_silent(self):
        # The old raw `!=` fired on the engine's OWN home whenever the recorded home was case-skewed or
        # `.git`-suffixed — posting a comment on every engine-template pull request. `.engine/engine.json` is
        # carved out of the overlay, so such a value persists and no update corrects it.
        self.assertFalse(od.is_deployed(self._repo(
            "skew", origin="git@github.com:starshipsuperjam/Engine-Template.git",
            home="StarshipSuperjam/engine-template.git")))

    def test_github_repository_env_cannot_flip_the_gate_either_way(self):
        # The gate judges the CHECKOUT, never this process's environment — both directions.
        home = self._repo("envhome", origin=f"https://github.com/{HOME}.git")
        copy = self._repo("envcopy", origin="https://github.com/acme/product.git")
        with mock.patch.dict(os.environ, {"GITHUB_REPOSITORY": "acme/product"}):
            self.assertFalse(od.is_deployed(home))
        with mock.patch.dict(os.environ, {"GITHUB_REPOSITORY": HOME}):
            self.assertTrue(od.is_deployed(copy))

    def test_a_malformed_manifest_raises_loud_not_a_silent_false(self):
        # Why the gate keys on is_downstream_copy_strict and NOT on `not is_home_repo()`: is_home_repo swallows
        # this, and a swallowed corrupt manifest would read to an operator as "nothing will be overwritten".
        # The contrast is pinned here, in one place, so a refactor of either cannot quietly converge them.
        repo = self._repo("corrupt", origin="https://github.com/acme/product.git")
        with open(os.path.join(repo, ".engine", "engine.json"), "w", encoding="utf-8") as fh:
            fh.write("{ not valid json ")
        with self.assertRaises(Exception):
            od.is_deployed(repo)
        self.assertTrue(repo_identity.is_home_repo(repo))


class TestEngineAuthoredExempt(unittest.TestCase):
    def test_update_branch_is_exempt(self):
        ev = {"pull_request": {"head": {"ref": "engine-update-v0.2.0"}}}
        self.assertTrue(od._is_engine_authored(ev))

    def test_remove_and_arrival_branches_are_exempt(self):
        self.assertTrue(od._is_engine_authored({"pull_request": {"head": {"ref": "engine-remove"}}}))
        self.assertTrue(od._is_engine_authored({"pull_request": {"head": {"ref": "engine-arrival"}}}))

    def test_an_ordinary_branch_is_not_exempt(self):
        self.assertFalse(od._is_engine_authored({"pull_request": {"head": {"ref": "fix-typo"}}}))
        # the fixed names are matched EXACTLY — a suffixed operator branch is not wrongly exempted
        self.assertFalse(od._is_engine_authored({"pull_request": {"head": {"ref": "engine-remove-cleanup"}}}))

    def test_main_is_silent_on_an_engine_update_pr(self):
        ev = {"pull_request": {"number": 9, "head": {"ref": "engine-update-v0.2.0",
                                                     "repo": {"full_name": "acme/product"}},
                               "base": {"repo": {"full_name": "acme/product"}}}}
        with mock.patch.object(od, "is_deployed", return_value=True), \
             mock.patch.object(od, "_load_event", return_value=ev), \
             mock.patch.object(od.weakening_guard, "fetch_all_changed_files",
                               side_effect=AssertionError("must not fetch on an exempt PR")), \
             mock.patch.dict(os.environ, {"GITHUB_REPOSITORY": "acme/product"}):
            with redirect_stdout(io.StringIO()):
                self.assertEqual(od.main(), 0)


class TestOverwrittenPaths(unittest.TestCase):
    SET = {".engine/tools/boot.py", ".engine/modules/core/manifest.json"}

    def _paths(self, changed):
        with mock.patch.object(od.module_manager, "overlay_replace_paths", return_value=self.SET):
            return od.overwritten_paths(changed)

    def test_modified_overlay_file_is_included(self):
        self.assertEqual(self._paths([{"filename": ".engine/tools/boot.py", "status": "modified"}]),
                         [".engine/tools/boot.py"])

    def test_module_manifest_is_included(self):
        self.assertEqual(
            self._paths([{"filename": ".engine/modules/core/manifest.json", "status": "modified"}]),
            [".engine/modules/core/manifest.json"])

    def test_preserved_file_is_excluded(self):
        self.assertEqual(self._paths([{"filename": ".engine/operator-overrides.json", "status": "modified"},
                                      {"filename": "CLAUDE.md", "status": "modified"}]), [])

    def test_pure_add_is_excluded(self):
        self.assertEqual(self._paths([{"filename": ".engine/tools/boot.py", "status": "added"}]), [])

    def test_rename_uses_the_canonical_side_in_the_set(self):
        out = self._paths([{"filename": "evil.py", "previous_filename": ".engine/tools/boot.py",
                            "status": "renamed"}])
        self.assertEqual(out, [".engine/tools/boot.py"])


class TestComment(unittest.TestCase):
    def test_body_is_plain_non_blocking_and_routes_with_home(self):
        body = od.compose_comment([".engine/tools/boot.py"], HOME)
        self.assertIn(od.COMMENT_MARKER, body)
        self.assertIn(".engine/tools/boot.py", body)
        self.assertIn("does not block your merge", body)
        self.assertIn("upstream", body)
        self.assertIn("/engine-tune", body)
        self.assertIn(HOME, body)                                        # the durable home is named

    def test_crafted_path_is_neutralized_not_escaped(self):
        # A rename can put a crafted name into the tree/overwrite set. It must be sanitized (unsafe chars
        # dropped), so no backtick can break the code span and no link/markup can form.
        crafted = ".engine/tools/a`b](http://evil.com).py"
        self.assertEqual(od._safe_path(crafted), ".engine/tools/a?b??http?//evil.com?.py")
        body = od.compose_comment([crafted], HOME)
        self.assertNotIn("`b]", body)                                    # the raw break-out never appears
        self.assertNotIn("http://evil.com", body)
        self.assertNotIn("](", body)

    def test_long_list_is_capped(self):
        many = [f".engine/tools/t{i}.py" for i in range(40)]
        body = od.compose_comment(many, HOME)
        self.assertIn("and 25 more", body)                               # 40 - 15 cap = 25 summarized


class TestReconcile(unittest.TestCase):
    def _client(self, comments=None):
        rec = _Recorder(comments)
        return od._Comments("acme/product", "tok", transport=rec), rec

    def test_posts_once_when_absent(self):
        client, rec = self._client()
        self.assertEqual(od.reconcile(client, 7, [".engine/tools/boot.py"], HOME), "posted")
        self.assertEqual(rec.counts()["POST"], 1)

    def test_unchanged_when_body_matches(self):
        body = od.compose_comment([".engine/tools/boot.py"], HOME)
        client, rec = self._client([{"id": 1, "body": body, "user": BOT}])
        self.assertEqual(od.reconcile(client, 7, [".engine/tools/boot.py"], HOME), "unchanged")
        self.assertEqual(rec.counts()["POST"], 0)
        self.assertEqual(rec.counts()["PATCH"], 0)

    def test_updates_in_place_when_body_differs(self):
        client, rec = self._client([{"id": 1, "body": od.COMMENT_MARKER + "\nstale", "user": BOT}])
        self.assertEqual(od.reconcile(client, 7, [".engine/tools/boot.py"], HOME), "updated")
        self.assertEqual(rec.counts()["PATCH"], 1)
        self.assertEqual(rec.counts()["POST"], 0)

    def test_retracts_when_empty(self):
        client, rec = self._client([{"id": 1, "body": od.COMMENT_MARKER + "\nprior notice", "user": BOT}])
        self.assertEqual(od.reconcile(client, 7, [], HOME), "retracted")
        self.assertEqual(rec.counts()["PATCH"], 1)

    def test_clean_when_nothing_and_no_prior(self):
        client, rec = self._client()
        self.assertEqual(od.reconcile(client, 7, [], HOME), "clean")
        self.assertEqual(rec.counts()["POST"], 0)
        self.assertEqual(rec.counts()["PATCH"], 0)

    def test_a_user_comment_quoting_the_marker_is_never_edited(self):
        # A non-bot comment carrying the marker string must NOT be overwritten — reconcile posts its own.
        client, rec = self._client([{"id": 1, "body": od.COMMENT_MARKER + "\nuser text",
                                     "user": {"type": "User"}}])
        self.assertEqual(od.reconcile(client, 7, [".engine/tools/boot.py"], HOME), "posted")
        self.assertEqual(rec.counts()["PATCH"], 0)                       # the user comment untouched
        self.assertEqual(rec.counts()["POST"], 1)

    def test_duplicate_bot_notice_is_resolved(self):
        client, rec = self._client([{"id": 1, "body": od.COMMENT_MARKER + "\nnotice A", "user": BOT},
                                    {"id": 2, "body": od.COMMENT_MARKER + "\nnotice B", "user": BOT}])
        od.reconcile(client, 7, [], HOME)                                # empty -> both resolved
        self.assertTrue(all(c["body"] == od._resolved_body() for c in rec.comments))


class TestFork(unittest.TestCase):
    def test_same_repo_is_not_fork(self):
        ev = {"pull_request": {"head": {"repo": {"full_name": "acme/product"}},
                               "base": {"repo": {"full_name": "acme/product"}}}}
        self.assertFalse(od._is_fork(ev, "acme/product"))

    def test_fork_is_fork(self):
        ev = {"pull_request": {"head": {"repo": {"full_name": "someone/fork"}},
                               "base": {"repo": {"full_name": "acme/product"}}}}
        self.assertTrue(od._is_fork(ev, "acme/product"))

    def test_missing_head_is_treated_as_fork(self):
        self.assertTrue(od._is_fork({"pull_request": {}}, "acme/product"))


class TestVisibleFailure(unittest.TestCase):
    def test_a_broken_run_exits_nonzero(self):
        ev = {"pull_request": {"number": 7, "head": {"ref": "fix", "repo": {"full_name": "acme/product"}},
                               "base": {"repo": {"full_name": "acme/product"}}}}
        with mock.patch.object(od, "is_deployed", return_value=True), \
             mock.patch.object(od, "_load_event", return_value=ev), \
             mock.patch.object(od.weakening_guard, "fetch_all_changed_files",
                               side_effect=RuntimeError("boom")), \
             mock.patch.dict(os.environ, {"GITHUB_REPOSITORY": "acme/product"}):
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(od.main(), 1)

    def test_silent_when_not_deployed(self):
        with mock.patch.object(od, "is_deployed", return_value=False):
            with redirect_stdout(io.StringIO()):
                self.assertEqual(od.main(), 0)

    def test_an_unreadable_engine_record_is_a_visible_red_not_a_traceback_and_not_a_green(self):
        # The gate raising must become the module's designed visible failure, never an uncaught traceback and
        # never the clean-run line — silence there reads to an operator as "nothing will be overwritten".
        with mock.patch.object(od, "is_deployed", side_effect=ValueError("bad manifest")):
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                self.assertEqual(od.main(), 1)
            self.assertIn("could not read this repository's engine record", err.getvalue())
            self.assertNotIn("nothing to disclose", out.getvalue())


class TestOverwriteSetMatchesOverlay(unittest.TestCase):
    """The load-bearing guarantee: the disclosure's overwrite set is EXACTLY what the real overlay copies —
    proven by running `_overlay_engine_code` and comparing to the shared `_overlay_copy_map`, and by the
    live-tree `overlay_replace_paths` covering all three categories (incl. module manifests)."""

    def test_overlay_copies_exactly_the_shared_map(self):
        with tempfile.TemporaryDirectory() as d:
            release, live = os.path.join(d, "release"), os.path.join(d, "live")
            os.makedirs(os.path.join(release, ".engine", "modules", "base"))
            os.makedirs(os.path.join(release, ".engine", "tools"))
            module_manager._write_json(
                os.path.join(release, ".engine", "modules", "base", "manifest.json"),
                {"id": "base", "version": "0.0.0", "status": "required",
                 "provides": {"tool": [".engine/tools/base_tool.py"]}, "depends": {}})
            with open(os.path.join(release, ".engine", "tools", "base_tool.py"), "w") as fh:
                fh.write("# base\n")
            with open(os.path.join(release, ".engine", "pyproject.toml"), "w") as fh:
                fh.write("x")            # a FOUNDATION_CODE member present in the release
            os.makedirs(live)
            candidates = {"base": validate.load_json(
                os.path.join(release, ".engine", "modules", "base", "manifest.json"))}
            map_keys = set(module_manager._overlay_copy_map(release, candidates).keys())
            with module_manager._redirect_root(live):
                copied, _ = module_manager._overlay_engine_code(release, ["base"])
            self.assertEqual(set(copied), map_keys)                       # overlay copies EXACTLY the map
            self.assertIn(".engine/modules/base/manifest.json", map_keys)  # manifests ARE in the set
            self.assertIn(".engine/tools/base_tool.py", map_keys)
            self.assertIn(".engine/pyproject.toml", map_keys)

    def test_live_overwrite_set_covers_all_three_categories(self):
        with tempfile.TemporaryDirectory() as d:
            with module_manager._redirect_root(d):
                module_manager._build_fixture(d)
                paths = module_manager.overlay_replace_paths()
        self.assertIn(".engine/tools/base_tool.py", paths)                # provides
        self.assertIn(".engine/modules/base/manifest.json", paths)        # MANIFEST category
        self.assertIn(".engine/modules/optx/manifest.json", paths)
        self.assertIn(".engine/pyproject.toml", paths)                    # FOUNDATION_CODE
        self.assertNotIn(".engine/engine.json", paths)                    # carve-out: bumped in place, not overlaid


class TestFoundationTraveler(unittest.TestCase):
    def test_workflow_is_an_engine_owned_traveler(self):
        self.assertIn(WORKFLOW_REL, module_coherence.FOUNDATION_INFRA)
        self.assertIn(WORKFLOW_REL, module_manager.FOUNDATION_CODE)       # overlay-replaced on update


class TestDemo(unittest.TestCase):
    def test_demo_runs_green(self):
        self.assertEqual(quiet_call.run(demo_overlay_disclosure.main), 0)


if __name__ == "__main__":
    unittest.main()
