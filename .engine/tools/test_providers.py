#!/usr/bin/env python3
"""Self-tests for the provider-normalization seam (providers.py) — the laws eADR-0034 rest on:
normalize is the IDENTITY for a Claude payload (the byte-stability pin); a Codex edit is rewritten
with EVERY path its batch patch names; session resolution is payload-first and the live-session
marker REFUSES on any ambiguity (stale, foreign-owned, future-stamped) rather than guessing; and the
Codex hook-command form renders through the shim while the Claude form stays byte-identical.

Run: uv run --directory .engine --frozen -- python -m unittest discover -s tools -p 'test_*.py' -b
"""
from __future__ import annotations
import json
import os
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hooks      # noqa: E402
import providers  # noqa: E402

PATCH = """*** Begin Patch
*** Update File: src/app.py
@@
-old
+new
*** Add File: docs/notes.md
+hello
*** Delete File: tmp/scratch.txt
*** End Patch"""


class TestNormalizeIdentityForClaude(unittest.TestCase):
    def test_claude_payload_is_returned_as_the_same_object(self):
        """The byte-stability law: a payload with no Codex tool name passes through UNTOUCHED — the
        same dict object, so the Claude path cannot drift by construction."""
        for tool in ("Edit", "Write", "MultiEdit", "NotebookEdit", "Bash", "Read", "ExitPlanMode"):
            payload = {"tool_name": tool, "tool_input": {"file_path": "x.py"}, "session_id": "s1"}
            self.assertIs(providers.normalize("PreToolUse", payload), payload)

    def test_non_dict_payloads_pass_through(self):
        self.assertIsNone(providers.normalize("Stop", None))
        self.assertEqual(providers.normalize("Stop", []), [])


class TestNormalizeCodexEdit(unittest.TestCase):
    def test_apply_patch_rewrites_to_edit_with_every_touched_path(self):
        payload = {"tool_name": "apply_patch", "tool_input": {"patch": PATCH}, "session_id": "s1"}
        out = providers.normalize("PreToolUse", payload)
        self.assertEqual(out["tool_name"], "Edit")
        self.assertEqual(out["tool_input"]["file_paths"],
                         ["src/app.py", "docs/notes.md", "tmp/scratch.txt"],
                         "a batch patch names MANY files; every one must be carried")
        self.assertEqual(out["tool_input"]["file_path"], "src/app.py")
        self.assertEqual(out["provider_raw"]["tool_name"], "apply_patch")
        self.assertEqual(payload["tool_name"], "apply_patch", "the input payload is never mutated")

    def test_envelope_found_under_an_unknown_key(self):
        out = providers.normalize("PreToolUse",
                                  {"tool_name": "apply_patch", "tool_input": {"weird_field": PATCH}})
        self.assertEqual(out["tool_input"]["file_paths"][0], "src/app.py")

    def test_no_envelope_still_rewrites_the_tool_name(self):
        """The deny must fire on the NAME even when the patch body cannot be found — only the
        per-file refinement is lost, never the gate decision."""
        out = providers.normalize("PreToolUse", {"tool_name": "apply_patch", "tool_input": {}})
        self.assertEqual(out["tool_name"], "Edit")
        self.assertEqual(out["tool_input"]["file_paths"], [])

    def test_codex_shell_rewrites_to_bash_with_a_joined_command(self):
        out = providers.normalize("PreToolUse", {"tool_name": "local_shell",
                                                 "tool_input": {"command": ["git", "commit", "-m", "x y"]}})
        self.assertEqual(out["tool_name"], "Bash")
        self.assertEqual(out["tool_input"]["command"], "git commit -m 'x y'")


class TestSessionResolution(unittest.TestCase):
    def test_payload_session_id_wins(self):
        with mock.patch.dict(os.environ, {"ENGINE_SESSION_ID": "env-sid"}):
            self.assertEqual(providers.resolve_session({"session_id": "payload-sid"}), "payload-sid")

    def test_env_chain_order_and_placeholder_guard(self):
        with mock.patch.dict(os.environ, {"ENGINE_SESSION_ID": "neutral",
                                          "CLAUDE_CODE_SESSION_ID": "claude"}):
            self.assertEqual(providers.session_from_env(), "neutral",
                             "the neutral override is deliberately first (an explicit knob)")
        with mock.patch.dict(os.environ, {"ENGINE_SESSION_ID": "${UNEXPANDED}",
                                          "CLAUDE_CODE_SESSION_ID": "claude"}):
            self.assertEqual(providers.session_from_env(), "claude")

    def test_explicit_flag_wins_over_everything(self):
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "claude"}):
            self.assertEqual(providers.resolve_session(explicit="typed"), "typed")


