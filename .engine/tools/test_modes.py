#!/usr/bin/env python3
"""Tests for modes, the operating stance + the Explore write-gate.

These lock the load-bearing behaviours a non-engineer cannot read code to verify: that the gate DENIES
each building action class in Explore with a plain sentence and ALLOWS everything else (reads, tests,
greps, subagent spawns, gh issue); that an unclassifiable action resolves to ALLOW (no default-deny);
that Build/Routine permit every write; that the stance signal boots Explore (absent / stale / unreadable
→ explore) and that clear_stance deletes it; that the gate fails open end-to-end (run_hook can never
return the blocking exit) and the deny rides the structured permissionDecision channel (exit 0 + the
hookSpecificOutput wrapper the platform honors), NEVER exit-2 block(); and that modes declares its block
on a block-eligible event.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from unittest import mock

import hooks
import modes
import quiet_call  # capture a demo/CLI walkthrough's stdout so it can't bury the suite summary
import validate


def _deny(decision: dict) -> bool:
    return (decision.get("action") == "decide"
            and decision.get("permissionDecision") == "deny"
            and bool(decision.get("reason")))


def _allow(decision: dict) -> bool:
    return decision.get("action") == "proceed"


def _explore_payload(tool_name: str, command: str = "") -> dict:
    # session_id=None -> current_stance is Explore (the safe floor), with no signal file needed.
    return {"session_id": None, "tool_name": tool_name,
            "tool_input": {"command": command} if command else {}}


class TestExploreGateDenies(unittest.TestCase):
    """In Explore the gate denies the enumerated building set, each with a plain reason."""

    def test_file_mutating_tools_are_denied(self):
        for tool in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
            d = modes.handler(_explore_payload(tool))
            self.assertTrue(_deny(d), f"{tool} must be denied in Explore")
            self.assertIn("exploring", d["reason"].lower())

    def test_bash_building_verbs_are_denied(self):
        for cmd in ("git commit -m wip", "git commit", "git checkout -b feat", "git switch -c feat",
                    "git branch newbranch", "gh pr create --fill",
                    # at command position after a shell separator (chaining), still denied:
                    "cd /tmp && git commit -m x", "git add . && git commit -m x", "true; gh pr create"):
            d = modes.handler(_explore_payload("Bash", cmd))
            self.assertTrue(_deny(d), f"Bash {cmd!r} must be denied in Explore")

    def test_github_mcp_pr_creation_is_denied(self):
        for tool in ("mcp__github__create_pull_request", "mcp__gh_server__create_pr"):
            d = modes.handler(_explore_payload(tool))
            self.assertTrue(_deny(d), f"{tool} must be denied in Explore")

    def test_deny_names_the_way_forward(self):
        d = modes.handler(_explore_payload("Edit"))
        self.assertIn("build", d["reason"].lower())          # tells the operator how to proceed


class TestExploreGateAllows(unittest.TestCase):
    """In Explore the gate allows everything else — exploring stays the comfortable place to work."""

    def test_reads_and_readonly_bash_are_allowed(self):
        self.assertTrue(_allow(modes.handler(_explore_payload("Read"))))
        self.assertTrue(_allow(modes.handler(_explore_payload("Grep"))))
        for cmd in ("pytest -q", "ls -la", "git status", "git diff", "git branch -a",
                    "git log --oneline", "rg pattern", "gh issue create -t x -b y", "gh issue list",
                    # a build verb inside a quoted/echoed/embedded string is NOT a building action — it
                    # must not trip a false deny (err toward allow; don't tax Explore):
                    "echo 'git commit -m x'", 'grep "gh pr create" notes.md',
                    "echo do not git commit here"):
            self.assertTrue(_allow(modes.handler(_explore_payload("Bash", cmd))),
                            f"Bash {cmd!r} must be allowed in Explore")

    def test_subagent_and_unknown_mcp_tools_are_allowed(self):
        self.assertTrue(_allow(modes.handler(_explore_payload("Task"))))
        self.assertTrue(_allow(modes.handler(_explore_payload("mcp__github__list_issues"))))

    def test_unclassifiable_action_resolves_to_allow_no_default_deny(self):
        # The no-default-deny law: an action the gate cannot classify is ALLOWED, never denied.
        self.assertTrue(_allow(modes.handler(_explore_payload("SomeFutureTool"))))
        self.assertTrue(_allow(modes.handler(_explore_payload("Bash", "some_unknown_binary --flag"))))


class TestStanceSignal(unittest.TestCase):
    """The ephemeral, session-keyed OS-temp signal: round-trips, and resolves to Explore in every
    ambiguous case (absent / unreadable / unrecognized → explore)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._patch = mock.patch.object(modes.tempfile, "gettempdir", return_value=self._tmp.name)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self._tmp.cleanup()

    def test_default_is_explore_when_no_signal(self):
        self.assertEqual(modes.current_stance("sess-1"), modes.EXPLORE)

    def test_set_build_then_read_then_clear(self):
        self.assertTrue(modes.set_stance("sess-1", modes.BUILD))
        self.assertEqual(modes.current_stance("sess-1"), modes.BUILD)
        self.assertTrue(modes.clear_stance("sess-1"))
        self.assertEqual(modes.current_stance("sess-1"), modes.EXPLORE)   # cleared -> explore

    def test_set_explore_clears_the_marker(self):
        modes.set_stance("sess-1", modes.BUILD)
        self.assertTrue(modes.set_stance("sess-1", modes.EXPLORE))         # explore == absence of a signal
        self.assertEqual(modes.current_stance("sess-1"), modes.EXPLORE)

    def test_unrecognized_signal_content_resolves_to_explore(self):
        path = modes._signal_path("sess-1")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("garbage-not-a-stance")
        self.assertEqual(modes.current_stance("sess-1"), modes.EXPLORE)   # stale/unknown -> the floor

    def test_clear_is_idempotent(self):
        self.assertTrue(modes.clear_stance("never-set"))                  # missing marker is success

    def test_absent_session_id_degrades_safe(self):
        self.assertEqual(modes.current_stance(None), modes.EXPLORE)
        self.assertFalse(modes.set_stance(None, modes.BUILD))             # no id -> cannot enter Build
        self.assertIsNone(modes._signal_path(""))


