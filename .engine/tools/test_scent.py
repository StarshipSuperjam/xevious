"""test_scent.py — the per-prompt recall cue: scent.py (memory-substrate-sqlite-fts5).

Run: uv run --directory .engine --frozen -- python tools/selftest.py

The scent is the per-prompt member of the orientation family: a UserPromptSubmit hook that injects one short
constant cue asking whether this project has already settled the thing at hand. These pin its laws: it fires on
EVERY prompt (the reflex is the deliverable — a sometimes-firing reflex teaches the model that silence means "no
memory"); the payload is identical regardless of the prompt's words and of what memory holds; it is content-free
and writes nothing; it stays under its tested character ceiling, because it is injected every turn and
`additionalContext` persists in history; it names an operation that actually exists; and it stays genuinely
near-zero — no subprocess, no store open, no path that grows with memory. Fail-open and the inert-seam
(no memory module -> silent) cases are pinned too.
"""

import inspect
import io
import json as _json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scent  # noqa: E402
import validate  # noqa: E402
from memory import index, ledger, records  # noqa: E402


def _inject(decision):
    """The text a handler decision injects, or None when it stays silent (proceed)."""
    if isinstance(decision, dict) and decision.get("action") == "inject":
        return decision.get("context", "")
    return None


def _run(prompt, session_id="s"):
    return _inject(scent.handler({"prompt": prompt, "session_id": session_id}))


# Two prompts with no word in common, NEITHER of which mentions anything past. The second is the shape the
# old keyword-matching pointer was structurally blind to: an instruction a stored preference may contradict.
_PROMPT_A = "should we use a nightly cron job for this?"
_PROMPT_B = "make the welcome copy longer"


class FiresEveryPromptTests(unittest.TestCase):
    """THE law of this slice. The old seam fired only on a strong keyword match, which made it silent on exactly
    the reworded and forward-looking prompts recall keeps failing. Any reintroduction of a firing condition —
    a keyword gate, a salience bar, a once-per-session dedup — turns one of these red."""

    def test_the_same_prompt_twice_gets_the_cue_both_times(self):
        first, second = _run(_PROMPT_A), _run(_PROMPT_A)
        self.assertIsNotNone(first)
        self.assertEqual(first, second, "a per-session dedup would make the reflex fire once and stop")

    def test_two_prompts_sharing_no_words_get_the_identical_cue(self):
        self.assertEqual(set(_PROMPT_A.split()) & set(_PROMPT_B.split()), set(),
                         "the fixture must genuinely share no vocabulary for this to mean anything")
        self.assertEqual(_run(_PROMPT_A), _run(_PROMPT_B))

    def test_a_prompt_of_only_common_words_still_gets_the_cue(self):
        # The old seam went silent here (no distinctive term to match). Silence is what this slice removes.
        self.assertIsNotNone(_run("the and now to of"))

    def test_a_prompt_naming_nothing_past_still_gets_the_cue(self):
        # The highest-value case: the operator does not know memory holds the answer, so nothing in the words
        # signals a past. A trigger keyed on backward reference would miss it.
        self.assertIsNotNone(_run("make the welcome copy longer"))

    def test_no_prompt_is_silent(self):
        self.assertIsNone(_inject(scent.handler({"session_id": "s"})))
        self.assertIsNone(_inject(scent.handler({"prompt": "   ", "session_id": "s"})))
        self.assertIsNone(_inject(scent.handler({"prompt": None})))

    def test_a_malformed_payload_is_silent_not_a_crash(self):
        for bad in (None, [], "not-a-dict", 7):
            self.assertIsNone(_inject(scent.handler(bad)), f"payload {bad!r} raised or spoke")


