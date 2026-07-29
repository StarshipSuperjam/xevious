#!/usr/bin/env python3
"""Render a plain-language change-profile for a pull request.

Run: uv run --directory .engine -- python tools/scope_profile.py
     (from a branch, with `origin/main` fetched; pass a different base as the first argument.)

Prints markdown to paste into the pull request's ## Scope section. This is REPORT-ONLY: it computes
nothing that gates a merge. Its whole job is to make the *shape* of a change legible to an operator who
reads the pull request rather than the code — how big it is, what kinds of engine surface it touches, and
whether it stands alone — so a change is judged by what it does, not by its line count.

The surface-kind of a file is read from the engine's own wiring map (`.engine/knowledge/graph.json`):
each catalogued file carries a `type` (agent, check, tool, operation, …); a file the map does not
catalogue buckets as "other". The aggregation is decoupled from git (it takes an explicit list of changed
files) so the same real logic runs under the CLI, the tests, and the demo. Git failures are signalled as
None (never a fabricated zero), so the profile can say "could not read the diff" rather than mislead the
operator into thinking a real change is empty.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

# repo root — three directories up from .engine/tools/scope_profile.py
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
GRAPH = os.path.join(ROOT, ".engine", "knowledge", "graph.json")


def _git(args: list, *, run=subprocess.run) -> "str | None":
    """Run a read-only git command from the repo root. Returns stdout on success, or None on ANY
    failure (non-zero exit, missing binary, timeout) — never '' — so a caller can tell a genuine empty
    result from a git that could not run, and never render a fabricated zero. `run` is injectable for
    tests."""
    try:
        proc = run(["git", "-C", ROOT, *args], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout if proc.returncode == 0 else None


def changed_files(base: str = "origin/main", *, run=subprocess.run) -> "list | None":
    """[(added, deleted, path), …] for the diff of `base`…HEAD (three-dot: since the merge base), or
    None if the diff could not be read (e.g. `base` not fetched). A binary file reports '-' for its
    counts, recorded here as 0 lines. `--no-renames` keeps a rename as a clean delete+add rather than an
    `old => new` path; `core.quotepath=false` keeps unicode paths unquoted so they match the catalogue;
    `--end-of-options` stops an option-shaped base from being read as a git flag."""
    out = _git(["-c", "core.quotepath=false", "diff", "--numstat", "--no-renames",
                "--end-of-options", f"{base}...HEAD"], run=run)
    if out is None:
        return None
    rows = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        added, deleted, path = parts
        rows.append((int(added) if added.isdigit() else 0,
                     int(deleted) if deleted.isdigit() else 0,
                     path))
    return rows


def commit_count(base: str = "origin/main", *, run=subprocess.run) -> "int | None":
    """Commits on this branch since the merge base with `base`, or None if git could not run."""
    out = _git(["rev-list", "--count", "--end-of-options", f"{base}..HEAD"], run=run)
    if out is None:
        return None
    out = out.strip()
    return int(out) if out.isdigit() else 0


def kind_map(graph_path: str = GRAPH) -> dict:
    """Map each catalogued file path to its surface-kind (the graph entity's `type`)."""
    with open(graph_path, encoding="utf-8") as fh:
        graph = json.load(fh)
    entities = graph["entities"] if isinstance(graph, dict) else graph
    out = {}
    for ent in entities:
        path = (ent.get("source") or {}).get("path")
        if path:
            out[path] = ent.get("type", "other")
    return out


def surface_kind(path: str, kmap: dict) -> str:
    """The catalogued surface-kind of a path, or 'other' when the map does not catalogue it."""
    return kmap.get(path, "other")


def area(path: str) -> str:
    """A coarse top-level bucket for a path, so 'where does this land' reads at a glance. A file sitting
    directly under a top dir (.engine/self-map.md) buckets to that dir, not its own filename — only a real
    subdirectory (3+ segments) earns a two-segment bucket, so the line reads as directories, not a mix."""
    parts = path.split("/")
    if path.startswith((".engine/", ".claude/", ".agents/", ".codex/")):
        return "/".join(parts[:2]) if len(parts) >= 3 else parts[0]
    if path.startswith(".github/"):
        return ".github"
    return parts[0] if parts and parts[0] else path


def profile(rows: list, kmap: dict) -> dict:
    """Aggregate a changed-file list into the report dimensions. Pure — no git, no I/O."""
    added = sum(a for a, _d, _p in rows)
    deleted = sum(d for _a, d, _p in rows)
    kinds: dict = {}
    areas: dict = {}
    for _a, _d, path in rows:
        kind = surface_kind(path, kmap)
        kinds[kind] = kinds.get(kind, 0) + 1
        areas[area(path)] = areas.get(area(path), 0) + 1
    return {
        "files": len(rows),
        "added": added,
        "deleted": deleted,
        "kinds": kinds,
        "areas": areas,
    }


def _plural(kind: str, n: int) -> str:
    if kind == "other":
        return f"{n} other file" + ("s" if n != 1 else "") + " (not in the engine's map)"
    return f"{n} {kind}" + ("s" if n != 1 else "")


def render(prof: dict, *, commits: "int | None" = 0) -> str:
    """Render the profile as plain-language markdown for the ## Scope section."""
    kinds = prof["kinds"]
    # catalogued kinds first (by count), "other" last so it reads as the aside it is
    ordered = sorted((k for k in kinds if k != "other"), key=lambda k: (-kinds[k], k))
    kind_bits = [_plural(k, kinds[k]) for k in ordered]
    if "other" in kinds:
        kind_bits.append(_plural("other", kinds["other"]))
    kinds_line = ", ".join(kind_bits) if kind_bits else "no files"

    areas = prof["areas"]
    areas_line = ", ".join(sorted(areas, key=lambda a: (-areas[a], a))) or "—"

    if not commits:
        commit_note = "no commits yet"
    else:
        commit_note = f"{commits} commit" + ("s" if commits != 1 else "")

    return (
        "**Change profile** — the shape of this pull request at a glance:\n\n"
        f"- **Size:** {prof['files']} file" + ("s" if prof["files"] != 1 else "") +
        f" changed, +{prof['added']} / −{prof['deleted']} lines.\n"
        f"- **Kinds of thing touched:** {kinds_line}.\n"
        f"- **Where:** {areas_line}.\n"
        f"- **Shape:** {commit_note} on this branch — a standalone change unless a `Part of #N` line "
        "below says it is one slice of a larger effort.\n\n"
        "_This is a description, not a gate — it never blocks a merge. It is here so you can weigh the "
        "change by what it touches, not by its line count._"
    )


def compute(base: str = "origin/main", graph_path: str = GRAPH, *, run=subprocess.run) -> str:
    """The CLI path: read the live diff and render the profile markdown, or a visible could-not-read note
    (never a fabricated zero) when the diff against `base` can't be read."""
    rows = changed_files(base, run=run)
    if rows is None:
        return (
            f"**Change profile** — could not read the diff against `{base}`.\n\n"
            f"- Is `{base}` fetched? Try `git fetch origin` (or pass a base that exists as the first "
            "argument), then re-run `.engine/tools/scope_profile.py`.\n\n"
            "_This tool only describes a change's shape; it never blocks a merge._"
        )
    return render(profile(rows, kind_map(graph_path)), commits=commit_count(base, run=run))


def main(argv: "list | None" = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    base = argv[0] if argv else "origin/main"
    print(compute(base))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
