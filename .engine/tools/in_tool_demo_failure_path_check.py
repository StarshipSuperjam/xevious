#!/usr/bin/env python3
"""In-tool demo failure-path floor (engine-template #171) — the custom/script
entry for engine/check/in-tool-demo-failure-path.

The engine's in-tool `demo`/`demo-*`/`hook-demo` subcommand is a promoted standing falsification capability — a falsification carried INSIDE a shipped tool, AI-run on demand, which travels into every generated
repo because the tool does. Every member inherits the behavioral-attestation shape law — it MUST be able to
fail (a recipe that can only succeed is not evidence). This is the optional durable floor:
it asserts each such subcommand has a REACHABLE NON-ZERO EXIT — an explicit conditional or non-zero `return`,
a `sys.exit` with a non-zero code, or an unguarded `raise` — i.e. the demo ACTS on the outcome it drives
rather than printing it and returning 0 regardless. A subcommand whose every exit is a literal `0`/`None` (a
print-only showcase) cannot fail and is flagged.

Scope: the in-tool subcommands that TRAVEL. It scans `.engine/tools/**/*.py`, excluding the standalone
`demo_*.py` files and `test_*.py` (a separate population — the standalone construction demos, governed by engine-template #191), and the first-run-retired assets (the `instantiator.py` construction
subcommands, removed at first run — read from the committed manifest, never imported). Honest static reach: it follows one level of `return _handler(...)` delegation and reads the dispatch branch's own returns;
a failure path produced only by deep indirection is a residual it does not claim to catch. It runs as a hard
CI custom/script check: finding.v1 JSON on stdout, return 0 on a successful evaluation (empty array = every
in-tool demo can fail). A crash returns non-zero, which the kind turns into a hard fail-closed finding.
"""
from __future__ import annotations
import ast
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate  # noqa: E402  (finding.v1, ROOT)

_MANIFEST_REL = os.path.join(".engine", "provisioning", "first-run-assets.json")
_TOOLS_REL = os.path.join(".engine", "tools")
_PRUNE_DIRS = {"__pycache__", ".venv", ".pytest_cache", ".cache", ".uv"}


def _is_demo_token(s) -> bool:
    """A dispatched subcommand token in the governed in-tool demo class: `demo`, `hook-demo`, or `demo-*`."""
    return isinstance(s, str) and (s == "demo" or s == "hook-demo" or s.startswith("demo-"))


