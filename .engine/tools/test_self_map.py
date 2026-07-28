#!/usr/bin/env python3
"""Self-tests for the self-map (surface-level + wiring-graph) + its drift gate.

Run: uv run --directory .engine --frozen -- python -m unittest discover -s tools -p 'test_*.py' -b

These lock the self-map's defenses: the map is DERIVED (sorted, deterministic, no volatile content);
it is human-readable Markdown with NO `](` byte-sequence (so link-integrity passes); the fingerprint
gate is regenerate-and-compare (a hand-edit or a stale map is a HARD finding, in sync is a note, an
absent map is HARD); generate/check round-trip and are idempotent; the custom/script entry emits []
in sync and a hard finding on drift; and the META-TEST proves the committed `.engine/self-map.md`
equals what the real sources regenerate — so a forgotten regenerate fails the unit suite, not only CI.
"""
from __future__ import annotations
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate          # noqa: E402
import hooks             # noqa: E402  (the run_hook harness the commit-boundary regen rides)
import self_map          # noqa: E402
import self_map_check    # noqa: E402
import census_completeness_check as _ccc   # noqa: E402  (reuse its construction-repo marker read, not a new copy)

# The closed seam vocabulary, read live from the schema so the wires render cannot silently diverge.
MODULE_SCHEMA = validate.load_json(os.path.join(validate.SCHEMAS_DIR, "module.v1.json"))
WIRES_ENUM = set(MODULE_SCHEMA["properties"]["wires"]["items"]["properties"]["type"]["enum"])

# ---- fixtures (the render layer is pure, so these drive it without any IO) --------------------

def _surface(**over):
    base = {"class": "structured", "location": ".engine/x/", "purpose": "a demo surface",
            "authority": "mechanics-and-guidance", "lifecycle": "artifact",
            "governing_schema": None, "template": None}
    base.update(over)  # `class` is a reserved word, so callers set it via over, e.g. _surface(**{"class": "code"})
    return base


CATALOG = {"surfaces": {
    # tool: governing_schema + template both null -> render the `(none)` sentinel.
    "tool": _surface(location=".engine/tools/", purpose="machinery", **{"class": "code"}),
    # check: a non-null governing_schema + template -> both values must render (the full record).
    "check": _surface(location=".engine/check/", purpose="rules",
                      governing_schema="check.v1.json", template="../templates/check.md"),
}}

CORE = {"id": "core", "version": "0.0.0-dev", "status": "required",
        "provides": {"tool": [".engine/tools/*.py"], "check": [".engine/check/*.json"]},
        "depends": {}}
WIDGET = {"id": "widget", "version": "1.2.0", "status": "optional",
          "provides": {"skill": [".claude/skills/widget/SKILL.md"]},
          "depends": {"core": ">=0.0.0"},
          "wires": [{"type": "gitignore", "key": "widget", "lines": ["x/"]},
                    {"type": "hook", "event": "PreToolUse", "hook": {"type": "command",
                     "command": ".engine/tools/h.py"}}]}
ENGINE = {"engine_release": "0.0.0-dev", "packages": {"core": "0.0.0-dev"}, "identity": "solo"}


class TestRenderSurfaces(unittest.TestCase):
    def test_every_surface_and_field_rendered_sorted(self):
        out = "\n".join(self_map.render_surfaces(CATALOG["surfaces"]))
        self.assertIn("(2 surfaces)", out)
        for name in ("tool", "check"):
            self.assertIn(f"`{name}`", out)
        # EVERY governed field of the locked surface record is rendered (the name is now true):
        self.assertIn("machinery", out)                 # purpose
        self.assertIn("`.engine/tools/`", out)          # home / location
        self.assertIn("code", out)                      # class
        self.assertIn("mechanics-and-guidance", out)    # authority
        self.assertIn("artifact", out)                  # lifecycle
        self.assertIn("`check.v1.json`", out)           # governing_schema (non-null)
        self.assertIn("`../templates/check.md`", out)   # template (non-null)
        self.assertIn("(none)", out)                    # tool's null governing_schema/template sentinel
        # sorted: check's row precedes tool's row
        self.assertLess(out.index("`check`"), out.index("`tool`"))

    def test_pipe_in_a_value_is_escaped_not_a_new_column(self):
        cat = {"weird": _surface(purpose="a | b")}
        out = "\n".join(self_map.render_surfaces(cat))
        self.assertIn("a \\| b", out)