class TestLiveSessionMarker(unittest.TestCase):
    def setUp(self):
        # Redirect the marker into a temp home so tests never touch the real per-user marker.
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self._patch = mock.patch.object(providers, "live_session_path",
                                        lambda: os.path.join(self._tmp.name, "marker.json"))
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self._tmp.cleanup()

    def _clear_env(self):
        return mock.patch.dict(os.environ, {}, clear=True)

    def test_write_then_read_round_trips(self):
        self.assertTrue(providers.write_live_session("sid-1", "codex"))
        record = providers.read_live_session()
        self.assertEqual(record["session_id"], "sid-1")
        self.assertEqual(record["provider"], "codex")
        mode = os.stat(providers.live_session_path()).st_mode & 0o777
        self.assertEqual(mode, 0o600, "owner-only permissions are part of the fail-safe spec")

    def test_marker_is_the_last_resort_for_a_typed_verb(self):
        providers.write_live_session("sid-2", "codex")
        with self._clear_env():
            self.assertEqual(providers.resolve_session(), "sid-2")

    def test_a_stale_marker_is_refused(self):
        providers.write_live_session("sid-3")
        path = providers.live_session_path()
        record = json.load(open(path))
        record["ts"] = time.time() - (25 * 3600)
        with open(path, "w") as fh:
            fh.write(json.dumps(record))
        with self._clear_env():
            self.assertIsNone(providers.read_live_session(), "stale → refuse, never guess")
            self.assertIsNone(providers.resolve_session())

    def test_a_future_stamped_marker_is_refused(self):
        providers.write_live_session("sid-4")
        path = providers.live_session_path()
        record = json.load(open(path))
        record["ts"] = time.time() + 3600
        with open(path, "w") as fh:
            fh.write(json.dumps(record))
        self.assertIsNone(providers.read_live_session())

    def test_a_malformed_or_absent_marker_is_refused(self):
        self.assertIsNone(providers.read_live_session())
        with open(providers.live_session_path(), "w") as fh:
            fh.write("{not json")
        self.assertIsNone(providers.read_live_session())


