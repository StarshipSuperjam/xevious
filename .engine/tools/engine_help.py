#!/usr/bin/env python3
"""`/engine-help` listing tool — the degradation-proof command index.

Backs the `/engine-help` operator command: a plain-language listing of the engine's own typed
commands so a non-engineer asking "what can I do here?" always gets an answer. It derives the listing
from committed files only — never an MCP substrate — so an outage cannot blank it (the discovery
axis; degrade-to-git-native). Two parts:

- Installed commands — the engine's OWN, engine-prefixed, operator-invocable verbs present on disk
  (`.claude/skills/engine-*/SKILL.md` and the legacy `.claude/commands/engine-*.md`), each shown as the
  command the operator types plus its one-line description. Operator-invocable = the invocation axis the
  operator can reach: `operator-typed` and `model-auto` (an omitted invocation defaulting to model-auto);
  `model-only` verbs are hidden from the operator's menu. Scoped to the engine's commands (the
  engine/operator wall, the same scope the self-election guard governs); the operator's own un-prefixed
  product commands, and the full command set, are the platform's bare `/` menu to show, not this one's.
- Available-if-installed commands — optional commands the operator could add, RELAYED from the committed
  module catalog the first-run setup maintains (a relay: the catalog's owner is provisioning; this tool
  only reads it, through the shared `module_catalog` reader so this index and the setup walkthrough cannot
  drift in how they parse it). The catalog ships empty and grows as optional modules are built, so this part
  is an empty relay today — present but with nothing to list yet.

Design fidelity notes (for a maintainer reading the source, not the operator):
- The verb shown is the TYPED name — the skill DIRECTORY (or the legacy command FILENAME), i.e. the
  string the operator actually types. The locked design says "the skill `name` (fallback: its directory)";
  on the platform the typed identity IS the directory and frontmatter `name` is only a display label
  (skill.v1 schema), so the typed name is the platform-correct source. For an engine command the two
  coincide.
- Each per-file frontmatter parse is wrapped: `validate.frontmatter` RAISES on malformed YAML (its
  halt-on-malformed posture), so a single broken command file must not crash the whole listing — the
  always-answers guarantee. This is the DELIBERATE OPPOSITE of the self-election guard
  (skill_coherence_check.engine_skills), which lets the raise propagate to fail closed: a detection guard
  must never silently pass, an operator listing must never go blank. Same discovery, opposite posture by
  design.
- The ~typed-name + engine-glob logic is intentionally a small local copy, not an import of the check
  tool's private helper (a verb tool must not reach up into a guard tool) nor an addition to the seed
  validator. When a third skill-globbing tool appears, extract a shared skill-discovery helper that
  exposes the raw per-file parse and lets each caller choose its posture.
"""
from __future__ import annotations
import glob
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate  # noqa: E402
import module_catalog  # noqa: E402  (the shared optional-module catalog reader — one parse path, no drift)

# The engine's own commands carry the engine- prefix (the engine/operator wall); this is the same scope
# the self-election guard governs. The legacy commands directory lives under .claude/commands/. The two
# runtime trees carry the SAME verbs (the Codex tree is a committed render of the Claude one), so the
# listing dedupes by typed name and surfaces a verb present in only one tree as partially installed.
_CLAUDE_VERB_GLOBS = (".claude/skills/engine-*/SKILL.md", ".claude/commands/engine-*.md")
_CODEX_VERB_GLOBS = (".agents/skills/engine-*/SKILL.md",)
_ENGINE_VERB_GLOBS = _CLAUDE_VERB_GLOBS + _CODEX_VERB_GLOBS

# What a one-tree-only verb's listing appends, so a broken mirror is surfaced, never hidden. Every engine
# command now has both twins (no runtime-only verb is expected); the sanctioned asymmetries live in the
# provider-exception ledger, and this line simply tells the operator which runtime a verb works on today if a
# mirror ever goes missing.
_ONLY_CLAUDE_NOTE = " (currently only available when working in Claude Code)"
_ONLY_CODEX_NOTE = " (currently only available when working in Codex)"

_HEADER = "Commands you can type:"
_AVAILABLE_HEADER = "You can also add these by installing more parts of your Engine:"
_EMPTY_AVAILABLE_LINE = "More commands become available as you add optional parts to your Engine."
_POINTER = "New to the Engine? Ask me to open the getting-started guide — it walks you through the basics."


def _typed_name(path: str) -> str:
    """The command the operator types for a verb file: the skill DIRECTORY name (a SKILL.md), or the
    legacy command FILENAME (a .claude/commands/<name>.md). Mirrors the self-election guard's helper."""
    parent = os.path.basename(os.path.dirname(path))
    if parent and parent != "commands":
        return parent
    return os.path.splitext(os.path.basename(path))[0]