class CueContentTests(unittest.TestCase):
    def test_the_cue_stays_under_its_tested_ceiling(self):
        # Not decoration: the cue rides EVERY prompt and additionalContext persists in history, so its length
        # is a standing per-turn cost. The bound is written as a LITERAL here, not as `scent._CUE_MAX_CHARS` —
        # comparing the module's constant against the module's own ceiling is satisfied by any edit that grows
        # both together, which is exactly the quiet growth this is meant to stop. Raising the real bound now
        # requires editing this line, where it shows up in review.
        self.assertLessEqual(len(scent._CUE), 400)
        self.assertLessEqual(scent._CUE_MAX_CHARS, 400, "the module's own ceiling may not exceed the pinned one")
        self.assertTrue(scent._CUE.strip(), "the per-prompt event may never thin to an empty payload")

    def test_the_cue_names_the_operation_that_carries_the_procedure(self):
        self.assertIn(scent._OPERATION, scent._CUE)

    def test_the_named_operation_actually_EXISTS(self):
        # The failure this catches is invisible otherwise: rename or move the runbook and the cue still fires
        # every turn, still reads correctly, and leads nowhere. No link check covers a string literal in a .py
        # file (engine/check/link-integrity targets **/*.md), so this assertion is the only thing that would.
        self.assertTrue(os.path.isfile(scent._OPERATION_FILE), scent._OPERATION_FILE)
        self.assertEqual(
            os.path.relpath(scent._OPERATION_FILE, os.path.dirname(validate.ENGINE_DIR)).replace(os.sep, "/"),
            scent._OPERATION,
            "the path in the cue and the path checked on disk must be the same one")

    def test_the_named_operation_CARRIES_what_the_cue_delegates_to_it(self):
        """The cue is one line; everything it stands on lives in the runbook it names. Three properties were
        deleted from this hook on the argument that the runbook already carries them — so they are pinned HERE,
        where a rewrite of that file goes red instead of silently dropping a disclosure:
          - the SAME widened trigger the cue leads with (a runbook that re-narrows to "leans on an earlier
            session" would undo the cue's whole marginal value one file later);
          - the verify-before-asserting property, which replaced this module's retired verify clause;
          - the verbatim-recoverable property, which replaced its retired completeness disclosure.
        """
        doc = validate.read(scent._OPERATION_FILE)
        # Scoped to the PURPOSE section, not the whole document. Whole-document matching is why the first
        # version of this test was nearly vacuous: five of its six phrases already appeared somewhere in the
        # narrow runbook it was written to reject — "already" in the title line, "preference" in an unrelated
        # role list — so a rewrite that re-narrowed the entry condition while leaving a phrase in a footnote
        # would still pass. The entry condition is what the cue delegates, so that is what is pinned.
        purpose = doc.split("## Purpose", 1)[-1].split("\n## ", 1)[0]
        for shape in ("already", "tried and rejected", "preference"):
            self.assertIn(shape, purpose,
                          f"the runbook's entry condition dropped the {shape!r} trigger the cue delegates")
        self.assertIn("points forwards", purpose,
                      "the runbook must be entered on a request that names no past, or the cue leads nowhere")
        self.assertIn("outrank", doc, "the canonical-outranks-memory property (the retired verify clause)")
        self.assertIn("exact wording", doc, "the verbatim-recoverable property (the retired completeness note)")
        self.assertIn("never instruction", doc, "recalled conversation must be framed as data, not directions")

    def test_the_cue_names_the_forward_looking_trigger_shapes(self):
        # The trigger is "may this project have already settled this", not "does this prompt mention the past".
        # Every backward-referencing shape already reaches recall through the engine-recall skill's own
        # description on both runtimes; these three are what nothing else catches.
        for shape in ("already decided", "already tried and rejected", "stated preference"):
            self.assertIn(shape, scent._CUE, f"the cue dropped the {shape!r} trigger")

    def test_the_cue_carries_no_engine_or_governance_vocabulary(self):
        # It is AI-facing, but it is also the most-repeated string the engine emits; keep it plain.
        for jargon in ("eADR", "guardrail", "validator", "suite", "gate"):
            self.assertNotIn(jargon, scent._CUE)


