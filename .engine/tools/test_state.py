#!/usr/bin/env python3
"""Self-tests for the state cursor: the state.v1 schema, the committed genesis
cursor, and the schema-kind rule that refuses a malformed or shape-invalid cursor.

Run: uv run --directory .engine --frozen -- python -m unittest discover -s tools -p 'test_*.py' -b

These lock the load-bearing teeth: the schema bites on each malformed shape (a missing field,
an out-of-grammar field, a wrong version stamp, a negative count, a non-UTC timestamp, an empty
pointer); the committed genesis cursor itself conforms; the committed rule names its schema
DIRECTLY via params.schema (state is a foundation, not a catalogued surface) and passes the real cursor;
and a malformed or shape-invalid cursor is REFUSED AS A PLAIN FINDING — never an uncaught crash —
which is the halt-on-malformed posture the design requires. The deliverable-gate
cold review attests each test's assertion matches its name; CI runs them as a step in engine-ci.
"""
from __future__ import annotations
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate  # noqa: E402

STATE_SCHEMA = validate.load_json(os.path.join(validate.SCHEMAS_DIR, "state.v1.json"))
REAL_CURSOR = os.path.join(validate.ROOT, ".engine", "state", "state.json")

# A fully-populated, valid cursor (non-null pointers, a real UTC marker, a register).
VALID_STATE = {
    "schema_version": 1,
    "standing_situation": {"milestone": "M1", "phase": "core"},
    "integration_debt": {
        "open_count": 3,
        "as_of": "2026-06-02T14:30:00Z",
        "register": "https://github.com/owner/repo/issues?q=is:open+label:engine",
    },
}


def _errors(schema, instance):
    return list(validate.Draft202012Validator(schema).iter_errors(instance))


def _write(d, name, obj):
    p = os.path.join(d, name)
    with open(p, "w", encoding="utf-8") as fh:
        json.dump(obj, fh) if isinstance(obj, (dict, list)) else fh.write(obj)
    return p


def _run_kind(kind_fn, rule, files):
    """Run a kind callable with validate.target_files stubbed to `files`."""
    orig = validate.target_files
    validate.target_files = lambda r: list(files)
    try:
        return kind_fn(rule, {})
    finally:
        validate.target_files = orig


class TestStateSchema(unittest.TestCase):
    def test_schema_is_well_formed(self):
        validate.Draft202012Validator.check_schema(STATE_SCHEMA)

    def test_genesis_cursor_conforms(self):
        """The committed genesis cursor itself matches its schema."""
        real = validate.load_json(REAL_CURSOR)
        self.assertEqual(_errors(STATE_SCHEMA, real), [])

    def test_valid_populated_cursor_passes(self):
        self.assertEqual(_errors(STATE_SCHEMA, VALID_STATE), [])

    def test_missing_top_level_required_field_is_flagged(self):
        for drop in ("schema_version", "standing_situation", "integration_debt"):
            bad = {k: v for k, v in VALID_STATE.items() if k != drop}
            self.assertTrue(_errors(STATE_SCHEMA, bad), f"missing {drop} should fail")

    def test_missing_nested_required_field_is_flagged(self):
        for parent, child in (("standing_situation", "milestone"),
                              ("standing_situation", "phase"),
                              ("integration_debt", "open_count"),
                              ("integration_debt", "as_of"),
                              ("integration_debt", "register")):
            bad = json.loads(json.dumps(VALID_STATE))
            del bad[parent][child]
            self.assertTrue(_errors(STATE_SCHEMA, bad), f"missing {parent}.{child} should fail")

    def test_field_outside_the_grammar_is_flagged(self):
        # An extra field at the root and inside each object — the "tiny / no store" guard.
        self.assertTrue(_errors(STATE_SCHEMA, {**VALID_STATE, "extra": 1}))
        root = json.loads(json.dumps(VALID_STATE))
        root["standing_situation"]["note"] = "history"
        self.assertTrue(_errors(STATE_SCHEMA, root))
        root2 = json.loads(json.dumps(VALID_STATE))
        root2["integration_debt"]["log"] = ["a", "b"]
        self.assertTrue(_errors(STATE_SCHEMA, root2))

    def test_wrong_schema_version_is_flagged(self):
        self.assertTrue(_errors(STATE_SCHEMA, {**VALID_STATE, "schema_version": 2}))
        self.assertTrue(_errors(STATE_SCHEMA, {**VALID_STATE, "schema_version": "1"}))

    def test_negative_debt_count_is_flagged(self):
        bad = json.loads(json.dumps(VALID_STATE))
        bad["integration_debt"]["open_count"] = -1
        self.assertTrue(_errors(STATE_SCHEMA, bad))

    def test_non_integer_debt_count_is_flagged(self):
        # A fractional or string count is not a count — type:integer must bite.
        # (Note: a float with a zero fractional part, e.g. 3.0, is an integer per
        # JSON Schema, so the teeth are a true fraction and a string.)
        for value in (3.5, "3"):
            bad = json.loads(json.dumps(VALID_STATE))
            bad["integration_debt"]["open_count"] = value
            self.assertTrue(_errors(STATE_SCHEMA, bad), f"open_count={value!r} should fail")

    def test_as_of_accepts_null_and_utc_z_only(self):
        def with_as_of(v):
            s = json.loads(json.dumps(VALID_STATE))
            s["integration_debt"]["as_of"] = v
            return s
        # accepted: null, a UTC ...Z moment, and a fractional-seconds ...Z moment
        for ok in (None, "2026-06-02T14:30:00Z", "2026-06-02T14:30:00.123Z"):
            self.assertEqual(_errors(STATE_SCHEMA, with_as_of(ok)), [], f"{ok!r} should pass")
        # rejected: a non-UTC offset, a no-Z local time, and garbage
        for bad in ("2026-06-02T14:30:00+02:00", "2026-06-02T14:30:00", "not-a-date"):
            self.assertTrue(_errors(STATE_SCHEMA, with_as_of(bad)), f"{bad!r} should fail")

    def test_empty_string_pointer_is_flagged(self):
        for parent, child in (("standing_situation", "milestone"),
                              ("standing_situation", "phase"),
                              ("integration_debt", "register")):
            bad = json.loads(json.dumps(VALID_STATE))
            bad[parent][child] = ""
            self.assertTrue(_errors(STATE_SCHEMA, bad), f"empty {parent}.{child} should fail")

    def test_milestone_accepts_list_string_or_null(self):
        # #496: the open milestones are read as they are — a list of names (empty = none). A bare string (a
        # pre-#496 cursor) and null both stay valid so an older cursor upgrades cleanly.
        for ok in ([], ["One"], ["One", "Two"], "Legacy single", None):
            s = json.loads(json.dumps(VALID_STATE))
            s["standing_situation"]["milestone"] = ok
            self.assertEqual(_errors(STATE_SCHEMA, s), [], f"milestone={ok!r} should pass")
        # a blank name (bare or inside the list) and a non-string member are still rejected (minLength/type bite)
        for bad in ("", [""], ["ok", ""], [1]):
            s = json.loads(json.dumps(VALID_STATE))
            s["standing_situation"]["milestone"] = bad
            self.assertTrue(_errors(STATE_SCHEMA, s), f"milestone={bad!r} should fail")


