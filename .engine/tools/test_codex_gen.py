#!/usr/bin/env python3
"""Self-tests for the Codex render tool (codex_gen.py) — the pipeline five enforcement surfaces
depend on. These pin the render transforms (typed-prefix rewrite, session-flag strip, routing
lines, the read-only floor and no-model rule) and give the render-sync drift gate its fail-side
witnesses: a hand-edited render, a stale render, and an orphaned render must each be caught.

Run: uv run --directory .engine --frozen -- python tools/selftest.py
"""
from __future__ import annotations
import os
import shutil
import sys
import tempfile
import tomllib
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import codex_gen   # noqa: E402
import validate    # noqa: E402


def _write(path: str, content: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)


AGENT_SRC = """---
name: qa-review-widget
description: Reviews widgets.
role: pre-submission-review
lens: widget
model-tier: judgment
permissions: read-only
output-contract: pre-submission-review-finding.v1
disallowedTools: [Edit, Write, NotebookEdit, Bash]
---

## Mandate

Review the widget. Run `/engine-status` first.
"""

SKILL_SRC = """---
name: engine-widget
description: Does widget things.
invocation: operator-typed
disable-model-invocation: true
allowed-tools: Bash(uv run *)
---

## Steps

1. Run `uv run --directory .engine -- python tools/widget.py --session "${CLAUDE_CODE_SESSION_ID}"`.
2. Then type `/engine-widget` again, or follow `.engine/operations/widget.md`.
"""


