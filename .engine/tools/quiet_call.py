#!/usr/bin/env python3
"""Run a callable with its stdout captured — the test-support helper that keeps a demo walkthrough
from burying the `Ran N tests … OK` summary when the suite is run without unittest's `-b`.

Why this exists: a handful of `test_*.py` self-tests call a `demo_*` module's functions — `main()` to
attest the happy path exits 0, and helper legs like `_scan_planted_secret()` to attest a real-binary
behavior. They assert the RETURN VALUE (an exit code, a bool), never the printed walkthrough — but each
of those demo functions prints. Run through `unittest discover` WITHOUT `-b`, those lines flood the tail
and hide the pass/fail summary, so the run has to be repeated (with `-b`) just to read a clean result.
`-b` is the CI default and the documented fix, but a session that reconstructs the command by habit
drops the flag and floods again — the recurring papercut this helper removes.

Capturing the demo's stdout AT THE CALL SITE makes the first run clean on ANY invocation — buffered or
not, `discover` or single-file — so the flag is no longer load-bearing for a legible summary. (`-b`
stays the CI default as general belt-and-suspenders; this just stops a dropped flag from mattering.)

Pass the callable as a REFERENCE — `quiet_call.run(demo_x.some_fn)` — never a call
(`demo_x.some_fn()`): a direct call prints before the helper can capture it.

Scope of the durability guard (test_quiet_call.py): it keys on the `demo_*` MODULE NAME, so it fails the
suite only if a `test_*.py` calls a `demo_*`-module function directly again. It does NOT cover a tool's
own printing leg reached under its real name — `pinning.demo()`, `compact._demo_trigger()`,
`modes.main(["set-build"])`, `audit_digest.main(["seal", …])`, etc. For those shapes the capture here is a
convention this helper provides, not a guarded invariant; a future test that adds such a leg unwrapped
re-floods with nothing red to stop it (the deliberate wrap-only scope — the reminder lives in build memory).

stdout only: stderr and exceptions are left alone, so a crashing demo still fails loudly. But a demo that
signals failure by RETURN VALUE while printing its diagnostic to stdout (e.g. compact.py's `!!!` lines) has
that diagnostic captured too — the test still fails on the return value, but to read the walkthrough behind
a bare `1 != 0`, re-run the demo directly (each is `__main__`-runnable).
"""
from __future__ import annotations

import contextlib
import io
from typing import Callable, TypeVar

_T = TypeVar("_T")


def run(fn: Callable[..., _T], *args, **kwargs) -> _T:
    """Call `fn(*args, **kwargs)` with its stdout captured, returning whatever it returns (an exit
    code, a bool — the caller asserts on that, not on the printed walkthrough, which is discarded). An
    exception propagates — `redirect_stdout` restores stdout and re-raises on the way out — so a
    crashing demo still fails loudly rather than being swallowed."""
    with contextlib.redirect_stdout(io.StringIO()):
        return fn(*args, **kwargs)
