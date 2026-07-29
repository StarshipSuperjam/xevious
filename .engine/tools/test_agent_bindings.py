#!/usr/bin/env python3
"""Self-tests for capability→model bindings: the model-bindings.v1 schema, the committed bindings file, the
schema-kind rule, and the agent_bindings render/check tool that stamps personas and keeps them in sync.

Run: uv run --directory .engine --frozen -- python tools/selftest.py

These lock: the schema rejects a versioned model id (rot) and accepts a durable alias; the committed bindings
conform; render stamps model/effort from the binding (override wins over tier) and is idempotent; check catches
drift and a stale override; and the REAL committed personas are in sync with the committed bindings (so a hand
edit to either without a re-render fails CI).
"""
from __future__ import annotations
import json
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate  # noqa: E402
import agent_bindings as ab  # noqa: E402

BIND_SCHEMA = validate.load_json(os.path.join(validate.SCHEMAS_DIR, "model-bindings.v1.json"))
REAL_BINDINGS = os.path.join(validate.ROOT, ".engine", "policies", "model-bindings.json")


def _errors(instance):
    return list(validate.Draft202012Validator(BIND_SCHEMA).iter_errors(instance))


def _valid_bindings(**over):
    b = {"schema_version": 1,
         "tiers": {"judgment": {"model": "opus", "effort": "high"},
                   "mechanical": {"model": "haiku", "effort": "low"}},
         "overrides": {}}
    b.update(over)
    return b


def _make_home(d):
    """Give a fixture a readable git origin that MATCHES its recorded home, so it reads as the home repo — the
    condition under which check() runs its dangling-override leg. A deployed repo (readable non-home origin) or
    one with no readable origin skips that leg, so a declined-module deployment does not red it (#646)."""
    subprocess.run(["git", "init", "-q", d], check=True)
    subprocess.run(["git", "-C", d, "remote", "add", "origin", "https://github.com/test/home.git"], check=True)
    os.makedirs(os.path.join(d, ".engine"), exist_ok=True)
    with open(os.path.join(d, ".engine", "engine.json"), "w", encoding="utf-8") as fh:
        json.dump({"home_repository": "test/home"}, fh)
    return d


def _fixture(d, agents, bindings):
    os.makedirs(os.path.join(d, ".claude", "agents"))
    for name, tier in agents.items():
        with open(os.path.join(d, ".claude", "agents", f"{name}.md"), "w", encoding="utf-8") as fh:
            fh.write(f"---\nname: {name}\nrole: pre-submission-review\nlens: x\nmodel-tier: {tier}\n"
                     f"permissions: read-only\noutput-contract: x.v1\ndisallowedTools: [Edit]\n---\nBody text.\n")
    os.makedirs(os.path.join(d, ".engine", "policies"))
    with open(os.path.join(d, ".engine", "policies", "model-bindings.json"), "w", encoding="utf-8") as fh:
        json.dump(bindings, fh)
    return d


class TestBindingsSchema(unittest.TestCase):
    def test_schema_well_formed(self):
        validate.Draft202012Validator.check_schema(BIND_SCHEMA)

    def test_committed_bindings_conform(self):
        self.assertEqual(_errors(validate.load_json(REAL_BINDINGS)), [])

    def test_model_alias_pattern_rejects_versioned_ids(self):
        for ok in ("opus", "sonnet", "haiku", "fable", "gpt-sol"):
            b = _valid_bindings()
            b["tiers"]["judgment"]["model"] = ok
            self.assertEqual(_errors(b), [], f"{ok!r} should pass")
        for bad in ("claude-opus-4-20250101", "opus-4", "Opus", "haiku4", "gpt_sol"):
            b = _valid_bindings()
            b["tiers"]["judgment"]["model"] = bad
            self.assertTrue(_errors(b), f"{bad!r} should fail (versioned/invalid alias)")

    def test_bad_effort_and_missing_fields_flagged(self):
        b = _valid_bindings()
        b["tiers"]["judgment"]["effort"] = "maximum"
        self.assertTrue(_errors(b))
        b2 = _valid_bindings()
        del b2["tiers"]["judgment"]["model"]
        self.assertTrue(_errors(b2))
        b3 = _valid_bindings()
        del b3["tiers"]["mechanical"]
        self.assertTrue(_errors(b3))

    def test_extra_field_flagged(self):
        self.assertTrue(_errors({**_valid_bindings(), "extra": 1}))
        b = _valid_bindings()
        b["tiers"]["judgment"]["provider"] = "claude"
        self.assertTrue(_errors(b))

    def test_rule_is_well_formed_and_passes_real_file(self):
        rule = validate.load_json(os.path.join(validate.CHECK_DIR, "model-bindings-schema.json"))
        check_schema = validate.load_json(os.path.join(validate.SCHEMAS_DIR, "check.v1.json"))
        self.assertEqual(list(validate.Draft202012Validator(check_schema).iter_errors(rule)), [])
        self.assertEqual(rule.get("params"), {"schema": ".engine/schemas/model-bindings.v1.json"})