class TestStateRuleIntegration(unittest.TestCase):
    """The committed rule joins CI, names its schema directly via params.schema (state is a
    foundation, not a catalogued surface), passes the real cursor, and refuses a malformed or
    shape-invalid cursor as a plain finding."""

    def _rule(self):
        return validate.load_json(os.path.join(validate.CHECK_DIR, "state-cursor.json"))

    def test_rule_is_well_formed_and_joins_ci(self):
        check_schema = validate.load_json(os.path.join(validate.SCHEMAS_DIR, "check.v1.json"))
        rule = self._rule()
        self.assertEqual(list(validate.Draft202012Validator(check_schema).iter_errors(rule)), [])
        self.assertIn("CI", rule.get("suites", []))

    def test_real_cursor_passes_via_the_schema_routed_rule(self):
        """The real rule names state.v1.json via params.schema (state is a foundation, not a
        catalogued surface), so .engine/state/state.json is schema-checked directly and the
        genesis cursor passes."""
        rule = self._rule()
        self.assertEqual(rule.get("params"), {"schema": ".engine/schemas/state.v1.json"})  # names schema directly
        passed, found = _run_kind(validate.kind_schema, rule, [REAL_CURSOR])
        self.assertTrue(passed)
        self.assertEqual(found, [])

    def test_malformed_json_is_refused_as_a_plain_finding_not_a_crash(self):
        rule = self._rule()  # the real rule now names its schema via params.schema
        with tempfile.TemporaryDirectory() as d:
            bad = _write(d, "state.json", "{ not json")
            passed, found = _run_kind(validate.kind_schema, rule, [bad])
        self.assertFalse(passed)
        self.assertTrue(any(f["severity"] == "hard" for f in found))
        self.assertTrue(any("is not valid JSON and cannot be schema-checked" in f["message"]
                            for f in found))

    def test_schema_invalid_cursor_is_refused_at_tier(self):
        rule = self._rule()  # the real rule now names its schema via params.schema
        with tempfile.TemporaryDirectory() as d:
            bad = _write(d, "state.json", {"schema_version": 1})  # missing the two required objects
            passed, found = _run_kind(validate.kind_schema, rule, [bad])
        self.assertFalse(passed)
        self.assertTrue(any(f["severity"] == "hard" for f in found))


if __name__ == "__main__":
    unittest.main()
