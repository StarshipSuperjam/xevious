#!/usr/bin/env python3
"""Self-tests for the wiring library + the comment-fenced-block helper.

Run: uv run --directory .engine --frozen -- python -m unittest discover -s tools -p 'test_*.py' -b

These lock the R5-firewall properties that, for four of the five seams, have NO behavioral demo
this slice (their target files are born later) and so rest entirely on test name↔assertion
fidelity: the closed dispatch rejects an unknown seam and mutates nothing; reverse keys on
engine-namespaced identity and never removes an operator's identical-looking entry; the
fenced-block helper never deletes to EOF on a malformed fence; the JSON mutator fails OPEN on a
malformed operator file (never clobbers, never a traceback) and preserves all operator keys; and
apply/reverse are idempotent. The deliverable-gate security lens re-reads each assertion against
its name; CI runs them as a step in engine-ci.
"""
from __future__ import annotations
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate   # noqa: E402
import wiring      # noqa: E402

MODULE_SCHEMA = validate.load_json(os.path.join(validate.SCHEMAS_DIR, "module.v1.json"))
WIRES_ENUM = set(MODULE_SCHEMA["properties"]["wires"]["items"]["properties"]["type"]["enum"])

HOOK = {"type": "hook", "event": "PreToolUse", "matcher": "Bash",
        "hook": {"type": "command",
                 "command": "${CLAUDE_PROJECT_DIR}/.engine/.venv/bin/python "
                            "${CLAUDE_PROJECT_DIR}/.engine/tools/hook.py"}}
PERM = {"type": "permission", "value": "Read(./src/**)"}
MCP = {"type": "mcp", "name": "engine-knowledge",
       "definition": {"command": "${CLAUDE_PROJECT_DIR:-.}/.engine/.venv/bin/python",
                      "args": ["${CLAUDE_PROJECT_DIR:-.}/.engine/tools/kg.py"]}}
RECORD = {"class": "structured", "location": ".engine/widget/", "purpose": "demo surface",
          "authority": "mechanics-and-guidance", "lifecycle": "artifact",
          "governing_schema": None, "template": None}
ONTO = {"type": "ontology-entry", "name": "widget", "record": RECORD}
GI = {"type": "gitignore", "key": "core", "lines": [".engine/.venv/"]}

VALID_CATALOG = {"$schema": "https://json-schema.org/draft/2020-12/schema",
                 "surfaces": {"existing": dict(RECORD, location=".engine/existing/")}}


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