class TestResolve(unittest.TestCase):
    def test_override_wins_over_tier(self):
        b = _valid_bindings(overrides={"a": {"model": "sonnet", "effort": "high"}})
        self.assertEqual(ab.resolve("a", "judgment", b), {"model": "sonnet", "effort": "high"})

    def test_tier_default_when_no_override(self):
        self.assertEqual(ab.resolve("b", "judgment", _valid_bindings()),
                         {"model": "opus", "effort": "high"})
        self.assertEqual(ab.resolve("b", "mechanical", _valid_bindings()),
                         {"model": "haiku", "effort": "low"})

    def test_unknown_tier_raises(self):
        with self.assertRaises(KeyError):
            ab.resolve("b", "wizard", _valid_bindings())


class TestRenderAndCheck(unittest.TestCase):
    def test_render_stamps_override_and_tier_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as d:
            _fixture(d, {"a": "judgment", "b": "judgment"},
                     _valid_bindings(overrides={"a": {"model": "sonnet", "effort": "high"}}))
            changed = ab.render(d)
            self.assertEqual(set(changed), {"a.md", "b.md"})
            self.assertEqual(ab.check(d), [])
            a = open(os.path.join(d, ".claude", "agents", "a.md"), encoding="utf-8").read()
            self.assertIn("model: sonnet", a)
            self.assertIn("effort: high", a)
            # body and other frontmatter preserved
            self.assertIn("Body text.", a)
            self.assertIn("output-contract: x.v1", a)
            # idempotent
            self.assertEqual(ab.render(d), [])

    def test_check_flags_hand_edited_drift(self):
        with tempfile.TemporaryDirectory() as d:
            _fixture(d, {"a": "judgment"}, _valid_bindings())
            ab.render(d)
            p = os.path.join(d, ".claude", "agents", "a.md")
            with open(p, encoding="utf-8") as fh:
                edited = fh.read().replace("model: opus", "model: haiku")
            with open(p, "w", encoding="utf-8") as fh:
                fh.write(edited)
            self.assertTrue(ab.check(d))

    def test_check_flags_stale_override(self):
        with tempfile.TemporaryDirectory() as d:
            _fixture(d, {"a": "judgment"},
                     _valid_bindings(overrides={"ghost": {"model": "sonnet", "effort": "high"}}))
            _make_home(d)                      # the dangling-override leg runs only when confidently home
            ab.render(d)
            self.assertTrue(any("ghost" in p for p in ab.check(d)))

    def test_check_ignores_stale_override_in_a_deployed_repo(self):
        # The #646 close: a deployed repo (readable origin that is NOT the recorded home) does NOT flag a
        # dangling override — a declined review pack legitimately leaves its personas' overrides behind.
        with tempfile.TemporaryDirectory() as d:
            _fixture(d, {"a": "judgment"},
                     _valid_bindings(overrides={"ghost": {"model": "sonnet", "effort": "high"}}))
            subprocess.run(["git", "init", "-q", d], check=True)
            subprocess.run(["git", "-C", d, "remote", "add", "origin",
                            "https://github.com/acme/product.git"], check=True)
            os.makedirs(os.path.join(d, ".engine"), exist_ok=True)
            with open(os.path.join(d, ".engine", "engine.json"), "w", encoding="utf-8") as fh:
                json.dump({"home_repository": "test/home"}, fh)   # origin != home -> deployed
            ab.render(d)
            self.assertEqual([p for p in ab.check(d) if "ghost" in p], [])

    def test_check_ignores_stale_override_when_origin_is_unreadable(self):
        # The #646 'arrival before its remote is set' case: no readable origin -> not confidently home ->
        # the dangling-override leg is skipped (fail toward not-home), so a declined-pack override does not red.
        with tempfile.TemporaryDirectory() as d:
            _fixture(d, {"a": "judgment"},
                     _valid_bindings(overrides={"ghost": {"model": "sonnet", "effort": "high"}}))
            with open(os.path.join(d, ".engine", "engine.json"), "w", encoding="utf-8") as fh:
                json.dump({"home_repository": "test/home"}, fh)   # a recorded home, but no readable origin
            ab.render(d)
            self.assertEqual([p for p in ab.check(d) if "ghost" in p], [])


class TestRealPersonasInSync(unittest.TestCase):
    def test_committed_personas_match_committed_bindings(self):
        # A hand edit to a persona's model/effort, or to the bindings, without a re-render fails here.
        self.assertEqual(ab.check(), [])


if __name__ == "__main__":
    unittest.main()