def installed_verbs(root: str | None = None) -> list:
    """The engine's installed operator-invocable commands as a list of {name, description}, sorted by the
    typed name. Globs only the engine-prefixed command files and keeps only the commands the operator can
    invoke — the operator-invocable axis: `operator-typed` and `model-auto` (an omitted invocation defaults
    to model-auto), but not `model-only`, which is hidden from the operator's menu. Each file's frontmatter
    parse is guarded: a malformed command file is skipped rather than allowed to crash the whole listing
    (degrade, never blank — the always-answers guarantee)."""
    base = root or validate.ROOT
    seen: dict = {}
    for tree, patterns in (("claude", _CLAUDE_VERB_GLOBS), ("codex", _CODEX_VERB_GLOBS)):
        for pattern in patterns:
            for path in sorted(glob.glob(os.path.join(base, pattern))):
                try:
                    fm = validate.frontmatter(path)
                except Exception:
                    # A broken command file must not blank the list — skip it, keep answering.
                    continue
                inv = fm.get("invocation") or "model-auto"   # an omitted invocation is model-auto (platform default)
                if inv not in ("operator-typed", "model-auto"):
                    continue   # model-only is hidden from the operator's menu; an unknown value too
                name = _typed_name(path)
                entry = seen.setdefault(name, {"name": name, "description": "", "trees": set()})
                entry["trees"].add(tree)
                if tree == "claude" or not entry["description"]:   # the Claude source's description wins
                    entry["description"] = str(fm.get("description") or "") or entry["description"]
    # Annotate a one-tree-only verb ONLY when BOTH runtime trees are actually populated — a repo
    # carrying just the Claude adapter (or a minimal test tree) gets no noise; once both adapters
    # are present, a verb missing its twin is surfaced, never hidden.
    both_present = all(any(t in e["trees"] for e in seen.values()) for t in ("claude", "codex"))
    verbs = []
    for entry in seen.values():
        desc = entry["description"]
        home = None
        if both_present and entry["trees"] == {"claude"}:
            desc += _ONLY_CLAUDE_NOTE
            home = "claude"      # render with the sigil it actually answers to, whatever the ambient runtime
        elif both_present and entry["trees"] == {"codex"}:
            desc += _ONLY_CODEX_NOTE
            home = "codex"
        verbs.append({"name": entry["name"], "description": desc.strip(), "home": home})
    return sorted(verbs, key=lambda v: v["name"])


def _installed_module_ids() -> set:
    """The ids of the modules installed in this engine (the engine manifest's `packages`), or an empty set
    when it cannot be read — the available list then degrades to listing everything rather than blanking
    (degrade, never blank). The catalog lists every optional module the engine ships; one that is ALREADY
    installed is shown under the installed commands, not as something to install, so it is excluded here."""
    try:
        engine = validate.load_json(os.path.join(validate.ROOT, ".engine", "engine.json"))
        return set((engine or {}).get("packages") or {})
    except Exception:  # noqa: BLE001 — an unreadable manifest degrades to no filter, never blanks the list
        return set()


def available_verbs(catalog_path: str | None = None) -> list:
    """The optional, not-yet-installed commands, RELAYED from the committed module catalog the first-run
    setup maintains — or an empty list when the catalog is absent, empty, or damaged (it narrows the
    listing, never breaks it). Returns each as {name, description}: the command the operator would type
    once the module is installed, plus its one-line gloss, sorted by the command. A module that is already
    installed is EXCLUDED (its command shows under the installed commands instead). A command-less optional
    module (one with no `verb`) is also EXCLUDED here: this index lists things to type, and that module has
    nothing to type — it is still offered in the first-run walkthrough by its description. This tool only
    relays; provisioning owns the catalog and the shared `module_catalog` reader parses it, so this index and
    the first-run walkthrough cannot drift in how they read it. `catalog_path` is injectable for tests; the
    committed catalog is read by default."""
    installed = _installed_module_ids()
    return [{"name": e["verb"], "description": e["description"]}
            for e in module_catalog.entries(catalog_path) if e["id"] not in installed and e["verb"]]


def ambient_provider() -> "str | None":
    """Which runtime the operator is typing in, for the prefix rendering: the launcher-exported
    provider tag when a hook chain set it, else the live-session marker's provider (boot records it
    at every SessionStart), else None — genuinely unknown, so the listing shows both forms."""
    try:
        import providers
        env = (os.environ.get(providers.PROVIDER_ENV) or "").strip().lower()
        if env in (providers.CLAUDE, providers.CODEX):
            return env
        record = providers.read_live_session()
        if record and record.get("provider") in (providers.CLAUDE, providers.CODEX):
            return record["provider"]
    except Exception:  # noqa: BLE001 — an unreadable seam degrades to the both-forms rendering
        pass
    return None


