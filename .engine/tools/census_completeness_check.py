#!/usr/bin/env python3
"""Demo census-completeness recurrence guard (#424 U13c) — the custom/script entry for
engine/check/census-completeness.

The engine's standalone construction demos (`.engine/tools/demo_*.py`) are maintainer build evidence, not
operator capability. Each must either RETIRE at first run (walled in the first-run retirement census so it does
not travel into a generated repo) or have a real reason to travel — a surviving file that reaches it. This check
catches the drift where a demo silently does NEITHER: it is absent from the census AND nothing that stays in a
finished project uses it, so it would ship into every generated repo as leftover workshop clutter. Nine such
demos had accreted before this check existed (#424 U13a walled them).

What it is, honestly (the demo-fate policy vs the reference-closure fact): this is a CENSUS-COMPLETENESS / reference-closure-
consistency guard, NOT a fate-enforcement gate. The demo-fate policy gives a construction demo two sanctioned fates — retired
once a regression test covers it, or PROMOTED by an explicit logged decision to a standing capability — and a
promotion decision lives in the design canon (the sibling planning repo), which is not machine-readable from
here. So "a surviving file imports it" is NOT read as "it was promoted"; it is the reference-closure fact
that walling it would dangle that surviving reference. This check therefore only catches the ORPHAN case (a demo
that neither retires nor is structurally forced to travel); whether a legitimately-travelling demo should instead
be a standing operator feature is a judgment for the change's own review, not this check.

It is HOME-SCOPED: it acts only in the engine's own home repository — the checkout whose git origin equals the
recorded `home_repository` (via the shared `repo_identity.is_home_repo` seam) — so it no-ops in any
generated/deployed repo, where the demos are already retired and there is nothing to check. (Historically this
keyed off the root CLAUDE.md "construction governance" marker; the structural, non-inherited origin==home signal
replaces that proxy, which both travels into every copy and vanishes when the floor is promoted.) Like
memory_pointer_public_safety_check.py it therefore ships-and-no-ops rather than retiring (its check.json travels
in validators-core; retiring the script would dangle that reference — the #411 trap).

It reads the census from the committed manifest (.engine/provisioning/first-run-assets.json) — NEVER by importing
the retiring instantiator (the #411 reference-closure lesson). Within the construction repo it FAILS CLOSED:
if the census can't be read it emits a hard finding rather than silently passing — a completeness guard that
degrades to a pass would green-light the very drift it exists to catch. It runs as a hard CI
custom/script check: finding.v1 JSON on stdout, return 0 on a successful evaluation (empty array = every demo is
accounted for); one hard finding per orphan demo, each carrying the full plain-language consequence + fix.
"""
from __future__ import annotations
import ast
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate  # noqa: E402  (finding.v1, ROOT, env_override_path)
import repo_identity  # noqa: E402  (is_home_repo — the shared origin==home seam this now gates on)

_MANIFEST_REL = os.path.join(".engine", "provisioning", "first-run-assets.json")
_TOOLS_REL = os.path.join(".engine", "tools")
_PRUNE_DIRS = {"__pycache__", ".venv", ".pytest_cache", ".cache", ".uv"}


def _in_home_repo() -> bool:
    """True iff this checkout is the engine's OWN home repo — its git origin equals the recorded
    `home_repository`, the non-inherited signal a downstream copy never carries. Reads the REAL root
    (validate.ROOT) via the shared `repo_identity.is_home_repo` seam, NOT overridable — a backdoor past this gate
    would let the check fire in a deployed repo where the demos are gone. Mirrors
    memory_pointer_public_safety_check._in_home_repo so the two home-scoped checks agree on what "the
    home repo" is (both now delegate to the one seam, which is stronger than the old identical-marker binding).
    Kept as a named predicate because this check's tests monkeypatch it to drive the gate."""
    return repo_identity.is_home_repo(validate.ROOT)


def _census(root: str) -> "set | None":
    """The first-run retirement set (repo-relative file paths), read from the committed manifest as PLAIN DATA —
    never by importing the retiring instantiator (#411). Returns None when the manifest is missing, unreadable,
    not valid JSON, or structurally malformed (not an object, or `files` not a list) — anything that stops this
    check reading the census; check() turns that into one hard fail-closed finding. The manifest is permanent (it
    never self-retires), so a missing one is always a fault, never a legitimate state. A present-but-empty `files`
    list is a valid 'nothing retired' set, not a fault."""
    try:
        with open(os.path.join(root, _MANIFEST_REL), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("files"), list):
        return None
    return {f for f in data["files"] if isinstance(f, str)}


def _demos(root: str) -> list:
    """Every committed `.engine/tools/**/demo_*.py`, repo-relative. Recursive, so a demo in a package
    subdirectory is still enumerated (the engine's demos are flat today; the walk fails SAFE if that ever
    changes — a subdir demo reached via a dotted `import pkg.demo_x` keys on `pkg`, not the demo's stem, so it
    would be FLAGGED for review, never silently shipped; if subdir demos become real the reachability keying
    below would extend to match module paths). This tool is NOT itself a `demo_*.py` (it is
    census_completeness_check.py), so it never enumerates itself — the self-flag trap the plan gate caught."""
    out = []
    tools = os.path.join(root, _TOOLS_REL)
    for cur, dirs, names in os.walk(tools):
        dirs[:] = [d for d in dirs if d not in _PRUNE_DIRS]
        for name in names:
            if name.startswith("demo_") and name.endswith(".py"):
                out.append(os.path.relpath(os.path.join(cur, name), root))
    return sorted(out)


