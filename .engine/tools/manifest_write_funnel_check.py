#!/usr/bin/env python3
"""Manifest-write funnel floor (StarshipSuperjam/engine-template#923) — the custom/script entry for
engine/check/manifest-write-funnel.

The engine must never write its own deployed manifest (`.engine/engine.json`) THROUGH a symlink or to a
path escaping the tree. StarshipSuperjam/engine-template#862 and StarshipSuperjam/engine-template#923 homed that invariant in `engine_write` and routed every known writer
through it — but that convergence rested on discipline: a NEW writer inherits the guard only if its author
remembers to. Eight writers were discovered across seven review rounds spanning two pull requests, and the
pattern kept recurring — so this check makes convergence MECHANICAL rather than disciplinary.

It statically scans `.engine/tools/**/*.py` and flags any raw write whose DESTINATION is the deployed
manifest slot, inside a function that does not route through the guarded funnel. Precisely — for each write
in a function's own (non-nested) body:
  1. a raw write PRIMITIVE — `os.replace`/`os.rename`, `shutil.copyfile`/`copy`/`copy2`/`move`, an
     `open(...)` in a write mode, a `pathlib` `.write_text`/`.write_bytes`, or a call to a GENERIC
     unguarded writer (`_write_json` / `_write_text` — the primitives whose real job is writing
     fixtures/release trees, deliberately left unguarded);
  2. whose DESTINATION argument is the deployed manifest slot — the path helpers `_engine_manifest_path` /
     `_engine_json_path` (called directly at the write, or via a local assigned from one), the
     `ENGINE_MANIFEST_REL` constant, or a literal `engine.json` joined onto `validate.ROOT`/`ENGINE_DIR`
     (the bare-literal-against-the-real-root shape, outside a fixture context); AND
  3. in a function that does NOT actually reference the funnel — a call to `engine_write.*`,
     `write_through_symlink_reason` (incl. the aliased `_write_through_symlink_reason`),
     `_write_engine_manifest`, or `_manifest_write_reason`, or the `EngineWriteRefused` exception. Marker
     matching is by EXACT identifier (a variable that merely CONTAINS `engine_write` does not count).

Correlating the taint with the specific write's destination (2) means a function that only READS the
manifest and writes an unrelated file is not flagged. Fixture/demo writers target a literal `engine.json`
joined onto a LOCAL temp root (`os.path.join(eng, "engine.json")`) under a fixture context, not the helper
and not `validate.ROOT`, so they never satisfy (2) — no allowlist is needed. This would have caught the
original bug shape (`_write_json(_engine_manifest_path(), engine)`).

DISCLOSED LIMITATIONS (single-function, syntactic — the convention is "use the helper at the write site"):
  - Indirection through a SECOND function (`p = _resolve(); open(p, "w")`, where `_resolve` returns the
    helper's value) is not followed — the destination's taint is traced only within the writing function.
  - The manifest path reaching a write buried inside a data structure (a list of `(path, data)` tuples the
    write iterates) is not traced.
  A writer using either shape escapes this gate; the funnel guard itself (`engine_write`) is still the real
  protection, and every real writer today names the slot directly at the write.

Scope: the DEPLOYED `.engine/engine.json` slot specifically — the clearest, best-defined invariant. It does
not police the other engine-owned slots (`.engine/pyproject.toml`, the sealed audit digest, `.engine/state/`),
which have their own path vocabularies and their own guards.

Runs as a hard CI custom/script check: finding.v1 JSON on stdout, return 0 on a successful evaluation (empty
array = every manifest write routes through the funnel). A crash returns non-zero, which the kind turns into
a hard fail-closed finding.
"""
from __future__ import annotations
import ast
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate  # noqa: E402  (finding.v1, ROOT)

_TOOLS_REL = os.path.join(".engine", "tools")
_PRUNE_DIRS = {"__pycache__", ".venv", ".pytest_cache", ".cache", ".uv"}

# (2) DEPLOYED-manifest path helpers — a call to one of these resolves the real slot.
_MANIFEST_HELPERS = ("_engine_manifest_path", "_engine_json_path")
_MANIFEST_REL_NAME = "ENGINE_MANIFEST_REL"
# (3) FUNNEL markers — EXACT identifiers (Name.id / Attribute.attr) that mean a write is guarded; plus any
# attribute access on the `engine_write` module (engine_write.write_json / .write_through_symlink_reason).
_FUNNEL_NAMES = frozenset({
    "write_through_symlink_reason", "_write_through_symlink_reason",
    "_manifest_write_reason", "_write_engine_manifest",
    "EngineWriteRefused", "_EngineWriteRefused",
})
# (1) GENERIC unguarded writers — a call to one of these is a raw write here; the guarded
# `engine_write.write_json` (attr `write_json`, no leading underscore) is deliberately NOT one.
_GENERIC_WRITERS = ("_write_json", "_write_text")
_PATHLIB_WRITES = ("write_text", "write_bytes")
# A fixture/demo context writes a throwaway `engine.json` under a REDIRECTED root (a tempdir), statically
# indistinguishable from the real slot by the bare literal alone — so the literal rule is suppressed there.
# The path-helper rule still applies (a fixture never calls the deployed-slot helper).
_FIXTURE_MARKERS = ("tempfile", "TemporaryDirectory", "mkdtemp", "_redirect_root")


