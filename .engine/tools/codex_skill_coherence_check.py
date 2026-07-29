#!/usr/bin/env python3
"""Codex-skill invocation guard — the custom/script entry for engine/check/codex-skill-coherence.

The Codex twin of the skill self-election guard. Most ENGINE Codex skills (engine-prefixed directories
under `.agents/skills/`) are operator-typed, and on Codex the operator-only property is not
a frontmatter flag but the companion policy file — `agents/openai.yaml` carrying
`policy.allow_implicit_invocation: false` (the model must never start the command on its own; the
operator's explicit $-invocation still works). The bar is each skill's OWN Claude source: a command
deliberately declared model-reachable (recall) is allowed a reachable twin, since pinning every skill
to operator-only would ship such a capability on one runtime and silently disable it on the other.
This check goes red when an engine Codex skill ships
without that companion, or with the policy absent or not false — so the self-election protection
cannot be dropped on one runtime while the other stays green (eADR-0034). Operator-authored,
un-prefixed product skills in the same directory are not engine-governed.

Reads local committed files only. Emits finding.v1 JSON on stdout, exit 0 on a successful
evaluation; a crash exits non-zero (the custom/script kind fails closed).
"""
from __future__ import annotations
import glob
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate  # noqa: E402

_MESSAGE = ("An engine command that only the operator should type must be one the assistant cannot "
            "start on its own — on Codex that protection is the skill's agents/openai.yaml with "
            "policy.allow_implicit_invocation: false. Regenerate the render (uv run --directory "
            ".engine --frozen -- python tools/codex_gen.py generate) or restore the policy file.")


def _policy_disallows_implicit(policy_path: str) -> bool:
    """True iff the companion policy file exists and pins allow_implicit_invocation to false."""
    try:
        import yaml
        with open(policy_path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except Exception:  # noqa: BLE001 — unreadable/malformed reads as unprotected (fail toward the finding)
        return False
    policy = (data or {}).get("policy") if isinstance(data, dict) else None
    return isinstance(policy, dict) and policy.get("allow_implicit_invocation") is False


def _claude_skills_root(skills_dir: "str | None") -> str:
    """The Claude sources matching the rendered tree being checked. When `findings` is pointed at a fixture
    tree, the sources must come from BESIDE it — a hard gate whose verdict depends on the real repo's files
    while checking a fixture is reading state it was never handed, and its bite would then rest on the
    accident that no real skill shares the fixture's name."""
    if not skills_dir:
        return os.path.join(validate.ROOT, ".claude", "skills")
    sibling = os.path.join(os.path.dirname(os.path.abspath(skills_dir)), "claude-skills")
    return sibling if os.path.isdir(sibling) else os.path.join(validate.ROOT, ".claude", "skills")


def _source_demands_protection(name: str, skills_root: "str | None" = None) -> bool:
    """True unless the Claude source for `name` EXPLICITLY declares itself model-reachable. Everything else —
    an operator-typed declaration, an omitted invocation, an unrecognized value, a missing or unreadable
    source — demands the protection. The exemption must be declared rather than inferred from an omission, so
    that a forgotten key can never quietly hand the model a command the operator meant to type; every form of
    doubt fails toward the finding."""
    root = skills_root or os.path.join(validate.ROOT, ".claude", "skills")
    src = os.path.join(root, name, "SKILL.md")
    if not os.path.isfile(src):
        return True
    try:
        return (validate.frontmatter(src) or {}).get("invocation") not in ("model-auto", "model-only")
    except Exception:  # noqa: BLE001 — unreadable source reads as protected-required (fail toward the finding)
        return True


def findings(tier: str, skills_dir: str | None = None) -> list:
    """The self-election safety property: a command the OPERATOR types must never be one the assistant can
    start on its own. The bar is the Claude source's own `invocation`, not a blanket rule — a deliberately
    model-reachable command (recall, for instance) is allowed to be reachable on Codex too, and pinning every
    skill to operator-only would silently disable such a capability on one of the two runtimes."""
    base = skills_dir or os.path.join(validate.ROOT, ".agents", "skills")
    sources = _claude_skills_root(skills_dir)
    out = []
    for skill_md in sorted(glob.glob(os.path.join(base, "engine-*", "SKILL.md"))):
        skill_dir = os.path.dirname(skill_md)
        name = os.path.basename(skill_dir)
        policy_path = os.path.join(skill_dir, "agents", "openai.yaml")
        if not _source_demands_protection(name, sources):
            continue                       # deliberately model-reachable — its twin may be reachable as well
        if not _policy_disallows_implicit(policy_path):
            out.append(validate.finding(
                tier,
                f"The engine Codex command '{name}' is missing its operator-only protection: the "
                f"assistant could start it on its own. {_MESSAGE}",
                validate.loc(skill_md)))
    return out


def main(argv: list) -> int:
    tier = os.environ.get("ENGINE_RULE_TIER", "hard")
    # ENGINE_CODEX_SKILL_FIXTURE_DIR (unset in production) points the scan at a seeded fixture tree so
    # the negative-fixture meta-check witnesses the guard biting a real bad input.
    fixture = validate.env_override_path("ENGINE_CODEX_SKILL_FIXTURE_DIR")
    print(json.dumps(findings(tier, skills_dir=fixture)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