class TestRenderModule(unittest.TestCase):
    def test_core_block(self):
        out = "\n".join(self_map.render_module(CORE))
        self.assertIn("### `core` — version `0.0.0-dev` (required)", out)
        self.assertIn("- depends on: nothing", out)
        self.assertIn("- wires: none (this module adds no shared-state edits)", out)
        # provides groups sorted (check before tool)
        self.assertLess(out.index("check:"), out.index("tool:"))

    def test_wired_block_renders_types_only_sorted(self):
        out = "\n".join(self_map.render_module(WIDGET))
        self.assertIn("### `widget` — version `1.2.0` (optional)", out)
        self.assertIn("- depends on: `core` >=0.0.0", out)
        # wire TYPES, sorted, from the closed vocabulary — and NO per-type body
        self.assertIn("- wires: gitignore, hook", out)
        self.assertNotIn("PreToolUse", out)
        self.assertNotIn("lines", out)
        for t in ("gitignore", "hook"):
            self.assertIn(t, WIRES_ENUM)

    def test_version_reads_from_manifest_not_engine_packages(self):
        # render_module takes only the manifest; changing its version changes the render,
        # proving the per-module version source is the manifest's own `version` field.
        m = dict(WIDGET, version="9.9.9")
        self.assertIn("version `9.9.9`", "\n".join(self_map.render_module(m)))


class TestRenderModulesGraph(unittest.TestCase):
    """The Modules section renders the dependency graph in TOPOLOGICAL order with an explicit edge
    view — not a flat alphabetical block (defect a)."""

    # alpha depends on zebra, so the topological order is [zebra, alpha] — the REVERSE of
    # alphabetical [alpha, zebra]. This is the case that proves the sort is real, not coincidental.
    ZEBRA = {"id": "zebra", "version": "1.0.0", "status": "required", "provides": {}, "depends": {}}
    ALPHA = {"id": "alpha", "version": "1.0.0", "status": "optional", "provides": {},
             "depends": {"zebra": ">=1.0.0"}}

    def test_modules_render_in_topological_not_alphabetical_order(self):
        # MUTATION-BOUND: revert render_modules to a sort-by-id and this assertion FAILS (alpha < zebra
        # alphabetically, so the alphabetical order would render alpha's block before zebra's).
        out = "\n".join(self_map.render_modules([self.ALPHA, self.ZEBRA]))
        self.assertLess(out.index("### `zebra`"), out.index("### `alpha`"),
                        "a dependency must render before the module that depends on it")

    def test_wiring_graph_edge_view_is_rendered(self):
        out = "\n".join(self_map.render_modules([self.ALPHA, self.ZEBRA]))
        self.assertIn("The dependency graph", out)
        self.assertIn("`alpha` → `zebra`", out)          # the edge
        self.assertIn("`zebra` (no dependencies)", out)  # the root
        # locality: the edge lines carry no `](` link sequence (also backstopped by TestNoLinkSequence)
        edge_lines = [ln for ln in out.split("\n") if "→" in ln]
        self.assertTrue(edge_lines)
        for ln in edge_lines:
            self.assertNotIn("](", ln)


