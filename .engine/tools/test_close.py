#!/usr/bin/env python3
"""Self-tests for close: the turn-close Stop disposition gate + ambient-capture trigger.

Run: uv run --directory .engine --frozen -- python -m unittest discover -s tools -p 'test_*.py' -b

Each test locks one load-bearing law, faking only the network (the
demo-fidelity rule): the ephemeral session-keyed record round-trips and degrades SAFE; the gate HOLDS a
turn while a recorded finding is undispositioned and ENDS it once dispositioned; a forced continuation
(stop_hook_active, under BOTH platform readings) logs the leftover down telemetry's promotion path and
proceeds — never re-blocks, never deadlocks, never loses a finding; the fail-open DIRECTION is to let the
turn end (an unreadable record / a crash never HOLDS the turn); the source_id is content-derived so a
recurring concern dedups to one Issue across turns; the ambient-capture trigger relays the turn delta into
memory's ledger LIVE (memory has shipped), and any fault — including a repo without the memory module — is a
no-op that never gates the handler; a repeated disposition loop stays legible (the loop-line on the 2nd+
block, a pre-announcement on the approach to the cap); routine is satisfiable non-interactively; and the
block invariant sits on a block-eligible event. The deliverable-gate cold review attests each test's
assertion matches its name.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import close      # noqa: E402
import hooks      # noqa: E402
import modes      # noqa: E402  (the canonical stance vocabulary the block-registry leg validates against)
import telemetry  # noqa: E402
import validate   # noqa: E402


def fake_gh():
    """A real telemetry.GitHubIssues over the in-memory FakeGitHub transport (only the network is faked;
    the promotion logic is real)."""
    fake = telemetry._FakeGitHub()
    return telemetry.GitHubIssues("you/proj", "tok", transport=fake.transport), fake


def open_issue_count(fake):
    return sum(1 for i in fake.issues.values() if i["state"] == "open")


def _stop(handler_payload, stdin_text=None):
    """Drive the REAL run_hook('Stop', close.handler) with captured streams -> (exit_code, out, err)."""
    if stdin_text is None:
        stdin_text = json.dumps(handler_payload)
    out, err = io.StringIO(), io.StringIO()
    code = hooks.run_hook("Stop", close.handler, stdin=io.StringIO(stdin_text), stdout=out, stderr=err)
    return code, out.getvalue(), err.getvalue()


class CloseBase(unittest.TestCase):
    def setUp(self):
        self.sid = f"engine-test-close-{self.id()}"
        close.clear(self.sid)

    def tearDown(self):
        close.clear(self.sid)


class TestFindingsRecord(CloseBase):
    def test_record_pending_dispose_roundtrip(self):
        self.assertEqual(close.pending(self.sid), [])
        fid = close.record_finding(self.sid, "the endpoint has no rate limit")
        self.assertTrue(fid)
        self.assertEqual([f["id"] for f in close.pending(self.sid)], [fid])
        self.assertTrue(close.dispose(self.sid, fid, "fixed"))
        self.assertEqual(close.pending(self.sid), [])              # disposed -> no longer pending

    def test_record_is_idempotent_on_same_open_message(self):
        a = close.record_finding(self.sid, "same concern")
        b = close.record_finding(self.sid, "same concern")
        self.assertEqual(a, b)                                     # one entry, not two
        self.assertEqual(len(close.read_findings(self.sid)), 1)

    def test_empty_message_is_not_recorded(self):
        self.assertIsNone(close.record_finding(self.sid, "   "))
        self.assertEqual(close.read_findings(self.sid), [])

    def test_dispose_rejects_unknown_kind(self):
        fid = close.record_finding(self.sid, "x")
        with self.assertRaises(ValueError):
            close.dispose(self.sid, fid, "ignored")

    def test_unreadable_record_reads_as_empty(self):
        # A malformed record on disk must read as empty (degrade SAFE) -> nothing pending -> turn ends.
        path = close._record_path(self.sid)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("{ not json")
        self.assertEqual(close.read_findings(self.sid), [])
        self.assertEqual(close.pending(self.sid), [])

    def test_no_session_id_degrades_safe(self):
        self.assertIsNone(close.record_finding(None, "x"))
        self.assertEqual(close.pending(None), [])                 # no usable id -> nothing held

    def test_summary_quiet_then_handled(self):
        self.assertEqual(close.summary(self.sid), "")             # quiet when nothing flagged
        f1 = close.record_finding(self.sid, "a")
        f2 = close.record_finding(self.sid, "b")
        close.dispose(self.sid, f1, "fixed")
        close.dispose(self.sid, f2, "logged")
        self.assertEqual(close.summary(self.sid),
                         "everything I flagged this turn is handled — 1 fixed, 1 saved as a follow-up item.")

    def test_record_is_off_repo_os_temp(self):
        import tempfile
        path = close._record_path(self.sid)
        self.assertTrue(path.startswith(tempfile.gettempdir()))   # OS-temp, never under the repo
        self.assertNotIn(".engine", path)


class TestGateBranches(CloseBase):
    def test_clean_turn_proceeds(self):
        code, _out, _err = _stop({"session_id": self.sid, "stop_hook_active": False})
        self.assertEqual(code, hooks.EXIT_PROCEED)                # nothing recorded -> the turn ends

    def test_pending_holds_the_turn(self):
        close.record_finding(self.sid, "needs a decision")
        code, _out, err = _stop({"session_id": self.sid, "stop_hook_active": False})
        self.assertEqual(code, hooks.EXIT_BLOCK)                  # exit 2 -> the turn is held
        self.assertIn("still needs a decision", err)             # the plain pushback, fed back to Claude
        self.assertIn("needs a decision", err)                   # ...naming what is still open

    def test_dispositioned_turn_ends(self):
        fid = close.record_finding(self.sid, "needs a decision")
        close.dispose(self.sid, fid, "logged")
        code, _out, _err = _stop({"session_id": self.sid, "stop_hook_active": False})
        self.assertEqual(code, hooks.EXIT_PROCEED)               # disposed -> the turn ends

    def test_pushback_leaks_no_raw_code_identifier(self):
        # The operator's pushback line must never carry a raw hook name / code identifier — a symbol
        # surfacing verbatim means an internal leaked (a bug), not a word choice. This guards SYMBOLS, not
        # vocabulary, so it is not a banned-word list: whether the prose leans
        # on jargon is a judgment (the audit probe + the per-PR review), never a filter.
        close.record_finding(self.sid, "x")
        _code, _out, err = _stop({"session_id": self.sid, "stop_hook_active": False})
        for sym in ("stop_hook_active", "finding-record", "PreToolUse", "source_id"):
            self.assertNotIn(sym, err, f"raw code identifier {sym!r} leaked into the operator pushback")


class TestForcedContinuation(CloseBase):
    """Robust to BOTH platform readings of stop_hook_active: it never deadlocks and never loses a finding.
    Reading A (true every continuation) -> one block then this forced path. Reading B (true only at the
    final forced continuation) -> blocks up to the cap then this forced path. Either way: log + proceed."""

    def test_forced_continuation_logs_then_proceeds(self):
        close.record_finding(self.sid, "an unsettled concern")
        gh, fake = fake_gh()
        with unittest.mock.patch.object(close, "_github", lambda: gh):
            code, _out, _err = _stop({"session_id": self.sid, "stop_hook_active": True})
        self.assertEqual(code, hooks.EXIT_PROCEED)               # never re-blocks -> never deadlocks
        self.assertEqual(open_issue_count(fake), 1)              # the leftover is LOGGED, not lost
        self.assertEqual(close.pending(self.sid), [])            # ...and cleared (never re-enters the gate)

    def test_forced_continuation_keeps_untracked_finding_never_lost(self):
        # Offline at the cap: promotion fails, so the leftover is KEPT (it re-surfaces next turn) rather
        # than silently dropped — the finding survives regardless. The turn still ENDS.
        # #412: the notice is HONEST about what failed — the disposition check RAN (it found the
        # leftover); only the durable SAVE was offline. So it says "couldn't save them as tracked", NOT the
        # gate-CRASH line "couldn't run the check" (which now belongs to a genuine gate crash).
        close.record_finding(self.sid, "an unsettled concern")
        with unittest.mock.patch.object(close, "_github", lambda: None):   # offline -> promotion fails
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                decision = close.handler({"session_id": self.sid, "stop_hook_active": True})
        self.assertEqual(decision, hooks.proceed())              # still proceeds (never deadlocks/strands)
        self.assertIn("couldn't save them as tracked items", err.getvalue())   # honest: the save failed, not the check
        self.assertNotIn("couldn't run the check", err.getvalue())             # NOT the gate-crash wording
        self.assertEqual(len(close.pending(self.sid)), 1)        # KEPT — never lost (re-surfaces next turn)

    def test_close_hook_wires_its_own_gate_crash_notice_into_run_hook(self):
        # The Stop hook hands run_hook close's OWN crash line, so a disposition-gate CRASH surfaces
        # "couldn't run the check that confirms nothing was dropped" (fails open, and says so),
        # not run_hook's generic wording — while the single central emit path is preserved.
        captured = {}
        def fake_run_hook(event, handler, **kw):
            captured.update(event=event, **kw)
            return 0
        with unittest.mock.patch.object(hooks, "run_hook", fake_run_hook):
            close.main(["hook"])
        self.assertEqual(captured.get("event"), "Stop")
        self.assertEqual(captured.get("fail_open_notice"), close._FAIL_OPEN_NOTICE)
        self.assertIn("couldn't run the check that confirms nothing was dropped", close._FAIL_OPEN_NOTICE)

    def test_normal_then_forced_one_block_then_ends(self):
        # Reading A end-to-end: a pending finding blocks once (sha False), then the forced continuation
        # (sha True) proceeds with the finding logged — the turn cannot loop forever.
        close.record_finding(self.sid, "concern")
        c1, _o, _e = _stop({"session_id": self.sid, "stop_hook_active": False})
        self.assertEqual(c1, hooks.EXIT_BLOCK)
        gh, fake = fake_gh()
        with unittest.mock.patch.object(close, "_github", lambda: gh):
            c2, _o2, _e2 = _stop({"session_id": self.sid, "stop_hook_active": True})
        self.assertEqual(c2, hooks.EXIT_PROCEED)
        self.assertEqual(open_issue_count(fake), 1)


class TestFailOpenDirection(CloseBase):
    def test_unreadable_record_lets_the_turn_end(self):
        # The fail-open DIRECTION: close fails open by letting the turn END (never holding it). An
        # unreadable record reads as empty -> nothing pending -> proceed. (Inverse of modes' write-gate.)
        path = close._record_path(self.sid)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("not json at all")
        code, _out, _err = _stop({"session_id": self.sid, "stop_hook_active": False})
        self.assertEqual(code, hooks.EXIT_PROCEED)

    def test_handler_crash_fails_open(self):
        # If the gate itself crashes, run_hook fails open (the turn ends) and flags — never a hard block.
        with unittest.mock.patch.object(close, "pending", side_effect=RuntimeError("boom")):
            code, _out, err = _stop({"session_id": self.sid, "stop_hook_active": False})
        self.assertEqual(code, hooks.EXIT_NONBLOCKING)           # non-blocking -> the turn ends
        self.assertIn("could not run", err)

    def test_malformed_payload_proceeds(self):
        code, _out, _err = _stop(None, stdin_text="{ not json")
        self.assertEqual(code, hooks.EXIT_NONBLOCKING)           # fail-open on an unreadable event


class TestPromoteRelay(CloseBase):
    def test_finding_record_shape_is_schema_valid(self):
        schema = validate.load_json(os.path.join(validate.SCHEMAS_DIR, "finding-record.v1.json"))
        rec = close._to_finding_record({"message": "x", "location": None}, "2026-06-05T01:00:00Z")
        self.assertEqual(list(validate.Draft202012Validator(schema).iter_errors(rec)), [])
        self.assertEqual(rec["severity"], telemetry.TRUST_CRITICAL)   # promotes immediately
        self.assertIn("location", rec)                                # explicit key (schema requires it)

    def test_source_id_is_content_derived_and_dedups(self):
        a = close._source_id({"message": "same concern"})
        b = close._source_id({"message": "same concern"})
        c = close._source_id({"message": "different concern"})
        self.assertEqual(a, b)                                        # same content -> same key
        self.assertNotEqual(a, c)
        self.assertTrue(a.startswith("close/disposition/"))

    def test_same_concern_across_turns_dedups_to_one_issue(self):
        # The content source_id means a recurring escaped concern collapses onto ONE tracked Issue.
        gh, fake = fake_gh()
        f = {"message": "recurring concern", "location": None}
        close._promote(f, "2026-06-05T01:00:00Z", github=gh)
        close._promote(f, "2026-06-05T02:00:00Z", github=gh)
        self.assertEqual(open_issue_count(fake), 1)                   # one issue, updated — not duplicated

    def test_promote_degrades_to_false_when_offline(self):
        # github=None means OFFLINE (the sentinel guards against a test ever reaching live GitHub).
        self.assertFalse(close._promote({"message": "x"}, "2026-06-05T01:00:00Z", github=None))


class TestAmbientTrigger(CloseBase):
    def test_ambient_capture_appends_to_ledger_when_memory_present(self):
        # Memory has SHIPPED: the trigger relays the completed turn's delta into memory's ledger LIVE (close
        # only TRIGGERS; memory owns the mechanism). Mirrors the demo-relay fixture — a throwaway ledger +
        # transcript, the REAL relay + real memory.capture. Skips on a repo with no memory-substrate (the
        # legitimate inert state), never fails there.
        try:
            from memory import capture, ledger  # noqa: E402
        except Exception:  # noqa: BLE001 — no memory module: the inert state is legitimate, not a failure
            self.skipTest("memory-substrate not installed (the relay's legitimate inert state)")
        tmp = tempfile.mkdtemp(prefix="engine-test-close-relay-")
        self.addCleanup(shutil.rmtree, tmp, True)
        prev = {ledger.ENV_DIR: os.environ.get(ledger.ENV_DIR),
                capture.TRANSCRIPT_DIR_ENV: os.environ.get(capture.TRANSCRIPT_DIR_ENV)}
        os.environ[ledger.ENV_DIR] = tmp
        os.environ[capture.TRANSCRIPT_DIR_ENV] = tmp
        try:
            transcript = os.path.join(tmp, "t.jsonl")
            with open(transcript, "w", encoding="utf-8") as fh:
                for role, text in (("user", "ship the calendar sync friday"),
                                   ("assistant", "agreed — the calendar sync ships friday")):
                    fh.write(json.dumps({"type": role, "message": {"role": role, "content": text}}) + "\n")

            def stored():
                return sum(1 for _ in ledger.iter_records(path=ledger.ledger_path()))

            before = stored()
            close._trigger_ambient_capture({"session_id": self.sid, "transcript_path": transcript})
            self.assertGreaterEqual(stored() - before, 1)        # the turn's delta landed in the ledger (LIVE)
        finally:
            for key, val in prev.items():
                os.environ.pop(key, None) if val is None else os.environ.__setitem__(key, val)

    def test_ambient_capture_fault_is_a_noop_and_never_gates(self):
        # Any capture fault — including a repo with NO memory module — is a silent no-op that never raises
        # into the handler and never gates the turn (capture never gates close).
        import builtins
        real_import = builtins.__import__

        def no_memory(name, *a, **k):
            if name == "memory" or name.startswith("memory."):
                raise ImportError("memory forced absent")
            return real_import(name, *a, **k)

        with unittest.mock.patch("builtins.__import__", side_effect=no_memory):
            close._trigger_ambient_capture({"session_id": self.sid})     # must not raise
            code, _out, _err = _stop({"session_id": self.sid})           # clean turn still proceeds
        self.assertEqual(code, hooks.EXIT_PROCEED)


class TestDispositionLoopLegibility(CloseBase):
    """A repeated disposition loop stays legible on the RELIABLE block channel (a disposition
    loop is legible): the loop-line on the 2nd+ block, a pre-announcement on the approach to the cap."""

    def _block_reason(self):
        # One consecutive normal block (stop_hook_active false); returns the block reason (stderr).
        _c, _o, err = _stop({"session_id": self.sid, "stop_hook_active": False})
        return err

    def test_first_block_is_the_full_ask_no_loop_line(self):
        close.record_finding(self.sid, "an unsettled concern")
        err = self._block_reason()                               # block #1
        self.assertIn("still needs a decision", err)             # the full ask
        self.assertNotIn(close._LOOP_LINE, err)                  # not the repeat line yet

    def test_repeated_pushback_surfaces_the_loop_line(self):
        close.record_finding(self.sid, "an unsettled concern")
        self._block_reason()                                     # block #1
        err = self._block_reason()                               # block #2 — repeated
        self.assertIn(close._LOOP_LINE, err)                     # one calm sentence, not a re-list

    def test_cap_approach_is_pre_announced_before_the_hang(self):
        close.record_finding(self.sid, "an unsettled concern")
        cap = close._effective_block_cap()
        errs = [self._block_reason() for _ in range(cap - 1)]    # blocks 1..cap-1
        self.assertIn(close._CAP_APPROACH, errs[-1])             # pre-announced on the last block before the cap
        self.assertNotIn(close._CAP_APPROACH, errs[0])           # ...never on the first

    def test_effective_cap_honors_operator_override(self):
        with unittest.mock.patch.dict(os.environ, {hooks.STOP_HOOK_BLOCK_CAP_ENV: "16"}):
            self.assertEqual(close._effective_block_cap(), 16)   # operator raised it -> announce later
        with unittest.mock.patch.dict(os.environ, {hooks.STOP_HOOK_BLOCK_CAP_ENV: "nope"}):
            self.assertEqual(close._effective_block_cap(), hooks.STOP_HOOK_BLOCK_CAP)  # garbage -> the default

    def test_clean_turn_resets_the_block_streak(self):
        fid = close.record_finding(self.sid, "concern")
        self._block_reason()
        self._block_reason()                                     # streak -> 2
        close.dispose(self.sid, fid, "logged")                  # settle it
        _c, _o, _e = _stop({"session_id": self.sid, "stop_hook_active": False})  # a clean turn ends the streak
        close.record_finding(self.sid, "another")               # a fresh concern next turn
        err = self._block_reason()                               # its FIRST block is the full ask again
        self.assertNotIn(close._LOOP_LINE, err)

    def test_loop_and_cap_lines_leak_no_raw_identifier(self):
        for line in (close._LOOP_LINE, close._CAP_APPROACH):
            for sym in ("stop_hook_active", "_LOOP_LINE", "_CAP_APPROACH", "source_id", "PreToolUse"):
                self.assertNotIn(sym, line, f"raw identifier {sym!r} leaked into an operator line")


class TestRoutineSatisfiable(CloseBase):
    def test_log_it_discharges_without_a_human(self):
        # An unattended (routine) forced continuation must satisfy the gate non-interactively via log-it,
        # never deadlock. No human acts; the leftover is logged and the turn ends.
        close.record_finding(self.sid, "routine-raised concern")
        gh, fake = fake_gh()
        with unittest.mock.patch.object(close, "_github", lambda: gh):
            decision = close.handler({"session_id": self.sid, "stop_hook_active": True})
        self.assertEqual(decision, hooks.proceed())
        self.assertEqual(open_issue_count(fake), 1)


class TestBlockInvariant(CloseBase):
    def test_block_invariant_is_stop_block_eligible_and_declares_its_modes(self):
        self.assertEqual(close.BLOCK_INVARIANT,
                         {"event": "Stop", "name": "findings-disposition", "owner": "close",
                          "modes": ["explore", "build", "routine"]})
        self.assertIn(close.BLOCK_INVARIANT["event"], hooks.BLOCK_ELIGIBLE_EVENTS)
        # the findings-disposition gate fires on any turn-close, so it is active in every stance.
        self.assertEqual(sorted(close.BLOCK_INVARIANT["modes"]), sorted(modes.STANCES))

    def test_block_budget_leg_clean_with_the_close_member(self):
        # The block-registry leg is silent when the close member sits on a block-eligible event and
        # declares the stances it is active in.
        findings = validate.block_budget_findings(
            [dict(close.BLOCK_INVARIANT)], "hard", "a block must sit on a block-eligible event",
            stances=modes.STANCES)
        self.assertEqual(findings, [])


if __name__ == "__main__":
    import unittest.mock  # noqa: E402  (imported lazily so the module body stays import-light)
    unittest.main()
