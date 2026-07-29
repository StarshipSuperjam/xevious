#!/usr/bin/env python3
"""Codex-render integrity guard — the custom/script entry for engine/check/codex-agent-coherence.

Two legs over the Codex renders (eADR-0034):
  1. REVIEWER FLOOR: every engine Codex persona (`.codex/agents/*.toml` rendered from a canonical
     Claude persona) keeps `sandbox_mode = "read-only"` and pins NO `model` — a reviewer that can
     write, or a model id that rots in a persona file, is exactly the drift the render rule exists
     to prevent.
  2. RENDER SYNC: every committed render (personas AND the `.agents/skills/` twins) matches what the
     render tool would produce from its canonical `.claude/` source, and no engine-prefixed render
     exists without a source — a hand-edited, stale, or orphaned render goes red (the drift gate
     that makes the generated-render doctrine enforceable).

Reads local committed files only. Emits finding.v1 JSON on stdout, exit 0 on a successful
evaluation; a crash exits non-zero (the custom/script kind fails closed).
"""
from __future__ import annotations
import glob
import json
import os
import sys
import tomllib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate    # noqa: E402
import codex_gen   # noqa: E402


def _floor_findings(tier: str, agents_dir: str) -> list:
    out = []
    for path in sorted(glob.glob(os.path.join(agents_dir, "*.toml"))):
        rel = os.path.relpath(path, validate.ROOT)
        try:
            with open(path, "rb") as fh:
                data = tomllib.load(fh)
        except Exception as exc:  # noqa: BLE001 — unreadable render = a finding, never a crash
            out.append(validate.finding(tier, f"'{rel}' could not be read as TOML ({exc}); a "
                       f"reviewer persona the platform cannot parse silently vanishes from review. "
                       f"Regenerate it (codex_gen.py generate).", validate.loc(path)))
            continue
        if data.get("sandbox_mode") != "read-only":
            out.append(validate.finding(tier, f"'{rel}' is a review persona whose sandbox is not "
                       f"read-only — a reviewer must report findings, never edit the work. Restore "
                       f"sandbox_mode = \"read-only\" (edit the Claude source and regenerate).",
                       validate.loc(path)))
        if "model" in data:
            out.append(validate.finding(tier, f"'{rel}' pins a model id, which rots and silently "
                       f"changes who reviews. Personas never pin a model; remove it from the "
                       f"canonical source and regenerate.", validate.loc(path)))
    return out


def findings(tier: str, agents_dir: str | None = None) -> list:
    if agents_dir is not None:
        return _floor_findings(tier, agents_dir)   # fixture seam: the floor leg over a seeded dir
    out = _floor_findings(tier, os.path.join(validate.ROOT, ".codex", "agents"))
    for problem in codex_gen.check():
        out.append(validate.finding(tier, f"{problem} A render out of sync with its canonical "
                   f"Claude source means the two runtimes review with different instructions.",
                   None))
    return out


def main(argv: list) -> int:
    tier = os.environ.get("ENGINE_RULE_TIER", "hard")
    # ENGINE_CODEX_AGENT_FIXTURE_DIR (unset in production) points the floor leg at a seeded fixture
    # dir so the negative-fixture meta-check witnesses the guard biting a real bad input.
    fixture = validate.env_override_path("ENGINE_CODEX_AGENT_FIXTURE_DIR")
    print(json.dumps(findings(tier, agents_dir=fixture)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