def _tool_files(root: str) -> list:
    """Every committed `.engine/tools/**/*.py` that is a shipped tool — excluding `test_*.py` (they plant
    deliberate violations as fixtures). Recursive, to reach the `memory/` and `product_design/` packages."""
    out = []
    tools = os.path.join(root, _TOOLS_REL)
    for cur, dirs, names in os.walk(tools):
        dirs[:] = [d for d in dirs if d not in _PRUNE_DIRS]
        for name in names:
            if not name.endswith(".py") or name.startswith("test_"):
                continue
            out.append(os.path.relpath(os.path.join(cur, name), root))
    return sorted(out)


def _own_body(node):
    """Every node lexically within a function's body WITHOUT descending into a nested function/lambda scope
    — a nested closure is analysed as its own unit (mirrors in_tool_demo_failure_path_check._walk_no_scope)."""
    stack = list(node.body)
    while stack:
        cur = stack.pop()
        yield cur
        if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        stack.extend(ast.iter_child_nodes(cur))


def _callee_name(func) -> str | None:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _write_dest(call: ast.Call):
    """If `call` is a raw write primitive, return its DESTINATION-path argument node (or None when the
    primitive has no path destination, e.g. `os.fdopen` writes a file descriptor); return the sentinel
    `False` when it is not a write primitive at all."""
    f = call.func
    # os.replace / os.rename (atomic rename → writes the 2nd arg); shutil.copyfile/copy/copy2/move (2nd arg)
    if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name):
        if f.value.id == "os" and f.attr in ("replace", "rename"):
            return call.args[1] if len(call.args) >= 2 else None
        if f.value.id == "shutil" and f.attr in ("copyfile", "copy", "copy2", "move"):
            return call.args[1] if len(call.args) >= 2 else None
        if f.value.id == "os" and f.attr == "fdopen":
            return None if _mode_is_write(call) else False   # writes an fd, never a path
    # open(dest, mode) in a write mode
    if isinstance(f, ast.Name) and f.id == "open":
        return (call.args[0] if call.args else None) if _mode_is_write(call) else False
    # pathlib: <path-expr>.write_text(...) / .write_bytes(...) — the receiver is the destination
    if isinstance(f, ast.Attribute) and f.attr in _PATHLIB_WRITES:
        return f.value
    # a GENERIC unguarded writer (_write_json / _write_text) — 1st arg is the destination path
    if (isinstance(f, ast.Name) and f.id in _GENERIC_WRITERS) \
            or (isinstance(f, ast.Attribute) and f.attr in _GENERIC_WRITERS):
        return call.args[0] if call.args else None
    return False


def _mode_is_write(call: ast.Call) -> bool:
    """True if an `open`/`os.fdopen` call's mode argument admits writing (contains w/a/x/+). A missing mode
    is a read; a NON-literal mode is treated as a write (fail-closed — can't prove a read). The mode is
    positional arg 1 for both (`open(path, mode)`, `os.fdopen(fd, mode)`)."""
    mode = call.args[1] if len(call.args) >= 2 else None
    for kw in call.keywords:
        if kw.arg == "mode":
            mode = kw.value
    if mode is None:
        return False
    if isinstance(mode, ast.Constant) and isinstance(mode.value, str):
        return any(c in mode.value for c in "wax+")
    return True


def _references_validate_root(nodes) -> bool:
    return any(isinstance(n, ast.Attribute) and n.attr in ("ROOT", "ENGINE_DIR")
               and isinstance(n.value, ast.Name) and n.value.id == "validate" for n in nodes)


def _has_manifest_literal(nodes) -> bool:
    return any(isinstance(n, ast.Constant) and isinstance(n.value, str) and "engine.json" in n.value
              for n in nodes)


def _reassigns_validate_root(body_nodes) -> bool:
    for n in body_nodes:
        if isinstance(n, (ast.Assign, ast.AnnAssign)):
            targets = n.targets if isinstance(n, ast.Assign) else [n.target]
            for t in targets:
                for sub in ast.walk(t):
                    if isinstance(sub, ast.Attribute) and sub.attr in ("ROOT", "ENGINE_DIR") \
                            and isinstance(sub.value, ast.Name) and sub.value.id == "validate":
                        return True
    return False


def _fixture_context(body_nodes) -> bool:
    idents = {n.id for n in body_nodes if isinstance(n, ast.Name)} \
        | {n.attr for n in body_nodes if isinstance(n, ast.Attribute)}
    return bool(idents & set(_FIXTURE_MARKERS)) or _reassigns_validate_root(body_nodes)