class _Redirected(unittest.TestCase):
    """Redirects every hardcoded target path into a fresh temp dir per test."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        d = self._tmp.name
        self._saved = (wiring.SETTINGS_PATH, wiring.MCP_PATH, wiring.GITIGNORE_PATH,
                       wiring.CATALOG_PATH, wiring.CODEX_HOOKS_PATH, wiring.CODEX_CONFIG_PATH)
        wiring.SETTINGS_PATH = os.path.join(d, ".claude", "settings.json")
        wiring.MCP_PATH = os.path.join(d, ".mcp.json")
        wiring.GITIGNORE_PATH = os.path.join(d, ".gitignore")
        wiring.CATALOG_PATH = os.path.join(d, "surface-catalog.json")
        wiring.CODEX_HOOKS_PATH = os.path.join(d, ".codex", "hooks.json")
        wiring.CODEX_CONFIG_PATH = os.path.join(d, ".codex", "config.toml")

    def tearDown(self):
        (wiring.SETTINGS_PATH, wiring.MCP_PATH, wiring.GITIGNORE_PATH,
         wiring.CATALOG_PATH, wiring.CODEX_HOOKS_PATH, wiring.CODEX_CONFIG_PATH) = self._saved
        self._tmp.cleanup()


# ---- the comment-fenced-block helper (the spine) ---------------------------------------------

class TestFenceHelper(unittest.TestCase):
    def test_apply_inserts_block_when_absent(self):
        out = wiring.fence_apply("build/\n*.log\n", "core", [".engine/.venv/"])
        self.assertIn("# BEGIN engine-managed block: core - do not edit inside", out)
        self.assertIn("# END engine-managed block: core", out)
        self.assertIn(".engine/.venv/", out)

    def test_apply_preserves_surrounding_content(self):
        out = wiring.fence_apply("build/\n*.log\n", "core", [".engine/.venv/"])
        self.assertTrue(out.startswith("build/\n*.log\n"))

    def test_reverse_removes_only_the_fence(self):
        applied = wiring.fence_apply("build/\n*.log\n", "core", [".engine/.venv/"])
        self.assertEqual(wiring.fence_reverse(applied, "core"), "build/\n*.log\n")

    def test_reverse_apply_roundtrip_is_identity(self):
        base = "build/\n*.log\n"
        self.assertEqual(wiring.fence_reverse(wiring.fence_apply(base, "core", ["x/"]), "core"), base)

    def test_reverse_is_noop_when_absent_and_idempotent(self):
        self.assertEqual(wiring.fence_reverse("build/\n", "core"), "build/\n")
        applied = wiring.fence_apply("build/\n", "core", ["x/"])
        once = wiring.fence_reverse(applied, "core")
        self.assertEqual(wiring.fence_reverse(once, "core"), once)

    def test_operator_identical_line_outside_fence_survives_reverse(self):
        # The headline R5 property (and the operator-run demo) at unit level.
        applied = wiring.fence_apply("build/\n", "core", [".engine/.venv/"])
        with_dup = applied + ".engine/.venv/\n"          # operator's identical line OUTSIDE the fence
        out = wiring.fence_reverse(with_dup, "core")
        self.assertNotIn("# BEGIN engine-managed block: core", out)   # engine block gone
        self.assertIn(".engine/.venv/", out)                          # operator's line survived
        self.assertEqual(out, "build/\n.engine/.venv/\n")

    def test_two_distinct_fences_coexist_and_reverse_targets_only_the_named_one(self):
        t = wiring.fence_apply("", "tool-runtime-venv", [".engine/.venv/"])
        t = wiring.fence_apply(t, "memory", ["index.db"])
        self.assertIn("block: tool-runtime-venv", t)
        self.assertIn("block: memory", t)
        out = wiring.fence_reverse(t, "memory")
        self.assertIn("block: tool-runtime-venv", out)   # the foundation fence is untouched
        self.assertNotIn("block: memory", out)

    def test_malformed_fence_begin_without_end_never_deletes_to_eof(self):
        # The single most dangerous reverser failure: a lone begin marker must NOT cause
        # everything below it to be deleted. Fail open: output == input, exactly.
        text = ("build/\n# BEGIN engine-managed block: core - do not edit inside\n"
                "operator-line-below\n")
        with self.assertRaises(wiring.WiringError):
            wiring.fence_reverse(text, "core")
        with self.assertRaises(wiring.WiringError):
            wiring.fence_apply(text, "core", ["x/"])

    def test_malformed_fence_orphan_end_and_duplicate_begin(self):
        orphan_end = "build/\n# END engine-managed block: core\n"
        with self.assertRaises(wiring.WiringError):
            wiring.fence_reverse(orphan_end, "core")
        dup = ("# BEGIN engine-managed block: core - do not edit inside\na/\n"
               "# END engine-managed block: core\n"
               "# BEGIN engine-managed block: core - do not edit inside\nb/\n"
               "# END engine-managed block: core\n")
        with self.assertRaises(wiring.WiringError):
            wiring.fence_reverse(dup, "core")

    def test_idempotency_boundary_cases(self):
        for base in ("nonl",                       # no trailing newline
                     "a\n\n",                      # trailing blank line
                     "a\r\nb\r\n"):                # CRLF operator content
            once = wiring.fence_apply(base, "core", ["x/"])
            twice = wiring.fence_apply(once, "core", ["x/"])
            self.assertEqual(once, twice, f"re-apply not a no-op for {base!r}")

    def test_crlf_operator_lines_survive_roundtrip(self):
        base = "a\r\nb\r\n"
        out = wiring.fence_reverse(wiring.fence_apply(base, "core", ["x/"]), "core")
        self.assertEqual(out, base)   # operator CRLF bytes preserved exactly

    def test_body_line_forging_a_marker_is_rejected(self):
        with self.assertRaises(wiring.WiringError):
            wiring.fence_apply("a\n", "core", ["# END engine-managed block: core"])

    def test_invalid_fence_id_is_rejected(self):
        for bad in ("../etc", "a/b", "Core", "", "a b", "core\n", "core\r", "x\ty"):
            with self.assertRaises(wiring.WiringError):
                wiring.fence_apply("a\n", bad, ["x/"])

    def test_body_line_containing_a_newline_is_rejected(self):
        # A multi-line body would inject a second (forged) marker into the block, making the
        # fence permanently malformed/unremovable — must be refused before any write.
        with self.assertRaises(wiring.WiringError):
            wiring.fence_apply("a\n", "core", ["harmless\n# END engine-managed block: core"])
        with self.assertRaises(wiring.WiringError):
            wiring.fence_apply("a\n", "core", ["x\r"])


class TestMdFenceStyle(unittest.TestCase):
    """The Markdown/HTML-comment fence style (the root CLAUDE.md floor — #234 6a). Same apply/reverse/present
    contract as the default '#' style; only the marker syntax differs, so a '#' line never renders as a
    heading in a Markdown file."""
    STYLE = wiring.MD_FENCE

    def test_apply_wraps_body_in_html_comment_markers(self):
        out = wiring.fence_apply("", "floor", ["# Floor", "", "body line"], style=self.STYLE)
        self.assertIn("<!-- BEGIN engine-managed block: floor - do not edit inside -->", out)
        self.assertIn("<!-- END engine-managed block: floor -->", out)
        self.assertIn("# Floor", out)
        self.assertNotIn("# BEGIN engine-managed block:", out)        # not the '#' style

    def test_replace_keeps_operator_content_outside_the_block(self):
        top, bottom = "# My doc\n\nintro\n\n", "\n## More\n\ntail\n"
        doc = top + wiring.fence_apply("", "floor", ["old floor"], style=self.STYLE) + bottom
        out = wiring.fence_apply(doc, "floor", ["new floor"], style=self.STYLE)
        self.assertIn(top, out)                                       # operator content above survives
        self.assertIn(bottom, out)                                   # operator content below survives
        self.assertIn("new floor", out)
        self.assertNotIn("old floor", out)                           # only the block body changed

    def test_reverse_removes_only_the_block(self):
        top, bottom = "# My doc\n\nintro\n\n", "\n## More\n\ntail\n"
        doc = top + wiring.fence_apply("", "floor", ["x"], style=self.STYLE) + bottom
        self.assertEqual(wiring.fence_reverse(doc, "floor", style=self.STYLE), top + bottom)

    def test_apply_then_reverse_round_trips_to_empty(self):
        applied = wiring.fence_apply("", "floor", ["a", "b"], style=self.STYLE)
        self.assertEqual(wiring.fence_reverse(applied, "floor", style=self.STYLE), "")

    def test_fence_present_true_false_and_raises_on_malformed(self):
        applied = wiring.fence_apply("", "floor", ["a"], style=self.STYLE)
        self.assertTrue(wiring.fence_present(applied, "floor", style=self.STYLE))
        self.assertFalse(wiring.fence_present("# raw floor, no fence\n", "floor", style=self.STYLE))
        self.assertFalse(wiring.fence_present("", "floor", style=self.STYLE))
        with self.assertRaises(wiring.WiringError):                  # two begins / two ends → malformed
            wiring.fence_present(applied + applied, "floor", style=self.STYLE)

    def test_the_two_styles_do_not_cross_detect(self):
        hash_block = wiring.fence_apply("", "floor", ["x"])          # default '#' style
        self.assertFalse(wiring.fence_present(hash_block, "floor", style=wiring.MD_FENCE))
        md_block = wiring.fence_apply("", "floor", ["x"], style=wiring.MD_FENCE)
        self.assertFalse(wiring.fence_present(md_block, "floor"))    # default '#'

    def test_forging_either_styles_marker_in_a_body_line_is_rejected(self):
        with self.assertRaises(wiring.WiringError):
            wiring.fence_apply("", "floor", ["<!-- END engine-managed block: floor -->"], style=self.STYLE)
        with self.assertRaises(wiring.WiringError):                  # a '#' marker is refused even in MD style
            wiring.fence_apply("", "floor", ["# BEGIN engine-managed block: x"], style=self.STYLE)


# ---- the gitignore seam (the live target / the demo) -----------------------------------------

class TestGitignoreSeam(_Redirected):
    def test_apply_creates_file_and_adds_block(self):
        f = wiring.gitignore_apply(GI)
        self.assertEqual(f["severity"], "note")
        self.assertIn("# BEGIN engine-managed block: core", _read(wiring.GITIGNORE_PATH))

    def test_reapply_is_noop(self):
        wiring.gitignore_apply(GI)
        before = _read(wiring.GITIGNORE_PATH)
        f = wiring.gitignore_apply(GI)
        self.assertEqual(_read(wiring.GITIGNORE_PATH), before)   # byte-for-byte
        self.assertIn("Nothing to change", f["message"])

    def test_reverse_removes_block_and_leaves_operator_identical_line(self):
        wiring._write_text(wiring.GITIGNORE_PATH, "build/\n")
        wiring.gitignore_apply(GI)
        wiring._write_text(wiring.GITIGNORE_PATH, _read(wiring.GITIGNORE_PATH) + ".engine/.venv/\n")
        f = wiring.gitignore_reverse(GI)
        out = _read(wiring.GITIGNORE_PATH)
        self.assertNotIn("# BEGIN engine-managed block: core", out)
        self.assertEqual(out, "build/\n.engine/.venv/\n")
        self.assertIn("your own lines are untouched", f["message"])

    def test_reverse_when_absent_is_noop(self):
        f = wiring.gitignore_reverse(GI)
        self.assertEqual(f["severity"], "note")
        self.assertIn("Nothing to remove", f["message"])

    def test_seam_reverse_on_malformed_fence_leaves_file_unchanged_and_flags(self):
        # The seam/IO layer (not just the pure function) must fail open: a malformed fence in a
        # real file is left byte-identical and a HARD finding is returned (the slice's single most
        # dangerous failure mode, locked at the operator-facing layer).
        malformed = ("build/\n# BEGIN engine-managed block: core - do not edit inside\n"
                     "operator-line-below\n")
        wiring._write_text(wiring.GITIGNORE_PATH, malformed)
        f = wiring.gitignore_reverse(GI)
        self.assertEqual(f["severity"], "hard")
        self.assertEqual(_read(wiring.GITIGNORE_PATH), malformed)   # byte-identical, no delete-to-EOF
        f2 = wiring.gitignore_apply(GI)
        self.assertEqual(f2["severity"], "hard")
        self.assertEqual(_read(wiring.GITIGNORE_PATH), malformed)

    def test_apply_missing_required_fields_is_hard(self):
        self.assertEqual(wiring.gitignore_apply({"type": "gitignore", "key": "core"})["severity"],
                         "hard")
        self.assertEqual(wiring.gitignore_apply({"type": "gitignore", "lines": ["x"]})["severity"],
                         "hard")


# ---- idempotency laws, per seam (apply∘apply, reverse∘reverse, reverse∘apply) -----------------

class TestIdempotencyLaws(_Redirected):
    def _apply_twice_is_noop(self, applier, directive, path):
        applier(directive)
        before = _read(path)
        applier(directive)
        self.assertEqual(_read(path), before)

    def test_hook_apply_idempotent(self):
        self._apply_twice_is_noop(wiring.hook_apply, HOOK, wiring.SETTINGS_PATH)

    def test_mcp_apply_idempotent(self):
        self._apply_twice_is_noop(wiring.mcp_apply, MCP, wiring.MCP_PATH)

    def test_permission_apply_idempotent(self):
        self._apply_twice_is_noop(wiring.permission_apply, PERM, wiring.SETTINGS_PATH)

    def test_ontology_apply_idempotent(self):
        wiring._write_json(wiring.CATALOG_PATH, VALID_CATALOG)
        self._apply_twice_is_noop(wiring.ontology_entry_apply, ONTO, wiring.CATALOG_PATH)

    def test_reverse_after_apply_is_identity_for_keyed_seams(self):
        # hook / mcp / ontology-entry restore the data exactly (permission is the documented
        # exception, tested separately).
        wiring._write_json(wiring.CATALOG_PATH, VALID_CATALOG)
        cases = [(wiring.hook_apply, wiring.hook_reverse, HOOK, wiring.SETTINGS_PATH),
                 (wiring.mcp_apply, wiring.mcp_reverse, MCP, wiring.MCP_PATH),
                 (wiring.ontology_entry_apply, wiring.ontology_entry_reverse, ONTO,
                  wiring.CATALOG_PATH)]
        for ap, rv, d, path in cases:
            before = json.loads(_read(path)) if os.path.exists(path) else {}
            ap(d)
            rv(d)
            after = json.loads(_read(path)) if os.path.exists(path) else {}
            self.assertEqual(after, before, f"reverse∘apply not identity for {d['type']}")

    def test_reverse_is_idempotent(self):
        wiring.hook_apply(HOOK)
        wiring.hook_reverse(HOOK)
        f = wiring.hook_reverse(HOOK)
        self.assertIn("Nothing to remove", f["message"])


# ---- engine-identity isolation (reverse keys on identity, never operator content) -------------

class TestIdentityIsolation(_Redirected):
    def test_hook_reverse_spares_operator_hook_on_same_event_and_matcher(self):
        wiring.hook_apply(HOOK)
        data = json.loads(_read(wiring.SETTINGS_PATH))
        # operator's own hook, SAME event+matcher, DIFFERENT command:
        data["hooks"]["PreToolUse"][0]["hooks"].append(
            {"type": "command", "command": "operator-only-tool"})
        wiring._write_json(wiring.SETTINGS_PATH, data)
        wiring.hook_reverse(HOOK)
        out = json.loads(_read(wiring.SETTINGS_PATH))
        cmds = [h["command"] for h in out["hooks"]["PreToolUse"][0]["hooks"]]
        self.assertEqual(cmds, ["operator-only-tool"])   # engine gone, operator survives

    def test_hook_reverse_spares_operator_hook_under_a_different_matcher(self):
        # Identity is the FULL {event, matcher, type, command} tuple — an operator who reuses the
        # engine command under a different matcher must keep it (keying must include matcher).
        wiring.hook_apply(HOOK)
        data = json.loads(_read(wiring.SETTINGS_PATH))
        data["hooks"]["PreToolUse"].append(
            {"matcher": "Edit", "hooks": [{"type": "command", "command": HOOK["hook"]["command"]}]})
        wiring._write_json(wiring.SETTINGS_PATH, data)
        wiring.hook_reverse(HOOK)
        out = json.loads(_read(wiring.SETTINGS_PATH))
        matchers = [g["matcher"] for g in out["hooks"]["PreToolUse"]]
        self.assertEqual(matchers, ["Edit"])             # only the engine's Bash group went

    def test_hook_reverse_is_exact_command_match_not_substring(self):
        # An operator who WRAPS the engine command (a strict superset) must keep their hook.
        wiring.hook_apply(HOOK)
        data = json.loads(_read(wiring.SETTINGS_PATH))
        wrapped = "logwrap " + HOOK["hook"]["command"] + " --verbose"
        data["hooks"]["PreToolUse"][0]["hooks"].append({"type": "command", "command": wrapped})
        wiring._write_json(wiring.SETTINGS_PATH, data)
        wiring.hook_reverse(HOOK)
        out = json.loads(_read(wiring.SETTINGS_PATH))
        cmds = [h["command"] for h in out["hooks"]["PreToolUse"][0]["hooks"]]
        self.assertEqual(cmds, [wrapped])                # exact-match removal spared the wrapper

    def test_hook_apply_refuses_a_command_not_pointing_into_engine(self):
        f = wiring.hook_apply({"type": "hook", "event": "PreToolUse", "matcher": "Bash",
                               "hook": {"type": "command", "command": "rm -rf /"}})
        self.assertEqual(f["severity"], "hard")
        self.assertFalse(os.path.exists(wiring.SETTINGS_PATH))   # nothing written

    def test_mcp_reverse_spares_operator_server(self):
        wiring.mcp_apply(MCP)
        data = json.loads(_read(wiring.MCP_PATH))
        data["mcpServers"]["operator-server"] = {"command": "x"}
        wiring._write_json(wiring.MCP_PATH, data)
        wiring.mcp_reverse(MCP)
        out = json.loads(_read(wiring.MCP_PATH))
        self.assertNotIn("engine-knowledge", out["mcpServers"])
        self.assertIn("operator-server", out["mcpServers"])

    def test_permission_reverse_errs_toward_leaving(self):
        wiring.permission_apply(PERM)
        f = wiring.permission_reverse(PERM)
        self.assertEqual(f["severity"], "note")          # a deliberate, surfaced no-op
        allow = json.loads(_read(wiring.SETTINGS_PATH))["permissions"]["allow"]
        self.assertIn(PERM["value"], allow)              # STILL PRESENT after reverse

    def test_mcp_reverse_never_touches_settings_json(self):
        wiring._write_json(wiring.SETTINGS_PATH, {"permissions": {"allow": ["operator"]}})
        before = _read(wiring.SETTINGS_PATH)
        wiring.mcp_apply(MCP)
        wiring.mcp_reverse(MCP)
        self.assertEqual(_read(wiring.SETTINGS_PATH), before)   # byte-unchanged


# ---- the firewall (closed vocabulary, reject-by-default) --------------------------------------

class TestFirewall(_Redirected):
    def test_unknown_seam_type_is_rejected_and_mutates_nothing(self):
        for bad in ({"type": "custom", "value": "x"}, {"type": "script"}, {"type": ""}, {}):
            f = wiring.apply(bad)
            self.assertEqual(f["severity"], "hard")
            self.assertIn("closed", f["message"])
        # nothing was written
        for p in (wiring.SETTINGS_PATH, wiring.MCP_PATH, wiring.GITIGNORE_PATH):
            self.assertFalse(os.path.exists(p))

    def test_reverse_of_unknown_seam_is_rejected(self):
        self.assertEqual(wiring.reverse({"type": "custom"})["severity"], "hard")

    def test_unhashable_seam_type_is_rejected_without_a_traceback(self):
        # A directive 'type' that is a list/dict (legal JSON, reachable via a malformed manifest)
        # must NOT raise a traceback to a non-engineer — it fails open with a hard finding.
        for bad in ({"type": ["hook"]}, {"type": {"k": "v"}}):
            f = wiring.apply(bad)
            self.assertEqual(f["severity"], "hard")
            self.assertEqual(wiring.reverse(bad)["severity"], "hard")

    def test_trailing_newline_identity_tokens_are_rejected(self):
        # Python's `$` matches before a trailing newline; the firewall must still reject a token
        # carrying a newline (it would forge a split marker / corrupt an engine identity).
        self.assertEqual(wiring.apply(
            {"type": "mcp", "name": "engine-x\n", "definition": {}})["severity"], "hard")
        self.assertEqual(wiring.apply(
            {"type": "gitignore", "key": "core\n", "lines": ["x"]})["severity"], "hard")
        self.assertEqual(wiring.apply(
            {"type": "ontology-entry", "name": "widget\n", "record": RECORD})["severity"], "hard")
        for p in (wiring.MCP_PATH, wiring.GITIGNORE_PATH):
            self.assertFalse(os.path.exists(p))          # nothing written

    def test_vocabulary_equals_the_live_schema_enum(self):
        # Bind the code firewall to the schema enum so they cannot silently diverge. The enum is
        # READ LIVE from module.v1.json, never a hand-copied literal.
        self.assertEqual(set(wiring._APPLIERS), WIRES_ENUM)
        self.assertEqual(set(wiring._REVERSERS), WIRES_ENUM)
        self.assertEqual(wiring.SEAMS, WIRES_ENUM)

    def test_every_seam_has_both_an_applier_and_a_reverser(self):
        self.assertEqual(set(wiring._APPLIERS), set(wiring._REVERSERS))


# ---- JSON-edit safety (the mutator posture; the key evidence for un-demoed seams) -------------

class TestJsonSafety(_Redirected):
    def test_absent_file_is_created_with_platform_canonical_shape(self):
        wiring.hook_apply(HOOK)
        data = json.loads(_read(wiring.SETTINGS_PATH))
        group = data["hooks"]["PreToolUse"][0]
        self.assertEqual(group["matcher"], "Bash")
        self.assertEqual(group["hooks"][0]["type"], "command")
        self.assertIn(".engine/", group["hooks"][0]["command"])

    def test_mcp_absent_file_uses_engine_prefix_and_project_dir_literal(self):
        wiring.mcp_apply(MCP)
        data = json.loads(_read(wiring.MCP_PATH))
        self.assertIn("engine-knowledge", data["mcpServers"])
        self.assertIn("${CLAUDE_PROJECT_DIR:-.}", data["mcpServers"]["engine-knowledge"]["command"])

    def test_empty_file_is_treated_as_empty_object(self):
        wiring._write_text(wiring.SETTINGS_PATH, "   \n")
        f = wiring.permission_apply(PERM)
        self.assertEqual(f["severity"], "note")
        self.assertIn(PERM["value"], json.loads(_read(wiring.SETTINGS_PATH))["permissions"]["allow"])

    def test_malformed_json_fails_open_and_flags_without_clobbering(self):
        wiring._write_text(wiring.SETTINGS_PATH, "{ this is not json")
        f = wiring.hook_apply(HOOK)                       # must not raise
        self.assertEqual(f["severity"], "hard")          # the flag half of fail-open
        self.assertEqual(_read(wiring.SETTINGS_PATH), "{ this is not json")  # bytes preserved
        self.assertIn("not valid JSON", f["message"])

    def test_operator_keys_preserved_by_deep_equality(self):
        original = {"env": {"X": "1"}, "permissions": {"allow": ["op"], "deny": ["d"]},
                    "hooks": {"PreToolUse": [{"matcher": "Bash",
                              "hooks": [{"type": "command", "command": "operator-cmd"}]}]}}
        wiring._write_json(wiring.SETTINGS_PATH, original)
        wiring.hook_apply(HOOK)
        out = json.loads(_read(wiring.SETTINGS_PATH))
        self.assertEqual(out["env"], {"X": "1"})                       # untouched
        self.assertEqual(out["permissions"], {"allow": ["op"], "deny": ["d"]})
        cmds = [h["command"] for h in out["hooks"]["PreToolUse"][0]["hooks"]]
        self.assertIn("operator-cmd", cmds)                            # operator hook kept
        self.assertEqual(len(cmds), 2)                                 # engine hook appended

    def test_settings_reverse_removes_only_the_engine_keyed_entry(self):
        wiring._write_json(wiring.SETTINGS_PATH,
                           {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [
                               {"type": "command", "command": "operator-cmd"}]}]}})
        wiring.hook_apply(HOOK)
        wiring.hook_reverse(HOOK)
        out = json.loads(_read(wiring.SETTINGS_PATH))
        cmds = [h["command"] for h in out["hooks"]["PreToolUse"][0]["hooks"]]
        self.assertEqual(cmds, ["operator-cmd"])


# ---- ontology-entry record validation (its target is the validator's own input) --------------

class TestOntologyEntry(_Redirected):
    def test_apply_refuses_when_catalog_missing(self):
        f = wiring.ontology_entry_apply(ONTO)             # create=False, no catalog
        self.assertEqual(f["severity"], "hard")
        self.assertFalse(os.path.exists(wiring.CATALOG_PATH))

    def test_apply_adds_a_valid_record(self):
        wiring._write_json(wiring.CATALOG_PATH, VALID_CATALOG)
        f = wiring.ontology_entry_apply(ONTO)
        self.assertEqual(f["severity"], "note")
        self.assertIn("widget", json.loads(_read(wiring.CATALOG_PATH))["surfaces"])

    def test_malformed_record_fails_open_catalog_untouched(self):
        wiring._write_json(wiring.CATALOG_PATH, VALID_CATALOG)
        before = _read(wiring.CATALOG_PATH)
        bad = {"type": "ontology-entry", "name": "widget", "record": {"class": "structured"}}
        f = wiring.ontology_entry_apply(bad)             # missing required record fields
        self.assertEqual(f["severity"], "hard")
        self.assertEqual(_read(wiring.CATALOG_PATH), before)   # catalog untouched

    def test_reverse_removes_only_the_engine_record(self):
        wiring._write_json(wiring.CATALOG_PATH, VALID_CATALOG)
        wiring.ontology_entry_apply(ONTO)
        wiring.ontology_entry_reverse(ONTO)
        cat = json.loads(_read(wiring.CATALOG_PATH))
        self.assertNotIn("widget", cat["surfaces"])
        self.assertIn("existing", cat["surfaces"])       # other record preserved
        self.assertEqual(list(validate.Draft202012Validator(
            validate.load_json(wiring.CATALOG_SCHEMA_PATH)).iter_errors(cat)), [])  # still valid

    # authority-tier reservation (issue #401) — the write-time seam half of the reservation law
    def test_seam_refuses_a_squatter_claiming_a_reserved_rank(self):
        wiring._write_json(wiring.CATALOG_PATH, VALID_CATALOG)
        before = _read(wiring.CATALOG_PATH)
        squatter = {"type": "ontology-entry", "name": "usurper",
                    "record": dict(RECORD, location=".engine/usurper/", authority="decisions")}
        f = wiring.ontology_entry_apply(squatter)
        self.assertEqual(f["severity"], "hard")
        self.assertIn("usurper", f["message"])
        self.assertIn("outrank", f["message"])
        self.assertEqual(_read(wiring.CATALOG_PATH), before)     # catalog untouched — refused at the seam

    def test_seam_refuses_downgrading_a_reserved_surface(self):
        wiring._write_json(wiring.CATALOG_PATH, VALID_CATALOG)
        before = _read(wiring.CATALOG_PATH)
        downgrade = {"type": "ontology-entry", "name": "contract",
                     "record": dict(RECORD, location=".engine/contracts/",
                                    authority="mechanics-and-guidance")}
        f = wiring.ontology_entry_apply(downgrade)
        self.assertEqual(f["severity"], "hard")
        self.assertIn("rank", f["message"])
        self.assertEqual(_read(wiring.CATALOG_PATH), before)     # catalog untouched

    def test_seam_degrades_gracefully_on_a_non_string_authority(self):
        # a module wire whose record.authority is a JSON list (module.v1.json puts no constraint on it) must
        # NOT crash the apply with an unhashable-type TypeError — the reservation guard stays total and the
        # schema re-check owns the graceful refusal (a hard finding, catalog untouched)
        wiring._write_json(wiring.CATALOG_PATH, VALID_CATALOG)
        before = _read(wiring.CATALOG_PATH)
        malformed = {"type": "ontology-entry", "name": "newthing",
                     "record": dict(RECORD, location=".engine/newthing/", authority=["decisions"])}
        f = wiring.ontology_entry_apply(malformed)
        self.assertEqual(f["severity"], "hard")               # gracefully refused, not an uncaught crash
        self.assertEqual(_read(wiring.CATALOG_PATH), before)   # catalog untouched

    def test_seam_allows_the_legit_reserved_pair(self):
        wiring._write_json(wiring.CATALOG_PATH, VALID_CATALOG)
        legit = {"type": "ontology-entry", "name": "policy",
                 "record": dict(RECORD, location=".engine/policies/", authority="standing-rules")}
        f = wiring.ontology_entry_apply(legit)
        self.assertEqual(f["severity"], "note")                  # the guard does not over-block
        self.assertIn("policy", json.loads(_read(wiring.CATALOG_PATH))["surfaces"])


# ---- path / marker injection (the directive can only touch its hardcoded target) -------------

class TestInjectionRejected(_Redirected):
    def test_mcp_name_must_be_engine_prefixed_and_clean(self):
        for name in ("evil", "engine-../x", "engine-a/b", "../engine-x"):
            f = wiring.apply({"type": "mcp", "name": name, "definition": {}})
            self.assertEqual(f["severity"], "hard")
        self.assertFalse(os.path.exists(wiring.MCP_PATH))

    def test_surface_name_must_be_clean(self):
        f = wiring.apply({"type": "ontology-entry", "name": "../x", "record": RECORD})
        self.assertEqual(f["severity"], "hard")

    def test_gitignore_key_must_be_clean(self):
        f = wiring.apply({"type": "gitignore", "key": "../etc", "lines": ["x"]})
        self.assertEqual(f["severity"], "hard")


# ---- no silent success -----------------------------------------------------------------------

class TestNoSilentSuccess(_Redirected):
    def test_a_could_not_apply_path_is_hard_not_a_soft_ok(self):
        wiring._write_text(wiring.MCP_PATH, "{ broken")
        f = wiring.mcp_apply(MCP)
        self.assertEqual(f["severity"], "hard")          # report() never renders this "OK"


# ---- is_applied (the reusable presence predicate for the module manager's coherence leg) ---------------

class TestIsApplied(_Redirected):
    def test_reports_presence_after_apply_and_absence_before(self):
        self.assertFalse(wiring.is_applied(GI))
        wiring.gitignore_apply(GI)
        self.assertTrue(wiring.is_applied(GI))
        wiring.gitignore_reverse(GI)
        self.assertFalse(wiring.is_applied(GI))

    def test_hook_and_permission_presence(self):
        self.assertFalse(wiring.is_applied(HOOK))
        wiring.hook_apply(HOOK)
        wiring.permission_apply(PERM)
        self.assertTrue(wiring.is_applied(HOOK))
        self.assertTrue(wiring.is_applied(PERM))

    def test_mcp_and_ontology_presence(self):
        self.assertFalse(wiring.is_applied(MCP))
        wiring.mcp_apply(MCP)
        self.assertTrue(wiring.is_applied(MCP))
        wiring._write_json(wiring.CATALOG_PATH, VALID_CATALOG)
        self.assertFalse(wiring.is_applied(ONTO))
        wiring.ontology_entry_apply(ONTO)
        self.assertTrue(wiring.is_applied(ONTO))

    def test_drifted_mcp_and_ontology_read_as_not_applied(self):
        # FULL-CONTENT (defect b): a same-NAME entry whose definition/record has DRIFTED must read
        # NOT applied — an apply would rewrite it, so it is not coherent. The old name-only check
        # wrongly read True here (mutation-bound: reverting the fix makes this test fail).
        wiring._write_json(wiring.CATALOG_PATH, VALID_CATALOG)
        wiring.mcp_apply(MCP)
        wiring.ontology_entry_apply(ONTO)
        self.assertTrue(wiring.is_applied(MCP))
        self.assertTrue(wiring.is_applied(ONTO))
        drifted_mcp = {**MCP, "definition": {**MCP["definition"],
                                             "args": MCP["definition"]["args"] + ["--EXTRA"]}}
        drifted_onto = {**ONTO, "record": {**ONTO["record"], "purpose": "DRIFTED"}}
        self.assertFalse(wiring.is_applied(drifted_mcp), "a drifted mcp definition must read NOT applied")
        self.assertFalse(wiring.is_applied(drifted_onto), "a drifted ontology record must read NOT applied")


# ---- CLI anchor (the exact thing the operator runs is tested) --------------------------------

class TestCLI(_Redirected):
    def _run(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = wiring.main(argv)
        return rc, out.getvalue(), err.getvalue()

    def test_demo_gitignore_runs_clean_and_shows_survival(self):
        path = os.path.join(self._tmp.name, "demo.gitignore")
        rc, out, _ = self._run(["demo-gitignore", path])
        self.assertEqual(rc, 0)
        self.assertIn("Nothing to change", out)
        self.assertIn("Removed only the engine", out)
        self.assertIn("survived", out)
        self.assertEqual(_read(path), "build/\n*.log\n.engine/.venv/\n")  # operator line survived

    def test_apply_then_reverse_verbs_print_plain_verdicts(self):
        path = os.path.join(self._tmp.name, "mine.gitignore")
        rc, out, _ = self._run(["gitignore-apply", path, ".engine/.venv/"])
        self.assertEqual(rc, 0)
        rc, out, _ = self._run(["gitignore-apply", path, ".engine/.venv/"])
        self.assertIn("Nothing to change", out)
        rc, out, _ = self._run(["gitignore-reverse", path])
        self.assertIn("your own lines are untouched", out)

    def test_bad_path_is_a_plain_config_error_not_a_traceback(self):
        afile = os.path.join(self._tmp.name, "afile")
        wiring._write_text(afile, "x")
        rc, out, err = self._run(["gitignore-apply", os.path.join(afile, "sub", "x"), "line"])
        self.assertEqual(rc, 2)
        self.assertIn("CONFIG ERROR", err)


class TestRenderCodeowners(unittest.TestCase):
    """The CODEOWNERS ownership-block renderer (core 25c PR-3) — the pure primitive; the live first-run /
    upgrade wire with the stored operator handle is owed to the instantiator."""

    PATHS = [".engine/engine.json", ".github/workflows/engine-ci.yml", "CLAUDE.md"]

    def test_greenfield_seeds_a_block_only_file(self):
        out = wiring.render_codeowners("", self.PATHS, "octocat")
        self.assertIn("BEGIN engine-managed block: codeowners", out)
        # file-precise, root-anchored, owner normalized with a leading @
        self.assertIn("/.engine/engine.json @octocat", out)
        self.assertIn("/CLAUDE.md @octocat", out)

    def test_brownfield_appends_after_operator_content_last_match_wins(self):
        out = wiring.render_codeowners("# mine\n/src/ @team\n", self.PATHS, "@octocat")
        self.assertTrue(out.startswith("# mine\n/src/ @team"))
        self.assertGreater(out.index("engine-managed block"), out.index("/src/ @team"))

    def test_re_render_replaces_the_block_and_keeps_operator_lines(self):
        first = wiring.render_codeowners("/src/ @team\n", self.PATHS, "@me")
        second = wiring.render_codeowners(first, [".engine/uv.lock"], "@me")
        self.assertEqual(second.count("BEGIN engine-managed block: codeowners"), 1)
        self.assertIn("/.engine/uv.lock @me", second)
        self.assertNotIn("engine.json", second)
        self.assertIn("/src/ @team", second)

    def test_handle_is_normalized_and_empty_is_refused(self):
        self.assertIn("@me", wiring.render_codeowners("", [".engine/engine.json"], "me"))
        with self.assertRaises(wiring.WiringError):
            wiring.render_codeowners("", self.PATHS, "   ")

    def test_fence_id_is_codeowners(self):
        self.assertEqual(wiring.CODEOWNERS_FENCE, "codeowners")
        out = wiring.render_codeowners("", [".engine/engine.json"], "@me")
        self.assertEqual(wiring.fence_reverse(out, "codeowners").strip(), "")


class TestApplyCodeowners(unittest.TestCase):
    """The render-and-write CODEOWNERS mutator wrapper — the ONE write home both first-run instantiation
    and an engine upgrade's re-render call, so the ownership wall renders one way."""

    PATHS = [".engine/engine.json", ".github/CODEOWNERS"]

    def test_writes_then_is_idempotent(self):
        with tempfile.TemporaryDirectory() as d:
            co = os.path.join(d, "sub", ".github", "CODEOWNERS")   # nested: the wrapper must makedirs
            first = wiring.apply_codeowners(co, self.PATHS, "@me")
            self.assertEqual(first["status"], "written")
            self.assertEqual(first["owner"], "@me")
            self.assertEqual(first["paths"], len(self.PATHS))
            self.assertIn("/.engine/engine.json @me", validate.read(co))
            second = wiring.apply_codeowners(co, self.PATHS, "@me")
            self.assertEqual(second["status"], "already")          # write-iff-changed: no rewrite

    def test_preserves_operator_lines(self):
        with tempfile.TemporaryDirectory() as d:
            co = os.path.join(d, "CODEOWNERS")
            with open(co, "w", encoding="utf-8") as fh:
                fh.write("# mine\n/src/ @team\n")
            self.assertEqual(wiring.apply_codeowners(co, self.PATHS, "@me")["status"], "written")
            text = validate.read(co)
            self.assertIn("/src/ @team", text)                     # the operator's own line is untouched
            self.assertIn("engine-managed block: codeowners", text)

    def test_bad_handle_raises_for_the_caller_to_degrade(self):
        with tempfile.TemporaryDirectory() as d:
            co = os.path.join(d, "CODEOWNERS")
            with self.assertRaises(wiring.WiringError):
                wiring.apply_codeowners(co, self.PATHS, "   ")
            self.assertFalse(os.path.exists(co))                   # nothing written on refusal


class TestFoundationIgnores(_Redirected):
    """#409: the foundation `.gitignore` block — a library-helper keyed fence (FOUNDATION_IGNORES_FENCE),
    NOT a module `wires` seam. It is placed by apply_foundation_ignores, is idempotent, never touches operator
    lines or module fences, and is carved out of the orphan-wire reverse leg."""

    def _gi(self):
        return wiring.GITIGNORE_PATH

    def test_apply_writes_the_keyed_fence_with_exactly_the_three_lines(self):
        outcome = wiring.apply_foundation_ignores(self._gi())
        self.assertEqual(outcome["status"], "written")
        text = _read(self._gi())
        b = wiring._find_fence(text.split("\n"), wiring.FOUNDATION_IGNORES_FENCE)
        self.assertIsNotNone(b)
        body = text.split("\n")[b[0] + 1:b[1]]
        self.assertEqual(body, wiring.FOUNDATION_IGNORE_LINES)

    def test_apply_is_idempotent_byte_for_byte(self):
        wiring.apply_foundation_ignores(self._gi())
        first = _read(self._gi())
        second_outcome = wiring.apply_foundation_ignores(self._gi())
        self.assertEqual(second_outcome["status"], "already")
        self.assertEqual(_read(self._gi()), first, "a re-apply of the foundation fence changes nothing")

    def test_operator_lines_and_a_module_fence_are_untouched(self):
        # pre-seed an operator line + a module gitignore fence, then place the foundation block
        with open(self._gi(), "w", encoding="utf-8") as fh:
            fh.write("# my own ignores\nnode_modules/\n")
        wiring.gitignore_apply({"type": "gitignore", "key": "some-module", "lines": [".engine/some/.cache/"]})
        wiring.apply_foundation_ignores(self._gi())
        text = _read(self._gi())
        self.assertIn("node_modules/", text)                       # the operator's own line survives
        self.assertIn("BEGIN engine-managed block: some-module", text)   # the module fence survives
        self.assertIn("BEGIN engine-managed block: foundation-ignores", text)

    def test_reverse_removes_only_the_foundation_fence(self):
        with open(self._gi(), "w", encoding="utf-8") as fh:
            fh.write("keep-me/\n")
        wiring.apply_foundation_ignores(self._gi())
        remainder = wiring.fence_reverse(_read(self._gi()), wiring.FOUNDATION_IGNORES_FENCE)
        self.assertIn("keep-me/", remainder)
        self.assertNotIn("foundation-ignores", remainder)
        self.assertNotIn(".engine/.venv/", remainder)

    def test_foundation_fence_is_NOT_reported_as_an_applied_engine_wire(self):
        # THE #409 regression guard (B1): the orphan-wire reverse leg reads applied_engine_wires; the
        # foundation fence must be carved out there, or check_coherence flags it as a HARD undeclared orphan
        # (pausing first-run + deadlocking retire) and tells the operator to remove the very fix.
        wiring.apply_foundation_ignores(self._gi())
        # also apply a real MODULE fence, which SHOULD appear (the carve-out is specific, not blanket)
        wiring.gitignore_apply({"type": "gitignore", "key": "some-module", "lines": [".engine/some/.cache/"]})
        gitignore_wires = [key for (seam, key, _label) in wiring.applied_engine_wires() if seam == "gitignore"]
        self.assertIn("some-module", gitignore_wires, "a real module fence IS an applied engine wire")
        self.assertNotIn(wiring.FOUNDATION_IGNORES_FENCE, gitignore_wires,
                         "the foundation fence is a library-helper block — never an orphan wire")

    def test_degrades_without_raising_on_a_marker_forging_file(self):
        # a file whose content forges a fence marker → fence_apply raises WiringError; the helper swallows it
        with open(self._gi(), "w", encoding="utf-8") as fh:
            fh.write("# BEGIN engine-managed block: foundation-ignores - do not edit inside\n")  # unterminated
        outcome = wiring.apply_foundation_ignores(self._gi())
        self.assertEqual(outcome["status"], "degraded")

    def test_a_module_wire_may_not_claim_the_reserved_foundation_key(self):
        # deliverable-gate hardening: a module `gitignore` wire keyed 'foundation-ignores' would collide with
        # the foundation body and its uninstall reverser would rip out the foundation block (hidden from
        # coherence by the orphan carve-out). Both apply and reverse must refuse it fail-closed, writing nothing.
        directive = {"type": "gitignore", "key": wiring.FOUNDATION_IGNORES_FENCE, "lines": [".engine/x/"]}
        apply_finding = wiring.gitignore_apply(directive)
        self.assertEqual(apply_finding["severity"], "hard")
        self.assertIn("reserved", apply_finding["message"])
        self.assertFalse(os.path.exists(self._gi()), "a refused reserved-key apply writes nothing")
        reverse_finding = wiring.gitignore_reverse(directive)
        self.assertEqual(reverse_finding["severity"], "hard")
        self.assertIn("reserved", reverse_finding["message"])


class TestCommittedFoundationIgnores(unittest.TestCase):
    """The binding between the single-source constant and the committed file (#409, plan-gate S-A): the
    committed `.gitignore`'s foundation fence body MUST equal wiring.FOUNDATION_IGNORE_LINES, or the two homes
    (the constant + the hand-committed file) can silently drift and a generated repo sees an unexplained
    first-apply diff. Reads the REAL committed file (not a redirected fixture)."""

    def test_committed_gitignore_fence_body_matches_the_constant(self):
        gi = os.path.join(validate.ROOT, ".gitignore")
        text = _read(gi)
        span = wiring._find_fence(text.split("\n"), wiring.FOUNDATION_IGNORES_FENCE)
        self.assertIsNotNone(span, "the committed .gitignore carries the foundation-ignores fence")
        body = text.split("\n")[span[0] + 1:span[1]]
        self.assertEqual(body, wiring.FOUNDATION_IGNORE_LINES,
                         "the committed fence body must equal the single-source constant (no drift)")


# ---- the Codex seams (codex-hook -> .codex/hooks.json; codex-mcp -> .codex/config.toml) -------

CODEX_HOOK = {"type": "codex-hook", "event": "PreToolUse",
              "hook": {"type": "command",
                       "command": "cd \"$(git rev-parse --show-toplevel)\" && "
                                  "sh .engine/tools/codex-hook-runner.sh \".engine/tools/modes.py\""}}
CODEX_HOOK_MATCHED = dict(CODEX_HOOK, event="SessionStart", matcher="startup")
CODEX_MCP = {"type": "codex-mcp", "name": "engine-knowledge",
             "definition": {"command": "uv",
                            "args": ["run", "--directory", ".engine", "--frozen", "--",
                                     "python", "tools/knowledge_mcp_server.py"],
                            "default_tools_approval_mode": "auto"}}


class TestCodexHookSeam(_Redirected):
    def test_apply_writes_entry_and_carries_the_retrust_notice(self):
        f = wiring.apply(CODEX_HOOK)
        self.assertEqual(f["severity"], "note")
        self.assertIn("/hooks", f["message"], "a change to the Codex registration must tell the "
                      "operator to re-trust (Codex skips changed hooks until re-trusted)")
        data = json.loads(_read(wiring.CODEX_HOOKS_PATH))
        [group] = data["hooks"]["PreToolUse"]
        self.assertNotIn("matcher", group, "an always-fire group omits the matcher key (Codex's "
                         "documented form; an empty matcher string is undocumented there)")
        self.assertEqual(group["hooks"][0]["command"], CODEX_HOOK["hook"]["command"])

    def test_matcher_group_carries_the_matcher_key(self):
        wiring.apply(CODEX_HOOK_MATCHED)
        data = json.loads(_read(wiring.CODEX_HOOKS_PATH))
        [group] = data["hooks"]["SessionStart"]
        self.assertEqual(group["matcher"], "startup")

    def test_apply_is_idempotent_and_noop_carries_no_retrust_notice(self):
        wiring.apply(CODEX_HOOK)
        before = _read(wiring.CODEX_HOOKS_PATH)
        f = wiring.apply(CODEX_HOOK)
        self.assertEqual(_read(wiring.CODEX_HOOKS_PATH), before)
        self.assertNotIn("/hooks", f["message"], "a no-op changed nothing, so no re-trust nag")

    def test_non_engine_command_refused(self):
        bad = dict(CODEX_HOOK, hook={"type": "command", "command": "rm -rf /"})
        f = wiring.apply(bad)
        self.assertEqual(f["severity"], "hard")
        self.assertFalse(os.path.exists(wiring.CODEX_HOOKS_PATH))

    def test_reverse_removes_only_the_engine_entry(self):
        os.makedirs(os.path.dirname(wiring.CODEX_HOOKS_PATH), exist_ok=True)
        product = {"type": "command", "command": "python3 scripts/my-own-hook.py"}
        with open(wiring.CODEX_HOOKS_PATH, "w", encoding="utf-8") as fh:
            json.dump({"hooks": {"PreToolUse": [{"hooks": [product]}]}}, fh)
        wiring.apply(CODEX_HOOK)
        f = wiring.reverse(CODEX_HOOK)
        self.assertEqual(f["severity"], "note")
        self.assertIn("/hooks", f["message"])
        data = json.loads(_read(wiring.CODEX_HOOKS_PATH))
        [group] = data["hooks"]["PreToolUse"]
        self.assertEqual(group["hooks"], [product], "the operator's own hook survives the reverse")

    def test_malformed_file_refused_and_unchanged(self):
        os.makedirs(os.path.dirname(wiring.CODEX_HOOKS_PATH), exist_ok=True)
        with open(wiring.CODEX_HOOKS_PATH, "w", encoding="utf-8") as fh:
            fh.write("{not json")
        f = wiring.apply(CODEX_HOOK)
        self.assertEqual(f["severity"], "hard")
        self.assertEqual(_read(wiring.CODEX_HOOKS_PATH), "{not json")

    def test_is_applied_and_identity(self):
        self.assertFalse(wiring.is_applied(CODEX_HOOK))
        wiring.apply(CODEX_HOOK)
        self.assertTrue(wiring.is_applied(CODEX_HOOK))
        seam, key = wiring.declared_wire_identity(CODEX_HOOK)
        self.assertEqual(seam, "codex-hook")
        self.assertEqual(key[1], None, "an empty/absent matcher normalizes to None in the identity")
        empty = dict(CODEX_HOOK, matcher="")
        self.assertEqual(wiring.declared_wire_identity(empty)[1][1], None)

    def test_orphan_enumerator_sees_the_engine_entry(self):
        wiring.apply(CODEX_HOOK)
        entries = [e for e in wiring.applied_engine_wires() if e[0] == "codex-hook"]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0][1], wiring.declared_wire_identity(CODEX_HOOK)[1])