class TestBuildAndRoutinePermit(unittest.TestCase):
    """In Build or Routine the gate permits the building actions it denies in Explore."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._patch = mock.patch.object(modes.tempfile, "gettempdir", return_value=self._tmp.name)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self._tmp.cleanup()

    def _payload(self, sid, tool, command=""):
        return {"session_id": sid, "tool_name": tool,
                "tool_input": {"command": command} if command else {}}

    def test_build_permits_writes(self):
        modes.set_stance("s", modes.BUILD)
        self.assertTrue(_allow(modes.handler(self._payload("s", "Edit"))))
        self.assertTrue(_allow(modes.handler(self._payload("s", "Bash", "git commit -m x"))))

    def test_routine_permits_writes(self):
        modes.set_stance("s", modes.ROUTINE)
        self.assertTrue(_allow(modes.handler(self._payload("s", "Write"))))
        self.assertTrue(_allow(modes.handler(self._payload("s", "Bash", "gh pr create"))))

    def test_engine_issue_reroute_fires_even_in_build(self):
        # The reroute is channel-scoped and STANCE-INDEPENDENT — it is checked before the stance short-circuit,
        # so a non-conforming engine-labelled creation is rerouted in Build too (the body contract is
        # unconditional), even though every other write is permitted here. An unlabelled issue still files freely.
        modes.set_stance("s", modes.BUILD)
        self.assertTrue(_deny(modes.handler(self._payload(
            "s", "Bash", 'gh issue create --label engine -b "just free text"'))))
        self.assertTrue(_allow(modes.handler(self._payload("s", "Bash", "gh issue create -b free"))))


class TestFailOpenAndChannel(unittest.TestCase):
    """The gate is fail-open, and a deny rides the structured channel (exit 0), never exit-2 block()."""

    def test_deny_is_exit_0_with_hookSpecificOutput_never_exit_2(self):
        # The platform honors a PreToolUse deny ONLY as exit 0 + a hookSpecificOutput wrapper; exit 2 is
        # read as a crash and the deny dropped. So a real deny must come back as EXIT_PROCEED (0) with the
        # structured permissionDecision, NEVER the blocking exit.
        out, err = io.StringIO(), io.StringIO()
        payload = json.dumps({"session_id": None, "tool_name": "Edit", "tool_input": {"file_path": "/x"}})
        code = hooks.run_hook("PreToolUse", modes.handler,
                              stdin=io.StringIO(payload), stdout=out, stderr=err)
        self.assertEqual(code, hooks.EXIT_PROCEED)            # exit 0, never EXIT_BLOCK (2)
        body = json.loads(out.getvalue())["hookSpecificOutput"]
        self.assertEqual(body["hookEventName"], "PreToolUse")
        self.assertEqual(body["permissionDecision"], "deny")
        self.assertIn("exploring", body["permissionDecisionReason"].lower())

    def test_allow_emits_nothing_and_proceeds(self):
        out, err = io.StringIO(), io.StringIO()
        payload = json.dumps({"session_id": None, "tool_name": "Read", "tool_input": {}})
        code = hooks.run_hook("PreToolUse", modes.handler,
                              stdin=io.StringIO(payload), stdout=out, stderr=err)
        self.assertEqual(code, hooks.EXIT_PROCEED)
        self.assertEqual(out.getvalue(), "")                 # a plain allow injects nothing

    def test_a_crashing_gate_fails_open_never_blocks(self):
        # If the gate's own logic raises, the action must STILL proceed (exit != block) and flag — a gate
        # that crashes must never strand the operator (the hooks fail-open law).
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(modes, "current_stance", side_effect=Exception("boom")):
            code = hooks.run_hook("PreToolUse", modes.handler,
                                  stdin=io.StringIO('{"tool_name":"Edit","tool_input":{}}'),
                                  stdout=out, stderr=err)
        self.assertNotEqual(code, hooks.EXIT_BLOCK)          # never the blocking exit
        self.assertEqual(code, hooks.EXIT_NONBLOCKING)       # fail-open: the action proceeds, flagged

    def test_malformed_payload_fails_open(self):
        out, err = io.StringIO(), io.StringIO()
        code = hooks.run_hook("PreToolUse", modes.handler,
                              stdin=io.StringIO("not json"), stdout=out, stderr=err)
        self.assertNotEqual(code, hooks.EXIT_BLOCK)


class TestBlockInvariantAndVocabulary(unittest.TestCase):
    def test_block_invariant_is_on_a_block_eligible_event_and_declares_its_modes(self):
        self.assertEqual(modes.BLOCK_INVARIANT["event"], "PreToolUse")
        self.assertIn(modes.BLOCK_INVARIANT["event"], hooks.BLOCK_ELIGIBLE_EVENTS)
        self.assertEqual(modes.BLOCK_INVARIANT["owner"], "modes")
        # the write-gate enforces only in Explore (it lets writes through in Build/Routine).
        self.assertEqual(modes.BLOCK_INVARIANT["modes"], [modes.EXPLORE])
        # the block-registry leg produces no finding over modes' declaration.
        self.assertEqual(
            validate.block_budget_findings([modes.BLOCK_INVARIANT], "hard", "x", stances=modes.STANCES), [])

    def test_reroute_invariant_is_stance_independent_and_block_eligible(self):
        # The engine-Issue-conformance reroute is a PreToolUse deny modes' handler composes; it fires in
        # every stance, so it declares all three — owner modes (it registers with modes), and it is a real
        # third member of the block-eligible registry.
        self.assertEqual(modes.REROUTE_BLOCK_INVARIANT["event"], "PreToolUse")
        self.assertEqual(modes.REROUTE_BLOCK_INVARIANT["name"], "engine-issue-conformance")
        self.assertEqual(modes.REROUTE_BLOCK_INVARIANT["owner"], "modes")
        self.assertEqual(sorted(modes.REROUTE_BLOCK_INVARIANT["modes"]), sorted(modes.STANCES))
        self.assertEqual(
            validate.block_budget_findings([modes.REROUTE_BLOCK_INVARIANT], "hard", "x",
                                           stances=modes.STANCES), [])

    def test_describe_stance_is_plain_and_falls_back_to_explore(self):
        self.assertIn("Exploring", modes.describe_stance(modes.EXPLORE))
        self.assertIn("Building", modes.describe_stance(modes.BUILD))
        self.assertEqual(modes.describe_stance("nonsense"), modes.describe_stance(modes.EXPLORE))

    def test_describe_explore_scope_is_self_labelled_and_faithful_to_the_gate(self):
        """The assistant-facing Explore-scope copy must name every allowed carve-out and every denied
        building action, be self-labelled "don't relay", and stay TRUE to the gate it describes — so an AI
        session does not over-restrict (the bug: switching to Build merely to log a GitHub issue). This is
        the fidelity pin between the prose and is_building_action / _MUTATING_TOOLS / _BASH_BUILD_PATTERNS:
        if the gate's allow/deny set changes, this test forces the copy to change with it."""
        scope = modes.describe_explore_scope().lower()
        # self-labelled assistant-facing: it must NOT be relayed into the operator-presentation channel
        self.assertIn("don't relay", scope)
        # every allowed carve-out the gate actually permits in Explore is named (no thin list)
        for allowed in ("read", "test", "search", "subagent", "plan file", "gh issue", "memory"):
            self.assertIn(allowed, scope, f"Explore-scope copy must name the ALLOWED action {allowed!r}")
        # the denied building set is named — and NOT path-scoped (the gate denies edits by tool, any file)
        for denied in ("edit or write any files", "branch", "commit", "pull request"):
            self.assertIn(denied, scope, f"Explore-scope copy must name the DENIED action {denied!r}")
        # the engine-Issue conformance reroute carve-out is named (the helper to author through + that a
        # non-conforming engine Issue is rerouted) so the copy stays faithful to the new gate leg
        for named in ("helper", "reroute"):
            self.assertIn(named, scope, f"Explore-scope copy must name the reroute carve-out term {named!r}")
        # fidelity to the live gate: what the copy calls "allowed" the gate allows; "denied" it denies
        self.assertTrue(_allow(modes.handler(_explore_payload("Bash", "gh issue create -t x -b y"))))
        self.assertTrue(_allow(modes.handler(_explore_payload("Read"))))
        # the engine's own saved-memory upkeep is a Bash CLI, not a Write/Edit tool → the gate allows it
        self.assertTrue(_allow(modes.handler(_explore_payload(
            "Bash", ".engine/.venv/bin/python .engine/tools/memory/pins.py add note"))))
        self.assertTrue(_deny(modes.handler(_explore_payload("Bash", "git commit -m x"))))
        self.assertTrue(_deny(modes.handler(_explore_payload("Bash", "gh pr create"))))
        self.assertTrue(_deny(modes.handler(_explore_payload("Write"))))
        # an UNLABELLED gh issue files freely (asserted above); an engine-labelled NON-conforming one is
        # rerouted (denied), exactly the carve-out the copy now names
        self.assertTrue(_deny(modes.handler(_explore_payload(
            "Bash", 'gh issue create --label engine -b "just free text"'))))


