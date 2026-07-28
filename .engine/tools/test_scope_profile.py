"""The change-profile tool renders a plain-language, report-only account of a pull request's shape, and
rides on a SOFT Behaviors nudge that never blocks a merge. These tests attest the facts a non-engineer
cannot read off the code: the aggregation is exact, files are classified by the wiring map's own kinds
(an uncatalogued file falls to 'other'), the render is plain language, and the check is registered, soft,
and schema-conformant. The rendered-against-real-graph behaviour is covered by demo_scope_profile."""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate               # noqa: E402
import scope_profile          # noqa: E402
import scope_behaviors_check  # noqa: E402
import demo_scope_profile     # noqa: E402
import quiet_call             # noqa: E402  (capture the demo walkthrough so it can't bury the summary)

CHECK_REL = ".engine/check/pr-behaviors-declared.json"
MANIFEST_REL = ".engine/modules/validators-core/manifest.json"


class TestAggregation(unittest.TestCase):
    """profile() is pure arithmetic over a changed-file list — no git, no I/O."""

    def test_counts_and_line_totals_sum_exactly(self):
        rows = [(10, 2, "a"), (5, 0, "b"), (0, 8, "c")]
        self.assertEqual(
            scope_profile.profile(rows, {}),
            {"files": 3, "added": 15, "deleted": 10,
             "kinds": {"other": 3}, "areas": {"a": 1, "b": 1, "c": 1}},
        )

    def test_empty_diff_is_zeroed_not_an_error(self):
        self.assertEqual(
            scope_profile.profile([], {}),
            {"files": 0, "added": 0, "deleted": 0, "kinds": {}, "areas": {}},
        )

    def test_binary_files_count_as_zero_lines(self):
        # changed_files records a '-' numstat count as 0; profile must tolerate it.
        prof = scope_profile.profile([(0, 0, "logo.png")], {})
        self.assertEqual((prof["files"], prof["added"], prof["deleted"]), (1, 0, 0))


class TestSurfaceKindJoin(unittest.TestCase):
    """A file's kind is the wiring map's own `type`; anything uncatalogued is 'other'."""

    def test_catalogued_paths_resolve_to_their_real_kind(self):
        # concrete, not tautological: known committed surfaces resolve to their real kinds
        kmap = scope_profile.kind_map()
        self.assertEqual(scope_profile.surface_kind(".engine/tools/validate.py", kmap), "tool")
        self.assertEqual(scope_profile.surface_kind(".engine/tools/weakening_guard.py", kmap), "tool")

    def test_uncatalogued_path_falls_to_other(self):
        kmap = scope_profile.kind_map()
        self.assertEqual(
            scope_profile.surface_kind(".engine/tools/__not_a_real_surface__.py", kmap), "other")

    def test_this_tool_and_check_are_catalogued_after_regen(self):
        # After the graph is regenerated (build step), the new tool and check are catalogued surfaces.
        kmap = scope_profile.kind_map()
        self.assertEqual(kmap.get(".engine/tools/scope_profile.py"), "tool")
        self.assertEqual(kmap.get(".engine/tools/scope_behaviors_check.py"), "tool")
        self.assertEqual(kmap.get(CHECK_REL), "check")


class TestAreaBucketing(unittest.TestCase):
    def test_engine_and_claude_buckets_keep_two_segments(self):
        self.assertEqual(scope_profile.area(".engine/tools/x.py"), ".engine/tools")
        self.assertEqual(scope_profile.area(".claude/agents/y.md"), ".claude/agents")

    def test_github_collapses_to_one_bucket(self):
        self.assertEqual(scope_profile.area(".github/workflows/ci.yml"), ".github")

    def test_root_file_is_its_own_area(self):
        self.assertEqual(scope_profile.area("README.md"), "README.md")

    def test_file_directly_under_a_top_dir_buckets_to_that_dir(self):
        # a file sitting directly under .engine/ reads as ".engine", not its own filename, so the
        # "Where" line stays a clean list of directories.
        self.assertEqual(scope_profile.area(".engine/self-map.md"), ".engine")