class ContentFreeAndSideEffectFreeTests(unittest.TestCase):
    """A throwaway ENGINE_MEMORY_DIR cabinet, so anything the handler *might* read or write lands there."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="engine-scent-test-")
        self._prev = os.environ.get(ledger.ENV_DIR)
        os.environ[ledger.ENV_DIR] = self.tmp
        self.bodies = ["we rejected the nightly cron job outright", "keep the welcome copy short"]
        now = int(time.time())
        for body, role, tags in ((self.bodies[0], "decision", ["scheduling", "cron"]),
                                 (self.bodies[1], "preference", ["onboarding", "copy"])):
            ledger.append({"ts": now, "role": role, "tags": tags, "text": body,
                           records.RECORD_ID_KEY: records.new_record_id()}, path=ledger.ledger_path())
        index.rebuild()

    def tearDown(self):
        if self._prev is None:
            os.environ.pop(ledger.ENV_DIR, None)
        else:
            os.environ[ledger.ENV_DIR] = self._prev
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_the_cue_is_identical_whether_or_not_memory_holds_the_answer(self):
        # A stored record answers _PROMPT_A directly. The cue must not vary — it is a reminder to look, never
        # a peek. If it ever varied with the store, it would be leaking retrieval signal into every turn.
        with_memory = _run(_PROMPT_A)
        shutil.rmtree(self.tmp, ignore_errors=True)
        os.makedirs(self.tmp, exist_ok=True)
        self.assertEqual(with_memory, _run(_PROMPT_A))

    def test_the_cue_quotes_nothing_that_was_stored(self):
        text = _run(_PROMPT_A) or ""
        self.assertTrue(text)
        for body in self.bodies:
            self.assertNotIn(body, text)
        for tag in ("scheduling", "cron", "onboarding"):
            self.assertNotIn(tag, text)

    def test_the_hook_appends_nothing_to_the_ledger(self):
        def count():
            return sum(1 for _ in ledger.iter_records(path=ledger.ledger_path()))
        before = count()
        _run(_PROMPT_A)
        self.assertEqual(count(), before)

    def test_source_has_no_write_or_reinforce_calls(self):
        src = "".join(inspect.getsource(fn) for fn in (scent.handler, scent._memory_installed))
        for forbidden in ("record_access", "ledger.append", "open("):
            self.assertNotIn(forbidden, src)


class NearZeroHotPathTests(unittest.TestCase):
    """The cost law, pinned mechanically rather than asserted in the docstring. This hook runs on every prompt
    in a fresh process, so nothing amortises: the moment it resolves the ledger's location it forks a
    `git rev-parse` (the ledger is shared across a clone's worktrees, so its path cannot be derived locally),
    and the moment it opens the index it pays a cost that grows with the store. Booby-trap both."""

    # Each of these asserts the expensive thing was NOT CALLED, rather than letting a raised trap escape the
    # handler. That distinction is the whole test: this codebase's idiom for reaching memory is a defensive
    # `try/except Exception` (the old handler used exactly that), which swallows a raised trap and leaves the
    # assertion passing while the cost is paid on every prompt.

    def test_the_handler_starts_no_subprocess(self):
        with mock.patch.object(subprocess, "run") as run, mock.patch.object(subprocess, "Popen") as popen:
            self.assertIsNotNone(_run(_PROMPT_A))
        self.assertFalse(run.called, "the hot path forked a subprocess (a git resolve costs ~9 ms per prompt)")
        self.assertFalse(popen.called, "the hot path forked a subprocess")

    def test_the_handler_never_resolves_the_store_or_opens_the_index(self):
        for module, name in ((ledger, "ledger_dir"), (ledger, "ledger_path"),
                             (ledger, "iter_records"), (index, "rebuild"), (index, "search"),
                             (index, "fts5_available"), (index, "index_path")):
            with mock.patch.object(module, name) as spy:
                self.assertIsNotNone(_run(_PROMPT_A))
            self.assertFalse(spy.called, f"the hot path called {name}, which grows with the store")

    def test_the_handler_neither_IMPORTS_memory_nor_writes_session_state(self):
        """Both laws, checked in a FRESH interpreter — the only place they are observable.

        `memory` is already in this test process's `sys.modules` (the module imports it at the top), so an
        in-process assertion could never see the import. And a source scan for `from memory import` is
        satisfied by `importlib.import_module("memory")`, which pays the whole chain — measured at ~500x the
        handler's cost. Likewise the no-session-state law: a temp-file write moved into any helper this test
        does not name is invisible to source inspection, so the subprocess runs with TMPDIR pointed at an empty
        directory and the law becomes "that directory is still empty afterwards".
        """
        # The watched surface is deliberately BOTH the temp dir and the whole engine tree. Watching only the
        # temp dir defends the place the OLD dedup store happened to live, not the place a new one would: a
        # helper writing `.engine/state/scent_seen.json` is a declared surface class in this codebase and is
        # exactly where such a store would land. Snapshot everything and compare.
        probe = (
            "import json, os, sys\n"
            f"sys.path.insert(0, {os.path.dirname(os.path.abspath(scent.__file__))!r})\n"
            "import tempfile\n"
            "import scent, validate\n"
            # Records each file's SIZE, not just its name. Watching names alone misses the realistic shape of
            # the regression: a store at a path that already exists (an earlier test in this same run, or a
            # previous session) is APPENDED to, creating nothing new and leaving a name-only diff empty.
            "def snap():\n"
            "    seen = {}\n"
            "    for root in (tempfile.gettempdir(), validate.ENGINE_DIR):\n"
            "        for base, _dirs, files in os.walk(root):\n"
            "            for f in files:\n"
            "                p = os.path.join(base, f)\n"
            "                try:\n"
            "                    seen[p] = os.path.getsize(p)\n"
            "                except OSError:\n"
            "                    pass\n"
            "    return seen\n"
            "before = snap()\n"
            "scent.handler({'prompt': 'should we use a nightly cron job?', 'session_id': 's'})\n"
            "scent.handler({'prompt': 'a different prompt entirely', 'session_id': 's'})\n"
            "scent.handler({'prompt': 'should we use a nightly cron job?', 'session_id': 't'})\n"
            "after = snap()\n"
            "touched = [p for p, size in after.items() if before.get(p) != size]\n"
            "print(json.dumps({'imported': 'memory' in sys.modules, 'created': sorted(touched)}))\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            env = {**os.environ, "TMPDIR": tmp, "TMP": tmp, "TEMP": tmp}
            out = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True,
                                 env=env, check=True).stdout
        result = _json.loads(out.strip().splitlines()[-1])
        self.assertFalse(result["imported"],
                         "the hot path imported the memory package; find_spec must only LOCATE it")
        created = [p for p in result["created"] if not p.endswith((".pyc", ".pyo"))]
        self.assertEqual(created, [],
                         "the hot path wrote session state; this seam keeps none (a dedup store would make "
                         "the reflex fire once and stop)")


class InertSeamTests(unittest.TestCase):
    def test_no_memory_module_is_silent(self):
        with mock.patch.object(scent, "_memory_installed", return_value=False):
            self.assertIsNone(_run(_PROMPT_A))

    def test_an_unreadable_path_entry_reads_as_absent_rather_than_crashing(self):
        with mock.patch.object(scent.importlib.util, "find_spec", side_effect=ValueError("odd path entry")):
            self.assertFalse(scent._memory_installed())
            self.assertIsNone(_run(_PROMPT_A))

    def test_the_real_gate_finds_the_memory_module_in_this_repo(self):
        # Non-vacuity for the two tests above: the gate must be capable of returning True, or they prove
        # nothing but that a stubbed-out seam is silent.
        self.assertTrue(scent._memory_installed())


class FailOpenTests(unittest.TestCase):
    def test_a_crash_in_the_handler_fails_open(self):
        with mock.patch.object(scent, "_memory_installed", side_effect=RuntimeError("boom")):
            out, err = io.StringIO(), io.StringIO()
            code = scent.hooks.run_hook(
                "UserPromptSubmit", scent.handler,
                stdin=io.StringIO(_json.dumps({"prompt": _PROMPT_A, "session_id": "s"})),
                stdout=out, stderr=err)
        self.assertNotEqual(code, 2)                  # never a hard block
        self.assertEqual(out.getvalue().strip(), "")  # injected nothing

    def test_the_wired_hook_path_injects_the_cue(self):
        # End-to-end through run_hook, the way the wired hook actually runs — not just the handler in isolation.
        out, err = io.StringIO(), io.StringIO()
        code = scent.hooks.run_hook(
            "UserPromptSubmit", scent.handler,
            stdin=io.StringIO(_json.dumps({"prompt": _PROMPT_A, "session_id": "s"})),
            stdout=out, stderr=err)
        self.assertEqual(code, 0)
        self.assertIn(scent._OPERATION, out.getvalue())


class DemoTests(unittest.TestCase):
    def test_demo_passes(self):
        from quiet_call import run as quiet_run
        self.assertEqual(quiet_run(scent.main, ["demo"]), 0)

    def test_demo_can_fail(self):
        # A demonstration that cannot fail is not evidence. Break the law it demonstrates — the cue firing on
        # every prompt — and the demo must notice and exit non-zero.
        from quiet_call import run as quiet_run
        calls = {"n": 0}

        def sometimes(payload):
            calls["n"] += 1
            return scent.hooks.proceed() if calls["n"] % 2 == 0 else scent.hooks.inject(scent._CUE)

        with mock.patch.object(scent, "handler", side_effect=sometimes):
            self.assertEqual(quiet_run(scent.main, ["demo"]), 1)


if __name__ == "__main__":
    unittest.main()
