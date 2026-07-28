#!/usr/bin/env python3
"""Self-tests for the hooks contract substrate: the closed event inventory, the block
budget + block cap, the per-OS interpreter-path resolver, the fail-open-and-flag harness, and the pure
block-budget coherence leg (validate.block_budget_findings).

Run: uv run --directory .engine --frozen -- python -m unittest discover -s tools -p 'test_*.py' -b

These lock the laws hooks owns:
  - the event inventory is the engine's chosen subset, with PostToolUse three-owner (validation·telemetry·
    modes), SessionEnd hooks-owned and non-blocking, UserPromptSubmit boot-owned injection; only PreToolUse
    and Stop are block-eligible;
    the block-eligible invariant set ships EMPTY.
  - the block cap is 8, overridable via CLAUDE_CODE_STOP_HOOK_BLOCK_CAP (verified on the live platform).
  - the interpreter path is ${CLAUDE_PROJECT_DIR}-rooted, per-OS (POSIX bin/python, Windows Scripts/
    python.exe), never bare python / uv run.
  - the harness FAILS OPEN: a crashing handler, a malformed event payload, or a block requested on a
    non-eligible event all PROCEED (a non-2 exit) and emit a plain-language finding — never a hard block;
    only a handler that returns block() on PreToolUse/Stop exits 2; on a forced Stop continuation
    (stop_hook_active) the handler STILL runs but its block is downgraded to proceed, so it can never
    re-block and loop the cap (close needs the give-up moment to log; the guarantee is the
    harness's, by construction, not the handler's).
  - the static block-budget leg flags a block declared on a non-eligible event, is silent on an empty set,
    and agrees with the runtime BLOCK_ELIGIBLE_EVENTS (a drift guard). The leg is built + fixture-tested
    with no live rule (the interface_resolution_findings / agent_coherence_findings precedent); the live
    rule wires at the first hook-wiring slice.
"""
from __future__ import annotations
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hooks     # noqa: E402
import modes     # noqa: E402  (the canonical stance vocabulary the block-registry leg validates against)
import validate  # noqa: E402


def _run(event, handler, payload=None, stdin_text=None):
    """Drive the real run_hook with captured streams. Returns (exit_code, stdout, stderr)."""
    if stdin_text is None:
        stdin_text = json.dumps(payload or {})
    out, err = io.StringIO(), io.StringIO()
    code = hooks.run_hook(event, handler, stdin=io.StringIO(stdin_text), stdout=out, stderr=err)
    return code, out.getvalue(), err.getvalue()


class TestEventInventory(unittest.TestCase):
    def test_the_seven_governed_events(self):
        self.assertEqual(hooks.EVENTS, {
            "SessionStart", "PreToolUse", "PostToolUse", "PreCompact",
            "Stop", "SessionEnd", "UserPromptSubmit"})

    def test_only_pretooluse_and_stop_are_block_eligible(self):
        self.assertEqual(hooks.BLOCK_ELIGIBLE_EVENTS, {"PreToolUse", "Stop"})
        for ev, meta in hooks.EVENT_INVENTORY.items():
            self.assertEqual(meta["blocks"], ev in {"PreToolUse", "Stop"},
                             f"{ev} block-eligibility")

    def test_posttooluse_enumerates_its_three_owners(self):
        # validation's touched-file run + telemetry's ambient capture + modes' plan-acceptance
        # Build-entry trigger coexist on one event (the owner inventory).
        self.assertEqual(hooks.EVENT_INVENTORY["PostToolUse"]["owners"],
                         ("validation", "telemetry", "modes"))

    def test_posttooluse_may_inject_and_stays_non_blocking(self):
        # modes' acceptance trigger injects an assistant-internal stance directive
        # (additionalContext) on Build entry, so PostToolUse may inject — but it never blocks.
        self.assertTrue(hooks.EVENT_INVENTORY["PostToolUse"]["injects"])
        self.assertFalse(hooks.EVENT_INVENTORY["PostToolUse"]["blocks"])

    def test_sessionend_is_hooks_owned_and_cannot_block(self):
        self.assertEqual(hooks.EVENT_INVENTORY["SessionEnd"]["owners"], ("hooks",))
        self.assertFalse(hooks.EVENT_INVENTORY["SessionEnd"]["blocks"])

    def test_userpromptsubmit_is_boot_owned_injection(self):
        self.assertEqual(hooks.EVENT_INVENTORY["UserPromptSubmit"]["owners"], ("boot",))
        self.assertTrue(hooks.EVENT_INVENTORY["UserPromptSubmit"]["injects"])

    def test_block_eligible_invariant_set_starts_empty(self):
        self.assertEqual(hooks.BLOCK_ELIGIBLE_INVARIANTS, ())


class TestBlockCap(unittest.TestCase):
    def test_cap_is_eight_with_the_platform_env_override(self):
        self.assertEqual(hooks.STOP_HOOK_BLOCK_CAP, 8)
        self.assertEqual(hooks.STOP_HOOK_BLOCK_CAP_ENV, "CLAUDE_CODE_STOP_HOOK_BLOCK_CAP")


class TestInterpreterPath(unittest.TestCase):
    def test_posix_form(self):
        self.assertEqual(hooks.interpreter_path("posix"),
                         "${CLAUDE_PROJECT_DIR}/.engine/.venv/bin/python")

    def test_windows_form(self):
        self.assertEqual(hooks.interpreter_path("nt"),
                         "${CLAUDE_PROJECT_DIR}/.engine/.venv/Scripts/python.exe")

    def test_is_project_dir_rooted_and_never_bare(self):
        for name in ("posix", "nt"):
            p = hooks.interpreter_path(name)
            self.assertTrue(p.startswith("${CLAUDE_PROJECT_DIR}/.engine/.venv/"))
            self.assertNotIn("uv ", p)
            self.assertNotEqual(p, "python")

    def test_hook_command_calls_the_launcher_with_the_explicit_interpreter(self):
        # The form is now a call to the hook launcher (.engine/tools/hook-runner.sh) with the explicit
        # ${CLAUDE_PROJECT_DIR}-rooted venv interpreter named as its first argument, then the
        # ${CLAUDE_PROJECT_DIR}-rooted script. The wait/exec mechanics live in the launcher; the command
        # stays legible. Byte-exact so a drift is caught.
        # The script PATH token is double-quoted (#390) so a spaced project dir does not word-split; the
        # interpreter and launcher tokens have always been quoted. Hand-derived to the intended form.
        self.assertEqual(
            hooks.hook_command("tools/some_hook.py", "posix"),
            'sh "${CLAUDE_PROJECT_DIR}/.engine/tools/hook-runner.sh" '
            '"${CLAUDE_PROJECT_DIR}/.engine/.venv/bin/python" "${CLAUDE_PROJECT_DIR}/tools/some_hook.py"')


