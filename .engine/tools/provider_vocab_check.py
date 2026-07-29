#!/usr/bin/env python3
"""Provider-vocabulary confinement — the custom/script entry for
engine/check/provider-vocabulary-confinement (eADR-0034).

The dual-runtime design holds only while provider-specific vocabulary stays inside the named seams:
a future feature that reads a runtime's session env var directly, or keys on a runtime's tool name
outside the normalizer, ships broken on the other runtime with every other check green. This guard
scans the engine's live tool code for the provider tokens and goes red when one appears in a file
outside that token's allowlisted seam set — the parity promise for CODE, where the capability-parity
check covers SURFACES. Tests and demos are out of scope (they exercise the seams by name).

To fix a red: route the new code through providers.py (session identity via
providers.resolve_session/session_from_env; tool identity via the normalized payload), or — when a
file genuinely becomes part of a seam — add it to the allowlist here IN THE SAME reviewed change.
Emits finding.v1 JSON on stdout, exit 0 on a successful evaluation; a crash exits non-zero.
"""
from __future__ import annotations
import glob
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate  # noqa: E402

# token -> the tool files (relative to the scanned dir) allowed to carry it. Comments count as
# carriage too — a token in prose invites the next copy into code — so membership is per-file.
ALLOWLIST = {
    "CLAUDE_CODE_": {
        "providers.py",            # the seam: the env chain itself
        "hooks.py",                # the platform block-cap override env var
        "modes.py",                # the CLI flag's documented expansion source
        "engine_status.py",        # the skill-passed flag's documented source
        "codex_gen.py",            # the render transform that strips the flag from skill bodies
        "memory/capture.py",       # the historical session/transcript env names (superset chain)   # the historical session env name (superset chain)
    },
    "ENGINE_PROVIDER": {
        "providers.py",            # the seam: detection itself
        "hooks.py",                # the command renderer documents the shim's provider tagging
        "boot.py",                 # the heartbeat records the detected provider
        "engine_help.py",          # the ambient-prefix rendering reads the seam's env name
    },
    "apply_patch": {
        "providers.py",            # the seam: the normalization map itself
        "modes.py",                # the second-belt denied set
        "validate.py",             # the second-belt mutating-tool set
        "wiring.py",               # the seam-vocabulary docs on the codex wiring targets
    },
    "local_shell": {
        "providers.py",            # the seam: the shell-name normalization map
        "modes.py",                # the second-belt shell-name set
    },
    "unified_exec": {
        "providers.py",            # the seam: the shell-name normalization map
        "modes.py",                # the second-belt shell-name set
    },
    "turn_id": {
        "providers.py",            # the seam: provider detection's payload sniff
    },
}


def findings(tier: str, tools_dir: str | None = None) -> list:
    base = tools_dir or os.path.join(validate.ROOT, ".engine", "tools")
    out = []
    for path in sorted(glob.glob(os.path.join(base, "**", "*.py"), recursive=True)):
        rel = os.path.relpath(path, base).replace(os.sep, "/")
        name = os.path.basename(rel)
        if name.startswith(("test_", "demo_")) or name == "provider_vocab_check.py":
            continue   # tests/demos exercise the seams by name; this file IS the allowlist
        try:
            text = validate.read(path)
        except OSError:
            continue
        for token, allowed in ALLOWLIST.items():
            if token in text and rel not in allowed:
                out.append(validate.finding(tier,
                           f"'{rel}' carries the provider-specific token '{token}' outside the "
                           f"provider seam — a runtime-specific assumption is leaking past the "
                           f"normalization boundary, so the feature would silently break on the "
                           f"other runtime. Route it through providers.py (or, if this file is "
                           f"genuinely becoming part of the seam, add it to the allowlist in "
                           f"provider_vocab_check.py in the same reviewed change).",
                           validate.loc(path)))
    return out


def main(argv: list) -> int:
    tier = os.environ.get("ENGINE_RULE_TIER", "hard")
    # ENGINE_VOCAB_FIXTURE_DIR (unset in production) points the scan at a seeded fixture dir so the
    # negative-fixture meta-check witnesses the guard biting a real bad input.
    fixture = validate.env_override_path("ENGINE_VOCAB_FIXTURE_DIR")
    print(json.dumps(findings(tier, tools_dir=fixture)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
