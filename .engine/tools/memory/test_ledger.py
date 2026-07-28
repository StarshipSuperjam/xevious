"""Self-tests for the memory ledger — the canonical store's integrity machinery.

Run by the checker-of-checkers self-test suite:
  uv run --directory .engine --frozen -- python -m unittest discover -s tools -p 'test_*.py' -b

These assert the locked ledger-integrity law: serialized writes (no torn lines under concurrency),
line-resilient reads (skip+count malformed, drop a torn trailing record, ignore blank lines), and the
ledger-before-hooks safety property (the close turn-hook still no-ops while capture is unbuilt).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # .engine/tools on path

from memory import ledger  # noqa: E402  (package-qualified import; .engine/tools is on sys.path)


class LedgerRoundTripTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self._tmp.name, "ledger.ndjson")

    def tearDown(self):
        self._tmp.cleanup()

    def test_append_then_read_roundtrip(self):
        records = [
            {"role": "decision", "body": "chose the ledger model"},
            {"role": "lesson", "body": "serialize the writes"},
            {"role": "observation", "body": "三 unicode and \"quotes\" survive"},
        ]
        for rec in records:
            ledger.append(rec, path=self.path)
        result = ledger.read(path=self.path)
        self.assertEqual(result.records, records)
        self.assertEqual(result.malformed, 0)
        self.assertFalse(result.torn_trailing)

    def test_missing_ledger_reads_empty(self):
        result = ledger.read(path=os.path.join(self._tmp.name, "absent.ndjson"))
        self.assertEqual(result.records, [])
        self.assertEqual(result.malformed, 0)
        self.assertFalse(result.torn_trailing)

    def test_empty_file_reads_empty(self):
        open(self.path, "w").close()
        result = ledger.read(path=self.path)
        self.assertEqual(result.records, [])
        self.assertEqual(result.malformed, 0)
        self.assertFalse(result.torn_trailing)

    def test_iter_records_matches_read(self):
        for i in range(3):
            ledger.append({"role": "intent", "n": i}, path=self.path)
        self.assertEqual(list(ledger.iter_records(path=self.path)),
                         ledger.read(path=self.path).records)


class LedgerResilienceTests(unittest.TestCase):
    """The line-resilient read law: one bad line never costs the records around it."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self._tmp.name, "ledger.ndjson")

    def tearDown(self):
        self._tmp.cleanup()

    def _raw_append(self, text: str) -> None:
        """Append raw bytes, bypassing the framing — to simulate corruption / a torn write."""
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(text)

    def test_malformed_line_skipped_and_counted(self):
        ledger.append({"role": "decision", "body": "first"}, path=self.path)
        self._raw_append("this is not json at all\n")
        ledger.append({"role": "lesson", "body": "after the garbage"}, path=self.path)
        result = ledger.read(path=self.path)
        self.assertEqual([r["body"] for r in result.records], ["first", "after the garbage"])
        self.assertEqual(result.malformed, 1)
        self.assertFalse(result.torn_trailing)

    def test_torn_trailing_record_dropped_good_records_survive(self):
        ledger.append({"role": "decision", "body": "DO NOT LOSE THIS"}, path=self.path)
        # A crash mid-append leaves a record with no terminating newline.
        self._raw_append('{"role":"lesson","body":"half-written, no newline"')
        result = ledger.read(path=self.path)
        self.assertEqual([r["body"] for r in result.records], ["DO NOT LOSE THIS"])
        self.assertTrue(result.torn_trailing)
        self.assertEqual(result.malformed, 0)

    def test_torn_raw_captures_the_exact_trailing_bytes(self):
        """#396 U07a: a torn trailing fragment's exact bytes are captured (torn_raw), so a rewriter
        (compaction) can re-emit it verbatim instead of erasing a healable tail."""
        ledger.append({"role": "decision", "body": "kept"}, path=self.path)
        self._raw_append('{"role":"lesson","body":"half-written, no newline"')  # no terminator
        result = ledger.read(path=self.path)
        self.assertTrue(result.torn_trailing)
        self.assertEqual(result.torn_raw, b'{"role":"lesson","body":"half-written, no newline"')

    def test_torn_raw_is_none_on_a_clean_ledger(self):
        ledger.append({"role": "decision", "body": "clean"}, path=self.path)
        result = ledger.read(path=self.path)
        self.assertFalse(result.torn_trailing)
        self.assertIsNone(result.torn_raw)

    def test_torn_raw_preserves_raw_bytes_of_a_multibyte_split(self):
        """A crash can split a multibyte UTF-8 character; torn_raw must carry the EXACT bytes, not the
        errors='replace' decoding (which would mangle them and change what a later append heals)."""
        ledger.append({"role": "decision", "body": "kept"}, path=self.path)
        with open(self.path, "ab") as fh:
            fh.write(b'{"role":"lesson","body":"\xe4\xb8')  # JSON prefix then a split 3-byte char, no newline
        result = ledger.read(path=self.path)
        self.assertTrue(result.torn_trailing)
        self.assertEqual(result.torn_raw, b'{"role":"lesson","body":"\xe4\xb8')

    def test_append_heals_a_torn_fragment_so_the_next_record_survives(self):
        """The write-side heal: a crash mid-write leaves a torn fragment (no newline); a LATER append
        first repairs the missing terminator, isolating the fragment as its own malformed line so the
        next record lands clean and SURVIVES. (An earlier build documented the OLD behavior — fusing the fragment
        onto the next record and silently losing it — as a tolerated residual; this upholds the read law
        'one bad line never costs the records after it' on the write side.)"""
        ledger.append({"role": "decision", "body": "A survives"}, path=self.path)
        self._raw_append('{"role":"lesson","body":"torn fragment')  # no newline (crash mid-write)
        ledger.append({"role": "lesson", "body": "B after the crash"}, path=self.path)
        result = ledger.read(path=self.path)
        self.assertEqual([r["body"] for r in result.records], ["A survives", "B after the crash"])
        self.assertEqual(result.malformed, 1)  # the isolated torn fragment, on its own line
        self.assertFalse(result.torn_trailing)

    def test_heal_recovers_a_complete_but_unterminated_prior_record(self):
        """A prior record that was complete VALID JSON but lost only its terminating newline to a crash
        is recovered by the next append's heal (favor-content): the terminator is added so it parses.
        A truly half-written fragment is NOT resurrected — see the next test."""
        self._raw_append('{"role":"decision","body":"COMPLETE no newline"}')  # valid JSON, no newline
        ledger.append({"role": "lesson", "body": "next"}, path=self.path)
        result = ledger.read(path=self.path)
        self.assertEqual([r["body"] for r in result.records], ["COMPLETE no newline", "next"])
        self.assertEqual(result.malformed, 0)

    def test_heal_never_resurrects_a_truly_torn_fragment(self):
        """The heal only adds a terminator; it never turns garbage into a record. A fragment torn
        MID-JSON stays invalid and is read back as malformed, even after the heal isolates it."""
        self._raw_append('{"role":"decision","body":"torn mid-jso')  # invalid JSON, no newline
        ledger.append({"role": "lesson", "body": "next"}, path=self.path)
        result = ledger.read(path=self.path)
        self.assertEqual([r["body"] for r in result.records], ["next"])
        self.assertEqual(result.malformed, 1)  # the torn fragment stays malformed, never a record

    def test_heal_is_a_noop_on_a_clean_file(self):
        """The heal fires ONLY on a missing terminator: normal appends never insert a stray newline,
        and the first append on an empty file gets no leading newline."""
        for i in range(3):
            ledger.append({"n": i}, path=self.path)
        with open(self.path, "rb") as fh:
            raw = fh.read()
        self.assertNotIn(b"\n\n", raw)                 # no stray blank line between clean records
        self.assertFalse(raw.startswith(b"\n"))        # the first append got no leading newline
        result = ledger.read(path=self.path)
        self.assertEqual([r["n"] for r in result.records], [0, 1, 2])
        self.assertEqual(result.malformed, 0)

    def test_blank_lines_are_not_malformed(self):
        ledger.append({"role": "intent", "body": "one"}, path=self.path)
        self._raw_append("\n   \n")
        ledger.append({"role": "intent", "body": "two"}, path=self.path)
        result = ledger.read(path=self.path)
        self.assertEqual([r["body"] for r in result.records], ["one", "two"])
        self.assertEqual(result.malformed, 0)

    def test_non_utf8_byte_does_not_crash_the_read(self):
        ledger.append({"role": "decision", "body": "before"}, path=self.path)
        with open(self.path, "ab") as fh:
            fh.write(b"\xff\xfe not valid utf-8\n")
        ledger.append({"role": "decision", "body": "after"}, path=self.path)
        result = ledger.read(path=self.path)
        self.assertEqual([r["body"] for r in result.records], ["before", "after"])
        self.assertEqual(result.malformed, 1)