class TestHookCommandWaitWrapper(unittest.TestCase):
    """The wait/exec mechanics moved from the inline command into the committed launcher
    (.engine/tools/hook-runner.sh) so the displayed command is legible, NOT a wall of shell. The launcher
    keeps exactly the fresh-worktree-race behaviour (issue #83): bounded wait, exec-only-the-given-venv-
    interpreter, never a system-Python fallback, args preserved, and the live wait/degrade behaviour."""

    WRAPPER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hook-runner.sh")

    def test_the_command_is_legible_not_a_wall_of_shell(self):
        # the presentation fix: the displayed command no longer carries shell control-flow — it just calls
        # the launcher. The wall (while/done/exec/sleep/loop-arithmetic) lives in hook-runner.sh now.
        cmd = hooks.hook_command(".engine/tools/boot.py", "posix")
        self.assertIn("hook-runner.sh", cmd)
        for control in ("while", "done", "exec", "sleep", "n=$(("):
            self.assertNotIn(control, cmd)

    def test_command_names_the_explicit_venv_interpreter_never_system_python(self):
        # the conformance witness: the explicit ${CLAUDE_PROJECT_DIR}-rooted venv interpreter is
        # named IN the command (the launcher's first arg), never a bare/system interpreter or `uv run`.
        cmd = hooks.hook_command(".engine/tools/boot.py", "posix")
        self.assertIn(f'"{hooks.interpreter_path("posix")}"', cmd)
        self.assertNotIn("exec python", cmd)
        self.assertNotIn("uv ", cmd)
        self.assertNotIn("/usr/bin/", cmd)
        self.assertNotIn("/usr/local/bin/", cmd)

    def test_the_launcher_waits_bounded_and_execs_only_the_given_interpreter(self):
        # the launcher source: a bounded (not infinite) wait, then a SINGLE exec of the resolved venv
        # interpreter (the named POSIX bin/python, or its Windows Scripts/python.exe sibling under the same
        # venv root — #407) with the forwarded args — never a bare/system Python fallback.
        with open(self.WRAPPER) as fh:
            src = fh.read()
        self.assertIn("while", src)
        self.assertIn("-lt", src)                       # a numeric cap, never an unbounded loop
        self.assertIn("shift", src)                     # the interpreter arg is consumed, so "$@" = script+args
        self.assertIn('exec "$interp" "$@"', src)       # one exec, of the passed interpreter, args forwarded
        self.assertEqual(src.count("exec "), 1)
        for forbidden in ("uv ", "/usr/bin/", "/usr/local/bin/", "exec python"):
            self.assertNotIn(forbidden, src)

    def test_per_os_form_carries_its_own_venv_interpreter(self):
        self.assertIn(".engine/.venv/bin/python",
                      hooks.hook_command(".engine/tools/boot.py", "posix"))
        self.assertIn(".engine/.venv/Scripts/python.exe",
                      hooks.hook_command(".engine/tools/boot.py", "nt"))

    def test_trailing_args_stay_bare_words_after_the_quoted_path(self):
        # the footgun guard, post-#390: the script PATH is now double-quoted, but the arg word (` hook` /
        # ` accept-hook`) stays OUTSIDE the quotes as the final, word-splittable token — so it still reaches
        # the launcher as its own positional param. The two conditions together (quoted path, bare arg) are
        # exactly what makes both a spaced project dir AND arg-passing work.
        kg = hooks.hook_command(".engine/tools/knowledge_gen.py hook", "posix")
        self.assertTrue(kg.rstrip().endswith('knowledge_gen.py" hook'), kg)   # path quoted, arg bare
        modes = hooks.hook_command(".engine/tools/modes.py accept-hook", "posix")
        self.assertTrue(modes.rstrip().endswith('modes.py" accept-hook'), modes)

    def test_spaced_project_dir_delivers_the_intact_script_path_and_arg(self):
        # #390 regression, driven through the REAL committed launcher and the REAL `sh -c` substitution:
        # a project directory whose path contains a space used to word-split the UNQUOTED script tail, so the
        # launcher forwarded a truncated path, python exited 2, and the platform read that exit-2 as a
        # fail-CLOSED BLOCK on every tool call and turn-end. This runs the rendered command under `sh -c`
        # with a spaced ${CLAUDE_PROJECT_DIR} and an ARG-BEARING wire, and asserts the interpreter receives
        # the WHOLE spaced path as ONE argument plus the arg. It FAILS on the pre-#390 unquoted form (the
        # path would split into two args, yielding three output lines), which is what makes it a real
        # falsification rather than a string-shape assertion.
        with tempfile.TemporaryDirectory() as base:
            proj = os.path.join(base, "my project")                     # the space is the whole point
            tools_dir = os.path.join(proj, ".engine", "tools")
            venv_bin = os.path.join(proj, ".engine", ".venv", "bin")
            os.makedirs(tools_dir)
            os.makedirs(venv_bin)
            shutil.copy(self.WRAPPER, os.path.join(tools_dir, "hook-runner.sh"))   # the real launcher
            interp = os.path.join(venv_bin, "python")                   # a stub that echoes each argv word
            with open(interp, "w") as fh:
                fh.write('#!/bin/sh\nfor a in "$@"; do printf \'%s\\n\' "$a"; done\n')
            os.chmod(interp, 0o755)
            open(os.path.join(tools_dir, "modes.py"), "w").close()      # a stub script so the path exists

            cmd = hooks.hook_command(".engine/tools/modes.py accept-hook", "posix")
            r = subprocess.run(["sh", "-c", cmd], capture_output=True, text=True, timeout=10,
                               env={**os.environ, "CLAUDE_PROJECT_DIR": proj,
                                    "ENGINE_HOOK_WAIT_POLLS": "3", "ENGINE_HOOK_WAIT_INTERVAL": "0.05"})
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(
                r.stdout.splitlines(),
                [os.path.join(proj, ".engine/tools/modes.py"), "accept-hook"],
                "the interpreter must receive the intact spaced script path as ONE arg, then the bare arg")

    def test_launcher_waits_then_execs_when_the_interpreter_appears_late(self):
        # the race, simulated deterministically against the REAL committed launcher: the interpreter is
        # created AFTER the launcher starts; it must wait, then exec it, forwarding the script + its arg.
        with tempfile.TemporaryDirectory() as td:
            interp = os.path.join(td, "python")
            script = os.path.join(td, "boot.py")

            def _provision_late():
                time.sleep(0.3)
                with open(interp, "w") as fh:               # write fully, THEN chmod +x — mirrors uv's
                    fh.write('#!/bin/sh\necho "STUB-RAN $@"\n')   # executable-on-create order
                os.chmod(interp, 0o755)

            t = threading.Thread(target=_provision_late)
            t.start()
            r = subprocess.run(["sh", self.WRAPPER, interp, script, "hook"],
                               capture_output=True, text=True, timeout=10)
            t.join()
            self.assertIn("STUB-RAN", r.stdout)             # the interpreter ran after the wait
            self.assertIn(script, r.stdout)                 # the script path passed through
            self.assertIn("hook", r.stdout)                 # the trailing arg passed through

    def test_launcher_runs_nothing_and_never_falls_back_when_interpreter_never_appears(self):
        with tempfile.TemporaryDirectory() as td:
            interp = os.path.join(td, "python")             # never created
            script = os.path.join(td, "boot.py")
            r = subprocess.run(["sh", self.WRAPPER, interp, script],
                               capture_output=True, text=True, timeout=10,
                               env={**os.environ, "ENGINE_HOOK_WAIT_POLLS": "3",
                                    "ENGINE_HOOK_WAIT_INTERVAL": "0.05"})       # ~0.15 s bound, fast
            self.assertEqual(r.stdout, "")                  # nothing ran — no system-Python fallback
            self.assertNotEqual(r.returncode, 0)            # neither venv layout exists → no exec → fail-open
            self.assertNotEqual(r.returncode, 2)            # and NEVER the platform's block code (#390 stranding)

    def test_launcher_fails_open_when_the_interpreter_is_present_but_not_executable(self):
        # a corrupt/partial venv: the named interpreter file EXISTS but is not runnable. The launcher must
        # still reach the plain-language fail-open readout (exit 1, non-blocking) — NOT exec the file and
        # surface a raw shell error (126). This pins the fix for the `-f`-guard regression the gate found:
        # the exec is gated on `-x`, so a non-executable interpreter waits out the bound and fails open.
        with tempfile.TemporaryDirectory() as td:
            interp = os.path.join(td, ".venv", "bin", "python")
            os.makedirs(os.path.dirname(interp))
            with open(interp, "w") as fh:                   # present as a regular file...
                fh.write("#!/bin/sh\necho SHOULD-NOT-RUN\n")
            os.chmod(interp, 0o644)                         # ...but NOT executable
            r = subprocess.run(["sh", self.WRAPPER, interp, os.path.join(td, "boot.py")],
                               capture_output=True, text=True, timeout=10,
                               env={**os.environ, "ENGINE_HOOK_WAIT_POLLS": "3",
                                    "ENGINE_HOOK_WAIT_INTERVAL": "0.05"})
            self.assertEqual(r.stdout, "")                  # did not exec the non-executable file
            self.assertNotEqual(r.returncode, 0)            # fail-open readout path
            self.assertNotEqual(r.returncode, 2)            # never the block code
            self.assertIn("not a block", r.stderr)          # the friendly readout, not a raw exec error

    def test_launcher_resolves_the_windows_sibling_when_the_posix_layout_is_absent(self):
        # the #407 fix, exercised on a POSIX host with a STUB at the Windows layout path: the committed
        # command always names the POSIX bin/python; when only the Windows Scripts/python.exe layout exists
        # (a Windows adopter, or a mixed-OS teammate on a repo whose committed command names the other OS),
        # the launcher resolves and runs THAT sibling under the same venv root — so one committed repo boots
        # on every OS. (The real .exe under Git Bash is unverifiable off Windows; the stub proves the
        # branch-selection + exec dispatch, which is the falsifiable part on a POSIX host.)
        with tempfile.TemporaryDirectory() as td:
            venv = os.path.join(td, ".venv")
            posix = os.path.join(venv, "bin", "python")          # NAMED in the command, but absent here
            win = os.path.join(venv, "Scripts", "python.exe")    # the only layout present on this "machine"
            os.makedirs(os.path.dirname(win))
            with open(win, "w") as fh:
                fh.write('#!/bin/sh\necho "WIN-STUB-RAN $@"\n')
            os.chmod(win, 0o755)                                 # executable so exec succeeds on the POSIX host
            script = os.path.join(td, "boot.py")
            r = subprocess.run(["sh", self.WRAPPER, posix, script, "hook"],
                               capture_output=True, text=True, timeout=10,
                               env={**os.environ, "ENGINE_HOOK_WAIT_POLLS": "3",
                                    "ENGINE_HOOK_WAIT_INTERVAL": "0.05"})
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("WIN-STUB-RAN", r.stdout)              # the Windows-layout interpreter ran
            self.assertIn(script, r.stdout)                      # the script path forwarded
            self.assertIn("hook", r.stdout)                      # the trailing arg forwarded

    def test_launcher_prefers_the_named_posix_interpreter_when_both_layouts_exist(self):
        # determinism: on the normal POSIX host the named bin/python is used and the Windows sibling is
        # never consulted, even if both happen to be present.
        with tempfile.TemporaryDirectory() as td:
            venv = os.path.join(td, ".venv")
            posix = os.path.join(venv, "bin", "python")
            win = os.path.join(venv, "Scripts", "python.exe")
            os.makedirs(os.path.dirname(posix))
            os.makedirs(os.path.dirname(win))
            for p, tag in ((posix, "POSIX"), (win, "WIN")):
                with open(p, "w") as fh:
                    fh.write(f'#!/bin/sh\necho "{tag}-RAN"\n')
                os.chmod(p, 0o755)
            r = subprocess.run(["sh", self.WRAPPER, posix, os.path.join(td, "boot.py")],
                               capture_output=True, text=True, timeout=10)
            self.assertIn("POSIX-RAN", r.stdout)
            self.assertNotIn("WIN-RAN", r.stdout)

    def test_launcher_os_literals_match_the_resolver_single_source(self):
        # F2 / single-source-of-truth: the per-OS layout fact (bin/python vs Scripts/python.exe) is DEFINED
        # once in hooks.interpreter_path; the launcher necessarily restates the two subpaths in shell. Pin
        # them to the resolver's forms — checking the EXECUTABLE lines only (the comments carry the literals
        # too, so a whole-file match would not catch a mangled code line) — so the two homes cannot diverge.
        with open(self.WRAPPER) as fh:
            code = "\n".join(ln for ln in fh.read().splitlines() if not ln.lstrip().startswith("#"))
        self.assertTrue(hooks.interpreter_path("posix").endswith("/bin/python"))
        self.assertTrue(hooks.interpreter_path("nt").endswith("/Scripts/python.exe"))
        self.assertIn("/bin/python", code)                       # the POSIX layout, in the executable body
        self.assertIn("/Scripts/python.exe", code)               # the Windows layout, in the executable body

    def test_launcher_drops_the_runtime_marker_when_no_interpreter_appears(self):
        # #412: a missing tool-runtime cannot reach Python, so the launcher leaves a PRESENCE marker for the
        # drain-inbox driver to promote next session. It is best-effort, EMPTY, and never changes the exit code.
        with tempfile.TemporaryDirectory() as td:
            interp = os.path.join(td, ".engine", ".venv", "bin", "python")   # named, never created
            os.makedirs(os.path.dirname(interp))
            r = subprocess.run(["sh", self.WRAPPER, interp, os.path.join(td, ".engine", "tools", "boot.py")],
                               capture_output=True, text=True, timeout=10,
                               env={**os.environ, "ENGINE_HOOK_WAIT_POLLS": "3", "ENGINE_HOOK_WAIT_INTERVAL": "0.05"})
            self.assertNotEqual(r.returncode, 0)                 # fail-open readout path
            self.assertNotEqual(r.returncode, 2)                 # never the block code (#390 stranding)
            marker = os.path.join(td, ".engine", "telemetry", ".cache", "runtime-health.marker")
            self.assertTrue(os.path.exists(marker), "the launcher drops the runtime-health marker")
            self.assertEqual(os.path.getsize(marker), 0)         # presence-only — no bytes can enter the finding

    def test_launcher_marker_path_matches_the_python_constant(self):
        # #412 single-source: the shell builds the marker path from the venv root; pin its two components to
        # telemetry.RUNTIME_HEALTH_MARKER_PATH so the shell↔Python contract cannot drift.
        import telemetry
        engine = os.path.join(validate.ROOT, ".engine")
        dir_tail = os.path.relpath(os.path.dirname(telemetry.RUNTIME_HEALTH_MARKER_PATH), engine)  # telemetry/.cache
        base = os.path.basename(telemetry.RUNTIME_HEALTH_MARKER_PATH)                               # runtime-health.marker
        with open(self.WRAPPER) as fh:
            code = "\n".join(ln for ln in fh.read().splitlines() if not ln.lstrip().startswith("#"))
        self.assertIn(dir_tail, code)
        self.assertIn(base, code)