class TestPlanArtifactCarveOut(unittest.TestCase):
    """#64: in Explore the gate EXEMPTS Claude Code's native plan file — recognized by the
    platform's own marker (`permission_mode == "plan"` / `is_plan_file`), NEVER a path — while every other
    write stays denied. (On the current platform `is_plan_file` appears only in conversation text, so the
    live marker is `permission_mode == "plan"`; the exact field is a build-spec leaf.)"""

    def _payload(self, tool_name, permission_mode=None, tool_input=None):
        # session_id=None -> current_stance is Explore (the gated default); no signal file needed.
        return {"session_id": None, "tool_name": tool_name,
                "tool_input": dict(tool_input or {}), "permission_mode": permission_mode}

    def test_plan_file_write_in_plan_mode_is_allowed(self):
        self.assertTrue(_allow(modes.handler(self._payload("Write", permission_mode="plan"))))
        self.assertTrue(_allow(modes.handler(self._payload("Edit", permission_mode="plan"))))

    def test_plan_file_allowed_even_when_plansdir_is_inside_the_repo(self):
        # marker-not-path: a plan folder relocated INTO the repo must not re-trip the gate.
        d = modes.handler(self._payload("Write", permission_mode="plan",
                                        tool_input={"file_path": ".engine/plans/p.md"}))
        self.assertTrue(_allow(d))

    def test_is_plan_file_flag_is_allowed(self):
        # the belt-and-suspenders marker: a platform that flags the write as the plan file is honored too.
        self.assertTrue(_allow(modes.handler(self._payload("Write", tool_input={"is_plan_file": True}))))

    def test_non_plan_engine_source_write_stays_denied(self):
        d = modes.handler(self._payload("Write", permission_mode="default",
                                        tool_input={"file_path": ".engine/tools/x.py"}))
        self.assertTrue(_deny(d))

    def test_non_plan_home_claude_settings_write_stays_denied(self):
        # a ~/.claude/settings.json write carries no plan marker -> denied (it has no merge backstop).
        d = modes.handler(self._payload("Write", permission_mode="default",
                                        tool_input={"file_path": "~/.claude/settings.json"}))
        self.assertTrue(_deny(d))

    def test_write_without_permission_mode_stays_denied(self):
        # pm absent (older platform / a plain edit) -> not the artifact -> denied (old behavior preserved).
        self.assertTrue(_deny(modes.handler(self._payload("Write"))))

    def test_build_verbs_not_exempted_even_in_plan_mode(self):
        # the carve-out is the file-mutating tools specifically; a commit/branch/PR is never the artifact.
        for cmd in ("git commit -m x", "gh pr create", "git checkout -b f"):
            d = modes.handler(self._payload("Bash", permission_mode="plan", tool_input={"command": cmd}))
            self.assertTrue(_deny(d), f"{cmd!r} must stay denied even under permission_mode=plan")

    def test_is_plan_artifact_predicate(self):
        self.assertTrue(modes.is_plan_artifact("Write", {}, "plan"))
        self.assertTrue(modes.is_plan_artifact("Edit", {"is_plan_file": True}, None))
        self.assertFalse(modes.is_plan_artifact("Write", {}, "default"))
        self.assertFalse(modes.is_plan_artifact("Write", {}, None))
        self.assertFalse(modes.is_plan_artifact("Bash", {"command": "x"}, "plan"))  # not a file-mutating tool


