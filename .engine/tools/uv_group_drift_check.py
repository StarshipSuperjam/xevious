#!/usr/bin/env python3
"""uv-group drift gate — the thin custom/script entry for engine/check/uv-group-drift.

Runs as a `custom/script` check rule in the CI suite: it confirms the committed `[tool.uv] default-groups`
in `.engine/pyproject.toml` still equals what the present module set derives
(`module_manager.derive_uv_groups`), so a hand-edit, a botched merge, or a missed re-derivation turns
engine-ci red until the selection is synced. This is the standing, first-class drift gate that closes the
hand-maintained CI uv-group seam that the pyproject comment cedes to the module manager, which
shipped the derivation + a unit test over `remove`'s write path; this rule guards the committed value against
drift from ANY source, including `add`'s write path and a direct edit.

It reads local committed files only — no network, no token (least-privilege: it does NOT opt into
`params.pass_token`, modelled on `self_map_check.py`) — so it runs unchanged in the head-checkout engine-ci
context. It emits finding.v1 JSON on stdout (the custom/script machine channel) and returns 0 on a successful
evaluation: an empty array when the selection is in sync, one `hard` finding (carrying the plain-language fix
command) on drift. An internal crash returns non-zero, which the custom/script kind turns into a hard
fail-closed finding (a guard can never silently pass).
"""
from __future__ import annotations
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate          # noqa: E402  (finding.v1)
import module_manager    # noqa: E402  (the derivation + the committed reader)


def check() -> dict | None:
    """One `hard` finding if the committed `[tool.uv] default-groups` differs from the derived selection,
    else None. Exact-list comparison: `remove`/`add`/`sync-groups` write the derived value (sorted), so the
    canonical committed form is that sorted list; an unsorted-but-equivalent hand-edit is flagged and the
    fixer re-canonicalizes it."""
    derived = module_manager.derive_uv_groups()
    # ENGINE_PYPROJECT_PATH (unset in production) lets the negative-fixture meta-check point the
    # committed-side read at a seeded pyproject whose default-groups drifts from the real derived
    # selection, so the drift gate is witnessed biting a real bad input (#286). The derived side
    # still reads the real installed module set, so the mismatch is genuine.
    committed = module_manager.committed_default_groups(validate.env_override_path("ENGINE_PYPROJECT_PATH"))
    if derived == committed:
        return None
    return validate.finding(
        "hard",
        f"The tool-runtime's dependency-group selection is out of date. The committed default-groups in "
        f".engine/pyproject.toml is {committed or '[]'}, but the installed modules now derive "
        f"{derived or '[]'}. This selection decides which modules' Python dependencies the engine installs, "
        f"so a mismatch can install the wrong set. To fix it, run "
        f"`uv run --directory .engine -- python tools/module_manager.py sync-groups` and commit the change.",
        {"file": ".engine/pyproject.toml", "line": None})


def main() -> int:
    f = check()
    print(json.dumps([f] if f else []))
    return 0


if __name__ == "__main__":
    sys.exit(main())