class TestHookCommandMatchesWiredLiterals(unittest.TestCase):
    """The wired hook commands ARE `hook_command`'s output, so the form and the literals can never drift:
    a command-form change must update `hooks.py`, the core manifest, AND `.claude/settings.json` in
    lockstep, or this reds (the architect-A1 / adversarial-S1 drift guard for issue #83)."""

    # every engine hook wire's script-relpath-with-args. Core wires boot on three SessionStart matchers;
    # the per-prompt scent on UserPromptSubmit; the commit-boundary regen for the knowledge
    # graph AND the self-map (the #136 self-map/graph-asymmetry close) on PreToolUse; memory-substrate
    # wires a PreCompact hook (the compaction trigger — the one thing that physically carries out an erasure
    # the operator merged), plus the cross-session erasure OBSERVER and the backup-vault push, each on three
    # SessionStart matchers. telemetry's ambient triage runs on two SessionStart matchers (startup + resume),
    # the same command, so it adds ONE entry to the SET while adding TWO to the registration COUNT (like
    # github-projects-sync).
    CORE_RELPATHS = (".engine/tools/boot.py", ".engine/tools/modes.py", ".engine/tools/knowledge_gen.py hook",
                     ".engine/tools/self_map.py hook", ".engine/tools/validate.py hook",
                     ".engine/tools/modes.py accept-hook", ".engine/tools/validate.py accept-hook",
                     ".engine/tools/close.py", ".engine/tools/scent.py",
                     ".engine/tools/telemetry.py run-ambient",
                     ".engine/tools/telemetry.py drain-inbox")
    MEMORY_RELPATHS = (".engine/tools/memory/compact.py pre-compact",
                       ".engine/tools/memory/erasure_observer.py session-start",
                       ".engine/tools/memory/backup_vault.py session-start")
    # github-projects-sync (optional) wires its board refresh on two SessionStart matchers (startup + resume),
    # the same command, so the SET has one entry while the registration COUNT is two.
    PROJECTS_SYNC_RELPATHS = (".engine/tools/projects_sync/projects_sync.py session-start",)
    # product-design (optional) wires ONE PreToolUse regen hook: its obligation-matrix commit-boundary refresh
    # (mirrors core's graph/self-map regen hooks) — product-design's first and only hook wire.
    PRODUCT_DESIGN_RELPATHS = (".engine/tools/product_design/obligation_matrix.py hook",)

    def _venv_hook_commands(self, commands):
        return [c for c in commands if ".venv/bin/python" in c]

    def _hook_cmds(self, manifest):
        return self._venv_hook_commands(
            w.get("hook", {}).get("command", "") for w in manifest["wires"] if w.get("type") == "hook")

    def test_manifest_and_settings_hook_commands_are_hook_command_output(self):
        expected_core = {hooks.hook_command(r, "posix") for r in self.CORE_RELPATHS}
        expected_memory = {hooks.hook_command(r, "posix") for r in self.MEMORY_RELPATHS}
        expected_projects = {hooks.hook_command(r, "posix") for r in self.PROJECTS_SYNC_RELPATHS}
        expected_product_design = {hooks.hook_command(r, "posix") for r in self.PRODUCT_DESIGN_RELPATHS}

        core = validate.load_json(os.path.join(validate.ROOT, ".engine/modules/core/manifest.json"))
        c_cmds = self._hook_cmds(core)
        self.assertEqual(len(c_cmds), 15, "the fifteen venv-rooted core hook wires (boot ×3 + 8: modes, "
                         "knowledge_gen, self_map, validate pre-commit, modes accept, validate accept, close, "
                         "scent + telemetry run-ambient ×2 + telemetry drain-inbox ×2: startup + resume)")
        self.assertEqual(set(c_cmds), expected_core, "every core manifest hook command is hook_command's output")

        memory = validate.load_json(
            os.path.join(validate.ROOT, ".engine/modules/memory-substrate-sqlite-fts5/manifest.json"))
        m_cmds = self._hook_cmds(memory)
        self.assertEqual(len(m_cmds), 7, "memory's one PreCompact compaction trigger + three erasure-observer "
                                         "SessionStart sweeps + three backup-vault SessionStart pushes")
        self.assertEqual(set(m_cmds), expected_memory, "every memory manifest hook command is hook_command's output")

        # Both modules below are OPTIONAL — declined at setup or removed later, their manifests are simply
        # gone. Reading one unconditionally raises a bare FileNotFoundError in a deployment that made a
        # supported choice, which reds its required self-tests over an add-on it never wanted.
        projects_path = os.path.join(validate.ROOT, ".engine/modules/github-projects-sync/manifest.json")
        pd_path = os.path.join(validate.ROOT, ".engine/modules/product-design/manifest.json")
        if not (os.path.exists(projects_path) and os.path.exists(pd_path)):
            self.skipTest("board-sync and/or product-design are not installed in this repository")
        projects = validate.load_json(projects_path)
        p_cmds = self._hook_cmds(projects)
        self.assertEqual(len(p_cmds), 2, "the board refresh on two SessionStart matchers (startup + resume)")
        self.assertEqual(set(p_cmds), expected_projects, "every board-sync manifest hook command is hook_command's output")

        product_design = validate.load_json(pd_path)
        pd_cmds = self._hook_cmds(product_design)
        self.assertEqual(len(pd_cmds), 1, "product-design's one obligation-matrix commit-boundary regen hook")
        self.assertEqual(set(pd_cmds), expected_product_design,
                         "product-design's manifest hook command is hook_command's output")

        # settings.json registers all installed modules' hooks: 15 core + 7 memory + 2 board-sync + 1 product-design venv-rooted.
        settings = validate.load_json(os.path.join(validate.ROOT, ".claude", "settings.json"))
        s_cmds = self._venv_hook_commands(
            h.get("command", "") for groups in settings["hooks"].values()
            for grp in groups for h in grp.get("hooks", []))
        self.assertEqual(len(s_cmds), 25,
                         "the twenty-five venv-rooted hook commands in settings "
                         "(15 core + 7 memory + 2 board-sync + 1 product-design)")
        self.assertEqual(set(s_cmds), expected_core | expected_memory | expected_projects | expected_product_design,
                         "settings matches the form (and so all four manifests) exactly")


