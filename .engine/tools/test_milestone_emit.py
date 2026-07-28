#!/usr/bin/env python3
"""Tests for milestone_emit — the core phase-emission tool.

These lock the load-bearing behavior the deliverable gate attests:
  - the pure build-order parse (`derive_phases`): order, dedupe, trim, header/separator/blank skip, BOM;
  - the GitHub boundary: pagination to exhaustion over the injectable transport, `state=all`, trimmed titles,
    raise-on-error (never read a failure as "no milestones");
  - `emit`: create only the missing phases, idempotent on a re-run, trimmed/duplicate match, and degrade-LOUD
    then re-runnable to completion;
  - the dispatch (demo returns clean; the env-guarded CLIs).

Run: uv run --directory .engine --frozen -- python -m unittest discover -s tools -p 'test_*.py' -b
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import milestone_emit  # noqa: E402


# ---- a tiny injectable transport for the boundary tests -----------------------------------------

class _Pages:
    """A transport over an in-memory milestone list that honors the requested per_page (so a small page size
    forces the real page-walk to continue), records creates, and can fail a chosen create."""

    def __init__(self, existing=(), *, fail_on=None):
        self.titles = list(existing)
        self.created = []
        self.fail_on = fail_on
        self.list_calls = 0

    def __call__(self, method, path, body):
        import re
        if method == "GET" and "/milestones" in path:
            self.list_calls += 1
            assert "state=all" in path, "the list must read open AND closed milestones"
            page = int(re.search(r"[?&]page=(\d+)", path).group(1))
            per_page = int(re.search(r"[?&]per_page=(\d+)", path).group(1))
            start = (page - 1) * per_page
            return 200, [{"title": t} for t in self.titles[start:start + per_page]]
        if method == "POST" and path.endswith("/milestones"):
            title = (body or {}).get("title")
            if title == self.fail_on:
                return 500, None
            self.titles.append(title)
            self.created.append(title)
            return 201, {"title": title}
        return 404, None


def _client(transport) -> milestone_emit.GitHubMilestones:
    return milestone_emit.GitHubMilestones("demo/proj", "tok", transport=transport)


# ---- the pure parse -----------------------------------------------------------------------------

class DerivePhasesTests(unittest.TestCase):
    def test_takes_the_phase_column_in_order(self):
        text = ("| Phase | Capability | Doc |\n"
                "| --- | --- | --- |\n"
                "| Foundation | Login | [Login](login.md) |\n"
                "| Core flows | Checkout | [Checkout](checkout.md) |\n")
        self.assertEqual(milestone_emit.derive_phases(text), ["Foundation", "Core flows"])

    def test_dedupes_preserving_first_appearance(self):
        text = ("| Phase | Capability | Doc |\n| --- | --- | --- |\n"
                "| Foundation | A | [a](a.md) |\n"
                "| Foundation | B | [b](b.md) |\n"
                "| Polish | C | [c](c.md) |\n")
        self.assertEqual(milestone_emit.derive_phases(text), ["Foundation", "Polish"])

    def test_trims_cells_and_treats_whitespace_variants_as_one(self):
        text = ("| Phase | Capability | Doc |\n| --- | --- | --- |\n"
                "|  Foundation  | A | [a](a.md) |\n"
                "| Foundation | B | [b](b.md) |\n")
        self.assertEqual(milestone_emit.derive_phases(text), ["Foundation"])

    def test_skips_header_separator_and_blank_first_cell(self):
        text = ("| Phase | Capability | Doc |\n"
                "| :--- | :---: | ---: |\n"
                "|  | orphan-ish | [x](x.md) |\n"
                "| Real | y | [y](y.md) |\n")
        self.assertEqual(milestone_emit.derive_phases(text), ["Real"])

    def test_ignores_non_table_lines(self):
        text = ("# Build order\n\nSome prose about phases.\n\n"
                "| Phase | Capability | Doc |\n| --- | --- | --- |\n"
                "| Foundation | A | [a](a.md) |\n\nMore prose.\n")
        self.assertEqual(milestone_emit.derive_phases(text), ["Foundation"])

    def test_tolerates_a_row_with_extra_columns(self):
        text = ("| Phase | Capability | Doc | Notes |\n| --- | --- | --- | --- |\n"
                "| Foundation | A | [a](a.md) | later |\n")
        self.assertEqual(milestone_emit.derive_phases(text), ["Foundation"])

    def test_empty_text_is_no_phases(self):
        self.assertEqual(milestone_emit.derive_phases(""), [])

    def test_a_numeric_phase_passes_through_verbatim(self):
        # The tool does not reject a bare "1" (the decompose step steers toward names); it takes it verbatim.
        text = "| Phase | Capability | Doc |\n| --- | --- | --- |\n| 1 | A | [a](a.md) |\n"
        self.assertEqual(milestone_emit.derive_phases(text), ["1"])

    def test_a_data_phase_literally_named_phase_is_not_dropped(self):
        # The header is skipped by POSITION (the first table row), not by matching the word "Phase", so a real
        # phase a build order happens to name "Phase" survives — it is not mistaken for the column header.
        text = ("| Phase | Capability | Doc |\n| --- | --- | --- |\n"
                "| Phase | Login | [x](x.md) |\n| Real | Y | [y](y.md) |\n")
        self.assertEqual(milestone_emit.derive_phases(text), ["Phase", "Real"])


class ReadBuildOrderTests(unittest.TestCase):
    def _root_with(self, body):
        d = tempfile.mkdtemp(prefix="engine-milestone-test-")
        self.addCleanup(__import__("shutil").rmtree, d, True)
        spec = os.path.join(d, "docs", "spec")
        os.makedirs(spec)
        with open(os.path.join(spec, "build-plan.md"), "w", encoding="utf-8") as fh:
            fh.write(body)
        return d

    def test_no_build_order_is_none_and_no_phases(self):
        d = tempfile.mkdtemp(prefix="engine-milestone-test-")
        self.addCleanup(__import__("shutil").rmtree, d, True)
        self.assertIsNone(milestone_emit.read_build_order(d))
        self.assertEqual(milestone_emit.phases_for(d), [])

    def test_a_bom_build_order_parses(self):
        # Authored on Windows: a leading BOM must be stripped (utf-8-sig), matching the spec readers, so the
        # same file derives phases AND passes the coverage check.
        root = self._root_with("﻿| Phase | Capability | Doc |\n| --- | --- | --- |\n"
                               "| Foundation | A | [a](a.md) |\n")
        self.assertEqual(milestone_emit.phases_for(root), ["Foundation"])


# ---- the GitHub boundary ------------------------------------------------------------------------

class ExistingTitlesTests(unittest.TestCase):
    def test_paginates_to_exhaustion(self):
        # three titles, page size two -> the walk must read page 2 to find the third (else it recreates it).
        t = _Pages(existing=["Foundation", "Core flows", "Polish"])
        self.assertEqual(_client(t).existing_titles(per_page=2),
                         {"Foundation", "Core flows", "Polish"})
        self.assertEqual(t.list_calls, 2, "must request a second page")

    def test_single_page_stops_after_one_call(self):
        t = _Pages(existing=["Foundation"])
        self.assertEqual(_client(t).existing_titles(per_page=100), {"Foundation"})
        self.assertEqual(t.list_calls, 1)

    def test_titles_are_trimmed(self):
        t = _Pages(existing=["  Foundation  "])
        self.assertEqual(_client(t).existing_titles(), {"Foundation"})

    def test_http_error_raises_never_reads_as_empty(self):
        def boom(method, path, body):
            return 403, None
        with self.assertRaises(milestone_emit.MilestoneEmitError):
            _client(boom).existing_titles()

    def test_non_list_body_raises(self):
        def weird(method, path, body):
            return 200, {"not": "a list"}
        with self.assertRaises(milestone_emit.MilestoneEmitError):
            _client(weird).existing_titles()


class CreateTests(unittest.TestCase):
    def test_create_posts_and_records(self):
        t = _Pages()
        _client(t).create("Foundation")
        self.assertEqual(t.created, ["Foundation"])

    def test_create_raises_on_error(self):
        t = _Pages(fail_on="Foundation")
        with self.assertRaises(milestone_emit.MilestoneEmitError):
            _client(t).create("Foundation")


# ---- emit ---------------------------------------------------------------------------------------

class EmitTests(unittest.TestCase):
    def test_creates_every_phase_on_a_fresh_project(self):
        t = _Pages()
        created = milestone_emit.emit(_client(t), ["Foundation", "Core flows", "Polish"])
        self.assertEqual(created, ["Foundation", "Core flows", "Polish"])
        self.assertEqual(t.created, ["Foundation", "Core flows", "Polish"])

    def test_idempotent_second_run_creates_nothing(self):
        order = ["Foundation", "Core flows", "Polish"]
        t = _Pages(existing=order)
        self.assertEqual(milestone_emit.emit(_client(t), order), [])
        self.assertEqual(t.created, [])

    def test_only_a_new_phase_is_created(self):
        t = _Pages(existing=["Foundation", "Core flows", "Polish"])
        self.assertEqual(milestone_emit.emit(_client(t), ["Foundation", "Core flows", "Hardening"]),
                         ["Hardening"])

    def test_a_whitespace_variant_is_not_a_new_phase(self):
        t = _Pages(existing=["Foundation"])
        self.assertEqual(milestone_emit.emit(_client(t), ["  Foundation  "]), [])

    def test_a_phase_repeated_in_one_order_is_created_once(self):
        t = _Pages()
        self.assertEqual(milestone_emit.emit(_client(t), ["Foundation", "Foundation"]), ["Foundation"])

    def test_empty_phase_list_is_a_no_op(self):
        t = _Pages()
        self.assertEqual(milestone_emit.emit(_client(t), []), [])
        self.assertEqual(t.created, [])

    def test_degrades_loud_then_re_runs_to_completion(self):
        order = ["Foundation", "Core flows", "Polish"]
        t = _Pages(fail_on="Core flows")
        with self.assertRaises(milestone_emit.MilestoneEmitError):
            milestone_emit.emit(_client(t), order)
        self.assertEqual(t.created, ["Foundation"], "stops loud after the failure, nothing further")
        t.fail_on = None                                   # the transient failure clears
        self.assertEqual(milestone_emit.emit(_client(t), order), ["Core flows", "Polish"])
        self.assertEqual(sorted(t.created), sorted(order), "the re-run finishes the rest, no duplicates")


# ---- dispatch -----------------------------------------------------------------------------------

class DispatchTests(unittest.TestCase):
    def test_demo_runs_clean(self):
        import contextlib
        import io
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(milestone_emit.main(["demo"]), 0)

    def test_main_usage_on_unknown_verb(self):
        import contextlib
        import io
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(milestone_emit.main(["bogus"]), 2)
            self.assertEqual(milestone_emit.main([]), 2)

    def test_emit_without_env_is_a_plain_usage_error(self):
        import contextlib
        import io
        saved = {k: os.environ.pop(k, None) for k in ("GITHUB_REPOSITORY", "GITHUB_TOKEN")}
        try:
            with contextlib.redirect_stderr(io.StringIO()) as err:
                rc = milestone_emit.main(["emit"])
            self.assertEqual(rc, 2)
            self.assertIn("GITHUB_REPOSITORY", err.getvalue())
        finally:
            for k, v in saved.items():
                if v is not None:
                    os.environ[k] = v

    def test_show_in_a_repo_without_a_build_order_is_clean(self):
        # This construction repo has no docs/spec/build-plan.md, so show is a clean no-op.
        import contextlib
        import io
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(milestone_emit.main(["show"]), 0)


if __name__ == "__main__":
    unittest.main()