class TestMemoryTargetDenial(unittest.TestCase):
    """#257: a blocked Write/Edit to a MEMORY store keeps the DECISION (deny) but earns a
    memory-specific RELAY — a competent "noted" with a correlate the operator can exercise, never the
    build-set "open a pull request" line and never the two-store seam. Message choice only; path
    recognition is safe here precisely because the write is denied either way."""

    def _payload(self, tool_name, file_path=None):
        # session_id=None -> current_stance is Explore (the gated default); no signal file needed.
        ti = {"file_path": file_path} if file_path else {}
        return {"session_id": None, "tool_name": tool_name, "tool_input": ti}

    def test_engine_memory_write_is_denied_with_the_memory_relay(self):
        d = modes.handler(self._payload("Write", ".engine/memory/ledger.ndjson"))
        self.assertTrue(_deny(d))
        self.assertEqual(d["reason"], modes._MEMORY_DENIAL)

    def test_harness_auto_memory_write_is_denied_with_the_memory_relay(self):
        # the harness notebook default shape: a `memory` dir nested under a `.claude` dir.
        d = modes.handler(self._payload("Write", "/Users/x/.claude/projects/slug/memory/MEMORY.md"))
        self.assertTrue(_deny(d))
        self.assertEqual(d["reason"], modes._MEMORY_DENIAL)

    def test_memory_relay_confirms_noted_and_names_a_real_correlate(self):
        r = modes._MEMORY_DENIAL.lower()
        self.assertIn("noted", r)                 # a competent "noted", not a refusal
        self.assertIn("read it back", r)          # a correlate the operator can exercise (the AI recalls)

    def test_memory_relay_does_not_mishear_remember_as_a_code_change(self):
        r = modes._MEMORY_DENIAL.lower()
        self.assertNotIn("pull request", r)       # never the build-set line for a "remember this"
        self.assertNotIn("build it", r)

    def test_memory_relay_does_not_leak_the_two_store_seam(self):
        r = modes._MEMORY_DENIAL.lower()
        self.assertNotIn("harness", r)            # vocabulary-leak: no "harness vs engine memory" tour
        self.assertNotIn("orientation", r)        # the dropped false correlate must not creep back in

    def test_non_memory_engine_source_write_keeps_the_generic_denial(self):
        # the .engine/tools/memory/ SOURCE dir is not the store; it earns the generic build-set denial —
        # including via the ABSOLUTE worktree path (…/.claude/worktrees/<wt>/.engine/tools/memory/…), which
        # carries both a `.claude` and a `memory` segment and must NOT be mistaken for the harness notebook.
        for path in (".engine/tools/x.py", ".engine/tools/memory/index.py", "README.md",
                     "/Users/x/.claude/worktrees/wt/.engine/tools/memory/pins.py"):
            d = modes.handler(self._payload("Write", path))
            self.assertTrue(_deny(d))
            self.assertEqual(d["reason"], modes._DENIAL, f"{path} must keep the generic denial")

    def test_is_memory_target_predicate(self):
        self.assertTrue(modes.is_memory_target("Write", {"file_path": ".engine/memory/ledger.ndjson"}))
        self.assertTrue(modes.is_memory_target("Write", {"file_path": "/Users/x/.engine/memory/ledger.ndjson"}))
        self.assertTrue(modes.is_memory_target("Edit", {"file_path": "~/.claude/projects/s/memory/x.md"}))
        self.assertTrue(modes.is_memory_target("NotebookEdit", {"notebook_path": ".engine/memory/n.ipynb"}))
        self.assertFalse(modes.is_memory_target("Write", {"file_path": ".engine/tools/memory/index.py"}))
        # the worktree case: engine source under a `.claude/worktrees/…` path is NOT a memory store.
        self.assertFalse(modes.is_memory_target(
            "Write", {"file_path": "/Users/x/.claude/worktrees/wt/.engine/tools/memory/pins.py"}))
        self.assertFalse(modes.is_memory_target("Write", {"file_path": ".engine/tools/x.py"}))
        self.assertFalse(modes.is_memory_target("Write", {}))                 # no path -> not classifiable
        self.assertFalse(modes.is_memory_target("Bash", {"command": "x"}))    # not a file-mutating tool