class LedgerSerializationTests(unittest.TestCase):
    """Serialized writes — the integrity law. Two tests: an OUTCOME smoke test (many concurrent
    appends are all readable), and a LOCK-ISOLATION test that proves the exclusive lock is the thing
    preventing a tear (it goes red if the lock is removed)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self._tmp.name, "ledger.ndjson")

    def tearDown(self):
        self._tmp.cleanup()

    def test_concurrent_appends_outcome_is_readable(self):
        """Outcome smoke test: under many concurrent writers, every appended record is read back
        intact. (For a single-syscall-sized record this outcome holds even without the lock because
        an O_APPEND write is atomic; the lock's specific role is proven by the next test.)"""
        writers, per_writer, payload_len = 8, 12, 9000

        def write_many(writer_id: int) -> None:
            body = f"W{writer_id}-START-" + ("x" * payload_len) + f"-END-W{writer_id}"
            for seq in range(per_writer):
                ledger.append({"writer": writer_id, "seq": seq, "body": body}, path=self.path)

        threads = [threading.Thread(target=write_many, args=(w,)) for w in range(writers)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        result = ledger.read(path=self.path)
        self.assertEqual(result.malformed, 0)
        self.assertFalse(result.torn_trailing)
        self.assertEqual(len(result.records), writers * per_writer)
        for rec in result.records:
            self.assertTrue(rec["body"].startswith(f"W{rec['writer']}-START-"))
            self.assertTrue(rec["body"].endswith(f"-END-W{rec['writer']}"))
        self.assertEqual(len({(r["writer"], r["seq"]) for r in result.records}), writers * per_writer)

    def test_exclusive_lock_prevents_torn_interleave(self):
        """The exclusive lock is load-bearing. We force the multi-syscall write path (the only path
        where the lock matters) by making os.write emit small chunks with a yield between them — this
        widens the interleave window. Run the SAME chunked write loop two ways: a lockless variant
        (which then tears) and the real ledger.append (which holds the lock across the whole record).
        The only difference is the lock, so an intact locked result + a torn lockless result proves
        the lock specifically is working. If the lock were removed from append(), this test goes red."""
        writers, per_writer = 6, 8
        big = "Z" * 4000

        def lockless_append(record: dict, path: str) -> None:
            line = (json.dumps(record) + "\n").encode("utf-8")
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
            try:
                view = memoryview(line)
                while view:
                    view = view[os.write(fd, view):]
            finally:
                os.close(fd)

        def run(append_fn, path: str) -> None:
            def work(writer_id: int) -> None:
                for seq in range(per_writer):
                    rec = {"writer": writer_id, "seq": seq, "body": f"<{writer_id}.{seq}-{big}-{writer_id}.{seq}>"}
                    append_fn(rec, path)
            threads = [threading.Thread(target=work, args=(w,)) for w in range(writers)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        lockless_path = os.path.join(self._tmp.name, "lockless.ndjson")
        locked_path = os.path.join(self._tmp.name, "locked.ndjson")
        real_os_write = os.write

        def chunked_write(fd, data):
            n = real_os_write(fd, bytes(data)[:128])  # write a small chunk...
            time.sleep(0)                              # ...then yield, widening the race window
            return n

        os.write = chunked_write  # force the multi-syscall path for BOTH variants
        try:
            run(lambda r, p: lockless_append(r, p), lockless_path)
            run(lambda r, p: ledger.append(r, path=p), locked_path)
        finally:
            os.write = real_os_write

        expected = writers * per_writer
        lockless = ledger.read(path=lockless_path)
        locked = ledger.read(path=locked_path)
        # WITH the lock: every record intact, none corrupted.
        self.assertEqual(locked.malformed, 0)
        self.assertEqual(len(locked.records), expected)
        # Guard against a false pass: the lockless writers must have actually run and produced bytes
        # (an errored, empty arm would otherwise look like "total loss" and pass spuriously).
        self.assertGreater(os.path.getsize(lockless_path), 0, "the lockless arm wrote nothing — it errored rather than tore")
        # WITHOUT the lock (same chunked write loop): the interleave tears lines — the guard bites.
        lockless_loss = lockless.malformed + (expected - len(lockless.records))
        self.assertGreater(
            lockless_loss, 0,
            "the lockless variant did not tear, so this test no longer exercises the lock — "
            "fix the test (widen the window) before trusting the locked result",
        )

    def test_heal_of_a_torn_tail_is_serialized_under_concurrency(self):
        """The heal runs UNDER the exclusive lock, so when many writers hit a torn-trailing file at
        once, exactly ONE healing newline lands (never a double-heal) and every record survives. If the
        heal moved outside the lock, concurrent writers would each prepend a newline — producing
        blank-line runs and/or a fused record."""
        with open(self.path, "a", encoding="utf-8") as fh:   # pre-seed a torn fragment (crash, no newline)
            fh.write('{"role":"decision","body":"torn tail from a crash')
        writers, per_writer = 8, 6
        big = "Q" * 4000

        def work(writer_id: int) -> None:
            for seq in range(per_writer):
                ledger.append({"writer": writer_id, "seq": seq, "body": f"<{writer_id}.{seq}-{big}>"},
                              path=self.path)

        threads = [threading.Thread(target=work, args=(w,)) for w in range(writers)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        with open(self.path, "rb") as fh:
            raw = fh.read()
        result = ledger.read(path=self.path)
        self.assertEqual(len(result.records), writers * per_writer)   # every record survived
        self.assertEqual(result.malformed, 1)                          # the torn fragment, isolated ONCE
        self.assertNotIn(b"\n\n", raw)                                 # no double-heal blank-line run
        self.assertFalse(result.torn_trailing)


class CaptureSeamSafetyTests(unittest.TestCase):
    """The close turn-hook's ambient-capture relay is now LIVE: capture lands the turn delta. The
    relay does `import memory; memory.capture_turn_delta(payload)`, so capture must be exposed on the
    package AND must be fail-soft — a bad/empty payload is a clean no-op return, never a raise, so
    capture can never gate close (which additionally wraps the call in `try/except Exception`)."""

    def test_memory_exposes_capture_turn_delta(self):
        import memory
        self.assertTrue(
            hasattr(memory, "capture_turn_delta") and callable(memory.capture_turn_delta),
            "capture exposes capture_turn_delta on the memory package; close's relay calls it",
        )

    def test_capture_call_is_fail_soft_on_a_bad_or_empty_payload(self):
        import memory
        # No transcript / not even a dict -> a clean no-op return (0 appended), NEVER a raise.
        self.assertEqual(memory.capture_turn_delta({"session_id": "x"}), 0)
        self.assertEqual(memory.capture_turn_delta({}), 0)
        self.assertEqual(memory.capture_turn_delta(None), 0)


class LedgerPathResolutionTests(unittest.TestCase):
    """The ledger is ONE store shared by every git worktree of a clone (the engine's AI sessions each
    work in their own worktree, but the project's memory is one file). These guard that promise — a
    regression that resolved the ledger per-worktree would silently fragment recall."""

    def setUp(self):
        self._saved_env = os.environ.pop(ledger.ENV_DIR, None)
        self._tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        os.environ.pop(ledger.ENV_DIR, None)
        if self._saved_env is not None:
            os.environ[ledger.ENV_DIR] = self._saved_env
        self._tmp.cleanup()

    def test_env_override_wins(self):
        os.environ[ledger.ENV_DIR] = self._tmp.name
        self.assertEqual(ledger.ledger_path(), os.path.join(self._tmp.name, ledger.LEDGER_FILENAME))

    def test_cwd_fallback_when_not_in_a_git_repo(self):
        # A directory that is not a git repo → no common root → CWD-relative fallback.
        self.assertEqual(
            ledger.ledger_dir(cwd=self._tmp.name),
            os.path.join(self._tmp.name, ledger.DATA_SUBDIR),
        )

    def test_worktrees_share_one_ledger_at_the_clone_root(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        env.pop(ledger.ENV_DIR, None)

        def git(*args, cwd):
            subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, env=env)

        clone = os.path.join(self._tmp.name, "clone")
        os.makedirs(clone)
        git("init", "-q", cwd=clone)
        open(os.path.join(clone, "seed"), "w").close()
        git("add", "seed", cwd=clone)
        git("-c", "user.email=t@e", "-c", "user.name=t", "commit", "-qm", "seed", cwd=clone)
        worktree = os.path.join(self._tmp.name, "wt")
        git("worktree", "add", "-q", worktree, "-b", "feature", cwd=clone)

        clone_ledger = os.path.realpath(os.path.join(clone, ledger.DATA_SUBDIR))
        # From the linked worktree AND from the main checkout, the ledger resolves to the SAME dir
        # at the clone root — not a per-worktree path.
        self.assertEqual(os.path.realpath(ledger.ledger_dir(cwd=worktree)), clone_ledger)
        self.assertEqual(os.path.realpath(ledger.ledger_dir(cwd=clone)), clone_ledger)


class GenerationStampTests(unittest.TestCase):
    """The generation stamp + its explicit setter (the restore path). `set_generation` writes the BACKUP's true
    generation onto a restored ledger — including a BACKWARD move (an older backup) — durably and fail-safe."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._led = os.path.join(self._tmp.name, ledger.LEDGER_FILENAME)

    def tearDown(self):
        self._tmp.cleanup()

    def test_set_generation_roundtrips(self):
        ledger.set_generation(7, for_path=self._led)
        self.assertEqual(ledger.generation(for_path=self._led), 7)

    def test_set_generation_allows_a_backward_move(self):
        ledger.set_generation(10, for_path=self._led)
        self.assertEqual(ledger.generation(for_path=self._led), 10)
        ledger.set_generation(5, for_path=self._led)                # an older backup lands a LOWER generation
        self.assertEqual(ledger.generation(for_path=self._led), 5)

    def test_set_generation_clamps_a_bad_value_to_zero(self):
        for bad in (-3, True, "x", None):
            self.assertEqual(ledger.set_generation(bad, for_path=self._led), 0)
            self.assertEqual(ledger.generation(for_path=self._led), 0)


class DemoFailureBranchTests(unittest.TestCase):
    """`_demo`'s failure branch writes to `sys.stderr`. `sys` must be imported at module scope so that branch
    reports instead of raising NameError when `_demo` is reached off the `__main__` path (e.g. via a dispatcher
    or test). This pins the module-scope import that keeps that branch working."""

    def test_sys_is_module_scope(self):
        self.assertIs(ledger.sys, sys)


if __name__ == "__main__":
    unittest.main()
