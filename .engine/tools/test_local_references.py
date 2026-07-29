"""Tests for local-reference containment — the declared vocabulary and the outbound scan.

Verifies: the declaration reader tells its FOUR states apart (absent / empty / declared / unreadable), so
only one of them can license the claim "I checked and found none"; a declared string is escaped, so it can never act as a pattern;
each of the three declared shapes matches its own form and no other; `section_refs` catches a citation while
leaving alone the capability prose that names the same document (the discrimination the whole shape exists
for); the diff reader returns added lines with line numbers and reports an unreadable diff as UNINSPECTED
rather than empty; renames are not allowed to carry content past the scan; the declaration file does not
match itself; findings are soft and name only the matched token; the demo runs; and — the cases that
matter most — REAL `git diff` output round-trips through the real transport, including the config
settings and the content shapes that would otherwise make the scan report clean over an unread diff.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import local_references as lr  # noqa: E402


def _decl(tmp, obj):
    p = os.path.join(tmp, "operator-local-references.json")
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(obj if isinstance(obj, str) else __import__("json").dumps(obj))
    return p


class TestReaderStates(unittest.TestCase):
    def test_absent_is_the_silent_empty_state(self):
        with tempfile.TemporaryDirectory() as d:
            vocab, state = lr.load_vocabulary(os.path.join(d, "nope.json"))
        self.assertEqual((vocab, state), ([], lr.ABSENT))

    def test_unparseable_is_reported_distinctly_never_as_absent(self):
        # The distinction is the whole point: a caller that cannot tell these apart would narrate an unread
        # declaration as "checked and clean", which is a false claim of cleanliness.
        with tempfile.TemporaryDirectory() as d:
            vocab, state = lr.load_vocabulary(_decl(d, "{not json"))
        self.assertEqual((vocab, state), ([], lr.UNREADABLE))

    def test_a_non_object_declaration_is_unreadable_not_empty(self):
        with tempfile.TemporaryDirectory() as d:
            _vocab, state = lr.load_vocabulary(_decl(d, ["ACME-"]))
        self.assertEqual(state, lr.UNREADABLE)

    def test_a_declaration_that_lists_nothing_is_its_own_state(self):
        # NOT `declared`. An empty skeleton is the natural first thing an operator writes, and collapsing it
        # into `declared` would let a caller say "I checked and found none" with no pattern compiled and the
        # change never read — the exact false claim of cleanliness this module exists to prevent.
        with tempfile.TemporaryDirectory() as d:
            for decl in ({}, {"id_prefixes": [], "phrases": [], "section_refs": []}):
                self.assertEqual(lr.load_vocabulary(_decl(d, decl)), ([], lr.EMPTY), decl)

    def test_entries_that_were_listed_and_discarded_are_not_reported_as_none(self):
        # "nothing compiled" is NOT "nothing listed". An unrecognised key, a too-short entry or a non-string
        # was still something the operator stated; reporting it back as "you told me you have none" would
        # misstate their own instruction at the moment they authorise a one-way act on someone else's repo.
        with tempfile.TemporaryDirectory() as d:
            for decl in ({"ticket_ids": ["ACME-"]}, {"phrases": ["A"]}, {"phrases": [123]}):
                self.assertEqual(lr.load_vocabulary(_decl(d, decl)), ([], lr.UNUSABLE), decl)

    def test_a_real_declaration_compiles_and_reports_declared(self):
        with tempfile.TemporaryDirectory() as d:
            vocab, state = lr.load_vocabulary(_decl(d, {"id_prefixes": ["ACME-"]}))
        self.assertEqual(state, lr.DECLARED)
        self.assertEqual([(k, t) for k, t, _p in vocab], [("id_prefixes", "ACME-")])

    def test_degenerate_and_non_string_members_are_dropped(self):
        # Belt-and-braces behind the hard shape gate: a single character would match nearly every line.
        vocab = lr.compile_vocabulary({"id_prefixes": ["ACME-", "D", "", 7], "phrases": ["  "]})
        self.assertEqual([t for _k, t, _p in vocab], ["ACME-"])


class TestDeclaredStringsAreNeverPatterns(unittest.TestCase):
    def test_a_regex_metacharacter_is_matched_literally(self):
        # The declaration is operator text. If it were compiled unescaped, `.*` would match everything and a
        # malformed one would raise inside the reader.
        vocab = lr.compile_vocabulary({"phrases": [".*"]})
        self.assertEqual(lr.scan(vocab, lines=[("a.py", 1, "anything at all")]), [])
        hits = lr.scan(vocab, lines=[("a.py", 1, "the literal .* token")])
        self.assertEqual([h["token"] for h in hits], [".*"])

    def test_an_unbalanced_bracket_does_not_raise(self):
        vocab = lr.compile_vocabulary({"phrases": ["ACME-["]})
        self.assertEqual(lr.scan(vocab, lines=[("a.py", 1, "ACME-[ here")])[0]["token"], "ACME-[")


class TestShapes(unittest.TestCase):
    def setUp(self):
        self.vocab = lr.compile_vocabulary({
            "id_prefixes": ["ACME-"], "phrases": ["Acme Handbook"], "section_refs": ["acme-topology"]})

    def _tokens(self, text):
        return [h["token"] for h in lr.scan(self.vocab, lines=[("a.py", 1, text)])]

    def test_id_prefix_needs_digits_and_its_own_boundary(self):
        self.assertEqual(self._tokens("see ACME-156 for why"), ["ACME-156"])
        self.assertEqual(self._tokens("the AACME-156 part"), [])       # a letter on the left
        self.assertEqual(self._tokens("D- with no number"), [])     # no digits
        self.assertEqual(self._tokens("ACME-156-migration notes"), ["ACME-156"])  # hyphen-joined still counts

    def test_a_phrase_matches_only_on_its_own_boundaries(self):
        self.assertEqual(self._tokens("follow the Acme Handbook"), ["Acme Handbook"])
        self.assertEqual(self._tokens("the acme handbookish thing"), [])

    def test_section_ref_catches_a_citation_and_leaves_capability_prose_alone(self):
        # THE discrimination this shape exists for. The bare document name appears both in a citation (the
        # defect) and in prose naming the rule it stands for (the FIX). Matching the bare name would flag the
        # wording that resolves the defect — a check firing on its own remedy trains people to ignore it.
        self.assertEqual(self._tokens("kept out of git (acme-topology Law 5)"),
                         ["acme-topology Law 5"])
        self.assertEqual(self._tokens("stays a viewing surface — the acme-topology rule"), [])
        self.assertEqual(self._tokens("the acme-topology wall"), [])

    def test_section_markers_are_a_closed_set(self):
        for cited in ("acme-topology §4", "acme-topology Section 2", "acme-topology Law 5"):
            self.assertTrue(self._tokens(cited), cited)
        self.assertEqual(self._tokens("acme-topology paragraph 5"), [])


class TestScanSurfaces(unittest.TestCase):
    def setUp(self):
        self.vocab = lr.compile_vocabulary({"id_prefixes": ["ACME-"]})

    def test_a_path_name_is_scanned_as_well_as_a_line(self):
        hits = lr.scan(self.vocab, paths=["docs/ACME-156-migration.md", "src/ok.py"])
        self.assertEqual([(h["where"], h["token"]) for h in hits], [("docs/ACME-156-migration.md", "ACME-156")])

    def test_the_pull_request_prose_is_scanned(self):
        # The body travels to the other repository exactly as the diff does, and is where this project's own
        # convention parks decision references — so a clean diff with a citation in the body is not clean.
        hits = lr.scan(self.vocab, blobs={"the pull-request description": "line one\nper ACME-309, restore it"})
        self.assertEqual([(h["where"], h["line"], h["token"]) for h in hits],
                         [("the pull-request description", 2, "ACME-309")])

    def test_only_the_real_declaration_path_is_skipped(self):
        # Skipping by file name would make any similarly-named file a scan-free zone.
        hits = lr.scan(self.vocab, lines=[("docs/operator-local-references.json", 1, "cites ACME-156"),
                                          ("vendor/x-operator-local-references.json", 1, "cites ACME-777")])
        self.assertEqual(sorted(h["token"] for h in hits), ["ACME-156", "ACME-777"])

    def test_the_declaration_file_does_not_match_itself(self):
        # Its own added lines contain the declared strings by definition; reporting the operator's vocabulary
        # back to them as a leak on every edit to it would be pure noise.
        self.assertEqual(lr.scan(self.vocab, lines=[(lr.DECLARATION_REL, 2, '  "id_prefixes": ["ACME-156"]')]), [])


class TestDiffReader(unittest.TestCase):
    _DIFF = (b"diff --git a/src/app.py b/src/app.py\n"
             b"--- a/src/app.py\n+++ b/src/app.py\n"
             b"@@ -0,0 +12,2 @@\n+first added line\n+second added line\n"
             b"@@ -40,1 +50,1 @@\n-a removed line\n+a later added line\n")

    def test_added_lines_carry_their_path_and_line_numbers(self):
        lines, inspected = lr.added_lines("upstream/main", run=lambda *_a, **_k: self._DIFF)
        self.assertTrue(inspected)
        self.assertEqual(lines, [("src/app.py", 12, "first added line"),
                                 ("src/app.py", 13, "second added line"),
                                 ("src/app.py", 50, "a later added line")])

    def test_a_removed_line_is_not_scanned(self):
        lines, _ = lr.added_lines("upstream/main", run=lambda *_a, **_k: self._DIFF)
        self.assertNotIn("a removed line", [t for _p, _n, t in lines])

    def test_an_unreadable_diff_is_uninspected_not_empty(self):
        # ([], False) and ([], True) must never collapse: the first is an unknown change, the second a clean
        # one, and a caller that treats them alike narrates cleanliness on something it never read.
        self.assertEqual(lr.added_lines("upstream/main", run=lambda *_a, **_k: None), ([], False))
        self.assertEqual(lr.added_lines("upstream/main", run=lambda *_a, **_k: b""), ([], True))

    def test_undecodable_bytes_cost_a_character_not_the_whole_read(self):
        raw = b"+++ b/x.py\n@@ -0,0 +1 @@\n+caf\xe9 ACME-156\n"
        lines, inspected = lr.added_lines("upstream/main", run=lambda *_a, **_k: raw)
        self.assertTrue(inspected, "a file that is not valid UTF-8 must not collapse the whole diff read")
        self.assertIn("ACME-156", lines[0][2])

    def test_the_read_forbids_rename_detection(self):
        # A rename renders as a header with NO added lines, so a file MOVED into the contribution would carry
        # its references straight past an added-lines scan.
        seen = {}

        def _run(args, checkout=None, **_k):
            seen["args"] = args
            return b""
        lr.added_lines("upstream/main", run=_run)
        self.assertIn("--no-renames", seen["args"])


class TestAgainstRealGit(unittest.TestCase):
    """Round-trips REAL `git diff` output through the real transport, in a throwaway repository.

    The hand-authored diffs above cannot catch a mismatch between what git actually emits and what the parser
    expects — and every such mismatch makes the scan report "clean" over material it never read, which is the
    one property this feature is sold on. These cases exist because that gap is not hypothetical: an operator's
    own git configuration can change the output format, and a change's own content can be shaped like diff
    syntax."""

    def setUp(self):
        if not shutil.which("git"):
            self.skipTest("git is not available")
        self.repo = tempfile.mkdtemp(prefix="engine-lr-realgit-")
        self.addCleanup(shutil.rmtree, self.repo, True)
        self._git("init", "-q", "-b", "main")
        self._git("config", "user.email", "t@example.invalid")
        self._git("config", "user.name", "T")
        self._write("seed.txt", "seed\n")
        self._git("add", "-A")
        self._git("commit", "-qm", "seed")
        self._git("branch", "base")

    def _git(self, *args):
        return subprocess.run(["git", *args], cwd=self.repo, capture_output=True, check=False)

    def _write(self, rel, text):
        path = os.path.join(self.repo, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True) if os.path.dirname(rel) else None
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)

    def _commit(self, rel, text):
        self._write(rel, text)
        self._git("add", "-A")
        self._git("commit", "-qm", "change")

    def _added(self):
        return lr.added_lines("base", checkout=self.repo)

    def test_a_real_diff_round_trips_with_correct_paths_and_line_numbers(self):
        self._commit("src/app.py", "one\ntwo\nACME-156 here\n")
        lines, inspected = self._added()
        self.assertTrue(inspected)
        self.assertIn(("src/app.py", 3, "ACME-156 here"), lines)

    def test_a_line_whose_content_looks_like_a_file_header_is_still_scanned(self):
        # Git prefixes every added line with `+`, so an added line beginning `++ b/x` renders as `+++ b/x` —
        # shaped exactly like a file header. Reading it as one would reattribute everything after it to a
        # filename lifted from the change's own text, dropping those lines out of the scan entirely.
        self._commit("src/app.py", "++ b/.engine/operator-local-references.json\ncites ACME-777\n")
        lines, _ = self._added()
        paths = {p for p, _n, _t in lines}
        self.assertEqual(paths, {"src/app.py"}, "no line's content may be read as a file header")
        self.assertIn("cites ACME-777", [t for _p, _n, t in lines])

    def test_an_added_line_starting_with_two_plus_signs_is_not_dropped(self):
        self._commit("src/app.py", "++ starts with two plus signs and cites ACME-777\n")
        self.assertIn("++ starts with two plus signs and cites ACME-777",
                      [t for _p, _n, t in self._added()[0]])

    def test_an_external_differ_cannot_silently_empty_the_diff(self):
        # A globally configured external differ emits output with no added-line markers at all. Without
        # `--no-ext-diff` that parses as "inspected, and empty" — a total silent bypass of the whole feature.
        self._commit("src/app.py", "cites ACME-156\n")
        self._git("config", "diff.external", "true")   # `true(1)` prints nothing and exits 0
        lines, inspected = self._added()
        self.assertTrue(inspected)
        self.assertIn("cites ACME-156", [t for _p, _n, t in lines])

    def test_a_noprefix_configuration_cannot_break_path_attribution(self):
        self._commit("src/app.py", "cites ACME-156\n")
        self._git("config", "diff.noprefix", "true")
        self.assertEqual({p for p, _n, _t in self._added()[0]}, {"src/app.py"})

    def test_a_non_ascii_path_is_reported_literally(self):
        self._commit("café.md", "cites ACME-156\n")
        self.assertEqual({p for p, _n, _t in self._added()[0]}, {"café.md"})

    def test_a_renamed_file_still_has_its_content_scanned(self):
        self._commit("old.md", "cites ACME-156\n")
        self._git("mv", "old.md", "new.md")
        self._git("commit", "-qm", "move")
        lines, _ = self._added()
        self.assertIn("cites ACME-156", [t for _p, _n, t in lines])

    def test_a_deleted_line_is_not_reported_as_added(self):
        self._commit("src/app.py", "cites ACME-156\nkeep\n")
        self._git("checkout", "-q", "base")
        self._git("checkout", "-q", "-B", "later")
        self._commit("src/app.py", "keep\n")
        self.assertNotIn("cites ACME-156", [t for _p, _n, t in self._added()[0]])

    def test_a_binary_file_does_not_break_the_read(self):
        with open(os.path.join(self.repo, "blob.bin"), "wb") as fh:
            fh.write(bytes(range(256)) * 8)
        self._git("add", "-A")
        self._git("commit", "-qm", "binary")
        self._commit("src/app.py", "cites ACME-156\n")
        lines, inspected = self._added()
        self.assertTrue(inspected, "a binary file must not collapse the whole read")
        self.assertIn("cites ACME-156", [t for _p, _n, t in lines])

    def test_a_file_the_project_marks_undiffable_is_still_scanned(self):
        # `.gitattributes` `-diff` is an ordinary setting on lock files and generated assets. Git then prints
        # "Binary files differ" and emits NO added lines — the read still succeeds, so without `--text` the
        # scan reports clean over a file it never saw. The same silent carry `--no-renames` already closes.
        self._write(".gitattributes", "hidden.py -diff\n")
        self._commit("hidden.py", "cites ACME-999\n")
        lines, inspected = self._added()
        self.assertTrue(inspected)
        self.assertIn("cites ACME-999", [t for _p, _n, t in lines])

    def test_a_page_separator_does_not_truncate_the_rest_of_a_file(self):
        # A form feed is the conventional page separator in Python and C source, and a lone carriage return
        # turns up in any file with mixed line endings. Python's splitlines() breaks on both; git counts a
        # line as ending at a newline and nothing else. Counting with the wrong notion of "line" desyncs the
        # hunk count and silently discards the rest of the file — while still reporting it inspected.
        self._commit("src.py", "harmless\npage\x0cbreak\ncites ACME-156\nand ACME-999\n")
        lines, inspected = self._added()
        self.assertTrue(inspected)
        vocab = lr.compile_vocabulary({"id_prefixes": ["ACME-"]})
        self.assertEqual(sorted(h["token"] for h in lr.scan(vocab, lines=lines)),
                         ["ACME-156", "ACME-999"], "content after a page separator must still be scanned")

    def test_a_lone_carriage_return_does_not_truncate_the_rest_of_a_file(self):
        self._commit("src.py", "harmless\nmixed\rendings\ncites ACME-156\n")
        lines, _ = self._added()
        vocab = lr.compile_vocabulary({"id_prefixes": ["ACME-"]})
        self.assertEqual([h["token"] for h in lr.scan(vocab, lines=lines)], ["ACME-156"])

    def test_the_end_to_end_scan_finds_a_real_reference_through_the_real_transport(self):
        self._commit("docs/ACME-156-migration.md", "see acme-topology Law 5\nthe acme-topology rule\n")
        lines, _ = self._added()
        paths, _ok = lr.changed_paths("base", checkout=self.repo)
        vocab = lr.compile_vocabulary({"id_prefixes": ["ACME-"], "section_refs": ["acme-topology"]})
        tokens = sorted(h["token"] for h in lr.scan(vocab, lines=lines, paths=paths))
        self.assertEqual(tokens, ["ACME-156", "acme-topology Law 5"],
                         "the citation and the path id are caught; the capability prose is not")


class TestFindings(unittest.TestCase):
    def test_no_hits_is_no_finding(self):
        self.assertEqual(lr.findings("soft", []), [])

    def test_a_finding_is_soft_and_names_only_the_matched_token(self):
        # The message is published verbatim into a GitHub Issue title and body, so it must never carry the
        # surrounding source line — that line could contain anything the change happened to touch.
        hits = lr.scan(lr.compile_vocabulary({"id_prefixes": ["ACME-"]}),
                       lines=[("src/app.py", 9, "SECRET_TOKEN = 'xyz'  # per ACME-156")])
        fs = lr.findings("hard", hits)          # tier is deliberately not honoured for the scan legs
        self.assertEqual(fs[0]["severity"], "soft")
        self.assertIn("ACME-156", fs[0]["message"])
        self.assertNotIn("SECRET_TOKEN", fs[0]["message"])

    def test_an_implausibly_broad_declaration_says_so_in_its_own_finding(self):
        # Breadth surfaces here — on the bounded diff — rather than in the merge gate, which cannot walk a
        # deployment's whole tree without risking a hard red over that tree's size or encoding.
        vocab = lr.compile_vocabulary({"phrases": ["the"]})
        hits = lr.scan(vocab, lines=[("a.py", n, "the thing") for n in range(40)])
        self.assertIn("too broad", lr.findings("soft", hits)[0]["message"])


class TestPublishedTokenIsBounded(unittest.TestCase):
    def test_a_long_digit_run_cannot_produce_an_unbounded_published_token(self):
        # An id prefix is followed by `\\d+` with no limit, and the matched token is embedded verbatim in an
        # engine-opened GitHub Issue.
        vocab = lr.compile_vocabulary({"id_prefixes": ["ACME-"]})
        hits = lr.scan(vocab, lines=[("a.py", 1, "ACME-" + "9" * 4000)])
        self.assertLessEqual(len(hits[0]["token"]), lr.TOKEN_CAP + 1)
        self.assertLess(len(lr.findings("soft", hits)[0]["message"]), 500)


class TestCLI(unittest.TestCase):
    def test_demo_runs_green(self):
        self.assertEqual(lr.main(["demo"]), 0)


if __name__ == "__main__":
    unittest.main()