class TestRenderIsPlainAndReportOnly(unittest.TestCase):
    def test_render_states_size_kinds_and_that_it_never_blocks(self):
        rows = [(10, 2, ".engine/tools/x.py"), (0, 8, "note.txt")]
        text = scope_profile.render(scope_profile.profile(rows, {".engine/tools/x.py": "tool"}), commits=2)
        self.assertIn("Change profile", text)
        self.assertIn("+10 / −10", text)
        self.assertIn("1 tool", text)
        self.assertIn("1 other file (not in the engine's map)", text)
        self.assertIn("never blocks", text)
        # plain language: no internal engine vocabulary leaks into the operator-facing render
        for jargon in ("subsystem", "dependency radius", "custom/script", "tier", "presence"):
            self.assertNotIn(jargon, text.lower())


class TestCheckIsRegisteredSoftAndConformant(unittest.TestCase):
    def test_check_is_soft_and_runs_the_behaviors_script(self):
        rule = json.loads(validate.read(os.path.join(validate.ROOT, CHECK_REL)))
        self.assertEqual(rule["tier"], "soft", "the Behaviors nudge must never block a merge")
        self.assertEqual(rule["target"]["context"], "pull-request-body")
        self.assertEqual(rule["kind"], "custom/script")
        self.assertEqual(rule["params"]["script"], ".engine/tools/scope_behaviors_check.py")

    def test_check_is_claimed_by_validators_core(self):
        manifest = json.loads(validate.read(os.path.join(validate.ROOT, MANIFEST_REL)))
        self.assertIn(CHECK_REL, manifest["provides"]["check"],
                      "a check must be claimed in a module's provides or it is an orphan")

    def test_template_carries_the_behaviors_subsection_not_a_ninth_section(self):
        template = validate.read(os.path.join(validate.ROOT, ".github/pull_request_template.md"))
        # a level-3 subsection under Scope — NOT a level-2 heading, which would break the
        # template<->completeness-check section lock (test_seed.py).
        self.assertIn("### Behaviors", template)
        self.assertNotIn("## Behaviors", template.replace("### Behaviors", ""))
        self.assertNotIn("Behaviors", validate.section_order(template))


class TestBehaviorsCheckScript(unittest.TestCase):
    """The soft check reads a `### Behaviors` subsection and nudges only when it is absent/unfilled."""

    _FILLED = ("## Scope\n\nstuff\n\n### Behaviors\n\n**The operator sees a profile.**\n\n"
               "- renders into Scope — test_scope_profile.py\n\n## Out of scope\n\nnone\n")
    _PLACEHOLDER = ("## Scope\n\nstuff\n\n### Behaviors\n\n**<the capabilities…>**\n\n"
                    "- <each behaviour…>\n\n## Out of scope\n\nnone\n")
    _ABSENT = "## Scope\n\nstuff\n\n## Out of scope\n\nnone\n"

    def test_filled_behaviors_yields_no_finding(self):
        self.assertIsNotNone(scope_behaviors_check._behaviors_block(self._FILLED))
        self.assertFalse(validate.is_empty_section(scope_behaviors_check._behaviors_block(self._FILLED)))

    def test_block_stops_at_the_next_section(self):
        block = scope_behaviors_check._behaviors_block(self._FILLED)
        self.assertNotIn("Out of scope", block)

    def test_absent_subsection_returns_none(self):
        self.assertIsNone(scope_behaviors_check._behaviors_block(self._ABSENT))

    def test_placeholder_only_reads_as_empty(self):
        self.assertTrue(validate.is_empty_section(scope_behaviors_check._behaviors_block(self._PLACEHOLDER)))

    def test_no_event_context_is_a_disclosed_soft_noop_not_a_false_nudge(self):
        # with no --pr-body-file and no $GITHUB_EVENT_PATH, get_pr_body returns None: the check must
        # disclose a soft no-op, never fabricate a nudge it cannot justify.
        saved = os.environ.pop("GITHUB_EVENT_PATH", None)
        try:
            out = scope_behaviors_check.findings()
        finally:
            if saved is not None:
                os.environ["GITHUB_EVENT_PATH"] = saved
        self.assertEqual(len(out), 1)
        self.assertTrue(out[0].get("not_applicable"))
        self.assertEqual(out[0]["severity"], "soft")

    def test_behaviors_heading_inside_a_code_fence_is_not_a_real_subsection(self):
        fenced = "## Scope\n\n```\n### Behaviors\nfake content\n```\n\n## Out of scope\n"
        self.assertIsNone(scope_behaviors_check._behaviors_block(fenced))

    def test_unreadable_event_is_a_soft_noop_never_a_hard_block(self):
        # a malformed event must not raise (a non-zero exit becomes a HARD fail-closed block); the
        # soft check degrades to a disclosed no-op instead.
        def boom(*_a, **_k):
            raise ValueError("bad event json")
        orig = validate.get_pr_body
        validate.get_pr_body = boom
        try:
            out = scope_behaviors_check.findings()
        finally:
            validate.get_pr_body = orig
        self.assertEqual(len(out), 1)
        self.assertTrue(out[0].get("not_applicable"))
        self.assertEqual(out[0]["severity"], "soft")


