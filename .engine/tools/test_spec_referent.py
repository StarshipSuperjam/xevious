#!/usr/bin/env python3
"""Tests for spec_referent — the core spec-path-read resolver (#307 + #282).

These lock the load-bearing behavior the deliverable gate attests:
  - the pure parse (frontmatter status, section body, criteria table, the docs/spec pointer): order, prefix
    columns, BOM, blob-URL + #fragment links, the multi-pointer and not-under-docs/spec partitions;
  - the GitHub boundary: a read failure RAISES (never read as "no spec"), a 200-no-pointer is the no-op;
  - resolve: the locked gate, each disclosed no-op reason, and the confined-read guard (a pointer escaping
    docs/spec — by `..` or a symlink — is never opened);
  - review-steps: the two-group split (operator-runnable vs the engine's account), the terminal-command/demo
    demotion, the all-engine no-op, and the verbatim, lifecycle-token-clean render;
  - the dispatch (demo returns clean; the env-guarded CLIs; resolve/review-steps over --doc).

Run: uv run --directory .engine --frozen -- python -m unittest discover -s tools -p 'test_*.py' -b
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import spec_referent  # noqa: E402


# ---- fixtures -----------------------------------------------------------------------------------

def _doc(status, rows="| It works | Open the screen and confirm | operator |\n"):
    head = (f"---\nstatus: {status}\n---\n\n# A capability\n\n## Summary\nx\n\n## Behavior\ny\n\n"
            "## Acceptance criteria\n")
    table = "\n| Criterion | How verified | Who checks it |\n| --- | --- | --- |\n" + rows
    return head + table


def _issue_transport(body="", *, status=200, shape="object"):
    """A fake transport over a single issue read: a chosen status (>=400 to exercise the RAISE), or a 200 with a
    canned body. `shape='nonobject'` returns a 200 whose payload is not a dict (the unexpected-shape RAISE)."""
    def transport(method, path, b):
        if "/issues/" in path:
            if status >= 400:
                return status, None
            if shape == "nonobject":
                return 200, ["not", "an", "object"]
            return 200, {"number": 1, "body": body}
        return 404, None
    return transport


def _gh(body="", **kw):
    return spec_referent.GitHubIssues("demo/proj", "tok", transport=_issue_transport(body, **kw))


class _Seeded(unittest.TestCase):
    def _root(self, files):
        d = tempfile.mkdtemp(prefix="engine-spec-referent-test-")
        self.addCleanup(shutil.rmtree, d, True)
        for rel, text in files.items():
            path = os.path.join(d, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(text)
        return d


# ---- the pure parse -----------------------------------------------------------------------------

class ParseHelperTests(unittest.TestCase):
    def test_frontmatter_status(self):
        self.assertEqual(spec_referent._frontmatter_status("---\nstatus: locked\n---\n# x"), "locked")
        self.assertEqual(spec_referent._frontmatter_status("---\nstatus: Draft  # wip\n---\n"), "draft")
        self.assertIsNone(spec_referent._frontmatter_status("# no frontmatter"))

    def test_section_body_extracts_between_h2(self):
        text = "## Summary\na\n\n## Acceptance criteria\nrow1\nrow2\n\n## Behavior\nb\n"
        self.assertIn("row1", spec_referent._section_body(text, "Acceptance criteria"))
        self.assertNotIn("Behavior", spec_referent._section_body(text, "Acceptance criteria"))
        self.assertIsNone(spec_referent._section_body(text, "Nope"))

    def test_table_with_columns_prefix_tolerates_trailing(self):
        text = ("| Criterion | How verified | Who checks it | Notes |\n| --- | --- | --- | --- |\n"
                "| A | demo | operator | later |\n")
        rows = spec_referent._table_with_columns(text, spec_referent._CRITERIA_COLUMNS)
        self.assertEqual(rows[0][:3], ["A", "demo", "operator"])

    def test_md_link_targets_handles_blob_url_and_fragment(self):
        body = ("[a](docs/spec/a.md) and [b](https://github.com/o/r/blob/main/docs/spec/b.md#sec) "
                "and [c](docs/spec/c.md?x=1) and [not](notes.txt)")
        self.assertEqual(spec_referent._md_link_targets(body),
                         ["docs/spec/a.md", "docs/spec/b.md", "docs/spec/c.md"])

    def test_spec_pointers_partitions_under_vs_total(self):
        # one under docs/spec, one .md not under it
        under, total = spec_referent._spec_pointers("[a](docs/spec/a.md) [r](README.md)")
        self.assertEqual(under, ["docs/spec/a.md"])
        self.assertEqual(total, 2)

    def test_spec_pointers_drops_escaping_link(self):
        under, total = spec_referent._spec_pointers("[x](docs/spec/../secret.md)")
        self.assertEqual(under, [])            # normalizes out of docs/spec
        self.assertEqual(total, 1)

    def test_spec_pointers_dedupes_and_sorts(self):
        under, _ = spec_referent._spec_pointers("[a](docs/spec/b.md) [a2](docs/spec/a.md) [a3](docs/spec/a.md)")
        self.assertEqual(under, ["docs/spec/a.md", "docs/spec/b.md"])


# ---- the GitHub boundary (fail-closed) -----------------------------------------------------------

class BoundaryTests(unittest.TestCase):
    def test_200_with_body_returns_it(self):
        self.assertEqual(_gh("hello").issue_body(1), "hello")

    def test_200_empty_body_is_empty_string_not_an_error(self):
        self.assertEqual(_gh("").issue_body(1), "")    # a real read of a body-less issue

    def test_403_raises(self):
        with self.assertRaises(spec_referent.SpecReferentError):
            _gh(status=403).issue_body(1)

    def test_404_raises(self):
        with self.assertRaises(spec_referent.SpecReferentError):
            _gh(status=404).issue_body(1)

    def test_non_object_shape_raises(self):
        with self.assertRaises(spec_referent.SpecReferentError):
            _gh(shape="nonobject").issue_body(1)

    def test_a_raising_transport_propagates_never_swallowed(self):
        # a network failure that the real _http converts to a raise (URLError) must propagate, never be read as
        # a benign no-op. Simulated by an injected transport that raises.
        def boom(method, path, body):
            raise spec_referent.SpecReferentError("network down")
        gh = spec_referent.GitHubIssues("demo/proj", "tok", transport=boom)
        with self.assertRaises(spec_referent.SpecReferentError):
            gh.issue_body(1)


# ---- resolve (the path read) --------------------------------------------------------------------

class ResolveDocTests(_Seeded):
    def test_locked_doc_resolves_to_criteria(self):
        root = self._root({"docs/spec/index.md": "# s\n",
                           "docs/spec/c.md": _doc("locked",
                                                  "| A | open the screen | operator |\n"
                                                  "| B | the storage test | engine |\n")})
        r = spec_referent.resolve(root, doc="docs/spec/c.md")
        self.assertTrue(r["ok"])
        self.assertEqual(r["doc_path"], "docs/spec/c.md")
        self.assertEqual([c["who"] for c in r["criteria"]], ["operator", "engine"])
        self.assertEqual(r["criteria"][0]["how_verified"], "open the screen")

    def test_capitalized_who_is_normalized(self):
        root = self._root({"docs/spec/index.md": "# s\n",
                           "docs/spec/c.md": _doc("locked", "| A | open the screen | Operator |\n")})
        self.assertEqual(spec_referent.resolve(root, doc="docs/spec/c.md")["criteria"][0]["who"], "operator")

    def test_bom_doc_resolves(self):
        root = self._root({"docs/spec/index.md": "# s\n", "docs/spec/c.md": "﻿" + _doc("locked")})
        self.assertTrue(spec_referent.resolve(root, doc="docs/spec/c.md")["ok"])

    def test_draft_doc_is_doc_not_locked(self):
        root = self._root({"docs/spec/index.md": "# s\n", "docs/spec/c.md": _doc("draft")})
        self.assertEqual(spec_referent.resolve(root, doc="docs/spec/c.md")["no_op_reason"], "doc-not-locked")

    def test_require_locked_false_resolves_a_draft_with_its_observed_status(self):
        # #420 intake count path: a draft doc resolves (the lock gate is skipped) and the returned status is the
        # doc's OBSERVED frontmatter — never a hardcoded "locked", so it can't be mistaken for a build referent.
        root = self._root({"docs/spec/index.md": "# s\n", "docs/spec/c.md": _doc("draft")})
        r = spec_referent.resolve_doc(root, "docs/spec/c.md", require_locked=False)
        self.assertTrue(r["ok"])
        self.assertEqual(r["status"], "draft")

    def test_require_locked_true_is_the_default_so_the_build_path_still_gates(self):
        root = self._root({"docs/spec/index.md": "# s\n", "docs/spec/c.md": _doc("draft")})
        self.assertEqual(spec_referent.resolve_doc(root, "docs/spec/c.md")["no_op_reason"], "doc-not-locked")

    def test_require_locked_false_still_enforces_the_confined_read_wall(self):
        # relaxing the lock gate must NOT relax the engine/product wall — an escaping pointer is still never read.
        root = self._root({"docs/spec/index.md": "# s\n",
                           "secret.md": _doc("draft")})
        r = spec_referent.resolve_doc(root, "docs/spec/../secret.md", require_locked=False)
        self.assertEqual(r["no_op_reason"], "pointer-not-under-docs-spec")

    def test_missing_doc_is_doc_missing(self):
        root = self._root({"docs/spec/index.md": "# s\n"})
        self.assertEqual(spec_referent.resolve(root, doc="docs/spec/ghost.md")["no_op_reason"], "doc-missing")

    def test_locked_doc_without_criteria_is_no_criteria(self):
        root = self._root({"docs/spec/index.md": "# s\n",
                           "docs/spec/c.md": "---\nstatus: locked\n---\n\n# C\n\n## Summary\nx\n"})
        self.assertEqual(spec_referent.resolve(root, doc="docs/spec/c.md")["no_op_reason"], "no-criteria")

    def test_no_docs_spec_tree_is_no_spec_installed(self):
        root = self._root({"README.md": "hi"})
        self.assertEqual(spec_referent.resolve(root, doc="docs/spec/c.md")["no_op_reason"], "no-spec-installed")

    def test_traversal_pointer_is_rejected_and_never_read(self):
        root = self._root({"docs/spec/index.md": "# s\n",
                           "secret.md": "---\nstatus: locked\n---\n\n## Acceptance criteria\n"
                                        "\n| Criterion | How verified | Who checks it |\n| - | - | - |\n"
                                        "| leak | x | operator |\n"})
        r = spec_referent.resolve(root, doc="docs/spec/../secret.md")
        self.assertEqual(r["no_op_reason"], "pointer-not-under-docs-spec")  # not read despite being a valid doc

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks unsupported")
    def test_symlink_escape_is_rejected_by_realpath(self):
        root = self._root({"docs/spec/index.md": "# s\n", "secret.md": _doc("locked")})
        try:
            os.symlink(os.path.join(root, "secret.md"), os.path.join(root, "docs", "spec", "link.md"))
        except (OSError, NotImplementedError):
            self.skipTest("symlink creation not permitted")
        r = spec_referent.resolve(root, doc="docs/spec/link.md")
        self.assertEqual(r["no_op_reason"], "pointer-not-under-docs-spec")


class ResolveFromIssueTests(_Seeded):
    def _rich(self):
        return self._root({"docs/spec/index.md": "# s\n", "docs/spec/c.md": _doc("locked")})

    def test_issue_pointer_resolves(self):
        root = self._rich()
        gh = _gh("Builds it. See [C](docs/spec/c.md).")
        self.assertTrue(spec_referent.resolve(root, issue=1, gh=gh)["ok"])

    def test_no_link_is_no_issue_pointer(self):
        root = self._rich()
        self.assertEqual(spec_referent.resolve(root, issue=1, gh=_gh("no link here"))["no_op_reason"],
                         "no-issue-pointer")

    def test_non_spec_link_is_pointer_not_under_docs_spec(self):
        root = self._rich()
        self.assertEqual(spec_referent.resolve(root, issue=1, gh=_gh("[r](README.md)"))["no_op_reason"],
                         "pointer-not-under-docs-spec")

    def test_two_distinct_specs_is_ambiguous(self):
        root = self._root({"docs/spec/index.md": "# s\n",
                           "docs/spec/a.md": _doc("locked"), "docs/spec/b.md": _doc("locked")})
        r = spec_referent.resolve(root, issue=1, gh=_gh("[a](docs/spec/a.md) [b](docs/spec/b.md)"))
        self.assertEqual(r["no_op_reason"], "ambiguous-pointer")

    def test_read_failure_raises_never_no_op(self):
        root = self._rich()
        with self.assertRaises(spec_referent.SpecReferentError):
            spec_referent.resolve(root, issue=1, gh=_gh(status=403))


# ---- review-steps (the #282 projection + render) -------------------------------------------------

class ReviewStepsTests(unittest.TestCase):
    def _resolved(self, criteria):
        return {"ok": True, "doc_path": "docs/spec/c.md", "status": "locked", "criteria": criteria}

    def test_operator_plain_row_is_runnable(self):
        proj = spec_referent.review_steps(self._resolved(
            [{"criterion": "A", "how_verified": "open the checkout screen", "who": "operator"}]))
        self.assertEqual(len(proj["runnable"]), 1)
        self.assertEqual(proj["engine_account"], [])
        self.assertIsNone(proj["no_op_reason"])

    def test_plain_operator_verb_stays_runnable(self):
        # a plain-language operator step opening with a word that is ALSO a command name ("Go", "Make", "Show")
        # must NOT be demoted — the form-kind read is confined to code spans, so plain prose stays runnable.
        proj = spec_referent.review_steps(self._resolved([
            {"criterion": "A", "how_verified": "Go to the checkout page and confirm the total", "who": "operator"},
            {"criterion": "B", "how_verified": "Make a purchase and confirm the receipt", "who": "operator"},
            {"criterion": "C", "how_verified": "Show the order history and check the latest order", "who": "operator"},
        ]))
        self.assertEqual(len(proj["runnable"]), 3)
        self.assertEqual(proj["engine_account"], [])

    def test_operator_terminal_command_is_demoted_to_engine_account(self):
        proj = spec_referent.review_steps(self._resolved(
            [{"criterion": "A", "how_verified": "Run `uv run pytest tests/x.py`", "who": "operator"}]))
        self.assertEqual(proj["runnable"], [])
        self.assertEqual(len(proj["engine_account"]), 1)
        self.assertEqual(proj["no_op_reason"], "all-engine-account")

    def test_demo_correlate_is_demoted(self):
        proj = spec_referent.review_steps(self._resolved(
            [{"criterion": "A", "how_verified": "the `demo` subcommand", "who": "operator"}]))
        self.assertEqual(proj["runnable"], [])

    def test_engine_typed_row_is_engine_account(self):
        proj = spec_referent.review_steps(self._resolved(
            [{"criterion": "A", "how_verified": "the storage test", "who": "engine"}]))
        self.assertEqual(len(proj["engine_account"]), 1)

    def test_unrecognized_who_falls_to_engine_account(self):
        proj = spec_referent.review_steps(self._resolved(
            [{"criterion": "A", "how_verified": "somehow", "who": "nobody"}]))
        self.assertEqual(len(proj["engine_account"]), 1)
        self.assertEqual(proj["runnable"], [])

    def test_no_op_referent_passes_reason_through(self):
        proj = spec_referent.review_steps({"ok": False, "no_op_reason": "doc-not-locked", "detail": "x"})
        self.assertEqual(proj["no_op_reason"], "doc-not-locked")

    def test_render_has_both_groups_verbatim_and_the_caveat(self):
        r = self._resolved([
            {"criterion": "Total shows tax", "how_verified": "Open the screen and confirm the total", "who": "operator"},
            {"criterion": "Encrypted", "how_verified": "the storage test", "who": "engine"},
        ])
        out = spec_referent.render_review_steps(r)
        self.assertIn("**Things you can confirm yourself**", out)
        self.assertIn("**Things I checked for you**", out)
        self.assertIn("Open the screen and confirm the total", out)   # verbatim
        self.assertIn("promise, not proof", out)

    def test_render_no_op_line_is_plain_no_token_leak(self):
        out = spec_referent.render_review_steps({"ok": False, "no_op_reason": "doc-not-locked", "detail": "x"})
        self.assertIn("run yourself", out.lower())
        for tok in ("locked", "draft", "doc-not-locked", "referent", "[operator]"):
            self.assertNotIn(tok, out)

    def test_render_all_engine_states_plain_cause_then_engine_group(self):
        r = self._resolved([{"criterion": "Encrypted", "how_verified": "the storage test", "who": "engine"}])
        out = spec_referent.render_review_steps(r)
        self.assertIn("engine's account", out)            # the sanctioned plain phrase
        self.assertIn("**Things I checked for you**", out)
        self.assertNotIn("**Things you can confirm yourself**", out)


# ---- acceptance-split (the #420 intake two-tier count) -------------------------------------------

class AcceptanceSplitTests(unittest.TestCase):
    def _resolved(self, criteria):
        return {"ok": True, "doc_path": "docs/spec/c.md", "status": "draft", "criteria": criteria}

    def test_count_reuses_the_classifier_including_the_terminal_demotion(self):
        # one plain operator row (verify-yourself), one engine row, one operator-but-terminal row (demoted to
        # the engine's account by rule 4) — the SAME split as review-steps, projected to a count.
        split = spec_referent.acceptance_split(self._resolved([
            {"criterion": "A", "how_verified": "open the checkout screen", "who": "operator"},
            {"criterion": "B", "how_verified": "the storage test", "who": "engine"},
            {"criterion": "C", "how_verified": "Run `uv run pytest x.py`", "who": "operator"},
        ]))
        self.assertEqual(split, {"operator_verifiable": 1, "engine_account": 2})

    def test_render_states_both_numbers(self):
        out = spec_referent.render_acceptance_split(self._resolved([
            {"criterion": "A", "how_verified": "open the screen", "who": "operator"},
            {"criterion": "B", "how_verified": "the storage test", "who": "engine"},
        ]))
        self.assertIn("1 is something you can confirm yourself", out)
        self.assertIn("1 is on the engine's account", out)

    def test_render_never_collapses_to_all_green_when_one_side_is_zero(self):
        # all-operator: the readout must still STATE the zero, never fold into one "all good".
        out = spec_referent.render_acceptance_split(self._resolved([
            {"criterion": "A", "how_verified": "open the screen", "who": "operator"},
            {"criterion": "B", "how_verified": "make a purchase and confirm", "who": "operator"},
        ]))
        self.assertIn("2 are something you can confirm yourself", out)
        self.assertIn("0 are on the engine's account", out)
        self.assertNotIn("all good", out.lower())
        self.assertNotIn("all green", out.lower())

    def test_render_no_op_is_intake_framed_and_plain(self):
        # the reachable intake no-ops (require_locked=False, --doc) get draft-framed, action-guiding phrasings —
        # NOT the merge-review "settled description"/"linked"/"this change" strings — and leak no raw token.
        no_crit = spec_referent.render_acceptance_split({"ok": False, "no_op_reason": "no-criteria", "detail": "x"})
        self.assertIn("acceptance criteria", no_crit.lower())
        self.assertNotIn("settled", no_crit.lower())          # never call the operator's draft "settled"
        missing = spec_referent.render_acceptance_split({"ok": False, "no_op_reason": "doc-missing", "detail": "x"})
        self.assertIn("couldn't find", missing.lower())
        self.assertNotIn("linked", missing.lower())
        wall = spec_referent.render_acceptance_split(
            {"ok": False, "no_op_reason": "pointer-not-under-docs-spec", "detail": "x"})
        self.assertIn("docs/spec", wall.lower())
        for out in (no_crit, missing, wall):
            for tok in ("locked", "draft", "stub", "doc-not-locked", "referent", "[operator]", "who checks it"):
                self.assertNotIn(tok, out)


# ---- dispatch -----------------------------------------------------------------------------------

class DispatchTests(_Seeded):
    def test_demo_runs_clean(self):
        import contextlib
        import io
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(spec_referent.main(["demo"]), 0)

    def test_unknown_verb_is_usage_error(self):
        import contextlib
        import io
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(spec_referent.main(["bogus"]), 2)
            self.assertEqual(spec_referent.main([]), 2)

    def test_resolve_doc_prints_json(self):
        import contextlib
        import io
        root = self._root({"docs/spec/index.md": "# s\n", "docs/spec/c.md": _doc("locked")})
        orig = spec_referent._ROOT
        spec_referent._ROOT = root
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = spec_referent.main(["resolve", "--doc", "docs/spec/c.md"])
            self.assertEqual(rc, 0)
            self.assertIn('"ok": true', buf.getvalue())
        finally:
            spec_referent._ROOT = orig

    def test_review_steps_doc_renders(self):
        import contextlib
        import io
        root = self._root({"docs/spec/index.md": "# s\n", "docs/spec/c.md": _doc("locked")})
        orig = spec_referent._ROOT
        spec_referent._ROOT = root
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = spec_referent.main(["review-steps", "--doc", "docs/spec/c.md"])
            self.assertEqual(rc, 0)
            self.assertIn("Things you can confirm yourself", buf.getvalue())
        finally:
            spec_referent._ROOT = orig

    def test_acceptance_split_verb_counts_a_draft_doc_pre_lock(self):
        # the load-bearing intake case: the verb must count a DRAFT doc (require_locked=False reaches resolve_doc),
        # not silently no-op it — a happy-path locked fixture would hide this.
        import contextlib
        import io
        root = self._root({"docs/spec/index.md": "# s\n",
                           "docs/spec/c.md": _doc("draft", "| A | open the screen | operator |\n"
                                                           "| B | the storage test | engine |\n")})
        orig = spec_referent._ROOT
        spec_referent._ROOT = root
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = spec_referent.main(["acceptance-split", "--doc", "docs/spec/c.md"])
            self.assertEqual(rc, 0)
            out = buf.getvalue()
            self.assertIn("1 is something you can confirm yourself", out)
            self.assertIn("1 is on the engine's account", out)
        finally:
            spec_referent._ROOT = orig

    def test_acceptance_split_verb_without_doc_is_usage_error(self):
        # `--doc`-only, offline: no --doc (and --issue is not offered) -> a plain usage error, never a remote read.
        import contextlib
        import io
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(spec_referent.main(["acceptance-split", "--issue", "1"]), 2)
        self.assertIn("--doc", err.getvalue())

    def test_resolve_issue_without_env_is_usage_error(self):
        import contextlib
        import io
        saved = {k: os.environ.pop(k, None) for k in ("GITHUB_REPOSITORY", "GITHUB_TOKEN")}
        try:
            with contextlib.redirect_stderr(io.StringIO()) as err:
                rc = spec_referent.main(["resolve", "--issue", "1"])
            self.assertEqual(rc, 2)
            self.assertIn("GITHUB_REPOSITORY", err.getvalue())
        finally:
            for k, v in saved.items():
                if v is not None:
                    os.environ[k] = v

    def test_resolve_without_target_is_usage_error(self):
        import contextlib
        import io
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(spec_referent.main(["resolve"]), 2)


if __name__ == "__main__":
    unittest.main()