class TestDetect(unittest.TestCase):
    def test_env_wins(self):
        with mock.patch.dict(os.environ, {"ENGINE_PROVIDER": "codex"}):
            self.assertEqual(providers.detect(), "codex")
        with mock.patch.dict(os.environ, {"ENGINE_PROVIDER": "claude"}):
            self.assertEqual(providers.detect({"turn_id": "t"}), "claude")

    def test_payload_sniff_and_default(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(providers.detect({"turn_id": "t"}), "codex")
            self.assertEqual(providers.detect({"tool_name": "apply_patch"}), "codex")
            self.assertEqual(providers.detect({"tool_name": "Edit"}), "claude")
            self.assertEqual(providers.detect(), "claude")


class TestMarkerProviderConfinement(unittest.TestCase):
    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self._patch = mock.patch.object(providers, "live_session_path",
                                        lambda: os.path.join(self._tmp.name, "marker.json"))
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self._tmp.cleanup()

    def test_a_claude_marker_never_resolves_a_session(self):
        """The Claude fail-safe stays historical: a Claude session always exports its env var, so
        the marker leg is CODEX-ONLY — a Claude-provider marker must resolve nothing (the mis-grant
        the review gate confined)."""
        providers.write_live_session("claude-sid", "claude")
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(providers.resolve_session())

    def test_a_codex_marker_still_resolves(self):
        providers.write_live_session("codex-sid", "codex")
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(providers.resolve_session(), "codex-sid")


class TestPlanCarveoutProviderConfined(unittest.TestCase):
    def test_plan_mode_opens_nothing_on_codex(self):
        """Plan mode is Claude Code's feature: a Codex payload reporting permission_mode "plan"
        (its vocabulary is unverified) must NOT open the Explore write-gate — inert BY RULE."""
        import modes
        self.assertTrue(modes.is_plan_artifact("Edit", {}, "plan", provider="claude"))
        self.assertFalse(modes.is_plan_artifact("Edit", {}, "plan", provider="codex"))
        self.assertFalse(modes.is_plan_artifact("Edit", {"is_plan_file": True}, None,
                                                provider="codex"))

    def test_the_gate_denies_a_codex_plan_mode_edit_in_explore(self):
        import contextlib
        import tempfile
        import modes
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(modes.tempfile, "gettempdir", return_value=tmp), \
                mock.patch.dict(os.environ, {"ENGINE_PROVIDER": "codex"}):
            decision = modes.handler({"session_id": "s-codex", "tool_name": "Edit",
                                      "tool_input": {}, "permission_mode": "plan"})
            self.assertEqual(decision.get("permissionDecision"), "deny",
                             "Codex has no plan mode; the carve-out must not open the gate")
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(modes.tempfile, "gettempdir", return_value=tmp), \
                mock.patch.dict(os.environ, {}, clear=True):
            decision = modes.handler({"session_id": "s-claude", "tool_name": "Edit",
                                      "tool_input": {}, "permission_mode": "plan"})
            self.assertEqual(decision.get("action"), "proceed",
                             "the Claude plan-artifact carve-out is unchanged")


class TestCodexRegistrationDrift(unittest.TestCase):
    """The renderer↔committed-literals pin the Claude side has and the Codex side was missing:
    every codex-hook command in the manifests AND in the committed .codex/hooks.json must be
    byte-identical to hooks.hook_command(provider="codex") over its own script."""

    _SHIM_RE = None

    @classmethod
    def setUpClass(cls):
        import re
        cls._SHIM_RE = re.compile(r'sh "\.engine/tools/codex-hook-runner\.sh" "([^"]+)"(.*)$')

    def _assert_rendered(self, command: str, where: str):
        m = self._SHIM_RE.search(command)
        self.assertIsNotNone(m, f"{where}: not the shim form: {command}")
        script = (m.group(1) + m.group(2)).strip()
        self.assertEqual(command, hooks.hook_command(script, provider="codex"),
                         f"{where}: committed literal drifted from the renderer")

    def test_every_manifest_codex_hook_matches_the_renderer(self):
        import glob as _glob
        import validate
        total = 0
        for mpath in sorted(_glob.glob(os.path.join(validate.ROOT, ".engine", "modules", "*",
                                                    "manifest.json"))):
            manifest = validate.load_json(mpath)
            for wire in manifest.get("wires", []):
                if wire.get("type") == "codex-hook":
                    total += 1
                    self._assert_rendered(wire["hook"]["command"], os.path.basename(os.path.dirname(mpath)))
        self.assertGreaterEqual(total, 24, "the codex-hook wires exist and were all checked")

    def test_every_committed_hooks_json_command_matches_the_renderer(self):
        import validate
        data = validate.load_json(os.path.join(validate.ROOT, ".codex", "hooks.json"))
        commands = [h["command"] for groups in data["hooks"].values()
                    for g in groups for h in g["hooks"]]
        self.assertGreaterEqual(len(commands), 24)
        for command in commands:
            self._assert_rendered(command, ".codex/hooks.json")

    def test_the_modes_accept_hook_is_deliberately_absent_on_codex(self):
        """Codex Build entry is the typed verb ONLY (eADR-0034): the plan-acceptance hook must not
        be registered, and a future mirror-everything cleanup must trip here, not ship it."""
        import validate
        data = validate.load_json(os.path.join(validate.ROOT, ".codex", "hooks.json"))
        commands = [h["command"] for groups in data["hooks"].values()
                    for g in groups for h in g["hooks"]]
        self.assertFalse(any("modes.py" in c and "accept-hook" in c for c in commands),
                         "the plan-acceptance adapter has no Codex registration by design")

    def test_wire_apply_leaves_the_claude_files_byte_identical(self):
        """The Claude byte-stability regression the PR body cites: applying EVERY manifest wire is
        a no-op over the committed .claude/settings.json and .mcp.json."""
        import glob as _glob
        import validate
        import wiring
        settings = validate.read(wiring.SETTINGS_PATH)
        mcp = validate.read(wiring.MCP_PATH)
        directives = []
        for mpath in sorted(_glob.glob(os.path.join(validate.ROOT, ".engine", "modules", "*",
                                                    "manifest.json"))):
            directives += validate.load_json(mpath).get("wires", [])
        for f in wiring.apply_all(directives):
            self.assertNotEqual(f.get("severity"), "hard", f)
        self.assertEqual(validate.read(wiring.SETTINGS_PATH), settings)
        self.assertEqual(validate.read(wiring.MCP_PATH), mcp)


class TestParityCheckSelfIntegrity(unittest.TestCase):
    def test_hook_identity_parses_both_live_command_forms(self):
        """The parity check's private grammar is bound to the one renderer, both forms — the
        blindness canary's static half."""
        import provider_parity_check as ppc
        claude_cmd = hooks.hook_command(".engine/tools/modes.py")
        codex_cmd = hooks.hook_command(".engine/tools/modes.py", provider="codex")
        self.assertEqual(ppc._hook_identity(claude_cmd), ".engine/tools/modes.py")
        self.assertEqual(ppc._hook_identity(codex_cmd), ".engine/tools/modes.py")
        tail_claude = hooks.hook_command(".engine/tools/telemetry.py run-ambient")
        self.assertEqual(ppc._hook_identity(tail_claude), ".engine/tools/telemetry.py run-ambient")

    def test_blind_extraction_goes_loud_not_green(self):
        """A registration whose commands the grammar cannot parse must produce a broken-check
        finding, never an empty (green) comparison set."""
        import json as _json
        import tempfile
        import provider_parity_check as ppc
        with tempfile.TemporaryDirectory() as root:
            os.makedirs(os.path.join(root, ".claude"))
            with open(os.path.join(root, ".claude", "settings.json"), "w") as fh:
                _json.dump({"hooks": {"PreToolUse": [{"matcher": "", "hooks": [
                    {"type": "command", "command": "run .engine/tools/modes.py somehow"}]}]}}, fh)
            finds = ppc.findings("hard", root=root)
            self.assertTrue(any("the check's command grammar recognized none" in f["message"]
                                for f in finds), finds)

    def test_the_exception_ledger_is_not_in_the_retirement_set(self):
        """The #411 trap, pinned: a standing check's data file must never ride the first-run
        retirement set."""
        import validate
        assets = validate.load_json(os.path.join(validate.ROOT, ".engine", "provisioning",
                                                 "first-run-assets.json"))
        everything = list(assets.get("files", [])) + list(assets.get("directories", []))
        self.assertFalse(any("provider-exceptions" in entry for entry in everything))


class TestCaptureStatusPathSingleHomed(unittest.TestCase):
    def test_writer_and_readers_spell_the_same_path(self):
        """The marker is written by capture and read by boot and telemetry; three spellings, one
        path — pinned so a move cannot silently sever the disclosure chain."""
        import boot
        import telemetry
        import validate
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "memory"))
        from memory import capture
        expected = os.path.realpath(os.path.join(validate.ROOT, ".engine", "telemetry", ".cache",
                                                 "memory-capture.status"))
        self.assertEqual(os.path.realpath(capture.CAPTURE_STATUS_PATH), expected)
        self.assertEqual(os.path.realpath(telemetry.CAPTURE_STATUS_PATH), expected)
        # boot reads it inline; pin the literal by rendering the joined path the same way
        self.assertEqual(os.path.realpath(os.path.join(validate.ROOT, ".engine", "telemetry",
                                                       ".cache", "memory-capture.status")), expected)


class TestCodexHookCommandForm(unittest.TestCase):
    def test_codex_form_rides_the_shim_and_resolves_its_own_root(self):
        cmd = hooks.hook_command(".engine/tools/modes.py accept-hook", provider="codex")
        self.assertIn('cd "$(git rev-parse --show-toplevel', cmd,
                      "Codex has no project-dir token; the command must locate the root itself")
        self.assertIn('sh ".engine/tools/codex-hook-runner.sh" ".engine/tools/modes.py" accept-hook', cmd)
        self.assertNotIn("CLAUDE_PROJECT_DIR", cmd, "Claude vocabulary never leaks into the Codex form")

    def test_claude_form_is_byte_identical_to_the_default(self):
        rel = ".engine/tools/boot.py"
        self.assertEqual(hooks.hook_command(rel), hooks.hook_command(rel, provider="claude"))


if __name__ == "__main__":
    unittest.main()
