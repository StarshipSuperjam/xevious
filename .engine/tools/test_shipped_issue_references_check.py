"""Tests for engine/check/shipped-issue-references — the shipped bare-issue-reference floor
(engine-template StarshipSuperjam/engine-template#640).

Run: uv run --directory .engine --frozen -- python tools/selftest.py
"""
from __future__ import annotations
import os
import re
import sys
import tempfile
import textwrap
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import shipped_issue_references_check as check  # noqa: E402
import validate  # noqa: E402


class _ForceHome(unittest.TestCase):
    """Base for the detection tests: pin the home-repo gate True so the check's scan logic runs deterministically
    wherever the suite runs. `check()` no-ops outside the home repo, and its `_in_home_repo()` reads the AMBIENT
    `validate.ROOT` — NOT the fixture root a test passes to `check(root)` — so inside the deployment gate's
    projected deployed tree (foreign origin) these fixture tests would silently get `[]` and fail. Pinning the
    gate is the exact idiom the two sibling home-scoped checks use (test_census_completeness,
    test_memory_pointer_public_safety); it does not weaken the check's real runtime home-scoping. Underscore-named
    with no `test_` methods so discovery collects nothing from it."""

    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(check, "_in_home_repo", lambda: True)
        patcher.start()
        self.addCleanup(patcher.stop)


class TestMatcher(unittest.TestCase):
    """The two-pass matcher — the load-bearing piece the cold review flagged."""

    def test_a_plain_bare_reference_is_found(self):
        self.assertEqual(check._find_refs("see #495 for detail"), ["#495"])

    def test_a_chain_tail_is_not_lost(self):
        # the exact blocking finding: a naive /-lookbehind drops the tail of a chain like #N/#M
        self.assertEqual(check._find_refs("chain #862/#923 both"), ["#862", "#923"])

    def test_a_triple_chain_is_fully_found(self):
        self.assertEqual(check._find_refs("triple #1/#2/#3"), ["#1", "#2", "#3"])

    def test_the_slice_suffix_form_is_found(self):
        self.assertEqual(check._find_refs("slice #754a and #754b"), ["#754a", "#754b"])

    def test_a_fully_qualified_reference_is_left_alone(self):
        self.assertEqual(check._find_refs("StarshipSuperjam/engine-template#640 ok"), [])

    def test_a_fixture_owner_repo_reference_is_left_alone(self):
        # the close-linkage fixtures use octo/o#9 etc. — genuinely qualified, must not be flagged
        self.assertEqual(check._find_refs("fixture octo/o#9 and owner/repo#5"), [])

    def test_a_deep_path_qualified_tail_is_left_alone(self):
        self.assertEqual(check._find_refs("some/deep/path#5 tail"), [])

    def test_a_vocab_subword_token_is_not_a_reference(self):
        self.assertEqual(check._find_refs("vocab ##2 token"), [])

    def test_a_url_fragment_is_not_a_reference(self):
        self.assertEqual(check._find_refs("https://x/issues/123 tail"), [])

    def test_the_partly_qualified_forms_are_found(self):
        self.assertEqual(check._find_refs("engine-template#902 partial"), ["engine-template#902"])
        self.assertEqual(check._find_refs("engine-template #37 spaced"), ["engine-template #37"])

    def test_the_swept_output_is_not_re_flagged(self):
        # the sweep rewrites to StarshipSuperjam/engine-template#N — the check must not re-flag its own output
        self.assertEqual(check._find_refs("fixed to StarshipSuperjam/engine-template#902 now"), [])