def _imports_of_surviving_non_demos(root: str, census: set) -> set:
    """The set of top-level module names imported by any SURVIVING NON-DEMO `.engine/tools/**/*.py` — a file that
    is neither in the census nor itself a `demo_*.py`. Tests count (they travel and their import keeps a demo
    reachable), demos do NOT (a demo kept alive only by another orphan demo is itself orphan drift). Uses
    `ast.walk` so an import INSIDE a function body is caught (e.g. pr_reconcile.py's `_demo()` delegates to
    `demo_pr_reconcile` at module-body depth > 0), and keys on the imported MODULE name so an alias
    (`import demo_x as demo`) still counts. Mirrors first_run_reference_closure_check._references's import legs
    — and, like that helper, assumes the engine's flat absolute-import tool layout: a relative import
    (`from . import demo_x`) or a package-name from-import (`from pkg import demo_x`) is out of that layout's
    reach and is not matched. The direction is FAIL-SAFE: an unmatched importer leaves its demo looking
    unreached, which SURFACES the demo as a hard finding for review — it never hides an orphan."""
    imported: set = set()
    tools = os.path.join(root, _TOOLS_REL)
    for cur, dirs, names in os.walk(tools):
        dirs[:] = [d for d in dirs if d not in _PRUNE_DIRS]
        for name in names:
            if not name.endswith(".py") or name.startswith("demo_"):
                continue
            rel = os.path.relpath(os.path.join(cur, name), root)
            if rel in census:  # a retired importer does not keep a demo reachable in a generated repo
                continue
            try:
                with open(os.path.join(cur, name), encoding="utf-8") as fh:
                    tree = ast.parse(fh.read(), filename=rel)
            except (OSError, SyntaxError):
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        imported.add(alias.name.split(".")[0])
                elif isinstance(node, ast.ImportFrom):
                    if node.level == 0 and node.module:
                        imported.add(node.module.split(".")[0])
                elif isinstance(node, ast.Call):
                    fn = node.func
                    is_import_call = (isinstance(fn, ast.Name) and fn.id == "__import__") or (
                        isinstance(fn, ast.Attribute) and fn.attr == "import_module")
                    if is_import_call and node.args and isinstance(node.args[0], ast.Constant) \
                            and isinstance(node.args[0].value, str):
                        imported.add(node.args[0].value.split(".")[0])
    return imported


def _message(demo_rel: str) -> str:
    """Operator-facing finding for an orphan demo: consequence + concrete file + the two real fixes, plain words.
    custom/script surfaces only the per-finding message, so it carries the whole story."""
    return (
        f"`{demo_rel}` is one of the engine's own build-evidence demonstration files, and it isn't accounted for: it isn't on the list "
        f"of files removed when a project is first set up, and nothing that stays in a finished project uses it. "
        f"As it stands it would ship into every generated project as leftover workshop clutter. Fix it one of two "
        f"ways: add it to the removal list (.engine/provisioning/first-run-assets.json and _FIRST_RUN_ASSET_FILES "
        f"in instantiator.py) so it's taken away at first setup, OR — if a shipped tool genuinely needs it — have "
        f"a file that stays in a finished project use it. (Whether it should instead become a standing feature "
        f"people can run is a call for this change's review, not this check.)")


def _manifest_fault_message() -> str:
    """Operator-facing finding when the census can't be read inside the engine's own home repository. Fail-closed, plain
    words: this check can't confirm no leftover demos ship, so it can't pass; restore the permanent list."""
    return (
        f"The engine can't read the list of files it removes when a project is first set up (`{_MANIFEST_REL}`). "
        f"Without that list this check can't confirm that no leftover demonstration files ship into a new project, "
        f"so it can't pass. This usually means the file was deleted, or its contents were damaged, in this change. "
        f"Restore it — it is permanent data that should always be present, so recover it from the project's "
        f"history — or fix its contents, then re-run this check.")


def check(root: str | None = None) -> list:
    """Every orphan demo as a list of `hard` findings (empty = every demo accounted for). No-ops (empty) OUTSIDE
    the home repo — the demos are already retired in a deployed copy. WITHIN the home repo it fails CLOSED: an
    unreadable/malformed census yields one hard finding, never a silent pass. Separated from main() so a test can
    drive it against a seeded fixture root."""
    if not _in_home_repo():
        return []
    root = root or validate.ROOT
    census = _census(root)
    if census is None:
        return [validate.finding("hard", _manifest_fault_message(), {"file": _MANIFEST_REL, "line": None})]
    reachable = _imports_of_surviving_non_demos(root, census)
    findings = []
    for demo_rel in _demos(root):
        if demo_rel in census:
            continue  # retired at first run — accounted for
        stem = os.path.splitext(os.path.basename(demo_rel))[0]
        if stem in reachable:
            continue  # a surviving non-demo file reaches it — travels for a real reference-closure reason
        findings.append(validate.finding("hard", _message(demo_rel), {"file": demo_rel, "line": None}))
    return findings


def main() -> int:
    # ENGINE_CENSUS_ROOT (unset in production) lets the negative-fixture meta-check point the scan at a seeded
    # mini-tree (an orphan demo + a census manifest that omits it + no surviving non-demo importer), so this
    # completeness guard is witnessed biting a real bad input (#286 fixture seam). The home-scope gate
    # still reads the REAL root, so the fixture bites only in the home repo's CI (never in a deployed one).
    print(json.dumps(check(validate.env_override_path("ENGINE_CENSUS_ROOT"))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