def _retired(root: str) -> set:
    """The first-run-retired file set (repo-relative), read from the committed manifest as plain data — never
    by importing the instantiator. The retired construction subcommands (instantiator.py demo/apply-demo/
    finish-demo/collision-demo) are removed at first run and so are out of the travelling class. Empty set if
    the manifest is unreadable (manifest presence is another check's job)."""
    try:
        with open(os.path.join(root, _MANIFEST_REL), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return set()
    return {f for f in data.get("files", []) if isinstance(f, str)}


def _tool_files(root: str, retired: set) -> list:
    """Every committed `.engine/tools/**/*.py` that is a shipped TOOL — excluding `test_*.py`, the standalone
    `demo_*.py` files, and the first-run-retired assets. Recursive, to reach the `memory/` package tools."""
    out = []
    tools = os.path.join(root, _TOOLS_REL)
    for cur, dirs, names in os.walk(tools):
        dirs[:] = [d for d in dirs if d not in _PRUNE_DIRS]
        for name in names:
            if not name.endswith(".py") or name.startswith("test_") or name.startswith("demo_"):
                continue
            rel = os.path.relpath(os.path.join(cur, name), root)
            if rel not in retired:
                out.append(rel)
    return sorted(out)


def _walk_no_scope(stmts):
    """Yield every node lexically within `stmts` WITHOUT descending into a nested function/lambda scope — so a
    `raise` or non-zero `return` inside a local helper-closure of the dispatch branch is not miscounted as the
    branch's own failure path."""
    stack = list(stmts)
    while stack:
        node = stack.pop()
        yield node
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue  # a nested scope: yield the def itself but never descend into its body
        for child in ast.iter_child_nodes(node):
            stack.append(child)


def _return_can_fail(value, funcs: dict, seen: set) -> bool:
    """True if a `return <value>` can yield a non-zero exit. A bare `return`/`return None`/`return 0`/
    `return False` is a success-only exit; a non-zero constant, or any computed value (an `x if c else y`
    conditional, a comparison, a name, a non-local call) can carry non-zero. A `return _handler(...)` to a
    LOCAL function is resolved by analysing that function (one level of delegation, cycle-guarded)."""
    if value is None:
        return False
    if isinstance(value, ast.Constant):
        return value.value not in (0, None, False)
    if isinstance(value, ast.Call) and isinstance(value.func, ast.Name) and value.func.id in funcs \
            and value.func.id not in seen:
        return _fn_can_fail(funcs[value.func.id], funcs, seen | {value.func.id})
    return True


def _body_can_fail(stmts, funcs: dict, seen: set) -> bool:
    """True if any statement directly in `stmts` (not in a nested scope) provides a reachable non-zero exit:
    a non-zero `return`, an unguarded `raise`, or a `sys.exit(<non-zero>)`."""
    for node in _walk_no_scope(stmts):
        if isinstance(node, ast.Return) and _return_can_fail(node.value, funcs, seen):
            return True
        if isinstance(node, ast.Raise):
            return True
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "exit" \
                and isinstance(node.func.value, ast.Name) and node.func.value.id == "sys" and node.args \
                and not (isinstance(node.args[0], ast.Constant) and node.args[0].value in (0, None, False)):
            return True
    return False


def _fn_can_fail(fndef, funcs: dict, seen: set) -> bool:
    return _body_can_fail(fndef.body, funcs, seen)


def _message(rel: str, token: str) -> str:
    """The operator-facing finding: consequence + concrete subcommand + disposition, in plain words."""
    return (
        f"The `{token}` demo subcommand in `{rel}` cannot fail: it drives the real surface but its only exit "
        f"is a literal success (0/None), so it prints the outcome and reports success even when the behaviour "
        f"is broken. A behavioral demonstration must be able to fail — a recipe that can only succeed is not "
        f"evidence. Give it a self-check that acts on the result it already "
        f"computes and returns non-zero on a mismatch (the `module_coherence.py` demo is the pattern), or "
        f"demote it out of the `demo` subcommand convention.")


def check(root: str | None = None) -> list:
    """Every in-tool demo subcommand with no reachable failure path, as a list of `hard` findings (empty =
    every in-tool demo can fail). A demo dispatch is found as an `if`/`elif` branch whose test names a demo
    token (`demo`/`hook-demo`/`demo-*`); the branch passes when it, or the local handler it returns, has a
    reachable non-zero exit."""
    root = root or validate.ROOT
    retired = _retired(root)
    findings = []
    for rel in _tool_files(root, retired):
        try:
            with open(os.path.join(root, rel), encoding="utf-8") as fh:
                tree = ast.parse(fh.read(), filename=rel)
        except (OSError, SyntaxError):
            continue
        funcs = {n.name: n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        for node in ast.walk(tree):
            if not isinstance(node, ast.If):
                continue
            tokens = sorted({c.value for c in ast.walk(node.test)
                             if isinstance(c, ast.Constant) and _is_demo_token(c.value)})
            if not tokens:
                continue
            if not _body_can_fail(node.body, funcs, set()):
                for tok in tokens:
                    findings.append(validate.finding("hard", _message(rel, tok),
                                                      {"file": rel, "line": node.lineno}))
    return findings


def main() -> int:
    # ENGINE_ROOT (unset in production) lets the negative-fixture meta-check point the scan at a
    # seeded mini-tree carrying a demo subcommand that cannot fail, so the gate is witnessed biting a
    # real bad input (#286).
    print(json.dumps(check(validate.env_override_path("ENGINE_ROOT"))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