class TestPlanAcceptanceBuildEntry(unittest.TestCase):
    """#67: a PostToolUse on the plan-exit completion (`ExitPlanMode`) flips the stance to
    Build AND injects a do-not-relay assistant-internal stance directive (gated on the flip succeeding);
    every other completion leaves it untouched and proceeds with no inject; the handler ALWAYS proceeds,
    never blocks; a rejected plan fires no PostToolUse so the stance stays Explore (fail-safe to the floor)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._patch = mock.patch.object(modes.tempfile, "gettempdir", return_value=self._tmp.name)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self._tmp.cleanup()

    def test_accepting_a_plan_enters_build(self):
        self.assertEqual(modes.current_stance("s"), modes.EXPLORE)
        d = modes.accept_handler({"session_id": "s", "tool_name": "ExitPlanMode"})
        self.assertEqual(d.get("action"), "inject")                # sets the signal AND injects the directive
        self.assertEqual(modes.current_stance("s"), modes.BUILD)
        self.assertIn(modes._STANCE_LINES[modes.BUILD], d.get("context", ""))

    def test_non_plan_exit_completion_does_not_enter_build(self):
        # The false-fire guard: ONLY ExitPlanMode enters Build. A subagent's inner tool calls do not fire
        # the parent PostToolUse, and a leave-without-approving fires no ExitPlanMode (platform behavior,
        # live-confirmed); this locks that the handler keys solely on the ExitPlanMode completion event.
        for tool in ("Edit", "Task", "Bash", "Read", "SomeFutureTool"):
            modes.accept_handler({"session_id": "s", "tool_name": tool})
            self.assertEqual(modes.current_stance("s"), modes.EXPLORE, f"{tool} must not enter Build")

    def test_handler_always_proceeds_and_tolerates_a_bad_payload(self):
        # The inject is gated on the durable flip succeeding, so a sessionless/bad payload proceeds with NO
        # inject (no split-brain) — set_stance returns False, the directive is never emitted.
        self.assertEqual(modes.accept_handler({"tool_name": "ExitPlanMode"}).get("action"), "proceed")  # no sid
        self.assertEqual(modes.accept_handler({}).get("action"), "proceed")
        self.assertEqual(modes.accept_handler({"session_id": "s"}).get("action"), "proceed")  # no tool_name
        self.assertEqual(modes.current_stance("s"), modes.EXPLORE)        # none of those entered Build

    def test_end_to_end_via_run_hook_sets_build_and_injects(self):
        out, err = io.StringIO(), io.StringIO()
        payload = json.dumps({"session_id": "s", "tool_name": "ExitPlanMode"})
        code = hooks.run_hook("PostToolUse", modes.accept_handler,
                              stdin=io.StringIO(payload), stdout=out, stderr=err)
        self.assertEqual(code, hooks.EXIT_PROCEED)        # the inject rides exit 0 — non-blocking, by design
        emitted = json.loads(out.getvalue())              # sets the signal AND emits the directive
        self.assertEqual(emitted["hookSpecificOutput"]["hookEventName"], "PostToolUse")
        self.assertIn(modes._STANCE_LINES[modes.BUILD],
                      emitted["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(modes.current_stance("s"), modes.BUILD)

    def test_build_entry_directive_is_do_not_relay_and_carries_no_operator_announcement(self):
        # The directive is assistant-facing machine context: it NAMES Build, is self-labelled
        # do-not-relay, points the turn into the kickoff, and carries NO imperative relay marker (the
        # `INFORM THE USER THAT…` class) and no raw mechanism jargon — so if it ever leaks it reads plainly.
        # It ALSO names the pre-work consent gate (the risk assessment + the operator's depth choice) as a
        # short label, so a cold session is primed to run it — without reproducing the risk-assessment copy.
        text = modes._build_entry_directive()
        self.assertIn(modes._STANCE_LINES[modes.BUILD], text)         # names the new stance (fidelity anchor)
        self.assertIn("don't relay", text.lower())                   # self-labelled do-not-relay
        self.assertIn("kickoff", text.lower())                       # triggers, never replaces, the kickoff
        self.assertIn("modes.py stance", text)                       # the live-signal re-read guard names its tool
        self.assertIn("risk assessment", text.lower())               # step 2: names the pre-work consent gate
        self.assertIn("depth", text.lower())                         # ... and the operator's how-careful choice
        low = text.lower()
        for marker in ("inform the user", "tell the operator", "let the user know"):
            self.assertNotIn(marker, low)                            # no imperative relay marker
        for jargon in ("posttooluse", "additionalcontext", "hookspecificoutput"):
            self.assertNotIn(jargon, low)                            # degrades to plain language if surfaced

    def test_resumes_to_explore_so_a_replayed_directive_is_inert(self):
        # Proves the cleared-signal floor + the live-signal authority: after accept (Build), a SessionStart
        # clear returns the LIVE signal to Explore, so the assistant reports Explore (not any replayed line)
        # and the gate denies an Edit. The genuine REPLAY of additionalContext is the platform ceiling, not a
        # unit test; this pins the half the engine owns — the live signal, and the gate as the mechanical floor.
        modes.accept_handler({"session_id": "s", "tool_name": "ExitPlanMode"})
        self.assertEqual(modes.current_stance("s"), modes.BUILD)
        modes.clear_stance("s")                                      # what boot does at every SessionStart
        self.assertEqual(modes.current_stance("s"), modes.EXPLORE)  # reports from the live signal, not a line
        d = modes.handler({"session_id": "s", "tool_name": "Edit", "tool_input": {}})
        self.assertEqual(d.get("permissionDecision"), "deny")       # the replayed 'you are in Build' is inert

    def test_main_accept_hook_routes_to_posttooluse(self):
        with mock.patch.object(modes.hooks, "run_hook", return_value=0) as rh:
            self.assertEqual(modes.main(["accept-hook"]), 0)
        rh.assert_called_once_with("PostToolUse", modes.accept_handler)

    def test_wired_command_ends_in_accept_hook(self):
        manifest_path = os.path.join(validate.ROOT, ".engine", "modules", "core", "manifest.json")
        with open(manifest_path, encoding="utf-8") as fh:
            wires = json.load(fh)["wires"]
        post = [w for w in wires if w.get("type") == "hook" and w.get("event") == "PostToolUse"]
        self.assertTrue(post, "core manifest must wire a PostToolUse hook (the modes plan-acceptance trigger)")
        self.assertTrue(any(w["hook"]["command"].rstrip().endswith(" accept-hook") for w in post),
                        "the PostToolUse modes wire must invoke `modes.py accept-hook`")


class TestResolveSession(unittest.TestCase):
    """The /engine-start session-id resolution: the skill body passes
    `--session "${CLAUDE_CODE_SESSION_ID}"` (the shell expands that env var), with a fallback to reading
    the CLAUDE_CODE_SESSION_ID env var directly so the Build verb still resolves the real session when the
    argument arrives empty or unexpanded."""

    def test_explicit_session_wins(self):
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "from-env"}):
            self.assertEqual(modes._resolve_session(["--session", "explicit"]), "explicit")

    def test_falls_back_to_env_when_absent(self):
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "from-env"}):
            self.assertEqual(modes._resolve_session([]), "from-env")

    def test_falls_back_to_env_on_unexpanded_token(self):
        # a shell that did not expand the env var passes the literal ${CLAUDE_CODE_SESSION_ID}
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "from-env"}):
            self.assertEqual(modes._resolve_session(["--session", "${CLAUDE_CODE_SESSION_ID}"]), "from-env")

    def test_none_when_neither_present(self):
        # The provider chain now ends at the live-session marker (the typed-Codex-verb fallback), so
        # "neither present" must ALSO mean no marker: point the marker override at a nonexistent path.
        import providers
        with mock.patch.dict(os.environ,
                             {providers.MARKER_ENV: os.path.join(tempfile.gettempdir(),
                                                                 "no-such-marker.json")},
                             clear=True):
            self.assertIsNone(modes._resolve_session([]))

    def test_set_build_cli_uses_env_fallback(self):
        # `modes.py set-build` with no --session resolves the env session and enters Build for it
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(modes.tempfile, "gettempdir", return_value=tmp), \
                mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "cli-env-session"}):
            self.assertEqual(quiet_call.run(modes.main, ["set-build"]), 0)
            self.assertEqual(modes.current_stance("cli-env-session"), modes.BUILD)

    def test_set_routine_cli_enters_routine_only_when_isolated(self):
        # set-routine grants the unattended write-stance ONLY on proven worktree isolation.
        import checkout_health
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(modes.tempfile, "gettempdir", return_value=tmp), \
                mock.patch.object(checkout_health, "is_isolated_worktree", return_value=True):
            self.assertEqual(quiet_call.run(modes.main, ["set-routine", "--session", "sR"]), 0)
            self.assertEqual(modes.current_stance("sR"), modes.ROUTINE)

    def test_set_routine_cli_declines_when_not_isolated(self):
        # In the operator's main checkout (isolation not provable) set-routine DECLINES — stays explore,
        # never grants an unattended write stance that could strand the operator's checkout.
        import checkout_health
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(modes.tempfile, "gettempdir", return_value=tmp), \
                mock.patch.object(checkout_health, "is_isolated_worktree", return_value=False):
            self.assertEqual(quiet_call.run(modes.main, ["set-routine", "--session", "sR"]), 1)
            self.assertEqual(modes.current_stance("sR"), modes.EXPLORE)

    def test_set_routine_cli_declines_when_session_unresolvable(self):
        # No resolvable session -> decline (return 1) before any stance is written (the session check comes
        # first, so a garbled/absent id never keys a routine marker to nothing).
        import providers
        with mock.patch.dict(os.environ,
                             {providers.MARKER_ENV: os.path.join(tempfile.gettempdir(),
                                                                 "no-such-marker.json")}, clear=True):
            self.assertEqual(quiet_call.run(modes.main, ["set-routine"]), 1)

    def test_set_build_makes_the_gate_allow_writes_for_that_session(self):
        # The end-to-end modes contract the Build verb relies on: set-build for a session id makes the
        # PreToolUse gate PERMIT a write for THAT SAME id (one it denies in explore), and ONLY that id.
        # Pins that the marker set-build writes is the one the gate reads — the binding the verb depends
        # on, independent of how the platform sources the id into --session.
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(modes.tempfile, "gettempdir", return_value=tmp):
            self.assertEqual(quiet_call.run(modes.main, ["set-build", "--session", "sX"]), 0)
            allow = modes.handler({"session_id": "sX", "tool_name": "Edit", "tool_input": {}})
            self.assertTrue(_allow(allow), "the gate allows a write for the session that entered Build")
            other = modes.handler({"session_id": "sOther", "tool_name": "Edit", "tool_input": {}})
            self.assertTrue(_deny(other), "a different session stays in explore — the marker is session-keyed")

    def test_stance_verb_uses_env_fallback_and_reports_the_true_stance(self):
        # The footgun fix: a bare `modes.py stance` resolves the session from $CLAUDE_CODE_SESSION_ID
        # and reports the REAL stance (Build here), not the safe-default explore it used to print with no flag.
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(modes.tempfile, "gettempdir", return_value=tmp), \
                mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "env-sid"}):
            modes.set_stance("env-sid", modes.BUILD)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = modes.main(["stance"])
            self.assertEqual(rc, 0)
            self.assertEqual(buf.getvalue().strip(), modes.BUILD)

    def test_stance_verb_explicit_session_wins(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(modes.tempfile, "gettempdir", return_value=tmp), \
                mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "env-sid"}):
            modes.set_stance("explicit-sid", modes.BUILD)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = modes.main(["stance", "--session", "explicit-sid"])
            self.assertEqual(rc, 0)
            self.assertEqual(buf.getvalue().strip(), modes.BUILD)

    def test_stance_verb_says_unknown_not_explore_when_unresolvable(self):
        # The footgun itself: with NO resolvable session the OLD verb printed a confident `explore`. Now it
        # says `unknown` and exits non-zero, so a self-check can never confirm the wrong belief. "No
        # resolvable session" includes the live-session marker (the typed-Codex-verb fallback), so the
        # marker override points at a nonexistent path.
        import providers
        with mock.patch.dict(os.environ,
                             {providers.MARKER_ENV: os.path.join(tempfile.gettempdir(),
                                                                 "no-such-marker.json")},
                             clear=True):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = modes.main(["stance"])
            self.assertEqual(rc, 1)
            self.assertIn("unknown", buf.getvalue().lower())
            self.assertNotIn(modes.EXPLORE, buf.getvalue())


if __name__ == "__main__":
    unittest.main()
