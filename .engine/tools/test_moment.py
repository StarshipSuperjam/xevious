#!/usr/bin/env python3
"""Self-tests for moment.py — the engine's one time seam.

Run: uv run --directory .engine --frozen -- python tools/selftest.py

Each test locks one law of the seam against the real functions: `utc_now()`/`to_z()` emit the fixed-width
trailing-Z wire shape a schema accepts (retargeted here from test_telemetry, since that invariant now lives
in moment); `to_z` is STRICT (raises on a naive datetime and on bool) while `parse_z`/`epoch` are TOLERANT
(a bad or wrong-typed value degrades to the caller's default, never raises, never returns None into a
sort unless the caller left the default None); the round-trip through epoch is stable. Two structural tests
guard the seam's reason for existing: the DERIVED drift test proves every schema timestamp pattern equals
`moment.Z_PATTERN` (a new schema is covered automatically — the list is scanned, never hard-coded), and the
recurrence guard proves no engine tool outside moment.py hand-rolls the three idioms the seam replaces.
"""
from __future__ import annotations

import datetime
import json
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import moment  # noqa: E402
import validate  # noqa: E402

_TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
_UTC = datetime.timezone.utc


class TestEmit(unittest.TestCase):
    def test_utc_now_shape(self):
        self.assertRegex(moment.utc_now(), moment.Z_PATTERN)

    def test_utc_now_matches_state_pattern(self):
        # Retargeted from test_telemetry.test_utc_now_matches_state_pattern: the moment seam now owns the
        # invariant that a produced timestamp satisfies the state.v1 schema.
        schema = validate.load_json(os.path.join(validate.SCHEMAS_DIR, "state.v1.json"))
        probe = {"schema_version": 1, "standing_situation": {"milestone": None, "phase": None},
                 "integration_debt": {"open_count": 0, "as_of": moment.utc_now(), "register": None}}
        self.assertEqual(list(validate.Draft202012Validator(schema).iter_errors(probe)), [])

    def test_today_utc_is_a_date_and_utc(self):
        # today_utc agrees with utc_now's calendar day (both read the UTC wall clock).
        self.assertIsInstance(moment.today_utc(), datetime.date)
        self.assertEqual(moment.utc_now()[:10], moment.today_utc().isoformat())

    def test_to_z_from_aware_datetime(self):
        dt = datetime.datetime(2026, 7, 28, 14, 30, 5, tzinfo=_UTC)
        self.assertEqual(moment.to_z(dt), "2026-07-28T14:30:05Z")

    def test_to_z_converts_offset_to_utc(self):
        dt = datetime.datetime(2026, 7, 28, 14, 30, 5, tzinfo=datetime.timezone(datetime.timedelta(hours=9)))
        self.assertEqual(moment.to_z(dt), "2026-07-28T05:30:05Z")

    def test_to_z_truncates_subseconds(self):
        dt = datetime.datetime(2026, 7, 28, 14, 30, 5, 987654, tzinfo=_UTC)
        self.assertEqual(moment.to_z(dt), "2026-07-28T14:30:05Z")

    def test_to_z_from_epoch(self):
        # 2026-07-28T14:30:05Z as epoch seconds.
        e = datetime.datetime(2026, 7, 28, 14, 30, 5, tzinfo=_UTC).timestamp()
        self.assertEqual(moment.to_z(e), "2026-07-28T14:30:05Z")

    def test_to_z_raises_on_naive(self):
        with self.assertRaises(ValueError):
            moment.to_z(datetime.datetime(2026, 7, 28, 14, 30, 5))

    def test_to_z_rejects_bool(self):
        with self.assertRaises(TypeError):
            moment.to_z(True)

    def test_to_z_rejects_non_finite_epoch(self):
        for bad in (float("nan"), float("inf"), float("-inf")):
            with self.assertRaises(ValueError):
                moment.to_z(bad)

    def test_to_z_rejects_string(self):
        with self.assertRaises(TypeError):
            moment.to_z("2026-07-28T14:30:05Z")


class TestParse(unittest.TestCase):
    def test_parse_z_trailing_z(self):
        dt = moment.parse_z("2026-07-28T14:30:05Z")
        self.assertEqual(dt, datetime.datetime(2026, 7, 28, 14, 30, 5, tzinfo=_UTC))

    def test_parse_z_explicit_offset_normalizes_to_utc(self):
        dt = moment.parse_z("2026-07-28T23:30:05+09:00")
        self.assertEqual(dt, datetime.datetime(2026, 7, 28, 14, 30, 5, tzinfo=_UTC))

    def test_parse_z_tolerates_fractional(self):
        dt = moment.parse_z("2026-07-28T14:30:05.5Z")
        self.assertEqual(dt, datetime.datetime(2026, 7, 28, 14, 30, 5, 500000, tzinfo=_UTC))

    def test_parse_z_refuses_naive_returns_default(self):
        self.assertIsNone(moment.parse_z("2026-07-28T14:30:05"))
        sentinel = object()
        self.assertIs(moment.parse_z("2026-07-28T14:30:05", default=sentinel), sentinel)

    def test_parse_z_refuses_nonstring_and_garbage(self):
        self.assertIsNone(moment.parse_z(None))
        self.assertIsNone(moment.parse_z(1690000000))
        self.assertIsNone(moment.parse_z("not a time"))

    def test_parse_z_default_is_returned_not_raised(self):
        # The law that keeps a bad stored value out of a sort as a crash: a caller can pass a
        # total-order-safe sentinel and never see an exception.
        floor = datetime.datetime.min.replace(tzinfo=_UTC)
        self.assertEqual(moment.parse_z("garbage", default=floor), floor)