class TestCarveOuts(unittest.TestCase):
    """The closed carve-out set — an ordinal or a PR-linkage clause is legitimate and must not be flagged,
    while a real reference wearing a similar word (a bare `check #N`) must still be caught."""

    def test_concern_ordinal_is_carved(self):
        self.assertEqual(check._find_refs("treat concern #2 as unreviewed"), [])

    def test_required_check_ordinal_is_carved(self):
        self.assertEqual(check._find_refs("the required check #1 and required check #2"), [])

    def test_a_real_reference_after_the_word_check_is_still_flagged(self):
        # a bare `check #N` is a real issue reference, NOT the CI-required-check ordinal — must be caught
        self.assertEqual(check._find_refs("the coverage check #663 failed"), ["#663"])

    def test_the_number_one_trust_ordinal_is_carved(self):
        self.assertEqual(check._find_refs("the #1 trust dependency"), [])

    def test_a_real_the_reference_is_still_flagged(self):
        # a bare `the #N footgun` is a real reference, not the ordinal-adjective form
        self.assertEqual(check._find_refs("the #665 footgun repair"), ["#665"])

    def test_pr_linkage_grammar_is_carved(self):
        self.assertEqual(check._find_refs("This work Closes #274 as it lands"), [])
        self.assertEqual(check._find_refs("Fixes #5 and resolved #6"), [])

    def test_the_comma_trap_is_carved_whole(self):
        # `Closes #1, #2` links only the first on GitHub — it is documented grammar, both refs stay bare
        self.assertEqual(check._find_refs("the rule Closes #1, #2 closes only the first"), [])

    def test_a_stray_reference_beside_a_linkage_clause_is_still_flagged(self):
        # only the linkage clause is carved; a genuine ref elsewhere on the line is caught
        self.assertEqual(check._find_refs("Part of #495 -- see also #862"), ["#862"])

    def test_ordinal_nouns_are_carved(self):
        # a numbered THING, never an issue reference — no real ref wears these nouns
        for s in ("step #3 of the flow", "option #1", "item #4 in the list", "tier #2", "phase #5"):
            self.assertEqual(check._find_refs(s), [], s)

    def test_the_single_digit_ordinal_adjective_is_carved(self):
        self.assertEqual(check._find_refs("the #2 priority here"), [])

    def test_a_multi_digit_the_reference_is_still_flagged(self):
        # "the #665 footgun" is a real reference — only SINGLE-digit "the #N <word>" is an ordinal
        self.assertEqual(check._find_refs("the #665 footgun repair"), ["#665"])


class TestBridge(unittest.TestCase):
    """A carve-out that wraps across a line boundary must still be recognised (the `concern` on line N, its
    `#N` opening line N+1 behind that line's own comment leader)."""

    def test_a_wrapped_ordinal_is_carved_via_the_prefix(self):
        prev = "so the persona still discloses concern"
        self.assertEqual(check._find_refs("#1's gap rather than skipping", prev), [])

    def test_a_real_reference_after_an_unrelated_prev_line_is_still_flagged(self):
        self.assertEqual(check._find_refs("see #495 here", "an ordinary previous line"), ["#495"])

    def test_a_linkage_clause_is_not_bridged_across_lines(self):
        # a genuine ref opening a line after a sentence merely ENDING in "resolve" must NOT be swallowed —
        # linkage is current-line-only, unlike the ordinal carve-outs (else a real reference is missed)
        self.assertEqual(check._find_refs("#623 is the timeout", "the retry loop will resolve"), ["#623"])


class TestProseExtractionPython(_ForceHome):
    """`.py`: comments and docstrings are scanned; string literals are NOT."""

    def _run(self, body: str):
        with tempfile.TemporaryDirectory() as root:
            _seed_tree(root, {".engine/tools/probe.py": textwrap.dedent(body)})
            return check.check(root)

    def test_a_reference_in_a_comment_is_flagged(self):
        f = self._run('def g():\n    # see #495 here\n    return 1\n')
        self.assertEqual(len(f), 1)
        self.assertIn("#495", f[0]["message"])

    def test_a_reference_in_a_module_docstring_is_flagged(self):
        f = self._run('"""A module that mentions #495 in prose."""\nx = 1\n')
        self.assertEqual(len(f), 1)

    def test_a_reference_in_a_function_docstring_is_flagged(self):
        f = self._run('def g():\n    """Handles the #495 case."""\n    return 1\n')
        self.assertEqual(len(f), 1)

    def test_a_reference_inside_a_string_literal_is_NOT_flagged(self):
        # the load-bearing exclusion: assertion data / behaviour-bearing strings must not be swept
        f = self._run('def g():\n    msg = "Closes #274"\n    other = "see #495"\n    return msg, other\n')
        self.assertEqual(f, [])

    def test_a_chain_in_a_comment_is_fully_flagged(self):
        f = self._run('def g():\n    # both #862/#923 apply\n    return 1\n')
        self.assertEqual(len(f), 1)
        self.assertIn("#862", f[0]["message"])
        self.assertIn("#923", f[0]["message"])