def _tainted_locals(body_nodes) -> set:
    """Local names assigned (directly) from an expression that references a deployed-manifest producer — so a
    `path = _engine_manifest_path()` then `open(path, "w")` is traced within the function."""
    tainted = set()
    for n in body_nodes:
        if not isinstance(n, ast.Assign) or len(n.targets) != 1 or not isinstance(n.targets[0], ast.Name):
            continue
        rhs = list(ast.walk(n.value))
        hit = any(isinstance(x, ast.Call) and _callee_name(x.func) in _MANIFEST_HELPERS for x in rhs) \
            or any(isinstance(x, (ast.Name, ast.Attribute))
                   and (getattr(x, "id", None) == _MANIFEST_REL_NAME or getattr(x, "attr", None) == _MANIFEST_REL_NAME)
                   for x in rhs)
        if hit:
            tainted.add(n.targets[0].id)
    return tainted


def _dest_is_manifest(dest, tainted: set, fixture_ctx: bool) -> bool:
    """True if this write's destination expression resolves to the deployed manifest slot."""
    if dest is None:
        return False
    nodes = list(ast.walk(dest))
    for x in nodes:
        if isinstance(x, ast.Call) and _callee_name(x.func) in _MANIFEST_HELPERS:
            return True
        if isinstance(x, ast.Name) and x.id in tainted:
            return True
        if isinstance(x, ast.Name) and x.id == _MANIFEST_REL_NAME:
            return True
        if isinstance(x, ast.Attribute) and x.attr == _MANIFEST_REL_NAME:
            return True
    # the bare-literal-against-the-real-root shape, outside a fixture context
    return not fixture_ctx and _has_manifest_literal(nodes) and _references_validate_root(nodes)


def _is_guarded(body_nodes) -> bool:
    """True if the function actually references the funnel — an EXACT-identifier call/attr, not a substring:
    any `engine_write.<attr>`, or any of the funnel/exception names as a Name or Attribute."""
    for n in body_nodes:
        if isinstance(n, ast.Attribute):
            if isinstance(n.value, ast.Name) and n.value.id == "engine_write":
                return True
            if n.attr in _FUNNEL_NAMES:
                return True
        if isinstance(n, ast.Name) and n.id in _FUNNEL_NAMES:
            return True
    return False


def _message(rel: str, func: str) -> str:
    return (
        f"`{func}` in `{rel}` writes the engine's deployed manifest (.engine/engine.json) with a raw write "
        f"that does not route through the guarded write funnel (engine_write / _write_engine_manifest / "
        f"_manifest_write_reason). A manifest writer that bypasses the funnel can follow a planted shortcut "
        f"(symlink) and place the engine's own file outside the repository — the out-of-tree-write class "
        f"#862/#923 exist to close. Route this write through `engine_write.write_json` (or pre-flight the "
        f"destination with `engine_write.write_through_symlink_reason`) before writing, as every other "
        f"manifest writer does. If this is a fixture/demo writing a throwaway tree, target a local temp "
        f"root, not `validate.ROOT` or a path helper, so it is not the deployed slot.")


def _funcs(tree):
    """Every function/async-function scope, plus a synthetic module-scope unit for top-level statements (so a
    bare module-level manifest write is caught too)."""
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield n.name, n, list(_own_body(n))
    module_stmts = [s for s in tree.body
                    if not isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
    if module_stmts:
        nodes = []
        for s in module_stmts:
            nodes.extend(ast.walk(s))
        yield "<module>", tree, nodes


def check(root: str | None = None) -> list:
    """Every function with a raw write to the deployed manifest outside the funnel, as `hard` findings (empty
    = every manifest write routes through the funnel)."""
    root = root or validate.ROOT
    findings = []
    for rel in _tool_files(root):
        try:
            with open(os.path.join(root, rel), encoding="utf-8") as fh:
                tree = ast.parse(fh.read(), filename=rel)
        except (OSError, SyntaxError):
            continue
        for fname, node, body_nodes in _funcs(tree):
            fixture_ctx = _fixture_context(body_nodes)
            tainted = _tainted_locals(body_nodes)
            guarded = _is_guarded(body_nodes)
            flagged = False
            for n in body_nodes:
                if flagged or not isinstance(n, ast.Call):
                    continue
                dest = _write_dest(n)
                if dest is False:                       # not a write primitive
                    continue
                if _dest_is_manifest(dest, tainted, fixture_ctx) and not guarded:
                    findings.append(validate.finding("hard", _message(rel, fname),
                                                     {"file": rel, "line": getattr(node, "lineno", None)}))
                    flagged = True                       # one finding per function is enough
    return findings


def main() -> int:
    # ENGINE_ROOT (unset in production) lets the negative-fixture meta-check point the scan at a seeded
    # mini-tree carrying a manifest writer that bypasses the funnel, so the gate is witnessed biting a real
    # bad input (StarshipSuperjam/engine-template#286 fixture seam).
    print(json.dumps(check(validate.env_override_path("ENGINE_ROOT"))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
