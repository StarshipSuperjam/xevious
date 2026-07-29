"""moment.py — the engine's one home for a moment in time.

A pure standard-library leaf. It reads and writes the single trailing-Z UTC wire shape the engine's
schemas enforce, and it bridges that shape to the epoch-seconds float some substrates rank by. It has no
engine imports, no CLI, no state, and no writes — so any module, including the pure leaves that document a
refusal to import telemetry (`attention_rank`, `work_record`, `hooks`, `attention`), can import it at
module level without a cycle and without breaking that refusal.

Two laws bind every caller:

1. **Determinism (eADR-0032).** `utc_now()` and `today_utc()` READ THE REAL WALL CLOCK. They live at the
   IO edge — the run/main boundary — and are NEVER called inside logic that should thread an explicit
   `as_of`/`now=` reference time. The determinism law is "same inputs → same ordering, reproducibly across
   clock skew or a host change"; a wall-clock read buried in ranking or reconcile logic breaks it. When a
   function takes an injected `now`, pass it down; do not reach for `utc_now()`/`today_utc()` instead.

2. **Strict on the way out, tolerant on the way in.** `to_z()` builds a wire value from your own data, so a
   naive (zone-unaware) datetime is a bug in the caller and RAISES. `parse_z()`/`epoch()` read
   possibly-stored, possibly-untrusted text, so a malformed or wrong-typed value DEGRADES to the caller's
   `default` (never raises, never returns a value that would make a comparison non-total). This asymmetry is
   deliberate — do not "normalise" it away: a lenient emitter hides caller bugs, and a strict parser turns a
   bad stored record into a crash in a sort key (the recall-crash class this engine has been bitten by).

The trailing-Z shape carries FIXED-WIDTH seconds and no fractional part, so two wire strings compare
correctly as raw strings. `Z_PATTERN` is the single source of that shape; a drift test pins every schema's
timestamp pattern to it.
"""

from __future__ import annotations

import datetime
import math

# The one canonical trailing-Z UTC wire shape. Fixed-width seconds, no fractional group — so a lexical
# comparison of two wire strings is a correct time comparison. Every schema timestamp pattern must equal
# this (enforced by the drift test in test_moment.py); the strftime form below produces exactly it.
Z_PATTERN = r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$"

_Z_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
_UTC = datetime.timezone.utc


def utc_now() -> str:
    """The current UTC moment in the trailing-Z wire shape. Reads the wall clock — IO-edge only (law 1)."""
    return datetime.datetime.now(_UTC).strftime(_Z_FORMAT)


def today_utc() -> datetime.date:
    """Today's UTC calendar day. Reads the wall clock — IO-edge only (law 1). This is the correct 'today'
    for anything compared against a UTC-sealed date; the machine's LOCAL day (`datetime.date.today()`) is
    not, and mixing the two is the local-vs-UTC calendar-day defect this module exists to retire."""
    return datetime.datetime.now(_UTC).date()


def to_z(when: "datetime.datetime | int | float") -> str:
    """Format an aware datetime OR an epoch-seconds number into the trailing-Z wire shape.

    Strict on output (law 2): a naive datetime RAISES `ValueError` — a wire timestamp with no zone is a
    caller bug, not something to guess a zone for. Sub-second precision is truncated to fixed-width seconds.
    """
    if isinstance(when, bool):  # bool is an int subclass; never a timestamp
        raise TypeError(f"to_z expected an aware datetime or epoch number, got bool: {when!r}")
    if isinstance(when, (int, float)):
        if not math.isfinite(when):  # NaN/±inf is not a moment — a caller bug on the emit path
            raise ValueError(f"to_z requires a finite epoch; got {when!r}")
        dt = datetime.datetime.fromtimestamp(when, _UTC)
    elif isinstance(when, datetime.datetime):
        if when.tzinfo is None or when.tzinfo.utcoffset(when) is None:
            raise ValueError(f"to_z requires an aware datetime; got naive: {when!r}")
        dt = when.astimezone(_UTC)
    else:
        raise TypeError(f"to_z expected an aware datetime or epoch number, got {type(when).__name__}")
    return dt.strftime(_Z_FORMAT)


def parse_z(text: object, *, default=None):
    """Parse a UTC wire string to an aware UTC `datetime`. Tolerant on input (law 2): accepts trailing `Z`,
    `+00:00`, any explicit offset, and fractional seconds. Anything it cannot turn into an AWARE moment — a
    non-string, an unparseable string, or a naive (zone-less) one — returns `default` rather than raising.

    Callers that feed the result into a sort or comparison MUST pass a total-order-safe `default` (e.g.
    `datetime.datetime.min.replace(tzinfo=datetime.timezone.utc)`), never leave it `None`: a `None` in a
    comparator is the non-total sort key that crashes (law 2)."""
    if not isinstance(text, str):
        return default
    try:
        dt = datetime.datetime.fromisoformat(text)  # 3.11+ parses trailing 'Z', offsets, fractional seconds
    except ValueError:
        return default
    if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
        return default  # refuse naive — an ambiguous moment is not a moment
    return dt.astimezone(_UTC)


def epoch(value: object) -> "float | None":
    """The wire-to-epoch bridge: a UTC wire string, an aware datetime, or an epoch number → epoch seconds
    as a float. Tolerant on input (law 2): anything it cannot resolve to an absolute moment returns `None`.
    As with `parse_z`, a caller feeding this into ordering must substitute a total-order-safe value for
    `None` itself."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if math.isfinite(value) else None  # NaN/±inf is not an absolute moment
    if isinstance(value, datetime.datetime):
        if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
            return None
        return value.timestamp()
    if isinstance(value, str):
        dt = parse_z(value)
        return None if dt is None else dt.timestamp()
    return None
