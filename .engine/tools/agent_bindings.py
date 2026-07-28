#!/usr/bin/env python3
"""Stamp each engine persona with the model + reasoning effort that realize its capability tier, from the one
committed bindings file (.engine/policies/model-bindings.json).

Personas stay capability-shaped — they declare a `model-tier` (judgment / mechanical), never a model name.
This tool is the single place that tier is turned into a concrete model + effort, so retuning the whole fleet
for a new model landscape is one edit to model-bindings.json followed by `agent_bindings.py render`. `check`
reports drift — a persona whose stamped model/effort no longer matches the binding, or an override that names
no installed persona — and backs the coherence unit test (test_agent_bindings.py). It edits only the two
frontmatter lines it owns; the persona's prose and every other frontmatter field are untouched.
"""
from __future__ import annotations
import json
import os
import re
import sys

_AGENTS_REL = os.path.join(".claude", "agents")
_BINDINGS_REL = os.path.join(".engine", "policies", "model-bindings.json")
_FM_KEY = re.compile(r"^([A-Za-z0-9_-]+):\s*(.*)$")
_OWNED = re.compile(r"^(model|effort):")   # the two lines this tool owns (not 'model-tier', which starts model-)


def _root(root: str | None = None) -> str:
    return root or os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def load_bindings(root: str | None = None) -> dict:
    with open(os.path.join(_root(root), _BINDINGS_REL), encoding="utf-8") as fh:
        return json.load(fh)


def resolve(name: str, model_tier: str, bindings: dict) -> dict:
    """The {model, effort} for a persona: its override if one exists, else its tier default."""
    override = (bindings.get("overrides") or {}).get(name)
    if override:
        return {"model": override["model"], "effort": override["effort"]}
    tier = (bindings.get("tiers") or {}).get(model_tier)
    if not tier:
        raise KeyError(f"no binding for capability tier {model_tier!r}")
    return {"model": tier["model"], "effort": tier["effort"]}


def _agent_files(root: str) -> list[str]:
    d = os.path.join(root, _AGENTS_REL)
    return sorted(os.path.join(d, f) for f in os.listdir(d) if f.endswith(".md"))


def _frontmatter(text: str):
    """(lines, end_index, {key: value}) for the leading YAML frontmatter, or None if absent."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    try:
        end = lines.index("---", 1)
    except ValueError:
        return None
    fm = {}
    for line in lines[1:end]:
        m = _FM_KEY.match(line)
        if m:
            fm[m.group(1)] = m.group(2).strip()
    return lines, end, fm


def _stamp(text: str, model: str, effort: str) -> str:
    """Return `text` with `model:`/`effort:` set immediately after the `model-tier:` line, preserving every
    other line. Removes any existing model/effort lines first, so the render is idempotent."""
    parsed = _frontmatter(text)
    if not parsed:
        raise ValueError("persona has no frontmatter")
    lines, end, _ = parsed
    body = [line for line in lines[1:end] if not _OWNED.match(line)]
    out = []
    for line in body:
        out.append(line)
        if line.startswith("model-tier:"):
            out.append(f"model: {model}")
            out.append(f"effort: {effort}")
    rebuilt = "\n".join(["---", *out, "---", *lines[end + 1:]])
    return rebuilt + "\n" if text.endswith("\n") else rebuilt


def render(root: str | None = None) -> list[str]:
    """Stamp every persona from the bindings. Returns the basenames actually changed."""
    root = _root(root)
    bindings = load_bindings(root)
    changed = []
    for path in _agent_files(root):
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        parsed = _frontmatter(text)
        if not parsed:
            continue
        _, _, fm = parsed
        binding = resolve(fm["name"], fm.get("model-tier"), bindings)
        new = _stamp(text, binding["model"], binding["effort"])
        if new != text:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(new)
            changed.append(os.path.basename(path))
    return changed


def check(root: str | None = None) -> list[str]:
    """Drift between the personas and the bindings: a persona whose stamped model/effort differs from its
    binding, or an override that names no installed persona. Empty list means in sync."""
    root = _root(root)
    bindings = load_bindings(root)
    problems, names = [], set()
    for path in _agent_files(root):
        with open(path, encoding="utf-8") as fh:
            parsed = _frontmatter(fh.read())
        if not parsed:
            continue
        _, _, fm = parsed
        names.add(fm["name"])
        want = resolve(fm["name"], fm.get("model-tier"), bindings)
        got = {"model": fm.get("model"), "effort": fm.get("effort")}
        if got != want:
            problems.append(f"{fm['name']}: stamped {got} != binding {want} — run agent_bindings.py render")
    for override in (bindings.get("overrides") or {}):
        if override not in names:
            problems.append(f"override '{override}' names no installed persona")
    return problems


def main(argv: list | None = None) -> int:
    argv = list(argv if argv is not None else sys.argv[1:])
    if argv and argv[0] == "render":
        changed = render()
        print("stamped: " + (", ".join(changed) if changed else "no changes"))
        return 0
    if argv and argv[0] == "check":
        problems = check()
        if problems:
            for p in problems:
                print("DRIFT: " + p, file=sys.stderr)
            return 1
        print("persona model/effort bindings are in sync")
        return 0
    print("usage: agent_bindings.py render | check")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