class _FixtureTree(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name
        _write(os.path.join(self.root, ".claude", "agents", "qa-review-widget.md"), AGENT_SRC)
        _write(os.path.join(self.root, ".claude", "skills", "engine-widget", "SKILL.md"), SKILL_SRC)

    def tearDown(self):
        self._tmp.cleanup()


class TestRenderTransforms(_FixtureTree):
    def test_agent_render_carries_the_floor_and_pins_no_model(self):
        codex_gen.generate(self.root)
        path = os.path.join(self.root, ".codex", "agents", "qa-review-widget.toml")
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
        self.assertEqual(data["sandbox_mode"], "read-only")
        self.assertNotIn("model", data)
        self.assertEqual(data["model_reasoning_effort"], "high")     # judgment tier
        self.assertIn("read-only", data["developer_instructions"])
        self.assertIn("Do not run shell commands", data["developer_instructions"],
                      "a Bash-denylisting source renders the no-shell instruction line")
        self.assertIn("Review the widget.", data["developer_instructions"])

    def test_stamped_effort_sources_from_frontmatter_and_model_never_leaks(self):
        # A persona stamped with model:/effort: by agent_bindings render — Codex takes the effort from the
        # stamped frontmatter (not the tier fallback, which would be 'high'), and STILL emits no model id
        # (a pinned model in a persona rots). This guards the codex_gen change + the no-model-leak rule.
        stamped = AGENT_SRC.replace("model-tier: judgment\n",
                                    "model-tier: judgment\nmodel: sonnet\neffort: low\n")
        _write(os.path.join(self.root, ".claude", "agents", "qa-review-widget.md"), stamped)
        codex_gen.generate(self.root)
        path = os.path.join(self.root, ".codex", "agents", "qa-review-widget.toml")
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
        self.assertNotIn("model", data, "the stamped model alias must never leak into the Codex render")
        self.assertNotIn("sonnet", validate.read(path), "no model alias appears anywhere in the render")
        self.assertEqual(data["model_reasoning_effort"], "low",
                         "effort comes from the stamped frontmatter, not the judgment-tier fallback (high)")

    def test_skill_render_rewrites_the_verb_and_strips_the_session_flag(self):
        codex_gen.generate(self.root)
        path = os.path.join(self.root, ".agents", "skills", "engine-widget", "SKILL.md")
        text = validate.read(path)
        self.assertIn("`$engine-widget`", text, "a backticked typed verb rewrites to the $ form")
        self.assertNotIn("CLAUDE_CODE_SESSION_ID", text, "the Claude session flag is stripped")
        self.assertNotIn("`/engine-widget`", text, "no typed reference keeps the Claude sigil")
        self.assertIn(".engine/operations/widget.md", text, "runbook paths are untouched")
        fm = validate.frontmatter(path)
        self.assertEqual(sorted(fm), ["description", "name"],
                         "the Codex frontmatter narrows to the two keys Codex reads")
        policy = validate.read(os.path.join(self.root, ".agents", "skills", "engine-widget",
                                            "agents", "openai.yaml"))
        self.assertIn("allow_implicit_invocation: false", policy)

    def test_generate_is_idempotent(self):
        self.assertTrue(codex_gen.generate(self.root))
        self.assertEqual(codex_gen.generate(self.root), [], "a second render changes nothing")


class TestSkillPolicyMirrorsTheSource(unittest.TestCase):
    """The self-election property, at the renderer. The policy is no longer a constant — it is read off the
    source's `invocation` — so BOTH branches need pinning: an operator-typed command must still refuse
    implicit invocation (the protection), and a deliberately model-reachable one must be allowed it (else the
    capability ships on Claude and is silently dead on Codex). An inverted or collapsed mapping here is a
    security regression, and before these tests nothing would have caught it."""

    def test_operator_typed_source_refuses_implicit_invocation(self):
        self.assertIn("allow_implicit_invocation: false", codex_gen.skill_policy("operator-typed"))

    def test_model_reachable_source_allows_implicit_invocation(self):
        self.assertIn("allow_implicit_invocation: true", codex_gen.skill_policy("model-auto"))

    def test_model_only_source_allows_implicit_invocation(self):
        self.assertIn("allow_implicit_invocation: true", codex_gen.skill_policy("model-only"))

    def test_an_omitted_invocation_fails_CLOSED(self):
        # The safety default. Model-reachability must be DECLARED: reading an omission as "model-auto" here
        # would make a forgotten key indistinguishable from a deliberate choice, and would hand the model a
        # command the operator meant to type. Doubt renders the protection.
        self.assertIn("allow_implicit_invocation: false", codex_gen.skill_policy(None))

    def test_an_unrecognized_value_fails_CLOSED(self):
        self.assertIn("allow_implicit_invocation: false", codex_gen.skill_policy("typo-auto"))


class TestCodexCoherenceExemptionIsSourceBound(unittest.TestCase):
    """The narrowed hard gate. `codex_skill_coherence_check` used to demand operator-only protection from
    EVERY engine Codex command; it now exempts one whose Claude source declares it model-reachable. That is a
    real narrowing of a merge-blocking security check, so its edges are pinned here: the protection still
    bites for an operator-typed command, the exemption applies only to a genuinely model-reachable source, and
    an absent or unreadable source fails TOWARD demanding protection rather than waiving it."""

    def setUp(self):
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import codex_skill_coherence_check as check
        self.check = check

    def test_operator_typed_source_still_demands_protection(self):
        self.assertTrue(self.check._source_demands_protection("engine-start"),
                        "an operator-typed command must still be held to the protection")

    def test_a_declared_model_reachable_source_is_exempt(self):
        self.assertFalse(self.check._source_demands_protection("engine-recall"),
                         "the deliberately model-reachable command is the one sanctioned exemption")

    def test_an_unknown_skill_fails_toward_demanding_protection(self):
        # No source to read is doubt, and doubt must not waive a security gate.
        self.assertTrue(self.check._source_demands_protection("engine-does-not-exist"))

    def test_the_exemption_must_be_DECLARED_not_inferred_from_an_omission(self):
        # The gate and the renderer must agree on failing closed: a command that simply omits the key is
        # protected, so a forgotten declaration can never silently become a model-startable command.
        self.assertNotIn("engine-recall", [])  # anchor: the exemption is by declaration, checked below
        import glob
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        exempt = [os.path.basename(os.path.dirname(p))
                  for p in sorted(glob.glob(os.path.join(root, "..", ".claude", "skills", "engine-*",
                                                         "SKILL.md")))
                  if not self.check._source_demands_protection(os.path.basename(os.path.dirname(p)))]
        self.assertEqual(exempt, ["engine-recall"])
        fm = validate.frontmatter(os.path.join(root, "..", ".claude", "skills", "engine-recall", "SKILL.md"))
        self.assertEqual(fm.get("invocation"), "model-auto",
                         "the exempt command must state its reachability, not imply it by omission")

    def test_every_operator_typed_skill_still_renders_refusing_implicit_invocation(self):
        # The end-to-end property the narrowing must not have broken: exactly one shipped command is
        # model-reachable, and every other one still refuses. Reads the real committed renders.
        import glob
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        reachable = []
        for policy_path in sorted(glob.glob(os.path.join(root, "..", ".agents", "skills", "engine-*",
                                                         "agents", "openai.yaml"))):
            name = os.path.basename(os.path.dirname(os.path.dirname(policy_path)))
            if "allow_implicit_invocation: true" in validate.read(policy_path):
                reachable.append(name)
        self.assertEqual(reachable, ["engine-recall"],
                         "only the deliberately model-reachable command may carry a reachable render")

    def test_the_residual_protection_depends_on_the_claude_side_coherence_rule(self):
        # Load-bearing dependency, pinned so a future narrowing of the sibling rule turns something red HERE.
        # This check reads `invocation` from the source; what keeps that value honest against the platform
        # flag (disable-model-invocation) is validate.skill_coherence_findings. If that rule stopped pairing
        # them, a skill could drop `invocation` while keeping the flag and slip past this check unnoticed.
        import inspect
        self.assertIn("disable-model-invocation", inspect.getsource(validate.skill_coherence_findings),
                      "the Codex exemption relies on the Claude-side rule pairing invocation with the flag")


class TestDriftGate(_FixtureTree):
    def test_in_sync_tree_is_clean(self):
        codex_gen.generate(self.root)
        self.assertEqual(codex_gen.check(self.root), [])

    def test_a_hand_edited_render_is_caught(self):
        codex_gen.generate(self.root)
        path = os.path.join(self.root, ".codex", "agents", "qa-review-widget.toml")
        with open(path, "a", encoding="utf-8") as fh:
            fh.write('\nsandbox_mode = "workspace-write"\n')
        problems = codex_gen.check(self.root)
        self.assertTrue(any("does not match its canonical source" in p for p in problems), problems)

    def test_a_stale_render_is_caught_when_the_source_changes(self):
        codex_gen.generate(self.root)
        src = os.path.join(self.root, ".claude", "skills", "engine-widget", "SKILL.md")
        _write(src, SKILL_SRC.replace("Does widget things.", "Does widget things, better."))
        problems = codex_gen.check(self.root)
        self.assertTrue(any("does not match its canonical source" in p for p in problems), problems)

    def test_an_orphaned_render_is_caught(self):
        codex_gen.generate(self.root)
        shutil.rmtree(os.path.join(self.root, ".claude", "skills", "engine-widget"))
        problems = codex_gen.check(self.root)
        self.assertTrue(any("has no canonical source" in p for p in problems), problems)

    def test_a_missing_render_is_caught(self):
        codex_gen.generate(self.root)
        os.remove(os.path.join(self.root, ".codex", "agents", "qa-review-widget.toml"))
        problems = codex_gen.check(self.root)
        self.assertTrue(any("is missing" in p for p in problems), problems)

    def test_no_skill_is_excluded_and_engine_routine_now_renders(self):
        # The routine backend shipped, so SKILL_EXCLUDE is empty: an engine-routine skill renders its twin
        # like every other (the old exclusion, which would have shipped a stub, is retired).
        self.assertEqual(codex_gen.SKILL_EXCLUDE, frozenset(),
                         "no skill is excluded once the routine backend exists")
        _write(os.path.join(self.root, ".claude", "skills", "engine-routine", "SKILL.md"),
               SKILL_SRC.replace("engine-widget", "engine-routine"))
        codex_gen.generate(self.root)
        self.assertTrue(os.path.isfile(os.path.join(self.root, ".agents", "skills", "engine-routine",
                                                    "SKILL.md")), "the twin now renders")
        self.assertTrue(os.path.isfile(os.path.join(self.root, ".agents", "skills", "engine-routine",
                                                    "agents", "openai.yaml")), "with its operator-only policy")
        self.assertEqual(codex_gen.check(self.root), [], "and the drift gate is clean")


class TestCommittedRendersInSync(unittest.TestCase):
    def test_the_committed_tree_is_render_clean(self):
        """The live drift gate over the REAL repo: every committed render matches its source."""
        self.assertEqual(codex_gen.check(), [])


if __name__ == "__main__":
    unittest.main()