class TestHarnessBlock(unittest.TestCase):
    def test_block_on_pretooluse_exits_two_with_reason_on_stderr(self):
        code, out, err = _run("PreToolUse", lambda p: hooks.block("finish first"))
        self.assertEqual(code, hooks.EXIT_BLOCK)
        self.assertEqual(code, 2)
        self.assertIn("finish first", err)
        self.assertEqual(out, "")

    def test_block_on_stop_exits_two(self):
        code, _out, _err = _run("Stop", lambda p: hooks.block("not done"))
        self.assertEqual(code, 2)

    def test_block_on_non_eligible_event_fails_open_and_flags(self):
        code, _out, err = _run("PostToolUse", lambda p: hooks.block("I cannot block here"))
        self.assertNotEqual(code, hooks.EXIT_BLOCK)
        self.assertEqual(code, hooks.EXIT_NONBLOCKING)
        self.assertIn("only", err)
        self.assertIn("PostToolUse", err)

    def test_block_on_every_non_eligible_event_fails_open(self):
        """The runtime gate (not just the static leg) must refuse a block on EVERY non-eligible event,
        so a _translate bug on an event other than PostToolUse cannot fail-closed."""
        for ev in sorted(hooks.EVENTS - hooks.BLOCK_ELIGIBLE_EVENTS):
            code, _out, _err = _run(ev, lambda p: hooks.block("should not block"))
            self.assertNotEqual(code, hooks.EXIT_BLOCK, f"{ev} must not honor a block")
            self.assertEqual(code, hooks.EXIT_NONBLOCKING, f"{ev} should fail open")