class TestScanDomain(_ForceHome):
    """The shipped surface: `.engine/**` minus retire minus excluded, plus foundation outside `.engine/`."""

    def test_a_retired_file_is_not_scanned(self):
        with tempfile.TemporaryDirectory() as root:
            _seed_tree(root, {
                ".engine/tools/engine_development.py": '"""Retires at first run; mentions #495."""\n',
                ".engine/provisioning/first-run-assets.json":
                    '{"files": [".engine/tools/engine_development.py"], "dirs": []}',
            })
            self.assertEqual(check.check(root), [])

    def test_a_file_in_a_retired_directory_is_not_scanned(self):
        with tempfile.TemporaryDirectory() as root:
            _seed_tree(root, {
                ".engine/tools/demo/demo_x.py": '"""A demo mentioning #495."""\n',
                ".engine/provisioning/first-run-assets.json":
                    '{"files": [], "dirs": [".engine/tools/demo"]}',
            })
            self.assertEqual(check.check(root), [])

    def test_an_excluded_path_is_not_scanned(self):
        with tempfile.TemporaryDirectory() as root:
            _seed_tree(root, {
                ".engine/knowledge/graph.json": '{"description": "derived, mentions #495"}',
                ".engine/tools/memory/semantic/vocab.txt": "##2\n##100\n",
                ".engine/_fixtures/x/probe.py": "# a fixture with #495\n",
            })
            self.assertEqual(check.check(root), [])

    def test_test_and_demo_files_are_not_scanned(self):
        # test/demo prose is synthetic scenario data (fixture numbers), not references — excluded by design
        with tempfile.TemporaryDirectory() as root:
            _seed_tree(root, {
                ".engine/tools/test_probe.py": '"""A test mentioning #495 in a comment."""\n# opens #6\n',
                ".engine/tools/demo_probe.py": '"""A demo mentioning #495."""\n',
            })
            self.assertEqual(check.check(root), [])

    def test_the_pull_request_template_is_not_scanned(self):
        with tempfile.TemporaryDirectory() as root:
            _seed_tree(root, {".github/pull_request_template.md": "Closes #123 (your issue)\nsee #495\n"})
            self.assertEqual(check.check(root), [])

    def test_a_foundation_file_outside_engine_is_scanned(self):
        with tempfile.TemporaryDirectory() as root:
            _seed_tree(root, {"CLAUDE.md": "This mentions #495 in prose.\n"})
            f = check.check(root)
            self.assertEqual(len(f), 1)
            self.assertEqual(f[0]["location"]["file"], "CLAUDE.md")

    def test_a_product_territory_root_file_is_not_scanned(self):
        # README.md / SECURITY.md are seeds — product territory that never ships; a bare #N there is correct
        with tempfile.TemporaryDirectory() as root:
            _seed_tree(root, {"README.md": "see #495\n", "SECURITY.md": "see #640\n"})
            self.assertEqual(check.check(root), [])

    def test_json_scans_only_prose_keyed_lines(self):
        with tempfile.TemporaryDirectory() as root:
            _seed_tree(root, {".engine/check/x.json":
                              '{\n  "version": "1",\n  "why": "closes the #495 gap"\n}\n'})
            f = check.check(root)
            self.assertEqual(len(f), 1)  # the prose "why" line, not the "version" value


class TestFailClosed(_ForceHome):
    def test_a_missing_retire_manifest_fails_closed(self):
        with tempfile.TemporaryDirectory() as root:
            _seed_tree(root, {".engine/tools/probe.py": "# clean\n"}, with_manifest=False)
            f = check.check(root)
            self.assertEqual(len(f), 1)
            self.assertIn("can't read the list of files", f[0]["message"])


class TestHomeScoped(unittest.TestCase):
    def test_no_op_outside_the_home_repo(self):
        orig = check._in_home_repo
        try:
            check._in_home_repo = lambda: False
            with tempfile.TemporaryDirectory() as root:
                _seed_tree(root, {".engine/tools/probe.py": "# see #495\n",
                                  ".engine/provisioning/first-run-assets.json": '{"files": [], "dirs": []}'})
                self.assertEqual(check.check(root), [])
        finally:
            check._in_home_repo = orig


