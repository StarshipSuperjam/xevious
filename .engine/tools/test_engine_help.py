"""Tests for `/engine-help`'s listing tool — the degradation-proof command index.

Verifies: engine-only scoping (un-prefixed product skills ignored); the operator-invocable filter
(operator-typed AND model-auto listed — including a skill with NO invocation, which defaults to model-auto —
while model-only verbs are excluded); the typed-name source (directory for a skill, filename for
a legacy command — NOT the display `name`); the load-bearing degradation guarantee (a malformed command
file is skipped, the listing never raises — contrasted with the merged `validate.frontmatter`, which
DOES raise on the same input); the available-commands relay (empty when absent, relayed-sorted when
present, empty on a malformed catalog); and that `render` shows one
plain line — not a bare heading — when nothing is available, carries the getting-started pointer, and is
deterministically ordered. CLI `main([])`/`main(["demo"])` run.
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import engine_help as eh  # noqa: E402
import module_catalog  # noqa: E402  (the optional-module catalog, for the roster-aware available-verbs expectation)
import validate  # noqa: E402


def _write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


_OP_TYPED = ("---\nname: engine-start\ndescription: Start building.\ninvocation: operator-typed\n"
             "disable-model-invocation: true\n---\n\n## Steps\n\n1. Go.\n")
_OP_TYPED_HELP = ("---\ndescription: List the commands.\ninvocation: operator-typed\n"
                  "disable-model-invocation: true\n---\n\n## Steps\n\n1. Go.\n")
_MODEL_AUTO = ("---\nname: engine-auto\ndescription: An auto one.\n---\n\n## Steps\n\n1. Go.\n")  # OMITTED invocation = model-auto
_MODEL_AUTO_EXPLICIT = ("---\nname: engine-pull\ndescription: An explicit auto one.\ninvocation: model-auto\n"
                        "---\n\n## Steps\n\n1. Go.\n")
_MODEL_ONLY = ("---\ndescription: A model-driven one.\ninvocation: model-only\nuser-invocable: false\n"
               "---\n\n## Steps\n\n1. Go.\n")
_MALFORMED = "---\ndescription: [unclosed\ninvocation: operator-typed\n---\n\n## Steps\n\n1. Go.\n"


class TestInstalledVerbsDiscovery(unittest.TestCase):
    def test_lists_engine_operator_invocable_sorted(self):
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, ".claude/skills/engine-start/SKILL.md"), _OP_TYPED)
            _write(os.path.join(d, ".claude/skills/engine-help/SKILL.md"), _OP_TYPED_HELP)
            _write(os.path.join(d, ".claude/skills/engine-auto/SKILL.md"), _MODEL_AUTO)            # omitted → model-auto → listed
            _write(os.path.join(d, ".claude/skills/engine-pull/SKILL.md"), _MODEL_AUTO_EXPLICIT)   # explicit model-auto → listed
            _write(os.path.join(d, ".claude/skills/engine-watch/SKILL.md"), _MODEL_ONLY)           # model-only → excluded
            _write(os.path.join(d, ".claude/skills/my-product/SKILL.md"), _OP_TYPED)               # un-prefixed → ignored
            names = [v["name"] for v in eh.installed_verbs(root=d)]
            self.assertEqual(names, ["engine-auto", "engine-help", "engine-pull", "engine-start"],
                             "the engine's own operator-invocable verbs (operator-typed + model-auto), alphabetical; "
                             "model-only and un-prefixed excluded")

    def test_typed_name_is_the_directory_not_the_display_label(self):
        with tempfile.TemporaryDirectory() as d:
            # frontmatter `name` differs from the directory → the verb shown is the DIRECTORY (what the
            # operator actually types), not the display label.
            _write(os.path.join(d, ".claude/skills/engine-start/SKILL.md"),
                   "---\nname: a-display-label\ndescription: Start.\ninvocation: operator-typed\n"
                   "disable-model-invocation: true\n---\n\n## Steps\n\n1. Go.\n")
            self.assertEqual(eh.installed_verbs(root=d)[0]["name"], "engine-start")

    def test_legacy_command_filename_is_the_typed_name(self):
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, ".claude/commands/engine-legacy.md"),
                   "---\ndescription: A legacy command.\ninvocation: operator-typed\n"
                   "disable-model-invocation: true\n---\n\nbody\n")
            verbs = eh.installed_verbs(root=d)
            self.assertEqual(verbs[0]["name"], "engine-legacy")

    def test_description_carried_from_frontmatter(self):
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, ".claude/skills/engine-start/SKILL.md"), _OP_TYPED)
            self.assertEqual(eh.installed_verbs(root=d)[0]["description"], "Start building.")

    def test_skills_and_legacy_commands_sorted_together(self):
        # The final sort must interleave skills and legacy commands by typed name — not merely sort
        # within each glob. A skill sorting AFTER a legacy command pins that the cross-source sort runs.
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, ".claude/skills/engine-zebra/SKILL.md"), _OP_TYPED_HELP)
            _write(os.path.join(d, ".claude/commands/engine-alpha.md"),
                   "---\ndescription: Alpha.\ninvocation: operator-typed\n"
                   "disable-model-invocation: true\n---\n\nbody\n")
            names = [v["name"] for v in eh.installed_verbs(root=d)]
            self.assertEqual(names, ["engine-alpha", "engine-zebra"],
                             "skills and legacy commands are sorted together, not per source")


class TestMalformedDegrades(unittest.TestCase):
    def test_frontmatter_itself_raises_so_the_catch_is_load_bearing(self):
        # Pins WHY installed_verbs must guard: the shared parser RAISES on this input.
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "bad.md")
            _write(p, _MALFORMED)
            with self.assertRaises(Exception):
                validate.frontmatter(p)

    def test_malformed_command_is_skipped_never_raises(self):
        # The always-answers guarantee: a broken command file must not blank the whole listing.
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, ".claude/skills/engine-start/SKILL.md"), _OP_TYPED)
            _write(os.path.join(d, ".claude/skills/engine-broken/SKILL.md"), _MALFORMED)
            verbs = eh.installed_verbs(root=d)  # must NOT raise
            names = [v["name"] for v in verbs]
            self.assertIn("engine-start", names, "the readable command still lists")
            self.assertNotIn("engine-broken", names, "the malformed command is skipped, not crashing the list")


class TestAvailableVerbsRelay(unittest.TestCase):
    def test_absent_catalog_returns_empty(self):
        # An explicit missing path narrows to nothing.
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(eh.available_verbs(os.path.join(d, "nope.json")), [])
        # No path = the committed catalog (the shared reader's default). `available_verbs` lists the optional
        # modules that are NOT installed (so the operator could install them), excluding the ones already
        # installed. In THIS repo every catalog module is installed, so the list is empty; a deployment that
        # DECLINED one legitimately sees it offered here, so derive the expectation from the installed set
        # rather than asserting empty (#646). This still checks the filter both ways: no installed module leaks
        # in, and every declined verb-bearing one is offered.
        installed = eh._installed_module_ids()
        expected = sorted(e["verb"] for e in module_catalog.entries()
                          if e["id"] not in installed and e["verb"])
        self.assertEqual([v["name"] for v in eh.available_verbs(None)], expected)

    def test_available_offers_a_not_installed_module_verb(self):
        # Two-directional proof: the home repo has every catalog module installed, so available_verbs(None) is
        # empty and the live assertion never exercises the "offered" branch. Given a catalog with a verb-bearing
        # module absent from engine.json packages, available_verbs offers it — the roster-aware behavior a
        # deployment that DECLINED it relies on (#646).
        with tempfile.TemporaryDirectory() as dd:
            p = os.path.join(dd, "catalog.json")
            with open(p, "w", encoding="utf-8") as fh:
                json.dump([{"id": "an-uninstalled-module", "verb": "engine-new", "description": "New.",
                            "category": "Product Management"}], fh)
            self.assertEqual([v["name"] for v in eh.available_verbs(p)], ["engine-new"])

    def test_present_catalog_relayed_sorted(self):
        # The catalog's per-entry command is `verb` (the reconciled cross-slice shape the shared
        # `module_catalog` reader parses); /engine-help shows it as the typed command (its `name`).
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "catalog.json")
            with open(p, "w", encoding="utf-8") as fh:
                json.dump([{"id": "z-mod", "verb": "engine-zeta", "description": "Z.",
                            "category": "Product Management"},
                           {"id": "a-mod", "verb": "engine-alpha", "description": "A.",
                            "category": "Verification & Validation"}], fh)
            got = eh.available_verbs(p)
            self.assertEqual([v["name"] for v in got], ["engine-alpha", "engine-zeta"], "relayed, sorted")
            self.assertEqual(got[0]["description"], "A.", "the gloss rides the command")

    def test_malformed_catalog_returns_empty(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "catalog.json")
            with open(p, "w", encoding="utf-8") as fh:
                fh.write("{not valid json")
            self.assertEqual(eh.available_verbs(p), [], "a broken catalog narrows, never breaks")

    def test_command_less_module_is_excluded(self):
        # A command-less optional module (no verb) reaches the shared reader now (#254), but /engine-help lists
        # things to type — so available_verbs filters it out HERE, while the verb-bearing module is still shown.
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "catalog.json")
            with open(p, "w", encoding="utf-8") as fh:
                json.dump([{"id": "lens-mod", "description": "A review lens, fired by the gate.",
                            "category": "Verification & Validation"},
                           {"id": "cmd-mod", "verb": "engine-do", "description": "Has a command.",
                            "category": "Product Management"}], fh)
            got = eh.available_verbs(p)
            self.assertEqual([v["name"] for v in got], ["engine-do"],
                             "the command-less module is excluded from /engine-help; the command-bearing one stays")


class TestRender(unittest.TestCase):
    def test_render_carries_the_pointer(self):
        out = eh.render([{"name": "engine-start", "description": "Start building."}], [])
        self.assertIn("/engine-start", out)
        self.assertIn("getting-started", out, "the closing pointer to the orientation guide")

    def test_empty_available_renders_one_plain_line(self):
        out = eh.render([{"name": "engine-start", "description": "Start."}], [])
        self.assertIn(eh._EMPTY_AVAILABLE_LINE, out, "one plain line, never a bare empty heading")
        self.assertNotIn(eh._AVAILABLE_HEADER, out)

    def test_available_rendered_when_present(self):
        out = eh.render([{"name": "engine-start", "description": "Start."}],
                        [{"name": "engine-extra", "description": "Extra."}])
        self.assertIn("/engine-extra", out)
        self.assertNotIn(eh._EMPTY_AVAILABLE_LINE, out)

    def test_verb_without_description_renders_alone_no_dangling_dash(self):
        out = eh.render([{"name": "engine-x", "description": ""}], [])
        self.assertIn("/engine-x", out)
        self.assertNotIn("/engine-x —", out, "no dangling em-dash when there is no description")


class TestCLI(unittest.TestCase):
    def test_main_prints_the_real_listing(self):
        # On the real repo this very command (/engine-help), /engine-start, and /engine-status are all listed
        # — an end-to-end check that the real installed skills render in the operator menu. Nearly every engine
        # verb is operator-typed; the exception is the model-reachable recall command, which the operator can
        # also type, so it belongs in this listing too. (The filter branch that HIDES a model-only skill is
        # covered separately by the _MODEL_AUTO fixtures in test_lists_engine_operator_invocable_sorted.)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = eh.main([])
        self.assertEqual(rc, 0)
        out = buf.getvalue()
        self.assertIn("/engine-help", out)
        self.assertIn("/engine-start", out)
        self.assertIn("/engine-status", out)

    def test_demo_runs_and_narrates_degradation(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = eh.main(["demo"])
        self.assertEqual(rc, 0)
        text = buf.getvalue()
        self.assertIn("/engine-start", text)
        self.assertIn("broken", text.lower(), "the demo narrates the broken-file degradation")


if __name__ == "__main__":
    unittest.main()