class TestHarnessFailOpen(unittest.TestCase):
    def test_a_crashing_handler_proceeds_and_flags_never_blocks(self):
        def boom(_payload):
            raise RuntimeError("kaboom")
        code, _out, err = _run("PreToolUse", boom)
        self.assertNotEqual(code, hooks.EXIT_BLOCK)
        self.assertEqual(code, hooks.EXIT_NONBLOCKING)
        self.assertTrue(err.strip(), "a fail-open crash must emit a plain-language finding")
        self.assertNotIn("Traceback", err)

    def test_fail_open_notice_overrides_the_operator_crash_line(self):
        # U08c (#412): a gate can supply its OWN operator-facing crash sentence (close passes the spec's
        # "I couldn't run the check that confirms nothing was dropped"). fail_open_notice replaces ONLY the
        # operator message; the promote path + non-blocking exit are unchanged.
        seen = {}
        def promote(event, kind, message):
            seen["message"] = message
            return True
        def boom(_payload):
            raise RuntimeError("kaboom")
        out, err = io.StringIO(), io.StringIO()
        code = hooks.run_hook("Stop", boom, stdin=io.StringIO("{}"), stdout=out, stderr=err,
                              promote=promote, fail_open_notice="MY-OWN-CRASH-LINE")
        self.assertEqual(code, hooks.EXIT_NONBLOCKING)
        self.assertIn("MY-OWN-CRASH-LINE", err.getvalue())                 # operator hears the gate's own line
        self.assertNotIn("a safety check on the stop step", err.getvalue().lower())  # not the generic wording
        self.assertIn("MY-OWN-CRASH-LINE", seen["message"])               # and it rides the promoted Issue too

    def test_without_a_fail_open_notice_the_generic_crash_line_is_used(self):
        def boom(_payload):
            raise RuntimeError("kaboom")
        code, _out, err = _run("PreToolUse", boom)
        self.assertEqual(code, hooks.EXIT_NONBLOCKING)
        self.assertIn("could not run", err.lower())                       # the generic fallback still applies

    def test_a_crash_records_a_locator_to_the_engine_only_file_not_the_operator_channel(self):
        # A fail-open crash records only the exception TYPE on the operator-facing surfaces (the plain
        # stderr finding the platform shows on exit 1, and the promoted Issue). The exception MESSAGE + a
        # file:line locator go ONLY to a gitignored engine-only FILE — never stderr, never the Issue — so a
        # transient crash is diagnosable WITHOUT putting backstage detail in front of a non-engineer.
        recorded = {}
        def promote(event, kind, message):
            recorded["message"] = message
            return True
        def boom(_payload):
            raise NameError("name 'wibble' is not defined")
        with tempfile.TemporaryDirectory() as d:
            logpath = os.path.join(d, ".cache", "hook-crash-debug.log")
            # point the recorder at a temp path (its `path` arg) so the test never writes the real cache
            real = hooks._record_crash_debug
            try:
                hooks._record_crash_debug = lambda ev, ex: real(ev, ex, logpath)
                out, err = io.StringIO(), io.StringIO()
                code = hooks.run_hook("PreToolUse", boom, stdin=io.StringIO("{}"),
                                      stdout=out, stderr=err, promote=promote)
            finally:
                hooks._record_crash_debug = real
            errtext = err.getvalue()
            with open(logpath, encoding="utf-8") as fh:
                filetext = fh.read()
        self.assertEqual(code, hooks.EXIT_NONBLOCKING)
        # the engine-only FILE carries the exception message AND a file:line locator
        self.assertIn("name 'wibble' is not defined", filetext)
        self.assertRegex(filetext, r"@ \S+\.py:\d+")
        # stderr (operator-visible on the non-blocking exit) stays plain: no raw message, no locator
        self.assertNotIn("wibble", errtext)
        self.assertNotRegex(errtext, r"\.py:\d+")
        self.assertIn("NameError", errtext)                            # the plain finding still names the type
        # the promoted (operator Issue) message names only the type
        self.assertIn("NameError", recorded["message"])
        self.assertNotIn("wibble", recorded["message"])
        self.assertNotRegex(recorded["message"], r"\.py:\d+")

    def test_no_handler_proceeds(self):
        code, out, err = _run("Stop", None)
        self.assertEqual(code, hooks.EXIT_PROCEED)
        self.assertEqual(out, "")
        self.assertEqual(err, "")

    def test_handler_calling_sys_exit_fails_open_never_fails_closed(self):
        """A handler that reaches past the decision protocol and calls sys.exit(2) must STILL fail
        open — the harness owns the exit code, so a handler bug can never fail-closed."""
        def rogue(_payload):
            sys.exit(2)
        for ev in ("PostToolUse", "PreToolUse", "Stop"):
            code, _out, err = _run(ev, rogue)
            self.assertNotEqual(code, hooks.EXIT_BLOCK, f"{ev}: sys.exit(2) must not become a block")
            self.assertEqual(code, hooks.EXIT_NONBLOCKING, f"{ev}: should fail open")
            self.assertTrue(err.strip())


