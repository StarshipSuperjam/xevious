#!/usr/bin/env python3
"""First-run reference-closure gate (issue #150) — the custom/script entry for
engine/check/first-run-reference-closure.

The first-run installer removes its own setup assets at the Retire step once setup is sound (provisioning's
definition-of-record). This check enforces the locked reference-closure invariant: NO file that SURVIVES that
removal may statically reference a removed first-run asset — by `import`, `importlib`, a subprocess invocation
of its path, or a hard-coded read of its path. In a generated repo such a reference fails the adopter's first
CI run at import/collection time with a Python error a non-engineer cannot read (`unittest discover` aborts
before any test runs). A surviving file that needs that machinery must itself be a first-run asset (removed in
the same pass) or stop referencing it; there is no "guard the import" escape (a top-level try/except still
red-fails when a test body later names the absent module).

It reads the removed-asset set from the committed manifest (.engine/provisioning/first-run-assets.json) — it
NEVER imports the instantiator it is about to verify nothing references (that would make this check the next
dangler).

Regenerated-asset carve-out (#404): a FEW removed first-run assets are not permanently gone — the engine
rewrites them at runtime after the Retire step. The audit digest is the case: it retires so a fresh repo starts
with no inherited self-review, then the audit cron writes a genuine one. A surviving reference to such a path is
not the dangling reference this gate exists to catch, so the path leg skips exactly the paths named in
`_REGENERATED_RETIRED_ASSETS`. That is a deliberately NARROW, named set — NOT "anything a module `provides`":
most provided-and-retired paths (the first-run setup guide) are construction-only and gone for good, and a
reference to those must still be caught. The import leg is never carved out (a genuine `import` of a removed
module always fails closed). It runs as a hard CI custom/script check: finding.v1 JSON on stdout, return 0 on a successful
evaluation (empty array = closed; one finding per surviving reference, each carrying the full plain-language
consequence + disposition the operator reads). A crash returns non-zero, which the kind turns into a hard
fail-closed finding (a guard can never silently pass).

Honest static reach: the `import` / `importlib` / `__import__` and literal subprocess-or-path
references are caught completely; a path assembled at runtime (a computed/indirected reference) is a behavioral
residual the invariant still forbids but this static check catches only best-effort — it is not claimed
complete. (A relative import — `from . import x` — is likewise not matched; the engine's tools use a flat,
absolute-import layout, so a removed top-level module is never reachable relatively, and such an import would
not resolve here anyway.) It no-ops (passes) once the first-run machinery is already removed — i.e. when no removed Python
module is present on disk (the adopter's post-setup tree) — there is nothing left to be closed over.
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

# The removed first-run assets the engine REGENERATES at runtime, so a reference to them is never left dangling
# (module docstring, #404). Deliberately a NAMED set — NOT "anything a module `provides`". The audit cron
# rewrites the digest, so a fresh repo gets a real one on its first run; but most provided-and-retired paths (the
# first-run setup guide, `.engine/operations/first-run.md`, is provided by core) are construction-only and gone
# for GOOD after retirement, and a surviving reference to those must still be caught. Add a path here only when
# something genuinely re-creates it after the Retire step — never merely because a manifest lists it.
_REGENERATED_RETIRED_ASSETS = frozenset({".engine/audits/audit-digest.md"})


def _load_removed(root: str):
    """The removed first-run asset set, read from the committed manifest as plain data (never by importing the
    instantiator). Returns (removed_files, removed_modules): repo-relative file paths, and the bare module names
    of the removed `.py` files (an `import <name>` of which a survivor must not carry). Returns (None, None) when
    the manifest is missing, unreadable, not valid JSON, or structurally malformed (not a JSON object, or its
    `files` is not a list) — anything that stops this check computing the removed set; check() turns that into one
    hard fail-closed finding. The manifest is permanent (it never self-retires), so a missing one is always a
    fault, never a legitimate state. A present-but-empty `files` list is NOT a fault — it is a valid 'nothing to
    remove' set. The manifest's own shape is separately governed by the engine/check/first-run-assets schema
    check; this leg fails closed on any shape it cannot read, so a bad manifest never reaches a raw traceback."""
    try:
        with open(os.path.join(root, _MANIFEST_REL), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None, None
    if not isinstance(data, dict) or not isinstance(data.get("files"), list):
        return None, None          # structurally malformed — route through the plain-language fault finding
    files = [f for f in data["files"] if isinstance(f, str)]
    modules = {os.path.splitext(os.path.basename(f))[0] for f in files if f.endswith(".py")}
    return set(files), modules


def _survivors(root: str, removed_files: set) -> list:
    """Every committed `.engine/tools/**/*.py` that is NOT itself a removed first-run asset, repo-relative. The
    walk is RECURSIVE — `unittest discover -s tools -p 'test_*.py'` recurses into package subdirectories (e.g.
    `.engine/tools/memory/`), so a dangling import there breaks adopter collection just the same; the scan must
    mirror that reach, not a single-level glob."""
    out = []
    tools = os.path.join(root, _TOOLS_REL)
    for cur, dirs, names in os.walk(tools):
        dirs[:] = [d for d in dirs if d not in _PRUNE_DIRS]
        for name in names:
            if not name.endswith(".py"):
                continue
            rel = os.path.relpath(os.path.join(cur, name), root)
            if rel not in removed_files:
                out.append(rel)
    return sorted(out)


def _references(tree: ast.AST, removed_modules: set, removed_files: set, regenerated: set = frozenset()) -> list:
    """Static references to a removed asset within one parsed survivor. Returns (lineno, kind, target) tuples:
    kind is 'import' (an import/importlib/__import__ of a removed module) or 'path' (a string literal equal to a
    removed file's exact repo-relative path — the read/subprocess-by-path leg). Exact-path match only, so a mere
    prose mention of a name (e.g. a docstring) is never flagged. The path leg skips a target in `regenerated`
    (a removed asset the engine rewrites at runtime, so not permanently gone; #404); the import leg is
    never carved out (a genuine import of a removed module always fails closed)."""
    refs = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in removed_modules:
                    refs.append((node.lineno, "import", alias.name))
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module and node.module.split(".")[0] in removed_modules:
                refs.append((node.lineno, "import", node.module))
        elif isinstance(node, ast.Call):
            fn = node.func
            is_import_call = (isinstance(fn, ast.Name) and fn.id == "__import__") or (
                isinstance(fn, ast.Attribute) and fn.attr == "import_module")
            if is_import_call and node.args and isinstance(node.args[0], ast.Constant) \
                    and isinstance(node.args[0].value, str) \
                    and node.args[0].value.split(".")[0] in removed_modules:
                refs.append((node.lineno, "import", node.args[0].value))
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) \
                and node.value in removed_files and node.value not in regenerated:
            refs.append((node.lineno, "path", node.value))
    return refs


def _message(survivor: str, kind: str, target: str) -> str:
    """The operator-facing finding: consequence + concrete file/reference + disposition, in plain words (no
    engineer shorthand). custom/script surfaces only the per-finding message, so it carries the whole story."""
    how = (f"imports `{target}`" if kind == "import" else f"reads or runs `{target}` by name")
    return (
        f"`{survivor}` {how}, which the engine removes when a project is first set up. In a new project that "
        f"reference would make the very first automated check stop before it starts, with a programmer error its "
        f"owner cannot read. Fix it one of two ways: stop pointing at the removed setup code (move what you need "
        f"into a file that stays), or add `{survivor}` to the removed-files list "
        f"(.engine/provisioning/first-run-assets.json and _FIRST_RUN_ASSET_FILES in instantiator.py) so it is "
        f"taken away in the same pass — not by hiding the reference behind an error-catch, which still fails when "
        f"the code later names the missing file.")


def _manifest_fault_message() -> str:
    """Operator-facing finding for a missing, damaged, or unreadable removed-files list. Plain words, no engineer
    shorthand: the consequence (this safety check cannot run, so it cannot pass), the concrete file, and the fix
    (restore or repair it). custom/script surfaces only this per-finding message, so it carries the whole story."""
    return (
        f"The engine can't read the list of setup files it removes when a project is first created "
        f"(`{_MANIFEST_REL}`). Without that list this safety check can't confirm a new project won't break on its "
        f"first run, so it can't pass. This usually means the file was deleted, or its contents were damaged, in "
        f"this change. Restore it — it is permanent data that should always be present, so recover it from the "
        f"project's history — or fix its contents, then re-run this check.")


def check(root: str | None = None) -> list:
    """Every surviving reference to a removed first-run asset, as a list of `hard` findings (empty = closed).
    Fails closed (one hard finding) when the removed-files list is missing, unreadable, or malformed — the check
    cannot do its job without it, and the list is permanent (it never self-retires), so a missing one is always a
    fault. Still no-ops to a pass in the separate, legitimate post-setup state: the first-run machinery is already
    removed (no removed Python module is present on disk — the adopter's post-setup tree)."""
    root = root or validate.ROOT
    removed_files, removed_modules = _load_removed(root)
    if removed_files is None:
        return [validate.finding("hard", _manifest_fault_message(), {"file": _MANIFEST_REL, "line": None})]
    removed_py = [f for f in removed_files if f.endswith(".py")]
    if removed_py and not any(os.path.isfile(os.path.join(root, f)) for f in removed_py):
        return []  # machinery already removed (post-setup tree) — nothing left to be closed over
    findings = []
    for survivor in _survivors(root, removed_files):
        try:
            with open(os.path.join(root, survivor), encoding="utf-8") as fh:
                tree = ast.parse(fh.read(), filename=survivor)
        except (OSError, SyntaxError):
            continue
        for lineno, kind, target in _references(tree, removed_modules, removed_files, _REGENERATED_RETIRED_ASSETS):
            findings.append(validate.finding("hard", _message(survivor, kind, target),
                                             {"file": survivor, "line": lineno}))
    return findings


def main() -> int:
    # ENGINE_ROOT (unset in production) lets the negative-fixture meta-check point the scan at a seeded
    # mini-tree (a removed-asset manifest + the removed module still on disk + a survivor that imports
    # it), so the closure gate is witnessed biting a real bad input (#286).
    print(json.dumps(check(validate.env_override_path("ENGINE_ROOT"))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