class TestRenderMapDeterminism(unittest.TestCase):
    def test_deterministic_and_order_independent(self):
        a = self_map.render_map(CATALOG, [CORE, WIDGET], ENGINE)
        b = self_map.render_map(CATALOG, [WIDGET, CORE], ENGINE)  # permuted module order
        self.assertEqual(a, b)
        # permuted catalog insertion order
        cat2 = {"surfaces": dict(reversed(list(CATALOG["surfaces"].items())))}
        self.assertEqual(a, self_map.render_map(cat2, [CORE, WIDGET], ENGINE))

    def test_clean_text_shape(self):
        out = self_map.render_map(CATALOG, [CORE, WIDGET], ENGINE)
        self.assertTrue(out.endswith("\n"))
        self.assertFalse(out.endswith("\n\n"))
        self.assertNotIn("\r", out)
        for ln in out.split("\n"):
            self.assertEqual(ln, ln.rstrip(), f"trailing whitespace on: {ln!r}")

    def test_header_carries_release_and_identity(self):
        out = self_map.render_map(CATALOG, [CORE], ENGINE)
        self.assertIn("`0.0.0-dev`", out)
        self.assertIn("`solo`", out)


class TestNoLinkSequence(unittest.TestCase):
    def test_no_bracket_paren_sequence(self):
        out = self_map.render_map(CATALOG, [CORE, WIDGET], ENGINE)
        self.assertNotIn("](", out)  # the exact pattern link-integrity's LINK_RE matches


class TestDriftLogic(unittest.TestCase):
    def test_in_sync_is_a_note(self):
        f = self_map.drift_finding("X", "X", "/r/.engine/self-map.md")
        self.assertEqual(f["severity"], "note")

    def test_drift_is_hard_with_fix(self):
        # real calls pass an absolute path (SELF_MAP_PATH); loc() makes it repo-relative
        f = self_map.drift_finding("X", "X tampered", self_map.SELF_MAP_PATH)
        self.assertEqual(f["severity"], "hard")
        self.assertIn("out of date", f["message"])
        self.assertIn("self_map.py generate", f["message"])
        self.assertEqual(f["location"]["file"], ".engine/self-map.md")

    def test_absent_is_hard(self):
        f = self_map.drift_finding("X", None, ".engine/self-map.md")
        self.assertEqual(f["severity"], "hard")
        self.assertIn("not been generated", f["message"])

    def test_finding_is_finding_v1_shaped(self):
        f = self_map.drift_finding("X", "Y", ".engine/self-map.md")
        self.assertEqual(set(f), {"severity", "message", "location"})


