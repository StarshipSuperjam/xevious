#!/usr/bin/env python3
"""Run the full self-test suite once, legibly — the canonical local full-suite launcher.

Why this exists: the prescribed serial `unittest discover -s tools -p 'test_*.py' -b` run is hard to
read while it happens and hard to trust when it ends. It can run for minutes while `-b` buffers every
test's output, so a session polls the process guessing whether it hung; a demo that reads stdin blocks
outright under an attached terminal; and when the run finally prints, the result is routinely piped
through `tail`/`grep`, which truncates the very tracebacks the session needs — forcing a re-run. This
launcher wraps the SAME proven run (same discovery, order, buffering, test set) and makes one run
enough: it announces progress on a heartbeat so a live run is never mistaken for a hang, forces the
child's stdin to end-of-input so no demo can block, and writes the full output to a log while printing
a clean, self-contained result — the complete list of failing tests plus their tracebacks — so nothing
needs to be piped and nothing needs to be re-run.

The log is a UNIQUE per-run temp file (so concurrent sessions — this repo's normal model — never share
or clobber one) and is KEPT whether the run passes or fails, so a session can always read its own run
rather than mistaking a vanished log for a failure; its path is printed at the start and again in the
closing summary. Cleanup is the daily sweep — a later run removes any of this user's logs older than a
day. The on-screen result is built from the run's own in-memory output, never by re-reading the file, so
a concurrent run can never make it show the wrong failures.

The load-bearing invariant: the launcher's exit status is the child suite's exit status, VERBATIM
(`proc.returncode`). It is NEVER derived from parsing the run's text for an `OK`/`FAILED` line. A test
that errors at import/collection time, or a child that is killed or crashes, exits non-zero without a
tidy summary — and must still surface as a failure, never a false green. The displayed summary is the
child's own words; only the display is textual, never the verdict.

How progress is read without scraping human output: the child runs discovery in-process under a custom
`TestResult` (with `buffer=True`, matching `-b`) that emits STRUCTURED events (a total, then one per
test completion) on a dedicated pipe. The parent reads that pipe for the heartbeat and never parses the
`-v` stream, whose format carries docstrings instead of ids and shifts across Python versions.

How draining stays deadlock- and hang-free: the parent drives ONE non-blocking `selectors` loop that
reads the child's combined output and the progress pipe as data arrives, firing the heartbeat on a
wall-clock tick. It never blocks in a read, so a full OS pipe can't stall it; and once the child exits
it drains only what is already buffered (a bounded, non-blocking sweep) rather than waiting on the pipe
to reach end-of-file — which a background grandchild process the child spawned could otherwise hold
open forever. That single-reader model is why teardown can never hang.

Layering: `quiet_call.py` silences one demo's stdout in-process at a single call site; this supervises
the whole run at the process level. Different layers — neither replaces the other. CI stays on the raw
`unittest discover ... -b` command (the merge gate is unchanged); this is the local build path.

Usage:
    uv run --directory .engine --frozen -- python tools/selftest.py

The flags exist for the regression fixture (test_selftest.py), which drives the launcher against tiny
synthetic suites in a temp directory with a millisecond heartbeat; a normal run needs none of them.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import selectors
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from typing import Optional

# Shared with release_gate.py: set on every nested in-process suite spawn so a suite run can never
# re-enter the real full-suite target from inside itself.
_NESTED_ENV = "ENGINE_NESTED_SELFTEST"

# tools/ sits directly under .engine/ — the canonical run is `discover -s tools` from `.engine`.
_ENGINE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_DEFAULT_START_DIR = "tools"
_DEFAULT_PATTERN = "test_*.py"
_DEFAULT_HEARTBEAT_S = 30.0
_DEFAULT_STALL_S = 30.0
_POLL_INTERVAL_S = 0.25  # re-check child exit at least this often, independent of the heartbeat cadence
_MAX_SHOWN_TRACEBACKS = 12   # cap on inline tracebacks; the full log always holds every one
_TAIL_LINES = 80             # fallback echo when a failure produced no standard failure block


_LOG_PREFIX = "engine-selftest-"
_LOG_MAX_AGE_S = 86400  # sweep run logs older than a day


def _log_user() -> str:
    try:
        return getpass.getuser()
    except Exception:
        return str(os.getuid()) if hasattr(os, "getuid") else "user"


def _user_log_prefix() -> str:
    """The temp-log name prefix for THIS user — both the minter and the sweeper key on it, so the sweep
    only ever touches this user's own logs, never another user's on a shared temp dir."""
    return f"{_LOG_PREFIX}{_log_user()}-"


def _open_run_log(explicit: Optional[str]):
    """Open the run log. A caller-supplied path (the fixture) is honoured verbatim; otherwise a UNIQUE
    per-run file is minted with `mkstemp` — 0600 and O_EXCL, so concurrent runs by the same user never
    collide (this repo runs many sessions as one user) and no other user can read it or pre-plant the
    name as a symlink. Returns (path, open file); raises OSError only for an unwritable explicit path."""
    if explicit:
        return explicit, open(explicit, "w", encoding="utf-8", errors="replace")
    fd, path = tempfile.mkstemp(prefix=_user_log_prefix(), suffix=".log")
    return path, os.fdopen(fd, "w", encoding="utf-8", errors="replace")


def _sweep_stale_logs() -> None:
    """Best-effort removal of THIS user's own leftover run logs older than a day. Never raises — a
    cleanup that fails must not fail the run."""
    try:
        tmp = tempfile.gettempdir()
        prefix = _user_log_prefix()
        now = time.time()
        for name in os.listdir(tmp):
            if name.startswith(prefix) and name.endswith(".log"):
                path = os.path.join(tmp, name)
                try:
                    if now - os.path.getmtime(path) > _LOG_MAX_AGE_S:
                        os.remove(path)
                except OSError:
                    pass
    except OSError:
        pass


# --------------------------------------------------------------------------------------------------
# Child mode — discover and run in-process, emitting structured progress on a dedicated fd.
# --------------------------------------------------------------------------------------------------


class _StructuredResult(unittest.TextTestResult):
    """A normal buffered result (so failing-test output is replayed and passing noise is discarded,
    exactly like `-b`) that ALSO writes one structured JSON line per test completion to a progress
    fd. The verdict still comes from the base class's `wasSuccessful()`, never from what we emit."""

    def __init__(self, *args, progress_write=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._progress_write = progress_write
        self._completed = 0

    def _emit(self, payload: dict) -> None:
        if self._progress_write is None:
            return
        try:
            self._progress_write.write(json.dumps(payload) + "\n")
            self._progress_write.flush()
        except (ValueError, OSError):
            # A broken progress pipe (parent gone) must never break the run itself.
            self._progress_write = None

    def startTest(self, test):
        super().startTest(test)
        self._emit({"event": "start", "id": str(test)})

    def stopTest(self, test):
        super().stopTest(test)
        self._completed += 1
        self._emit({"event": "stop", "completed": self._completed, "id": str(test)})


def _run_child(args: argparse.Namespace) -> int:
    """Discover and run in-process; return 0 on success, 1 otherwise. A discovery/import failure is
    surfaced as a non-zero exit, never swallowed."""
    progress_write = None
    if args.progress_fd is not None and args.progress_fd >= 0:
        progress_write = os.fdopen(args.progress_fd, "w", buffering=1)

    loader = unittest.TestLoader()
    try:
        suite = loader.discover(start_dir=args.start_dir, pattern=args.pattern)
    except Exception as exc:  # pragma: no cover - defensive; discover usually defers import errors
        if progress_write is not None:
            try:
                progress_write.write(json.dumps({"event": "total", "total": 0}) + "\n")
                progress_write.flush()
            except (ValueError, OSError):
                pass
        print(f"selftest: discovery failed: {exc!r}", file=sys.stderr)
        return 1

    total = suite.countTestCases()
    if progress_write is not None:
        try:
            progress_write.write(json.dumps({"event": "total", "total": total}) + "\n")
            progress_write.flush()
        except (ValueError, OSError):
            progress_write = None

    def _factory(stream, descriptions, verbosity):
        return _StructuredResult(stream, descriptions, verbosity, progress_write=progress_write)

    runner = unittest.TextTestRunner(stream=sys.stderr, verbosity=1, buffer=True, resultclass=_factory)
    try:
        result = runner.run(suite)
    finally:
        if progress_write is not None:
            try:
                progress_write.close()
            except OSError:
                pass
    return 0 if result.wasSuccessful() else 1


# --------------------------------------------------------------------------------------------------
# Parent mode — spawn the child, drain non-blocking, heartbeat on a timer, propagate exit verbatim.
# --------------------------------------------------------------------------------------------------


class _Progress:
    """The child's progress, updated by the single reader loop and read by the heartbeat. Single
    reader → no lock needed."""

    def __init__(self) -> None:
        self.total: Optional[int] = None
        self.completed = 0
        self.current: Optional[str] = None
        self.last_completion = time.monotonic()

    def apply(self, line: str) -> None:
        line = line.strip()
        if not line:
            return
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return
        kind = event.get("event")
        if kind == "total":
            self.total = int(event.get("total", 0))
        elif kind == "start":
            self.current = str(event.get("id", ""))
        elif kind == "stop":
            self.completed = int(event.get("completed", 0))
            self.last_completion = time.monotonic()

    def snapshot(self, now: float) -> dict:
        return {"total": self.total, "completed": self.completed,
                "current": self.current, "since_last": now - self.last_completion}


def _heartbeat_line(snap: dict, elapsed: float, stall_threshold: float) -> str:
    total_s = str(snap["total"]) if snap["total"] is not None else "?"
    current = snap["current"] or "(starting)"
    line = (f"  … {snap['completed']}/{total_s} tests  |  {elapsed:0.0f}s elapsed  |  "
            f"{snap['since_last']:0.0f}s since last completion  |  now: {current}")
    if snap["since_last"] > stall_threshold:
        # Can't tell a slow-but-alive test from a stall without mid-test signal — say so honestly,
        # and name the test in hand (above) so the reader can judge.
        line += "  |  ⚠ slow or possibly stalled"
    return line


def _extract_failure_blocks(lines: list) -> list:
    """Pull each `FAIL:`/`ERROR:` block (id + traceback) out of the child's unittest output. Display
    only — the verdict never depends on this."""
    def is_sep(s: str) -> bool:
        t = s.strip()
        return len(t) >= 20 and set(t) == {"="}

    def is_dash(s: str) -> bool:
        t = s.strip()
        return len(t) >= 20 and set(t) == {"-"}

    blocks = []
    i, n = 0, len(lines)
    while i < n:
        if is_sep(lines[i]) and i + 1 < n and (lines[i + 1].startswith("FAIL:") or lines[i + 1].startswith("ERROR:")):
            header = lines[i + 1].strip()
            block = [lines[i + 1]]
            j = i + 2
            while j < n:
                # End only at a real NEXT block or the final summary trailer — a stray banner of `=`
                # or `-` inside a traceback's own text must not cut the block short.
                if is_sep(lines[j]) and j + 1 < n and (lines[j + 1].startswith("FAIL:") or lines[j + 1].startswith("ERROR:")):
                    break
                if is_dash(lines[j]) and j + 1 < n and lines[j + 1].startswith("Ran "):
                    break
                block.append(lines[j])
                j += 1
            blocks.append((header, block))
            i = j
        else:
            i += 1
    return blocks


def _print_result(rc: int, elapsed: float, log_path: Optional[str], output: str) -> None:
    print()
    print("=" * 78)
    verdict = "PASSED" if rc == 0 else f"FAILED (exit {rc})"
    print(f"Self-tests {verdict} in {elapsed:0.0f}s")
    if log_path:  # always set now — the log is kept for every run, and its path is printed so the session can read it
        print(f"Full output: {log_path}")
    if rc == 0:
        print("=" * 78)
        return

    # Parse the run's OWN captured output — never re-read the log file, so a concurrent run writing a
    # different file (or this run's log being swept) can never make this result show the wrong failures.
    lines = output.splitlines()
    blocks = _extract_failure_blocks(lines)
    print("-" * 78)
    if blocks:
        # The complete list of what failed always prints — even at 50 failures it is 50 short lines.
        print(f"Failing tests ({len(blocks)}):")
        for header, _ in blocks:
            print(f"  {header}")
        print("-" * 78)
        for idx, (_, block) in enumerate(blocks):
            if idx >= _MAX_SHOWN_TRACEBACKS:
                print(f"… and {len(blocks) - idx} more failing test(s) — full tracebacks in the log above.")
                break
            for ln in block:
                print(ln)
            print()
    else:
        # No standard failure block (a killed/crashed child, or a failure before any test) — the exit
        # code already told the truth; show the tail so the cause is still visible.
        print("The run failed without a standard failure list; last lines of the output:")
        for ln in lines[-_TAIL_LINES:]:
            print(ln)
    print("=" * 78)


def _final_drain(fd: int, log_file, captured: list, deadline_s: float = 2.0) -> None:
    """After the child exits, sweep up its already-buffered output without blocking. A background
    grandchild can hold the pipe open forever, so this never waits on EOF — it stops as soon as no
    more data is immediately available, or a short deadline passes."""
    end = time.monotonic() + deadline_s
    while time.monotonic() < end:
        try:
            chunk = os.read(fd, 65536)
        except BlockingIOError:
            return
        except OSError:
            return
        if not chunk:
            return
        text = chunk.decode("utf-8", "replace")
        log_file.write(text)
        log_file.flush()
        captured.append(text)


def _run_parent(args: argparse.Namespace) -> int:
    start_dir = args.start_dir
    real_target = os.path.abspath(start_dir) == os.path.abspath(_DEFAULT_START_DIR) or start_dir == _DEFAULT_START_DIR
    if real_target and os.environ.get(_NESTED_ENV):
        print("selftest: refusing to run the real full suite while nested "
              f"({_NESTED_ENV} is set) — this would recurse.", file=sys.stderr)
        return 2

    _sweep_stale_logs()
    try:
        log_path, log_file = _open_run_log(args.log_path)
    except OSError as exc:
        print(f"selftest: cannot open the run log at {args.log_path}: {exc}", file=sys.stderr)
        return 2

    progress_read_fd, progress_write_fd = os.pipe()
    child_cmd = [
        sys.executable, os.path.abspath(__file__), "--child",
        "--start-dir", start_dir,
        "--pattern", args.pattern,
        "--progress-fd", str(progress_write_fd),
    ]
    env = {**os.environ, _NESTED_ENV: "1"}

    progress = _Progress()
    captured: list = []   # the run's own output, held in memory for a concurrency-safe result printout
    start = time.monotonic()
    previous_handlers: dict = {}
    sel = selectors.DefaultSelector()
    proc = None
    try:
        try:
            proc = subprocess.Popen(
                child_cmd,
                cwd=args.cwd or _ENGINE_DIR,
                env=env,
                stdin=subprocess.DEVNULL,  # end-of-input, so no demo blocks on stdin under a real tty
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,  # one combined stream to drain
                pass_fds=(progress_write_fd,),
                start_new_session=True,    # own process group, so teardown reaches demo grandchildren
            )
        except OSError as exc:
            print(f"selftest: failed to start the suite: {exc}", file=sys.stderr)
            os.close(progress_write_fd)  # the `finally` closes the read fd and the log
            # The suite never started, so the log is empty — the sole exception to the otherwise-always-
            # keep rule: there is nothing to read, and this run reports its failure loudly via the exit
            # code and the message above, so a dropped empty log can never read as a vanished one.
            if not args.log_path:
                try:
                    os.remove(log_path)
                except OSError:
                    pass
            return 2

        os.close(progress_write_fd)  # parent holds only the read end
        # Announce the log path only now the child is running, so an announced path always names a file
        # that will be kept. A run that never started (the branch above) reports its failure loudly on
        # stderr instead, and so never leaves a session hunting for a log it was promised.
        print(f"Running the self-test suite (log: {log_path})", flush=True)
        out_fd = proc.stdout.fileno()
        os.set_blocking(out_fd, False)
        os.set_blocking(progress_read_fd, False)
        sel.register(out_fd, selectors.EVENT_READ, "out")
        sel.register(progress_read_fd, selectors.EVENT_READ, "prog")

        def _forward(signum, _frame):
            try:
                os.killpg(os.getpgid(proc.pid), signum)
            except (ProcessLookupError, PermissionError):
                pass

        for sig in (signal.SIGINT, signal.SIGTERM):
            previous_handlers[sig] = signal.signal(sig, _forward)

        prog_buf = ""
        registered = {"out", "prog"}
        next_beat = start + args.heartbeat_interval
        while True:
            # Cap the wait so the child's exit is noticed promptly (within _POLL_INTERVAL_S) even when
            # no fd is readable — a background grandchild holding the pipe open means the "out" fd never
            # signals again, and gating exit-detection on the full heartbeat interval would look hung.
            timeout = min(_POLL_INTERVAL_S, max(0.0, next_beat - time.monotonic()))
            for key, _ in sel.select(timeout=timeout):
                try:
                    chunk = os.read(key.fd, 65536)
                except BlockingIOError:
                    continue
                except OSError:
                    chunk = b""
                if not chunk:  # this stream reached EOF
                    sel.unregister(key.fd)
                    registered.discard(key.data)
                    continue
                if key.data == "out":
                    text = chunk.decode("utf-8", "replace")
                    log_file.write(text)
                    log_file.flush()
                    captured.append(text)
                else:
                    prog_buf += chunk.decode("utf-8", "replace")
                    parts = prog_buf.split("\n")
                    prog_buf = parts.pop()
                    for line in parts:
                        progress.apply(line)

            now = time.monotonic()
            if now >= next_beat:
                print(_heartbeat_line(progress.snapshot(now), now - start, args.stall_threshold), flush=True)
                next_beat = now + args.heartbeat_interval

            if proc.poll() is not None:
                if "out" in registered:
                    _final_drain(out_fd, log_file, captured)
                break

        rc = proc.returncode
    finally:
        for sig, handler in previous_handlers.items():
            signal.signal(sig, handler)
        sel.close()
        if proc is not None:
            if proc.poll() is None:
                # Leaving with the child still alive (an unexpected error mid-loop) — never orphan it
                # or the demo grandchildren it may have started; escalate to SIGKILL if it clings on.
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                except (ProcessLookupError, PermissionError, OSError):
                    pass
            try:
                proc.stdout.close()
            except OSError:
                pass
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except (ProcessLookupError, PermissionError, OSError):
                    pass
                try:
                    proc.wait(timeout=5)
                except (subprocess.TimeoutExpired, OSError):
                    pass
            except OSError:
                pass
        try:
            os.close(progress_read_fd)
        except OSError:
            pass
        log_file.close()

    # The log is kept for every run — pass or fail — so a session can always read its own run and never
    # reads a vanished log as a failure. Cleanup is the daily sweep (_sweep_stale_logs) at the next run.
    elapsed = time.monotonic() - start
    _print_result(rc, elapsed, log_path, "".join(captured))
    return rc  # VERBATIM — the child's exit status is the launcher's verdict.


# --------------------------------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Run the full self-test suite once, legibly.")
    p.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--progress-fd", type=int, default=None, help=argparse.SUPPRESS)
    p.add_argument("--start-dir", default=_DEFAULT_START_DIR, help=argparse.SUPPRESS)
    p.add_argument("--pattern", default=_DEFAULT_PATTERN, help=argparse.SUPPRESS)
    p.add_argument("--cwd", default=None, help=argparse.SUPPRESS)
    p.add_argument("--heartbeat-interval", type=float,
                   default=float(os.environ.get("ENGINE_SELFTEST_HEARTBEAT_S", _DEFAULT_HEARTBEAT_S)),
                   help=argparse.SUPPRESS)
    p.add_argument("--stall-threshold", type=float,
                   default=float(os.environ.get("ENGINE_SELFTEST_STALL_S", _DEFAULT_STALL_S)),
                   help=argparse.SUPPRESS)
    p.add_argument("--log-path", default=None, help=argparse.SUPPRESS)
    return p


def main(argv: Optional[list] = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.child:
        return _run_child(args)
    return _run_parent(args)


if __name__ == "__main__":
    sys.exit(main())