class TestEpoch(unittest.TestCase):
    def test_epoch_from_wire_string(self):
        self.assertEqual(moment.epoch("2026-07-28T14:30:05Z"),
                         datetime.datetime(2026, 7, 28, 14, 30, 5, tzinfo=_UTC).timestamp())

    def test_epoch_from_number_passthrough(self):
        self.assertEqual(moment.epoch(1690000000), 1690000000.0)
        self.assertIsInstance(moment.epoch(5), float)

    def test_epoch_from_aware_datetime(self):
        dt = datetime.datetime(2026, 7, 28, 14, 30, 5, tzinfo=_UTC)
        self.assertEqual(moment.epoch(dt), dt.timestamp())

    def test_epoch_degrades_to_none(self):
        self.assertIsNone(moment.epoch(None))
        self.assertIsNone(moment.epoch(True))
        self.assertIsNone(moment.epoch("not a time"))
        self.assertIsNone(moment.epoch(datetime.datetime(2026, 7, 28)))  # naive
        # NaN / ±inf are not absolute moments — they must degrade, never leak into a sort as a non-total key.
        self.assertIsNone(moment.epoch(float("nan")))
        self.assertIsNone(moment.epoch(float("inf")))
        self.assertIsNone(moment.epoch(float("-inf")))

    def test_round_trip(self):
        s = "2026-07-28T14:30:05Z"
        self.assertEqual(moment.to_z(moment.epoch(s)), s)


class TestSchemaPatternDrift(unittest.TestCase):
    """Every schema timestamp pattern must be the single canonical Z_PATTERN. Derived, not hard-coded: a
    future schema that adds a timestamp field is covered automatically."""

    _TS_MARKER = re.compile(r"T\[0-9\]\{2\}:\[0-9\]\{2\}:\[0-9\]\{2\}")  # the H:M:S core of a Z timestamp

    def _patterns(self, node):
        if isinstance(node, dict):
            for k, v in node.items():
                if k == "pattern" and isinstance(v, str) and self._TS_MARKER.search(v):
                    yield v
                else:
                    yield from self._patterns(v)
        elif isinstance(node, list):
            for item in node:
                yield from self._patterns(item)

    def test_every_schema_timestamp_pattern_is_canonical(self):
        found = []
        for name in sorted(os.listdir(validate.SCHEMAS_DIR)):
            if not name.endswith(".json"):
                continue
            with open(os.path.join(validate.SCHEMAS_DIR, name), encoding="utf-8") as fh:
                doc = json.load(fh)
            for pat in self._patterns(doc):
                found.append((name, pat))
        # Non-vacuity floor: the seam is known to govern several timestamp fields; a scan that found none
        # would silently pass. This is a floor, not an enumeration — it does not care which files matched.
        self.assertGreaterEqual(len(found), 7, f"scan found too few timestamp patterns: {found}")
        drifted = [(n, p) for n, p in found if p != moment.Z_PATTERN]
        self.assertEqual(drifted, [], f"schema timestamp pattern(s) differ from moment.Z_PATTERN: {drifted}")


class TestRecurrenceGuard(unittest.TestCase):
    """No engine tool outside moment.py may hand-roll the three idioms the seam replaces. This is the
    structural close on the root cause (five time defects in five weeks, each an easy hand-roll)."""

    # Regexes, not literal substrings, so a hand-roll can't slip the net by quote style or spacing — e.g.
    # `.replace('Z', '+00:00')` (single-quoted) is caught exactly as the double-quoted form is.
    _BANNED = [
        (re.compile(r"\b(?:date|datetime)\.today\("), "read the wall clock via moment.today_utc()"),
        (re.compile(re.escape("%Y-%m-%dT%H:%M:%SZ")), "format via moment.utc_now()/moment.to_z()"),
        (re.compile(r"""\.replace\(\s*['"]Z['"]\s*,\s*['"]\+00:00['"]\s*\)"""),
         "parse via moment.parse_z()/moment.epoch()"),
    ]
    _EXEMPT = {"moment.py"}  # the seam's home. Test files are exempt too: they reference these idioms in
    # assertions and comments (and may legitimately build fixture timestamps) — the recurrence risk the
    # guard closes is production/demo code drifting back to a hand-roll, not test scaffolding.

    def test_no_handrolled_time_idioms_outside_moment(self):
        violations = []
        for root, _dirs, files in os.walk(_TOOLS_DIR):
            for fname in files:
                if not fname.endswith(".py") or fname in self._EXEMPT or fname.startswith("test_"):
                    continue
                path = os.path.join(root, fname)
                with open(path, encoding="utf-8") as fh:
                    for lineno, line in enumerate(fh, 1):
                        for pattern, remedy in self._BANNED:
                            if pattern.search(line):
                                rel = os.path.relpath(path, _TOOLS_DIR)
                                violations.append(f"{rel}:{lineno}  hand-rolls `{pattern.pattern}` — {remedy}")
        self.assertEqual(violations, [], "hand-rolled time idioms found:\n" + "\n".join(violations))


if __name__ == "__main__":
    unittest.main()