class TestGenerateCheckIO(unittest.TestCase):
    """Real sources (catalog/manifests/engine on disk) + a redirected committed path."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self._tmp.name, "self-map.md")

    def tearDown(self):
        self._tmp.cleanup()

    def test_generate_then_check_is_in_sync(self):
        # Freeze ONE live derivation so generate() and the following check() compare the same content
        # (check() re-derives canonical_map() internally). Re-deriving per call made this depend on the
        # live source tree staying byte-stable across the two calls, which flaked under full-suite
        # discovery (#202); the two-derivations-agree property is the job of TestSourceDeterminismRoundTrip.
        snapshot = self_map.canonical_map()
        with mock.patch.object(self_map, "canonical_map", return_value=snapshot):
            g = self_map.generate(self.path)
            self.assertEqual(g["severity"], "note")
            self.assertIn("Wrote", g["message"])
            self.assertEqual(self_map.check(self.path)["severity"], "note")

    def test_generate_is_idempotent(self):
        # Freeze ONE live derivation so the two back-to-back generate() calls compare identical content.
        # This test is about generate()'s idempotency/round-trip logic, not about whether two independent
        # live derivations agree — re-deriving canonical_map() per call made it flake under full-suite
        # discovery (#202). The real write/read round-trip (and the on-disk byte-equality below) still runs
        # on both calls; the determinism property itself is covered by TestSourceDeterminismRoundTrip.
        snapshot = self_map.canonical_map()
        with mock.patch.object(self_map, "canonical_map", return_value=snapshot):
            self_map.generate(self.path)
            first = self_map.read_committed(self.path)
            second_finding = self_map.generate(self.path)
            self.assertIn("already up to date", second_finding["message"])
            self.assertEqual(first, self_map.read_committed(self.path))

    def test_hand_edit_is_caught(self):
        self_map.generate(self.path)
        with open(self.path, "a", encoding="utf-8", newline="") as fh:
            fh.write("junk a human typed\n")
        self.assertEqual(self_map.check(self.path)["severity"], "hard")

    def test_absent_committed_file_is_hard_no_traceback(self):
        self.assertFalse(os.path.exists(self.path))
        f = self_map.check(self.path)
        self.assertEqual(f["severity"], "hard")
        self.assertIn("not been generated", f["message"])


class TestMetaCommittedInSync(unittest.TestCase):
    """The committed .engine/self-map.md must equal what the REAL sources regenerate."""

    def test_committed_map_matches_real_sources(self):
        committed = self_map.read_committed(self_map.SELF_MAP_PATH)
        self.assertIsNotNone(committed, "the self-map is not committed")
        self.assertEqual(committed, self_map.canonical_map(),
                         "committed self-map is out of date — run self_map.py generate")

    def test_committed_map_has_no_link_sequence_and_clean_shape(self):
        committed = self_map.read_committed(self_map.SELF_MAP_PATH)
        self.assertNotIn("](", committed)
        self.assertTrue(committed.endswith("\n"))
        self.assertFalse(committed.endswith("\n\n"))
        self.assertNotIn("\r", committed)


class TestCheckEntry(unittest.TestCase):
    """self_map_check.py — the thin custom/script entry."""

    def test_emits_empty_array_when_in_sync(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = self_map_check.main()
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(buf.getvalue()), [])

    def test_emits_hard_finding_on_drift(self):
        saved = self_map.SELF_MAP_PATH
        tmp = tempfile.TemporaryDirectory()
        try:
            drifted = os.path.join(tmp.name, "self-map.md")
            with open(drifted, "w", encoding="utf-8", newline="") as fh:
                fh.write("not the canonical map\n")
            self_map.SELF_MAP_PATH = drifted  # check() resolves the path at call time
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = self_map_check.main()
            self.assertEqual(rc, 0)
            payload = json.loads(buf.getvalue())
            self.assertEqual(len(payload), 1)
            self.assertEqual(payload[0]["severity"], "hard")
        finally:
            self_map.SELF_MAP_PATH = saved
            tmp.cleanup()


class TestCLI(unittest.TestCase):
    def _run(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = self_map.main(argv)
        return rc, out.getvalue(), err.getvalue()

    def test_show_prints_the_readout(self):
        rc, out, _ = self._run(["show"])
        self.assertEqual(rc, 0)
        self.assertIn("What this engine is made of", out)

    def test_default_command_is_show(self):
        rc, out, _ = self._run([])
        self.assertEqual(rc, 0)
        self.assertIn("What this engine is made of", out)

    def test_check_in_sync_returns_zero(self):
        rc, out, _ = self._run(["check"])
        self.assertEqual(rc, 0)
        self.assertIn("in sync", out)

    def test_check_absent_path_returns_one(self):
        with tempfile.TemporaryDirectory() as d:
            rc, out, _ = self._run(["check", os.path.join(d, "nope.md")])
        self.assertEqual(rc, 1)

    def test_generate_to_path(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "m.md")
            rc, out, _ = self._run(["generate", p])
            self.assertEqual(rc, 0)
            self.assertTrue(os.path.isfile(p))

    def test_demo_runs_clean_and_never_touches_the_real_file(self):
        before = self_map.read_committed(self_map.SELF_MAP_PATH)
        rc, out, _ = self._run(["demo"])
        after = self_map.read_committed(self_map.SELF_MAP_PATH)
        self.assertEqual(rc, 0)
        self.assertEqual(before, after, "demo must not touch the committed self-map")
        self.assertIn("in sync", out)
        self.assertIn("out of date", out)
        self.assertIn("never touched", out)

    def test_unknown_command_is_config_error(self):
        rc, _out, err = self._run(["bogus"])
        self.assertEqual(rc, 2)
        self.assertIn("unknown command", err)


class TestSourceDeterminismRoundTrip(unittest.TestCase):
    """Source-determinism enforcing correlate: regenerating the self-map from the same committed source
    tree yields byte-identical output — including across a PROCESS BOUNDARY under a different
    PYTHONHASHSEED. This guards the source-determinism *property against a future regression*; it does NOT
    by itself prove the generator nondeterminism-free — it passes trivially today because every collection
    is sorted before it reaches output. The process/hash-seed axis is the only coverage this adds over
    TestRenderMapDeterminism, which already permutes module + catalog order in-process.
    """

    @staticmethod
    def _canonical_in_subprocess(seed: str) -> bytes:
        # Absolute tools dir on sys.path (NOT a cwd-relative "tools") so the subprocess resolves the
        # import regardless of the runner's working directory.
        tools_dir = os.path.dirname(os.path.abspath(self_map.__file__))
        code = (
            "import sys\n"
            f"sys.path.insert(0, {tools_dir!r})\n"
            "import self_map\n"
            "sys.stdout.buffer.write(self_map.canonical_map().encode('utf-8'))\n"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code], capture_output=True,
            env={**os.environ, "PYTHONHASHSEED": seed}, check=True)
        return proc.stdout

    def test_regeneration_is_byte_identical_same_process(self):
        self.assertEqual(self_map.canonical_map(), self_map.canonical_map())

    def test_regeneration_is_byte_identical_across_hash_seeds(self):
        in_process = self_map.canonical_map().encode("utf-8")
        a = self._canonical_in_subprocess("1")
        b = self._canonical_in_subprocess("2")
        self.assertEqual(a, b, "self-map bytes differ across PYTHONHASHSEED — a nondeterministic generation path")
        self.assertEqual(a, in_process, "subprocess self-map differs from in-process — nondeterminism leaked in")


class TestCommitBoundaryRegen(unittest.TestCase):
    """The PreToolUse commit-boundary regen hook (resolves the #136 self-map/graph asymmetry). On a
    `git commit` it refreshes the self-map best-effort and ALWAYS proceeds — it never blocks, never
    injects, fails open, and is reached via the `hook` verb (whose absence/mistyping would BLOCK the
    commit: the no-arg default is `show`, an stdout dump, and an unknown verb returns exit 2)."""

    _COMMIT = {"tool_name": "Bash", "tool_input": {"command": "git commit -m 'x'"}}
    _STATUS = {"tool_name": "Bash", "tool_input": {"command": "git status"}}

    # The `git commit` classifier moved to hooks._is_git_commit (shared with the other commit-boundary
    # hooks); its unit tests live in test_hooks.py. These tests exercise the regen handler's use of it.

    def test_handler_regenerates_on_commit_and_proceeds(self):
        with tempfile.TemporaryDirectory() as d:
            scratch = os.path.join(d, "self-map.md")
            with mock.patch.object(self_map, "SELF_MAP_PATH", scratch), \
                    contextlib.redirect_stderr(io.StringIO()):
                decision = self_map._regen_handler(self._COMMIT)
            self.assertEqual(decision, hooks.proceed())             # ALWAYS proceed
            self.assertTrue(os.path.exists(scratch))                # the map was refreshed...
            with open(scratch, encoding="utf-8") as fh:
                self.assertIn("What this engine is made of", fh.read())  # ...with the real map

    def test_handler_does_not_regenerate_on_non_commit(self):
        with tempfile.TemporaryDirectory() as d:
            scratch = os.path.join(d, "self-map.md")               # never created
            with mock.patch.object(self_map, "SELF_MAP_PATH", scratch):
                decision = self_map._regen_handler(self._STATUS)
            self.assertEqual(decision, hooks.proceed())
            self.assertFalse(os.path.exists(scratch))               # no regen on a non-commit

    def test_handler_fails_open_on_generate_error(self):
        err = io.StringIO()
        with mock.patch.object(self_map, "generate", side_effect=OSError("boom")), \
                contextlib.redirect_stderr(err):
            decision = self_map._regen_handler(self._COMMIT)
        self.assertEqual(decision, hooks.proceed())                 # fail-open: proceed, never block
        self.assertIn("could not run", err.getvalue())              # not silent
        self.assertIn("commit was not affected", err.getvalue())

    def test_handler_returns_proceed_on_every_path(self):
        # PreToolUse is block-eligible, so a block/deny return here would BLOCK the commit. The handler
        # must return proceed on every path.
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(self_map, "SELF_MAP_PATH", os.path.join(d, "m.md")), \
                    contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(self_map._regen_handler(self._COMMIT), hooks.proceed())
            self.assertEqual(self_map._regen_handler(self._STATUS), hooks.proceed())
        with mock.patch.object(self_map, "generate", side_effect=ValueError("x")), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(self_map._regen_handler(self._COMMIT), hooks.proceed())

    def test_end_to_end_via_run_hook_proceeds_exit_zero_no_stdout(self):
        with tempfile.TemporaryDirectory() as d:
            scratch = os.path.join(d, "self-map.md")
            out, err = io.StringIO(), io.StringIO()
            with mock.patch.object(self_map, "SELF_MAP_PATH", scratch):
                code = hooks.run_hook("PreToolUse", self_map._regen_handler,
                                      stdin=io.StringIO(json.dumps(self._COMMIT)), stdout=out, stderr=err)
            self.assertEqual(code, hooks.EXIT_PROCEED)              # exit 0 — the commit proceeds
            self.assertEqual(out.getvalue(), "")                   # no structured-output corruption
            self.assertTrue(os.path.exists(scratch))               # the map was refreshed

    def test_main_hook_verb_routes_to_run_hook(self):
        # The `hook` verb MUST route to run_hook; its absence would fall through to the usage error
        # (exit 2 = a PreToolUse BLOCK) and the no-arg default `show` would dump the map to stdout.
        with mock.patch.object(hooks, "run_hook", return_value=0) as run:
            self.assertEqual(self_map.main(["hook"]), 0)
        run.assert_called_once()
        self.assertEqual(run.call_args.args[0], "PreToolUse")
        self.assertIs(run.call_args.args[1], self_map._regen_handler)

    def test_wired_command_passes_the_hook_arg(self):
        # The footgun guard: the wired command MUST end in ` hook`. Dropping the arg re-enables the
        # no-arg `show` -> stdout dump on every PreToolUse. Assert it in both the manifest and settings.
        manifest = validate.load_json(os.path.join(validate.ROOT, ".engine/modules/core/manifest.json"))
        sm_wires = [w for w in manifest["wires"] if w.get("type") == "hook"
                    and "self_map.py" in w.get("hook", {}).get("command", "")]
        self.assertEqual(len(sm_wires), 1, "exactly one self_map hook wire")
        self.assertEqual(sm_wires[0]["event"], "PreToolUse")
        self.assertTrue(sm_wires[0]["hook"]["command"].rstrip().endswith(" hook"))
        settings = validate.load_json(os.path.join(validate.ROOT, ".claude", "settings.json"))
        sm_cmds = [h["command"] for grp in settings["hooks"].get("PreToolUse", [])
                   for h in grp.get("hooks", []) if "self_map.py" in h.get("command", "")]
        self.assertEqual(len(sm_cmds), 1)
        self.assertTrue(sm_cmds[0].rstrip().endswith(" hook"))


class TestRetiredAssetFilter(unittest.TestCase):
    """#513: a provides entry that first-run retired AND that is absent on disk is filtered from the render,
    so a deployed repo's map never advertises a file the retire step deleted. While the file still exists
    (this construction repo, or a fresh not-yet-set-up copy) nothing is filtered. The retired paths are
    read from the committed census, never named literally here — this test file survives retirement, and
    the reference-closure check forbids a survivor naming a removed asset."""

    _MANIFEST = {"id": "core", "version": "1.0.0", "status": "active",
                 "provides": {"operation": [".engine/operations/example-retired.md",
                                            ".engine/operations/boot-session-start.md"],
                              "skill": [".claude/skills/example-retired-skill/SKILL.md",
                                        ".claude/skills/example-kept-skill/SKILL.md"]}}

    @unittest.skipUnless(
        _ccc._in_home_repo(),
        "construction-only invariant: nothing is retired here yet, so _retired_absent() is empty. In a deployed "
        "repo the first-run retire step has legitimately removed those assets, so this would fail — the deployed "
        "shape is covered by test_deployed_shape_filters_the_real_retired_entries below.")
    def test_construction_repo_filters_nothing(self):
        # In this repo every census entry exists on disk, so the filter sets are empty and the map renders
        # every provides entry as before.
        self.assertEqual(self_map._retired_absent(), (set(), ()))
        block = "\n".join(self_map.render_module(self._MANIFEST))
        self.assertIn("example-retired.md", block)

    def test_retired_and_absent_file_is_filtered_siblings_survive(self):
        with mock.patch.object(self_map, "_retired_absent",
                               return_value=({".engine/operations/example-retired.md"}, ())):
            block = "\n".join(self_map.render_module(self._MANIFEST))
        self.assertNotIn("example-retired.md", block)
        self.assertIn("boot-session-start.md", block)

    def test_file_under_a_retired_directory_is_filtered(self):
        # The directories leg: retire deletes whole skill trees, and the manifest advertises files INSIDE
        # them — prefix matching must catch those, directory-boundary-safe.
        with mock.patch.object(self_map, "_retired_absent",
                               return_value=(set(), (".claude/skills/example-retired-skill",))):
            block = "\n".join(self_map.render_module(self._MANIFEST))
        self.assertNotIn("example-retired-skill", block)
        self.assertIn("example-kept-skill", block)

    def test_prefix_match_is_directory_boundary_safe(self):
        self.assertTrue(self_map._is_retired_absent(".claude/skills/x/SKILL.md", set(), (".claude/skills/x",)))
        self.assertTrue(self_map._is_retired_absent(".claude/skills/x", set(), (".claude/skills/x",)))
        self.assertFalse(self_map._is_retired_absent(".claude/skills/xy/SKILL.md", set(), (".claude/skills/x",)))

    def test_missing_census_reads_as_no_filter(self):
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(validate, "ROOT", d), \
                 mock.patch.object(validate, "ENGINE_DIR", os.path.join(d, ".engine")):
                self.assertEqual(self_map._retired_absent(), (set(), ()))

    def test_deployed_shape_filters_the_real_retired_entries(self):
        # The real defect end-to-end: the REAL census + the REAL core manifest, on a tree where the retired
        # files and directories are absent (the post-first-run deployed shape) — the map must drop EVERY
        # advertised entry the retire step removes, across every provides group (files AND entries under
        # retired directories), and keep every sibling that survives.
        census_src = os.path.join(validate.ENGINE_DIR, "provisioning", "first-run-assets.json")
        core_src = os.path.join(validate.ENGINE_DIR, "modules", "core", "manifest.json")
        core = validate.load_json(core_src)
        census = validate.load_json(census_src)
        retired_files = set(census.get("files") or [])
        retired_dirs = tuple(census.get("directories") or [])

        def is_doomed(p):
            return self_map._is_retired_absent(p, retired_files, retired_dirs)

        entries = [p for group in (core.get("provides") or {}).values() for p in (group or [])]
        doomed = sorted(p for p in entries if is_doomed(p))
        survivors = sorted(p for p in entries if not is_doomed(p))
        self.assertGreaterEqual(len(doomed), 4,
                                "the defect's class must exist: the retired operation file plus the "
                                "setup-skill files under the retired directories")
        self.assertTrue(any(any(p == d or p.startswith(d + "/") for d in retired_dirs) for p in doomed),
                        "at least one doomed entry must come from the directories leg")
        self.assertTrue(survivors)
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, ".engine", "provisioning"))
            with open(census_src, encoding="utf-8") as src, \
                 open(os.path.join(d, ".engine", "provisioning", "first-run-assets.json"),
                      "w", encoding="utf-8") as dst:
                dst.write(src.read())
            with mock.patch.object(validate, "ROOT", d), \
                 mock.patch.object(validate, "ENGINE_DIR", os.path.join(d, ".engine")):
                block = "\n".join(self_map.render_module(core))
        for path in doomed:            # full code-span paths — basenames collide across skills
            self.assertNotIn(f"`{path}`", block)
        for path in survivors:
            self.assertIn(f"`{path}`", block)


if __name__ == "__main__":
    unittest.main()