class TestStopHookActive(unittest.TestCase):
    def test_forced_continuation_runs_handler_but_never_reblocks(self):
        # A deliberate law change from the earlier skip-the-handler behaviour: on a forced continuation the
        # handler STILL runs — close uses the give-up moment to log a still-undispositioned finding — but
        # its block is downgraded to proceed in run_hook, so the no-re-block / no-loop guarantee holds by
        # construction (the harness owns it, not the handler).
        called = []

        def would_block(_payload):
            called.append(True)
            return hooks.block("disposition still open")
        code, _out, err = _run("Stop", would_block, payload={"stop_hook_active": True})
        self.assertEqual(code, hooks.EXIT_PROCEED)   # downgraded to proceed — never re-blocks
        self.assertEqual(called, [True])             # ...but the handler DID run (its side effects fire)
        self.assertEqual(err, "")                    # and no block reason was emitted

    def test_forced_continuation_proceed_passes_through(self):
        code, _out, _err = _run("Stop", lambda p: hooks.proceed(), payload={"stop_hook_active": True})
        self.assertEqual(code, hooks.EXIT_PROCEED)

    def test_forced_continuation_handler_crash_fails_open(self):
        # The give-up handler itself crashing must fail open (the turn ends) and flag — never block.
        def boom(_payload):
            raise RuntimeError("give-up handler crashed")
        code, _out, err = _run("Stop", boom, payload={"stop_hook_active": True})
        self.assertEqual(code, hooks.EXIT_NONBLOCKING)   # non-blocking → the turn ends, never strands
        self.assertIn("could not run", err)              # ...and the failure is surfaced as a finding

    def test_normal_stop_still_blocks(self):
        code, _out, _err = _run("Stop", lambda p: hooks.block("nope"),
                                payload={"stop_hook_active": False})
        self.assertEqual(code, 2)


class TestMalformedAndEmptyPayload(unittest.TestCase):
    def test_malformed_stdin_fails_open(self):
        code, _out, err = _run("PreToolUse", lambda p: hooks.block("x"), stdin_text="{not json")
        self.assertNotEqual(code, hooks.EXIT_BLOCK)
        self.assertEqual(code, hooks.EXIT_NONBLOCKING)
        self.assertTrue(err.strip())

    def test_empty_stdin_is_an_empty_payload(self):
        code, _out, _err = _run("PreToolUse", lambda p: hooks.proceed(), stdin_text="")
        self.assertEqual(code, hooks.EXIT_PROCEED)

    def test_non_object_json_is_treated_as_empty(self):
        seen = {}

        def handler(payload):
            seen["payload"] = payload
            return hooks.proceed()
        code, _out, _err = _run("PreToolUse", handler, stdin_text="[1, 2, 3]")
        self.assertEqual(code, hooks.EXIT_PROCEED)
        self.assertEqual(seen["payload"], {})


class TestInjectAndDecide(unittest.TestCase):
    def test_inject_emits_additional_context(self):
        code, out, _err = _run("SessionStart", lambda p: hooks.inject("orientation pack"))
        self.assertEqual(code, hooks.EXIT_PROCEED)
        payload = json.loads(out)
        self.assertEqual(payload["hookSpecificOutput"]["hookEventName"], "SessionStart")
        self.assertEqual(payload["hookSpecificOutput"]["additionalContext"], "orientation pack")

    def test_pretooluse_permission_decision(self):
        code, out, _err = _run("PreToolUse", lambda p: hooks.decide("deny", "blocked by gate"))
        self.assertEqual(code, hooks.EXIT_PROCEED)
        hso = json.loads(out)["hookSpecificOutput"]
        self.assertEqual(hso["permissionDecision"], "deny")
        self.assertEqual(hso["permissionDecisionReason"], "blocked by gate")

    def test_permission_decision_without_reason_omits_the_reason_key(self):
        code, out, _err = _run("PreToolUse", lambda p: hooks.decide("allow"))
        self.assertEqual(code, hooks.EXIT_PROCEED)
        hso = json.loads(out)["hookSpecificOutput"]
        self.assertEqual(hso["permissionDecision"], "allow")
        self.assertNotIn("permissionDecisionReason", hso)

    def test_permission_decision_on_non_pretooluse_is_flagged(self):
        code, _out, err = _run("Stop", lambda p: hooks.decide("deny"))
        self.assertEqual(code, hooks.EXIT_NONBLOCKING)
        self.assertTrue(err.strip())

    def test_invalid_permission_value_is_flagged(self):
        code, _out, err = _run("PreToolUse", lambda p: hooks.decide("maybe"))
        self.assertEqual(code, hooks.EXIT_NONBLOCKING)
        self.assertTrue(err.strip())

    def test_proceed_is_silent(self):
        code, out, err = _run("PostToolUse", lambda p: hooks.proceed())
        self.assertEqual(code, hooks.EXIT_PROCEED)
        self.assertEqual(out, "")
        self.assertEqual(err, "")