class TestCodexMcpSeam(_Redirected):
    def _parsed(self):
        import tomllib
        return tomllib.loads(_read(wiring.CODEX_CONFIG_PATH))

    def test_apply_renders_a_parseable_fenced_table(self):
        f = wiring.apply(CODEX_MCP)
        self.assertEqual(f["severity"], "note")
        parsed = self._parsed()
        self.assertEqual(parsed["mcp_servers"]["engine-knowledge"]["command"], "uv")
        self.assertEqual(parsed["mcp_servers"]["engine-knowledge"]["args"],
                         CODEX_MCP["definition"]["args"])
        self.assertIn(wiring.FENCE_BEGIN.format(id="engine-knowledge"),
                      _read(wiring.CODEX_CONFIG_PATH))

    def test_per_tool_approval_subtables_render(self):
        directive = {"type": "codex-mcp", "name": "engine-memory",
                     "definition": {"command": "uv", "args": ["run"],
                                    "tools": {"store": {"approval_mode": "writes"}}}}
        wiring.apply(directive)
        parsed = self._parsed()
        self.assertEqual(parsed["mcp_servers"]["engine-memory"]["tools"]["store"]["approval_mode"],
                         "writes")

    def test_apply_is_idempotent_and_full_content_mirrored(self):
        wiring.apply(CODEX_MCP)
        before = _read(wiring.CODEX_CONFIG_PATH)
        wiring.apply(CODEX_MCP)
        self.assertEqual(_read(wiring.CODEX_CONFIG_PATH), before)
        self.assertTrue(wiring.is_applied(CODEX_MCP))
        drifted = dict(CODEX_MCP, definition=dict(CODEX_MCP["definition"], command="uvx"))
        self.assertFalse(wiring.is_applied(drifted), "a drifted definition reads as NOT applied")

    def test_product_content_survives_apply_and_reverse(self):
        os.makedirs(os.path.dirname(wiring.CODEX_CONFIG_PATH), exist_ok=True)
        product = '# my own notes\n[mcp_servers.my-server]\ncommand = "npx"\n'
        with open(wiring.CODEX_CONFIG_PATH, "w", encoding="utf-8") as fh:
            fh.write(product)
        wiring.apply(CODEX_MCP)
        self.assertIn('# my own notes', _read(wiring.CODEX_CONFIG_PATH))
        self.assertEqual(self._parsed()["mcp_servers"]["my-server"]["command"], "npx")
        f = wiring.reverse(CODEX_MCP)
        self.assertEqual(f["severity"], "note")
        text = _read(wiring.CODEX_CONFIG_PATH)
        self.assertNotIn("engine-knowledge", text)
        self.assertIn('command = "npx"', text, "the operator's own server survives the reverse")

    def test_unparseable_file_refused_and_unchanged(self):
        os.makedirs(os.path.dirname(wiring.CODEX_CONFIG_PATH), exist_ok=True)
        broken = 'x = """unterminated\n'
        with open(wiring.CODEX_CONFIG_PATH, "w", encoding="utf-8") as fh:
            fh.write(broken)
        f = wiring.apply(CODEX_MCP)
        self.assertEqual(f["severity"], "hard")
        self.assertEqual(_read(wiring.CODEX_CONFIG_PATH), broken, "refuse means no write")

    def test_result_that_would_not_parse_is_refused_and_not_written(self):
        # A product-owned table already claims the engine server's name: appending the engine's
        # fenced table would redeclare it, which TOML forbids — the post-transform guard must
        # refuse and leave the file byte-identical.
        os.makedirs(os.path.dirname(wiring.CODEX_CONFIG_PATH), exist_ok=True)
        clash = '[mcp_servers.engine-knowledge]\ncommand = "npx"\n'
        with open(wiring.CODEX_CONFIG_PATH, "w", encoding="utf-8") as fh:
            fh.write(clash)
        f = wiring.apply(CODEX_MCP)
        self.assertEqual(f["severity"], "hard")
        self.assertEqual(_read(wiring.CODEX_CONFIG_PATH), clash)

    def test_non_engine_name_refused(self):
        f = wiring.apply({"type": "codex-mcp", "name": "my-server",
                          "definition": {"command": "npx"}})
        self.assertEqual(f["severity"], "hard")
        self.assertFalse(os.path.exists(wiring.CODEX_CONFIG_PATH))

    def test_orphan_enumerator_sees_the_engine_fence(self):
        wiring.apply(CODEX_MCP)
        entries = [e for e in wiring.applied_engine_wires() if e[0] == "codex-mcp"]
        self.assertEqual(entries, [("codex-mcp", "engine-knowledge",
                                    wiring._rel(wiring.CODEX_CONFIG_PATH))])
        self.assertEqual(wiring.declared_wire_identity(CODEX_MCP),
                         ("codex-mcp", "engine-knowledge"))


if __name__ == "__main__":
    unittest.main()