class TestRealTreeAndFixture(unittest.TestCase):
    def test_the_committed_negative_fixture_bites(self):
        fixture = os.path.join(validate.ROOT, ".engine", "_fixtures", "shipped-issue-references")
        # Pin the gate for THIS method only: it scans the committed fixture (which survives the deployment gate's
        # projection — `.engine/_fixtures` is not a first-run retired asset), and the check would otherwise no-op
        # against the projection's foreign origin. The class's other two methods are deliberately mode-aware
        # (one relies on the no-op, one self-skips) and must NOT be forced home — hence a local patch, not a base.
        with mock.patch.object(check, "_in_home_repo", lambda: True):
            f = check.check(fixture)
        self.assertTrue(any("resolves to THAT repository's own issue" in x["message"] for x in f),
                        "the negative fixture no longer bites the check")

    def test_the_real_engine_tree_is_clean(self):
        # the whole point after the sweep: no bare reference ships. Runs only in the home repo (where the
        # gate is True); elsewhere check() no-ops and this is trivially satisfied.
        self.assertEqual(check.check(), [], "a bare issue reference still ships in the template")

    def test_reconciliation_no_prose_hit_escapes_the_matcher(self):
        # an INDEPENDENT naive scan of the same prose regions must find nothing the check missed — the
        # structural defence against a matcher blind spot (a chain tail, a new shape) silently under-sweeping.
        if not check._in_home_repo():
            self.skipTest("home-scoped: reconciliation runs in the engine's own repo")
        retire = check._retire_set(validate.ROOT)
        self.assertIsNotNone(retire, "the retire census must be readable in the home repo")
        rf, rd = retire
        flagged = {_loc(x) for x in check.check()}
        naive = re.compile(r"(?<![\w#])#\d+")
        qual = re.compile(r"[A-Za-z][\w.-]*/[\w.-]+#\d+")
        missed = []
        for rel in check._scan_targets(validate.ROOT, rf, rd):
            try:
                with open(os.path.join(validate.ROOT, rel), encoding="utf-8") as fh:
                    text = fh.read()
            except OSError:
                continue
            if rel.endswith(".py"):
                import ast
                try:
                    ast.parse(text)
                except SyntaxError:
                    continue
                frags = check._py_prose_fragments(text)
            else:
                # Independent of the check's line selection: scan EVERY raw line (not the JSON prose-key
                # filter), so a bare ref in a JSON value on a key missing from _JSON_PROSE_KEYS surfaces here
                # as an un-accounted hit — the drift canary that forces the allowlist to be completed.
                frags = [(i, line) for i, line in enumerate(text.splitlines(), start=1)]
            pbl: dict = {}
            for ln, fr in frags:
                pbl[ln] = (pbl.get(ln, "") + " " + check._prose(fr)).strip()
            for ln in sorted(pbl):
                prev = pbl.get(ln - 1, "")
                combined = (prev + " " + pbl[ln]) if prev else pbl[ln]
                off = len(prev) + 1 if prev else 0
                masked = qual.sub(lambda mm: " " * len(mm.group()), combined)
                for pat in check._ORDINAL_CARVEOUTS:   # ordinals bridge; linkage does not (mirror the check)
                    masked = pat.sub(lambda mm: " " * len(mm.group()), masked)
                cur = check._LINKAGE_CARVEOUT.sub(lambda mm: " " * len(mm.group()), masked[off:])
                cur = check._PARTIAL.sub(lambda mm: " " * len(mm.group()), cur)
                if naive.search(cur) and (rel, ln) not in flagged:
                    missed.append((rel, ln))
        self.assertEqual(missed, [], f"a naive scan found references the check missed: {missed[:10]}")


def _loc(finding: dict):
    g = re.search(r"`([^`]+)` line (\d+)", finding["message"])
    return (g.group(1), int(g.group(2)))


def _seed_tree(root: str, files: dict, with_manifest: bool = True) -> None:
    """Seed a mini checkout under `root`. Every value is written at its repo-relative path; unless a test opts
    out (or supplies its own), a default empty retire manifest is added so the scan is not the fail-closed path.
    Note: the home-repo gate is NOT driven by this seeded tree — `check()`'s `_in_home_repo()` reads the AMBIENT
    `validate.ROOT`, not the passed root — so the detection tests pin the gate via the `_ForceHome` base instead
    (that is what lets them run inside the deployment gate's foreign-origin projection)."""
    for rel, content in files.items():
        path = os.path.join(root, *rel.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content)
    manifest = os.path.join(root, ".engine", "provisioning", "first-run-assets.json")
    if with_manifest and not os.path.exists(manifest):
        os.makedirs(os.path.dirname(manifest), exist_ok=True)
        with open(manifest, "w", encoding="utf-8") as fh:
            fh.write('{"files": [], "dirs": []}')


if __name__ == "__main__":
    unittest.main()