class TestBlockBudgetFindings(unittest.TestCase):
    MSG = "Register the block with its owning system on an eligible event."
    STANCES = modes.STANCES  # the canonical vocabulary the mode-dimension rule validates against

    def test_empty_set_is_silent(self):
        self.assertEqual(validate.block_budget_findings([], "hard", self.MSG, stances=self.STANCES), [])

    def test_eligible_events_with_declared_modes_pass(self):
        blocks = [{"event": "Stop", "name": "findings-disposition", "owner": "close",
                   "modes": ["explore", "build", "routine"]},
                  {"event": "PreToolUse", "name": "explore-write-gate", "owner": "modes",
                   "modes": ["explore"]}]
        self.assertEqual(validate.block_budget_findings(blocks, "hard", self.MSG, stances=self.STANCES), [])

    def test_non_eligible_event_is_flagged(self):
        # A declared `modes` isolates the event rule so only it fires (len 1).
        found = validate.block_budget_findings(
            [{"event": "PostToolUse", "name": "bad", "modes": ["explore"]}], "hard", self.MSG,
            stances=self.STANCES)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["severity"], "hard")
        self.assertIn("PostToolUse", found[0]["message"])
        self.assertIn(self.MSG, found[0]["message"])

    def test_owner_is_used_when_name_absent(self):
        found = validate.block_budget_findings(
            [{"event": "PreCompact", "owner": "memory", "modes": ["explore"]}], "soft", self.MSG,
            stances=self.STANCES)
        self.assertEqual(len(found), 1)
        self.assertIn("memory", found[0]["message"])
        self.assertEqual(found[0]["severity"], "soft")

    def test_missing_modes_is_flagged(self):
        # The mode dimension is declared data: a block that names no stances it is active in fires.
        found = validate.block_budget_findings(
            [{"event": "PreToolUse", "name": "no-modes", "owner": "modes"}], "hard", self.MSG,
            stances=self.STANCES)
        self.assertEqual(len(found), 1)
        self.assertIn("does not declare the modes it is active in", found[0]["message"])

    def test_empty_modes_is_flagged(self):
        found = validate.block_budget_findings(
            [{"event": "Stop", "name": "empty", "owner": "close", "modes": []}], "hard", self.MSG,
            stances=self.STANCES)
        self.assertEqual(len(found), 1)
        self.assertIn("does not declare the modes", found[0]["message"])

    def test_unknown_mode_is_flagged(self):
        found = validate.block_budget_findings(
            [{"event": "Stop", "name": "typo", "owner": "close", "modes": ["explor"]}], "hard", self.MSG,
            stances=self.STANCES)
        self.assertEqual(len(found), 1)
        self.assertIn("unknown mode", found[0]["message"])
        self.assertIn("explor", found[0]["message"])

    def test_agrees_with_runtime_block_eligible_events(self):
        """Drift guard: for every governed event, the static leg flags a block on it iff the event is
        outside the runtime's BLOCK_ELIGIBLE_EVENTS constant — the leg's own {PreToolUse, Stop} literal
        and the harness's eligibility constant cannot drift. A declared `modes` isolates the event rule.
        (The runtime _translate gate itself is exercised in
        TestHarnessBlock.test_block_on_every_non_eligible_event_fails_open.)"""
        for ev in hooks.EVENTS:
            findings = validate.block_budget_findings(
                [{"event": ev, "name": ev, "modes": ["explore"]}], "hard", "", stances=self.STANCES)
            flagged = bool(findings)
            self.assertEqual(flagged, ev not in hooks.BLOCK_ELIGIBLE_EVENTS, f"{ev} agreement")