def _verb_line(verb: dict, prefix: "str | None" = "/") -> str:
    """One listing line. `prefix` is the typed sigil for the ambient runtime ("/" on Claude Code,
    "$" on Codex); None means the runtime is unknown, so both forms are shown once per verb. A verb
    installed for only ONE runtime always renders with THAT runtime's sigil (its note names the
    runtime), so the listing never presents a verb in a form that would not answer."""
    desc = verb.get("description") or ""
    name = verb.get("name", "")
    home = verb.get("home")
    if home:
        typed = f"/{name}" if home == "claude" else f"${name}"
    elif prefix is None:
        typed = f"/{name}  (in Codex: ${name})"
    else:
        typed = f"{prefix}{name}"
    return f"  {typed} — {desc}" if desc else f"  {typed}"


def render(installed: list, available: list, prefix: "str | None" = "/") -> str:
    """The plain-language listing the operator sees. Installed commands first (alphabetical), then the
    optional ones (alphabetical) or a single plain line when there are none — never a bare empty heading
    — and a closing pointer to the getting-started guide. A pure function of its inputs: no clock, no
    network, no MCP. `prefix` renders each verb in the ambient runtime's own typed form (None = both)."""
    lines = [_HEADER, ""]
    lines.extend(_verb_line(v, prefix) for v in installed)
    lines.append("")
    if available:
        lines.append(_AVAILABLE_HEADER)
        lines.append("")
        lines.extend(_verb_line(v, prefix) for v in available)
    else:
        lines.append(_EMPTY_AVAILABLE_LINE)
    lines.append("")
    lines.append(_POINTER)
    return "\n".join(lines)


def _demo() -> int:
    """An operator-runnable demonstration that the listing always answers. It prints the real listing,
    then re-runs the REAL listing logic over a throwaway temporary copy of the commands that has no
    optional-commands catalog and one deliberately broken command file — showing the listing still
    renders (the broken command skipped, the rest intact, nothing crashing). Real files are untouched."""
    import shutil
    import tempfile

    print("Your Engine's commands, the way /engine-help lists them:\n")
    live = render(installed_verbs(), available_verbs())
    print(live)
    print("\n" + "-" * 70 + "\n")
    print("The same listing when a command file is broken — to show the help always answers.\n"
          "This copies your commands into a throwaway temporary folder (your real files are NOT\n"
          "touched), then plants one deliberately broken command file in the copy:\n")
    with tempfile.TemporaryDirectory() as tmp:
        dst_skills = os.path.join(tmp, ".claude", "skills")
        os.makedirs(dst_skills, exist_ok=True)
        src_skills = os.path.join(validate.ROOT, ".claude", "skills")
        for entry in sorted(glob.glob(os.path.join(src_skills, "engine-*"))):
            if os.path.isdir(entry):
                shutil.copytree(entry, os.path.join(dst_skills, os.path.basename(entry)))
        broken_dir = os.path.join(dst_skills, "engine-broken")
        os.makedirs(broken_dir, exist_ok=True)
        with open(os.path.join(broken_dir, "SKILL.md"), "w", encoding="utf-8") as fh:
            fh.write("---\ndescription: [this line is broken\ninvocation: operator-typed\n---\n\n"
                     "## Steps\n\n1. Go.\n")
        broken_listing = render(installed_verbs(root=tmp), available_verbs(None))
        print(broken_listing)
    print("\nThe broken command was skipped, the rest are still listed, and nothing crashed — so\n"
          "\"what can I do here?\" always gets an answer, even during an outage or with a damaged file.")
    # Self-check: the live listing rendered, and the throwaway-copy listing still rendered with the broken
    # command skipped — so "what can I do here?" always gets an answer, even with a damaged command file.
    ok = bool(live) and bool(broken_listing) and "engine-broken" not in broken_listing
    if not ok:
        print("\nDEMO UNEXPECTED: a listing did not render, or the broken command was not skipped.",
              file=sys.stderr)
        return 1
    return 0


def main(argv: list) -> int:
    if argv and argv[0] == "demo":
        return _demo()
    provider = ambient_provider()
    prefix = {"claude": "/", "codex": "$"}.get(provider)   # None (unknown) → both forms shown
    print(render(installed_verbs(), available_verbs(), prefix))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