class _FakeProc:
    def __init__(self, stdout, returncode=0):
        self.stdout = stdout
        self.returncode = returncode


def _fake_run(stdout, returncode=0):
    return lambda *_a, **_k: _FakeProc(stdout, returncode)


class TestGitLayer(unittest.TestCase):
    """The git-facing layer: --numstat parsing, and signalling failure as None (never a fake zero)."""

    def test_changed_files_parses_numstat_including_binary_rows(self):
        out = "10\t2\t.engine/tools/x.py\n-\t-\tlogo.png\n0\t3\tnote.txt\n"
        rows = scope_profile.changed_files("base", run=_fake_run(out))
        self.assertEqual(rows, [(10, 2, ".engine/tools/x.py"), (0, 0, "logo.png"), (0, 3, "note.txt")])

    def test_git_failure_returns_none_not_empty_string(self):
        self.assertIsNone(scope_profile._git(["diff"], run=_fake_run("", returncode=1)))
        self.assertIsNone(scope_profile.changed_files("nope", run=_fake_run("", returncode=1)))

    def test_missing_git_binary_degrades_to_none(self):
        def boom(*_a, **_k):
            raise FileNotFoundError("git not on PATH")
        self.assertIsNone(scope_profile._git(["diff"], run=boom))

    def test_compute_surfaces_a_note_when_the_diff_cannot_be_read(self):
        text = scope_profile.compute("nope", run=_fake_run("", returncode=1))
        self.assertIn("could not read the diff", text)
        self.assertNotIn("0 files changed", text)  # never a fabricated zero for a real change


class TestBehaviorsSubsectionDoesNotWeakenCompleteness(unittest.TestCase):
    """Regression (found by the divergence-hunter): a bare `### Behaviors` heading inside `## Scope` must
    NOT flip a placeholder Scope to 'filled' and silently defeat the hard pr-body-completeness gate."""

    def test_bare_behaviors_heading_does_not_mark_a_placeholder_scope_filled(self):
        scope = ("**<one-line summary>**\n\n- <the specific items>\n\n*Impact: really filled*\n\n"
                 "### Behaviors\n\n**<the capabilities>**\n\n- <each behaviour>\n")
        self.assertTrue(validate.is_empty_section(scope, "Impact"),
                        "placeholder Scope + only a ### Behaviors heading must still read as empty")

    def test_a_genuinely_filled_scope_with_behaviors_passes(self):
        scope = ("**Adds a change-profile.**\n\n- the tool\n\n*Impact: filled*\n\n"
                 "### Behaviors\n\n**Operator sees a profile.**\n\n- renders — test_scope_profile.py\n")
        self.assertFalse(validate.is_empty_section(scope, "Impact"))


class TestDemo(unittest.TestCase):
    def test_demo_main_exits_zero(self):
        self.assertEqual(quiet_call.run(demo_scope_profile.main), 0)

    def test_classification_and_arithmetic_and_advisory_legs_hold(self):
        self.assertTrue(quiet_call.run(demo_scope_profile._classification_holds))
        self.assertTrue(quiet_call.run(demo_scope_profile._arithmetic_holds))
        self.assertTrue(quiet_call.run(demo_scope_profile._never_blocks_holds))


if __name__ == "__main__":
    unittest.main()