class TestDemoRuns(unittest.TestCase):
    def test_demo_executes_cleanly(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = hooks.main(["demo"])
        self.assertEqual(code, 0)
        self.assertIn("fail-open", buf.getvalue())


class TestFailOpenPromotion(unittest.TestCase):
    """#391: a fail-open finding is PROMOTED to a tracked engine Issue (best-effort, fail-safe), and the
    in-session copy is HONEST about whether that landed — the old unconditional 'this was recorded as a
    problem to fix' (which recorded nothing) is gone."""

    @staticmethod
    def _crash(_payload):
        raise RuntimeError("boom")

    def _run(self, promote):
        out, err = io.StringIO(), io.StringIO()
        code = hooks.run_hook("PreToolUse", self._crash, stdin=io.StringIO("{}"),
                              stdout=out, stderr=err, promote=promote)
        return code, err.getvalue()

    def test_promoted_finding_says_recorded(self):
        code, err = self._run(promote=lambda *a: 4242)      # a landed promotion returns the Issue number
        self.assertEqual(code, hooks.EXIT_NONBLOCKING)
        self.assertIn("recorded as a tracked item", err)
        self.assertNotIn("not durably", err)

    def test_offline_finding_says_not_recorded_and_never_lies(self):
        code, err = self._run(promote=lambda *a: False)     # offline / unreachable -> not durably tracked
        self.assertEqual(code, hooks.EXIT_NONBLOCKING)
        self.assertIn("not durably", err)
        self.assertNotIn("recorded as a tracked item", err)
        self.assertNotIn("recorded as a problem to fix", err)   # the old false copy is gone entirely

    def test_a_promoter_that_itself_throws_never_fails_the_gate_closed(self):
        # belt-and-suspenders: recording the crash must NEVER re-break the fail-open path into a block/crash.
        def boom_promote(*_a):
            raise OSError("disk full")
        code, err = self._run(promote=boom_promote)
        self.assertEqual(code, hooks.EXIT_NONBLOCKING)      # still non-blocking, never exit 2
        self.assertIn("not durably", err)                   # degrades to surfaced-not-recorded

    def test_source_id_is_coarse_and_marker_safe(self):
        a = hooks._fail_open_source_id("PreToolUse", "crash")
        self.assertEqual(a, hooks._fail_open_source_id("PreToolUse", "crash"))   # recurrences -> ONE Issue
        self.assertEqual(a, "hooks/fail-open/PreToolUse/crash")
        self.assertNotEqual(a, hooks._fail_open_source_id("Stop", "crash"))
        for bad in ("<!--", "-->", "\n"):                   # cannot forge telemetry's dedup marker
            self.assertNotIn(bad, a)

    def test_real_promoter_refuses_live_github_under_a_test_harness(self):
        # the SAFETY BACKSTOP: the real promoter must never open a live Issue from a test run.
        self.assertIn("unittest", sys.modules)
        self.assertFalse(hooks._promote_fail_open("PreToolUse", "crash", "msg"))

    def test_do_promote_degrades_to_false_when_emit_does(self):
        # After the un-inversion the hook no longer resolves the GitHub boundary — telemetry.emit_finding
        # does (and owns the no-token/offline -> False behaviour, covered in test_telemetry). Here the hook's
        # job is only to relay emit_finding's verdict: a False emit -> a False promote (surfaced-not-recorded).
        import telemetry
        with mock.patch.object(telemetry, "emit_finding", return_value=False):
            self.assertFalse(hooks._do_promote_fail_open("Stop", "crash", "m"))

    def test_do_promote_emits_a_trust_critical_sourced_record(self):
        # The hook builds the coarse, marker-safe, trust-critical record and hands it to the emit-and-done
        # seam; it NO LONGER holds telemetry's GitHub boundary (that reach-in was the inverted seam that was fixed).
        import telemetry
        captured = {}

        def fake_emit(record, *, gh=None):
            captured["record"] = record
            captured["gh"] = gh
            return 77
        with mock.patch.object(telemetry, "emit_finding", side_effect=fake_emit):
            got = hooks._do_promote_fail_open("PreToolUse", "input", "could not read input")
        self.assertTrue(got)                                # a landed promotion -> truthy
        self.assertIsNone(captured["gh"])                   # telemetry resolves the boundary, not the hook
        self.assertEqual(captured["record"]["source_id"], "hooks/fail-open/PreToolUse/input")
        self.assertEqual(captured["record"]["severity"], telemetry.TRUST_CRITICAL)
        self.assertEqual(captured["record"]["message"], "could not read input")


class TestMissingRuntimeReadout(unittest.TestCase):
    """#391: when the venv interpreter never appears, the launcher NAMES the absent runtime on stderr and
    stays NON-blocking, instead of exiting silently (the missing-runtime variant of fail-open-and-flag)."""

    WRAPPER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hook-runner.sh")

    def test_names_the_absent_runtime_and_stays_non_blocking(self):
        with tempfile.TemporaryDirectory() as td:
            interp = os.path.join(td, "python")             # never created -> never appears
            r = subprocess.run(["sh", self.WRAPPER, interp, os.path.join(td, "boot.py")],
                               capture_output=True, text=True, timeout=10,
                               env={**os.environ, "ENGINE_HOOK_WAIT_POLLS": "3",
                                    "ENGINE_HOOK_WAIT_INTERVAL": "0.05"})
            self.assertEqual(r.stdout, "")                  # still ran nothing (no system-Python fallback)
            self.assertNotEqual(r.returncode, 2)            # NON-blocking (never the platform's block code)
            self.assertNotEqual(r.returncode, 0)            # and did not silently succeed
            self.assertIn("private Python runtime is not ready", r.stderr)   # names the absent runtime
            self.assertIn(interp, r.stderr)                 # the concrete path, for a literate operator
            self.assertIn("not a block", r.stderr)          # tells the operator it did not block


class TestIsGitCommit(unittest.TestCase):
    """The shared `git commit` classifier the commit-boundary hooks agree on (factored out of the
    per-consumer copies in self_map / knowledge_gen). Command-start anchored; degrades safe."""

    def test_true_on_commit_amend_and_compound(self):
        for cmd in ("git commit -m 'x'", "git commit --amend", "git add -A && git commit -m y",
                    "git status\ngit commit -m z"):
            p = {"tool_name": "Bash", "tool_input": {"command": cmd}}
            self.assertTrue(hooks._is_git_commit(p), cmd)

    def test_false_on_non_commit_non_bash_and_malformed(self):
        self.assertFalse(hooks._is_git_commit(
            {"tool_name": "Bash", "tool_input": {"command": "git status"}}))
        self.assertFalse(hooks._is_git_commit(
            {"tool_name": "Bash", "tool_input": {"command": "git log --oneline"}}))
        # a non-Bash tool never fires, even if its input text contains the words
        self.assertFalse(hooks._is_git_commit(
            {"tool_name": "Read", "tool_input": {"file_path": "git commit"}}))
        # an echoed / quoted occurrence is not at a command-start position -> no fire
        self.assertFalse(hooks._is_git_commit(
            {"tool_name": "Bash", "tool_input": {"command": "echo 'git commit'"}}))
        self.assertFalse(hooks._is_git_commit({"tool_name": "Bash"}))   # no tool_input
        self.assertFalse(hooks._is_git_commit(None))                    # malformed
        self.assertFalse(hooks._is_git_commit({}))
        # a non-string command degrades safe (no TypeError, no spurious match)
        for bad in (["git", "commit"], 123, {"x": 1}, None):
            self.assertFalse(hooks._is_git_commit(
                {"tool_name": "Bash", "tool_input": {"command": bad}}), repr(bad))


class TestCapShed(unittest.TestCase):
    """The #495 measure-and-shed contract, at the helper's own seam: order preserved, pinned never shed,
    notice counted — and content always beats the label (a class is never shed to fit the notice)."""

    def _blocks(self, p0="G" * 100, p1="D" * 100, p2="O" * 100):
        return [(0, "governance", p0), (2, "orientation", p2), (1, "dashboard", p1)]

    def test_wide_cap_keeps_all_in_original_order(self):
        text, shed = hooks.cap_shed(self._blocks("AAA", "BBB", "CCC"), cap=1000,
                                    notice=lambda names: "N:" + ",".join(names))
        self.assertEqual(text, "AAA\nCCC\nBBB")     # original order, priorities never reorder
        self.assertEqual(shed, [])

    def test_sheds_highest_priority_first_with_counted_notice(self):
        notice = lambda names: "shed:" + ",".join(names)
        text, shed = hooks.cap_shed(self._blocks(), cap=250, notice=notice)
        self.assertEqual(shed, ["orientation"])
        self.assertIn("shed:orientation", text)
        self.assertLessEqual(len(text), 250)

    def test_content_beats_the_label_notice_shrinks_not_another_class(self):
        # Governance+dashboard fit; the FULL notice would tip it over — the compact one must be used
        # and the dashboard KEPT (the converged #495 review finding: never shed 4.9k for a 390-char note).
        big_notice = lambda names: "X" * 60
        compact = lambda names: "c"
        text, shed = hooks.cap_shed(self._blocks(), cap=210, notice=big_notice, compact_notice=compact)
        self.assertEqual(shed, ["orientation"])
        self.assertIn("D" * 100, text)               # dashboard kept
        self.assertIn("c", text)
        self.assertLessEqual(len(text), 210)

    def test_notice_drops_entirely_before_content_does(self):
        big_notice = lambda names: "X" * 60
        compact = lambda names: "y" * 50
        text, shed = hooks.cap_shed(self._blocks(), cap=201, notice=big_notice, compact_notice=compact)
        self.assertEqual(shed, ["orientation"])
        self.assertIn("D" * 100, text)               # dashboard still kept
        self.assertNotIn("X", text)
        self.assertNotIn("y", text)                  # even the compact notice gave way to content
        self.assertLessEqual(len(text), 201)

    def test_pinned_alone_oversize_is_emitted_whole(self):
        text, shed = hooks.cap_shed([(0, "governance", "G" * 500)], cap=100)
        self.assertEqual(text, "G" * 500)            # never truncated; documented tradeoff
        self.assertEqual(shed, [])


class TestCrashDebugHermeticGuard(unittest.TestCase):
    """#495's rider: under the test harness, _record_crash_debug with the DEFAULT path writes nothing
    (production crash-log stays clean); an explicit path (the unit tests' own temp file) still writes."""

    def test_default_path_is_a_noop_under_unittest(self):
        with tempfile.TemporaryDirectory() as d:
            target = os.path.join(d, "crash.log")
            class _T:                                 # stands in for the lazy telemetry import's constant
                HOOK_CRASH_DEBUG_PATH = target
            with mock.patch.dict(sys.modules, {"telemetry": _T}):
                hooks._record_crash_debug("PreToolUse", RuntimeError("boom"))
            self.assertFalse(os.path.exists(target))

    def test_explicit_path_still_writes(self):
        with tempfile.TemporaryDirectory() as d:
            target = os.path.join(d, "crash.log")
            hooks._record_crash_debug("PreToolUse", RuntimeError("boom"), path=target)
            with open(target, encoding="utf-8") as fh:
                self.assertIn("RuntimeError: boom", fh.read())


if __name__ == "__main__":
    unittest.main()
