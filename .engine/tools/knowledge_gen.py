#!/usr/bin/env python3
"""The knowledge graph — the engine's generated, committed structural readout.

Knowledge answers "how does this world work?" — the purely STRUCTURAL, purely DERIVED layer:
what engine surfaces exist and how they relate. This tool generates ONE committed JSON file,
`.engine/knowledge/graph.json`, holding one entity per engine surface-instance file (each schema,
each check, each tool, and — as they appear — each contract, policy, operation,
skill, agent, interface, and doc) plus one entity per installed module, with the mechanical edges
between them. It is DERIVED from the declarations the engine already requires — the surface catalog,
the module manifests, and the check rules — so it cannot diverge from them, and it is never
hand-authored (it would drift) and never boot-only (latency while building is tolerable; latency
while using is not). Entity beliefs are forbidden: every field is read from the catalog, a manifest,
a check's target, or the file's own bytes — never "why" a choice was made (that is memory's, behind
the structure/belief wall).

The graph is kept honest by a FINGERPRINT GATE: the committed file is checked against its canonical
derivation. The committed content IS the fingerprint of its sources (each entity also carries a
sha256 of its own source file, so a changed source flips a hash), and the checker regenerates the
graph in memory and compares; any difference is drift — a surface changed, was added, or was removed
without a regenerate. The gate runs in CI as the `coverage`-kind rule engine/check/knowledge-coverage
(mode: fingerprint), which RELAYS to check() here — knowledge owns the detection, the rule relays it.

DERIVED, NOT THE QUERY LAYER: the derived query index and the graph-query MCP server are separate,
regenerable, gitignored layers; the prioritized boot slice (#37) is a further gitignored
layer — a never-committed cache rebuilt on demand, read live by boot. This committed file is
the source of truth and the offline cold-start readout. Reverse traversal (who governs/enforces/provides
me) is the derived index's job — entities store OUTGOING edges only.

Library + CLI (mirrors self_map.py — plain language first; no JSON channel needed):

  uv run --directory .engine -- python tools/knowledge_gen.py show       # print the graph (live)
  uv run --directory .engine -- python tools/knowledge_gen.py generate   # (re)write .engine/knowledge/graph.json
  uv run --directory .engine -- python tools/knowledge_gen.py check       # is the committed graph in sync?
  uv run --directory .engine -- python tools/knowledge_gen.py demo        # safe fail->pass on a temp copy
  uv run --directory .engine -- python tools/knowledge_gen.py hook-demo   # show the commit-boundary regen (no writes)

REGENERATION AT THE COMMIT BOUNDARY: the `hook` verb is the
`PreToolUse` entry the engine wires. On a `git commit` it regenerates the graph best-effort and ALWAYS
proceeds — because the hook fires BEFORE the commit, the refreshed graph lands UNSTAGED in the working
tree and is captured by a FOLLOWING commit (it is not guaranteed to ride the commit that triggered it);
the fingerprint gate above is the unbypassable CI backstop that forces capture before merge. Regeneration
is a MUTATION, not a gate: it registers no block, never blocks the commit, and on any failure proceeds
(the staleness is caught downstream at CI).

Reuse: the present-set + ownership readers (discover_manifests / engine_file_inventory /
provides_claims) come from module_coherence.py; finding.v1, the catalog, and path/glob helpers from
validate.py, via the sibling-import precedent. The committed-artifact + drift-gate shape mirrors
self_map.py. The generic catalog-driven walk and the JSON edge vocabulary are new to this slice
(informed by the Engine_Prototype KG, not ported from it).
"""
from __future__ import annotations
import ast
import glob as _glob
import hashlib
import json
import os
import re
import sys
import tempfile
import unicodedata

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate          # noqa: E402
import module_coherence  # noqa: E402
import hooks             # noqa: E402  (the run_hook harness for the commit-boundary regen hook)
import weakening_guard   # noqa: E402  (the guardrail classifier — the `guarded` attribute's public seam)

# The committed graph's home: a directory (the gitignored query index lives alongside under .cache/;
# the gitignored boot slice is its rung-1 cache, built on demand into the same .cache/), owned by core's provides.knowledge
# so the ownership leg does not flag it an orphan. NOT a catalogued
# surface (the knowledge map is derived-observational, excluded from the catalog by design), so it
# never becomes an entity of itself.
KNOWLEDGE_DIR = os.path.join(validate.ENGINE_DIR, "knowledge")
GRAPH_PATH = os.path.join(KNOWLEDGE_DIR, "graph.json")
SCHEMA_VERSION = 1
REGEN_CMD = "uv run --directory .engine -- python tools/knowledge_gen.py generate"

# The deployment-owned per-instance eADR stream: a deployment authors its
# OWN engine-decision eADRs under this path, in NO module's `provides`. The two contract populations are told
# apart by provides-membership, NEVER a path or content marker — a CANON contract entity carries a `provided_by`
# edge (Pass 1); a deployment eADR carries none (Pass 1b), and that is what canon detection keys off (Pass 3b).
# A deployment entity's `owner` is the reserved token below (never a module id), so it stays schema-valid
# (`owner` is required, minLength 1) and `find --owner deployment` lists a deployment's own decisions.
DEPLOYMENT_CONTRACTS_PREFIX = ".engine/contracts/instance/"
DEPLOYMENT_OWNER = "deployment"


# ---- small shared helpers --------------------------------------------------------------------

def _rel(abs_path: str) -> str:
    """A repo-relative path with forward slashes (so committed bytes are identical on any host)."""
    return os.path.relpath(abs_path, validate.ROOT).replace(os.sep, "/")


def _display(path: str) -> str:
    """A path for human messages: repo-relative inside the repo, else absolute — never a `../` chain
    (matters for the demo's throwaway copy outside the repo)."""
    rel = os.path.relpath(path, validate.ROOT)
    return rel.replace(os.sep, "/") if not rel.startswith("..") else os.path.abspath(path)


def _loc_opt(path: str):
    """A finding.v1 location (repo-relative) — or None when the path is outside the repo."""
    rel = os.path.relpath(path, validate.ROOT)
    return None if rel.startswith("..") else {"file": rel.replace(os.sep, "/"), "line": None}


def source_fingerprint(rel_path: str) -> str:
    """A sha256 of a source file's RAW bytes (read with no newline translation), prefixed 'sha256:'.
    The per-entity provenance hash — a changed source flips it, so drift is caught and pinpointed."""
    with open(os.path.join(validate.ROOT, rel_path), "rb") as fh:
        return "sha256:" + hashlib.sha256(fh.read()).hexdigest()


def _slug(rel_path: str) -> str:
    """The id suffix: the file's stem (basename without its final extension), e.g.
    `.engine/check/state-cursor.json` -> `state-cursor`, `check.v1.json` -> `check.v1`."""
    return os.path.splitext(os.path.basename(rel_path))[0]


def _instance_slug(surface_type: str, rel_path: str) -> str:
    """The id suffix for a surface INSTANCE. A skill IS its directory under .claude/skills/ — the file is
    always SKILL.md, so the file stem would collide every skill onto 'SKILL'; its slug is that parent
    directory's name (e.g. `.claude/skills/engine-help/SKILL.md` -> `engine-help`). A tool PACKAGE marker is
    always `__init__.py`, so the bare stem would collide every package's marker onto '__init__'; it is
    qualified by its package directory (e.g. `.engine/tools/memory/__init__.py` -> `memory.__init__`) so two
    tool packages stay distinct. Every other surface has a distinct filename, so its slug is the file stem
    (`_slug`); an agent is `.claude/agents/<name>.md`, whose stem is already its name. A codex-skill is the
    same directory-identity shape as a skill, with a second per-directory file (the invocation policy,
    `agents/openai.yaml`), so its slug is the skill directory's name, `.policy`-qualified for the policy
    file (`.agents/skills/engine-help/agents/openai.yaml` -> `engine-help.policy`) so the pair stays
    distinct."""
    if surface_type == "skill":
        return os.path.basename(os.path.dirname(rel_path))
    if surface_type == "codex-skill":
        parent = os.path.dirname(rel_path)
        if os.path.basename(rel_path) == "SKILL.md":
            return os.path.basename(parent)
        if os.path.basename(parent) == "agents":
            return os.path.basename(os.path.dirname(parent)) + ".policy"
        return os.path.basename(parent) + "." + _slug(rel_path)
    stem = _slug(rel_path)
    if stem == "__init__":
        return os.path.basename(os.path.dirname(rel_path)) + ".__init__"
    if surface_type == "contract" and rel_path.startswith(DEPLOYMENT_CONTRACTS_PREFIX):
        # A deployment-authored eADR is path-qualified (`instance.<stem>`) so a same-stem overlap with a canon
        # eADR (`contract:<stem>`) is structurally impossible — the entity dict is keyed by id, and an
        # unqualified collision would silently overwrite one of the two. Mirrors the `<dir>.__init__` qualify.
        return "instance." + stem
    return stem


def _surface_for(rel_path: str, surfaces: dict):
    """The catalogued surface NAME whose `location` is the longest directory-prefix of rel_path,
    or None when the file lives under no catalogued surface (foundation/infra/module-manifest, or
    the derived knowledge dir itself)."""
    best_name, best_len = None, -1
    for name, rec in surfaces.items():
        location = (rec or {}).get("location", "")
        if location and rel_path.startswith(location) and len(location) > best_len:
            best_name, best_len = name, len(location)
    return best_name


def surface_instance_inventory(catalog: dict, claims: dict) -> list:
    """The surface-instance file relpaths the graph entitizes: every ENGINE-OWNED file (a key of `claims` —
    i.e. claimed by some module's `provides`) that lives under a catalogued surface `location`, across BOTH
    .engine/ and .claude/ (skills and agents are catalogued surfaces located under .claude/), with `.gitkeep`
    directory-placeholders excluded (a placeholder is not a ratified instance). Sorted for byte-determinism.

    This is the catalog-location-driven walk the graph needs. It deliberately does NOT reuse
    module_coherence.engine_file_inventory(), which is hard-scoped to .engine/ ('the product never owns a file
    under .engine/') for the ownership/orphan checks that depend on that scope — widening it would break that
    invariant and never reach the .claude/ surfaces. Engine/product wall: only files a module's `provides`
    claims appear in `claims`, so an operator's own un-prefixed product skill is never entitized."""
    surfaces = (catalog or {}).get("surfaces", {})
    out = []
    for rel in claims:
        if os.path.basename(rel) == ".gitkeep":
            continue                                   # a directory-placeholder is not a real instance
        if _surface_for(rel, surfaces) is None:
            continue                                   # not under any catalogued surface location
        out.append(rel)
    return sorted(out)


# ---- pure attribute harvesters (operate on already-parsed dicts; NO file IO; fixture-testable) ----
# Each takes parsed frontmatter / JSON / manifest dicts and returns a declared STATE/IDENTITY token or a
# discriminator map — never an INTERPRETATION of what a surface means (the four-gate rule: declared,
# structural, not belief). Copying the file's own declared words VERBATIM is not interpretation and stays
# within the gate: the Pass-4 `summary` attribute copies a tool's module-docstring first line as-is, the same
# footing as `title` (a declared identity token) — neither reads meaning out of prose. The file IO stays in
# derive_entities' passes; these stay pure so they unit-test on dicts.

def _status_for(surface_type: str, frontmatter: dict, manifest: dict | None) -> str:
    """The declared lifecycle STATE TOKEN (the 'else active' rule): a module manifest's `status` and a
    contract frontmatter's `status` are harvested verbatim; EVERY other surface is `active` (a declared
    status elsewhere is not echoed). A missing value on the two declaring surfaces degrades to `active`
    (a non-conforming instance, never a crash). Never the *why* of a supersession."""
    if surface_type == "module":
        val = (manifest or {}).get("status")
        return val if isinstance(val, str) and val else "active"
    if surface_type == "contract":
        val = (frontmatter or {}).get("status")
        return val if isinstance(val, str) and val else "active"
    return "active"


def _tier_for(surface_type: str, rule: dict | None):
    """CHECKS ONLY: the check rule's own bite tier (`hard`|`soft`) from its `tier` key; None for every
    other surface and for a check whose tier is absent/non-string (a malformed check, caught by its own
    schema check). A policy's prose enforcement tier is NEVER parsed."""
    if surface_type != "check":
        return None
    val = (rule or {}).get("tier")
    return val if val in ("hard", "soft") else None


# A small, closed lexicon of leading imperative verbs marking a command/description, not an identity. It is
# a FORWARD-DRIFT tripwire with ZERO live effect: every live policy/interface title is a bare noun-phrase
# that passes, and the live excluded titles (operations) are rejected by the structural em-dash rule, not
# by this list.
_IMPERATIVE_VERBS = frozenset({
    "add", "set", "start", "stop", "show", "list", "run", "make", "create", "remove", "delete", "update",
    "shape", "adjust", "switch", "enable", "disable", "configure", "open", "close", "build", "fix", "keep",
    "use", "write", "author", "tune",
})

# The identity-title surfaces and the SINGLE declared key each harvests: never operation/
# doc/contract (purpose/decision clauses), never a description, never a slug fallback.
_TITLE_KEYS = {"policy": "title", "interface": "title", "skill": "name"}


def _is_noun_phrase_title(s: str) -> bool:
    """The noun-phrase shape-guard: accept a bare identity name; reject a purpose clause / sentence /
    imperative. The two STRUCTURAL rejections do the live work (em-dash or spaced-hyphen purpose clause;
    mid-string sentence punctuation); the imperative-verb lexicon is a forward tripwire (zero live effect)."""
    if "—" in s or " - " in s:                    # em-dash / spaced hyphen -> a purpose clause
        return False
    if re.search(r"[.:]\s+\S", s):                     # mid-string sentence punctuation ('. ' or ': ')
        return False
    parts = s.split()
    if parts and parts[0].rstrip(",").lower() in _IMPERATIVE_VERBS:   # leading imperative verb -> a command
        return False
    return True


def _title_for(surface_type: str, data: dict):
    """The verbatim IDENTITY title for policy/interface/skill ONLY (`policy.title` / `interface.title` /
    `skill.name`), harvested from the already-parsed `data` (frontmatter for policy/skill, JSON for
    interface). Returns None (OMIT the attribute — no slug fallback) when the key is absent/empty or the
    value fails the noun-phrase shape-guard."""
    key = _TITLE_KEYS.get(surface_type)
    if key is None:
        return None
    val = (data or {}).get(key)
    if not isinstance(val, str) or not val.strip():
        return None
    val = val.strip()
    return val if _is_noun_phrase_title(val) else None


def _discriminators_for(surface_type: str, frontmatter: dict, json_doc: dict, manifest: dict | None) -> dict:
    """The per-surface discriminator attributes, each from its DECLARED key (check `kind`+`suites`; agent
    `role`+`lens`+`model-tier`; skill `invocation`; interface `operations`+`fallback`; module `version`).
    Returns the {attr: value} to merge onto the entity; only non-empty members are present; all lists are
    sorted for byte-determinism."""
    out: dict = {}
    fm, jd = (frontmatter or {}), (json_doc or {})
    if surface_type == "check":
        kind = jd.get("kind")
        if isinstance(kind, str) and kind:
            out["kind"] = kind
        suites = jd.get("suites")
        if isinstance(suites, list):
            out["suites"] = sorted(s for s in suites if isinstance(s, str))
    elif surface_type == "agent":
        for k in ("role", "lens", "model-tier"):
            v = fm.get(k)
            if isinstance(v, str) and v:
                out[k] = v
    elif surface_type == "skill":
        v = fm.get("invocation")
        if isinstance(v, str) and v:
            out["invocation"] = v
    elif surface_type == "interface":
        ops = jd.get("operations")
        if isinstance(ops, list):
            names = sorted(o["name"] for o in ops
                           if isinstance(o, dict) and isinstance(o.get("name"), str))
            if names:
                out["operations"] = names
        fb = jd.get("fallback")
        handle = fb.get("handle") if isinstance(fb, dict) else None
        if isinstance(handle, str) and handle:
            out["fallback"] = handle
    elif surface_type == "module":
        ver = (manifest or {}).get("version")
        if isinstance(ver, str) and ver:
            out["version"] = ver
    return out


def _supersedes_edges(contract_entities: list, fm_by_id: dict, canon_ids) -> dict:
    """{contract_id: [superseded_contract_id]} — contract->contract, DEPLOYMENT-STREAM (non-canon) ONLY.
    `fm_by_id` maps a contract entity id to its parsed frontmatter; `canon_ids` is the set of canon
    contract entity ids (those a module's `provides` claims — told apart by provides-membership,
    NEVER a path or content marker). An edge is emitted only when BOTH ends are non-canon and the target
    resolves in-graph by the target's declared frontmatter `id`. A canon end on either side, a dangling
    target, or a self-reference emits NOTHING — so no persisted edge ever targets a canon eADR."""
    by_eadr: dict = {}                                 # declared frontmatter `id` (eADR-NNNN) -> entity id
    for e in contract_entities:
        decl = (fm_by_id.get(e["id"]) or {}).get("id")
        if isinstance(decl, str) and decl:
            by_eadr[decl] = e["id"]
    canon = set(canon_ids or ())
    edges: dict = {}
    for e in contract_entities:
        src_id = e["id"]
        if src_id in canon:                            # a canon contract never declares/emits supersedes
            continue
        target_eadr = (fm_by_id.get(src_id) or {}).get("supersedes")
        if not isinstance(target_eadr, str):
            continue
        target_id = by_eadr.get(target_eadr)
        if target_id is None or target_id == src_id or target_id in canon:
            continue                                   # dangling / self / canon target -> emit nothing
        edges.setdefault(src_id, []).append(target_id)
    return {k: sorted(v) for k, v in edges.items()}


# ---- Pass 4: code-dependency, wiring, and identity harvesters (the tool tree's DECLARED facts) -----
# These reach past frontmatter into the tool tree's own declarations — its import statements, a check's
# `params.script`, a manifest hook command, an interface's fallback handle, and the file's own module
# docstring — each a machine-declared fact read byte-deterministically, never an interpretation of what the
# code MEANS. A tool's one-line `summary` is a VERBATIM COPY of the module docstring's first line (the file's
# own declared words, mechanical self-description), which is why it clears the "declared, not belief" gate
# that `title` (an identity token) also clears; neither reads meaning out of prose.

TOOLS_DIRNAME = "tools"                                   # under .engine/; the import-resolution root.
# A .engine/tools/<...>.py path as it appears inside a manifest hook command string (the shared launcher
# hook-runner.sh is a `.sh`, so this deliberately matches only the .py payload the hook actually runs).
_TOOLS_PY_IN_CMD = re.compile(r"\.engine/tools/[A-Za-z0-9_./-]+\.py")


class DanglingImportError(ValueError):
    """Raised (loud) when a tool imports something UNDER an in-repo package or module that resolves to no file
    — `from <in-repo-pkg> import <gone>`, or `import <in-repo-pkg>.<gone>` — the residue a rename/removal
    leaves in a still-present importer (e.g. `from memory import consolidate` after consolidate.py moved). A
    subclass of ValueError so the CLI and the CI fingerprint gate catch it on their existing fail-closed paths,
    and the commit-boundary hook proceeds best-effort; the message names the file and the exact import so the
    session that introduced it knows the fix.

    SCOPE, stated honestly: this fires only when the import's HEAD is an in-repo package/module. A bare
    top-level `import <name>` whose head is not in-repo is indistinguishable from stdlib/external here (we do
    not probe the environment, which would break byte-determinism), so a deleted TOP-LEVEL tool referenced by
    a bare `import <it>` is dropped as if external, not raised on — that narrower case is backstopped by
    Python's own ModuleNotFoundError when the file runs and by the test suite importing the tool modules, not
    by this gate."""


def _dangling_import_message(source_rel: str, name: str) -> str:
    return (f"knowledge graph: {source_rel} imports '{name}', which resolves to no file under "
            f".engine/{TOOLS_DIRNAME}/. This is a dangling in-repo import — a reference to a module or name "
            f"that does not exist (often residue of a rename or removal). Fix or remove the import; the graph "
            f"refuses to record an edge to something that is not there. Regenerate with `{REGEN_CMD}`.")


def _parse_tool_ast(abs_path: str):
    """Parse a `.py` tool to an AST, or None if it is MALFORMED (a SyntaxError — its own lint/schema check is
    the gate, so a broken tool still entitizes with `guarded` but harvests no imports/summary/entrypoint). A
    read error is left to propagate: the file was already read to fingerprint it in Pass 1, so it is readable
    here — this deliberately catches ONLY SyntaxError, never OSError."""
    with open(abs_path, "rb") as fh:
        data = fh.read()
    try:
        return ast.parse(data)
    except SyntaxError:
        return None


def _py_declared_names(abs_init_path: str) -> frozenset:
    """The names an `__init__.py` DECLARES at module scope — import aliases, top-level assignments, defs and
    classes — so a `from pkg import name` that is a re-export (not a submodule) resolves to the package. Pure
    over the file's AST at module scope only (a name bound inside a function is not a package export); a parse
    or read failure yields the empty set (the file's own checks are its gate)."""
    try:
        with open(abs_init_path, "rb") as fh:
            tree = ast.parse(fh.read())
    except (OSError, SyntaxError):
        return frozenset()
    names: set = set()
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for a in node.names:
                # a `from .sub import *` re-export can expose any name; record the star so the resolver treats
                # an otherwise-unresolvable `from pkg import x` as a re-export (edge to the package) rather than
                # a dangling import — no tools package uses star imports today, so this only guards the future.
                names.add("*" if a.name == "*" else (a.asname or a.name).split(".")[0])
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return frozenset(names)


def _tool_module_index(tools_root_abs: str):
    """Walk the tool tree ONCE and return (packages, modules, init_symbols) for import resolution. Top-level
    tools are on sys.path (a bare `import validate` -> the tuple `('validate',)`), so names resolve relative
    to the tools root, not to the importing file's directory.
      packages     : set of dotted-name tuples for every directory carrying `__init__.py` (root excluded)
      modules      : set of dotted-name tuples for every `.py` module (`__init__.py` excluded — it is the
                     package marker, carried by `packages`)
      init_symbols : {package-tuple: frozenset of names its `__init__.py` declares}
    Non-source directories (`.cache`, `__pycache__`, dot-dirs) are pruned so a build artifact never shadows a
    real module."""
    packages: set = set()
    modules: set = set()
    init_symbols: dict = {}
    for dp, dns, fns in os.walk(tools_root_abs):
        dns[:] = [d for d in dns if d != "__pycache__" and not d.startswith(".")]
        rel = os.path.relpath(dp, tools_root_abs)
        base = () if rel == "." else tuple(rel.split(os.sep))
        if base and "__init__.py" in fns:
            packages.add(base)
            init_symbols[base] = _py_declared_names(os.path.join(dp, "__init__.py"))
        for fn in fns:
            if fn.endswith(".py") and fn != "__init__.py":
                modules.add(base + (fn[:-3],))
    return packages, modules, init_symbols


def _resolve_tool_imports(source_rel: str, tree, index, tools_root_rel: str) -> list:
    """The IN-REPO import targets of one tool source, as repo-relative `.py` paths, from its parsed AST
    (`ast.walk` so a lazy in-function import counts too). `index` is `_tool_module_index`'s triple. Raises
    `DanglingImportError` on an in-repo name that resolves to nothing (so no silent in-repo miss); drops a
    name whose head is not an in-repo top-level module (stdlib/external). A RELATIVE import is resolved
    against the source file's own package (dropped only if it climbs above the tools root); the engine
    convention is flat absolute imports, so none exist today, but resolving rather than skipping keeps the
    no-silent-miss guarantee. Package-before-module, matching CPython's own resolution order. Not deduped —
    the caller dedupes, drops self-edges, and maps through `path_to_id`."""
    packages, modules, init_symbols = index

    def _head_in_repo(parts) -> bool:
        head = (parts[0],)
        return head in packages or head in modules

    def _resolve(parts):
        key = tuple(parts)
        if key in packages:
            return tools_root_rel + "/" + "/".join(parts) + "/__init__.py"
        if key in modules:
            return tools_root_rel + "/" + "/".join(parts) + ".py"
        return None

    # the importing file's own package parts (relative to the tools root), for relative-import resolution.
    inside = source_rel[len(tools_root_rel) + 1:] if source_rel.startswith(tools_root_rel + "/") else source_rel
    src_pkg = tuple(inside.split("/")[:-1])
    out: list = []

    def _emit_from(parts, node_names, label):
        """Emit the edges of a `from <parts> import <names>`. `parts` is the resolved (absolute) dotted target;
        empty parts means the names are top-level (a relative import climbed to the tools root). Raises on a
        name that resolves to nothing in-repo; `"*"` in a package `__init__` (a star re-export) lets an
        otherwise-unresolvable name resolve to the package."""
        modpath, syms = None, frozenset()
        if parts:
            modpath = _resolve(parts)
            if modpath is None:
                raise DanglingImportError(_dangling_import_message(source_rel, label))
            if not modpath.endswith("/__init__.py"):       # a module file: imported names are its attributes
                out.append(modpath)
                return
            syms = init_symbols.get(tuple(parts), frozenset())
        for a in node_names:
            if a.name == "*":
                continue
            sub = _resolve(list(parts) + [a.name])
            if sub is not None:
                out.append(sub)                            # a submodule
            elif modpath is not None and (a.name in syms or "*" in syms):
                out.append(modpath)                        # a re-exported symbol -> the package itself
            else:
                # a clean spec for the message: top-level (no parts) -> the bare name; a dotted/relative
                # label already ending in a dot -> no extra dot (so `from . import x` reads '.x', not '..x').
                spec = a.name if not parts else (f"{label}{a.name}" if label.endswith(".")
                                                 else f"{label}.{a.name}")
                raise DanglingImportError(_dangling_import_message(source_rel, spec))

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                parts = a.name.split(".")
                if not _head_in_repo(parts):
                    continue
                resolved = _resolve(parts)
                if resolved is None:
                    raise DanglingImportError(_dangling_import_message(source_rel, a.name))
                out.append(resolved)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0:
                if not node.module:
                    continue
                parts = node.module.split(".")
                if not _head_in_repo(parts):
                    continue                               # stdlib / external
                _emit_from(parts, node.names, node.module)
            else:                                          # a relative import: climb (level-1) packages up
                if node.level - 1 > len(src_pkg):
                    continue                               # climbs above the tools root -> not an in-repo target
                base = list(src_pkg[: len(src_pkg) - (node.level - 1)])
                parts = base + (node.module.split(".") if node.module else [])
                _emit_from(parts, node.names, "." * node.level + (node.module or ""))
    return out


def _hook_wired_tools(manifests: list) -> dict:
    """{module_id: sorted[tool repo-relative .py path]} — the payload tools each module wires as a hook, from
    its manifest `wires[]` entries of type `hook` (the `.py` path(s) in the hook command; the shared launcher
    `.sh` is not a payload and is excluded by the regex)."""
    out: dict = {}
    for _, m in manifests:
        mid = m.get("id")
        tools: set = set()
        for w in (m.get("wires") or []):
            if w.get("type") != "hook":
                continue
            cmd = ((w.get("hook") or {}).get("command")) or ""
            tools.update(_TOOLS_PY_IN_CMD.findall(cmd))
        if tools:
            out[mid] = sorted(tools)
    return out


def _mcp_handle_to_tool(mcp_abs_path: str) -> dict:
    """{server handle: tool repo-relative path} from `.mcp.json` `mcpServers[].args` (the `tools/<x>.py` arg,
    prefixed `.engine/`). A missing or malformed `.mcp.json` yields `{}` (fail-soft; its presence is checked
    elsewhere)."""
    out: dict = {}
    try:
        with open(mcp_abs_path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return out
    for handle, spec in (data.get("mcpServers") or {}).items():
        for arg in (spec.get("args") or []):
            if isinstance(arg, str) and arg.startswith(TOOLS_DIRNAME + "/") and arg.endswith(".py"):
                out[handle] = ".engine/" + arg
                break
    return out


def _summary_for(tree) -> "str | None":
    """The tool's one-line `summary`: the FIRST line of its module docstring, VERBATIM — control and format
    characters stripped, inner whitespace collapsed, truncated to 160 chars. None when the module declares no
    docstring. The file's own declared words, copied not interpreted. The scrub keeps ordinary spacing but
    drops every Unicode control/format/separator character (`C*`, `Zl`, `Zp`) — so a bidi override, a
    zero-width or DEL/C1 control in a docstring cannot ride invisible or direction-flipping text into the
    committed graph and the cold-start readout. Whitespace (incl. tabs) is preserved as a separator and then
    collapsed. Mirrors boot._one_line's category-based scrub."""
    doc = ast.get_docstring(tree)
    if not doc:
        return None
    lines = doc.strip().splitlines()
    first = lines[0] if lines else ""
    kept = "".join(ch for ch in first
                   if ch.isspace() or unicodedata.category(ch)[0] != "C" and unicodedata.category(ch) not in ("Zl", "Zp"))
    return " ".join(kept.split())[:160].rstrip() or None


def _has_main_guard(tree) -> bool:
    """True iff the module body has a top-level `if __name__ == '__main__':` guard (the CLI marker)."""
    for node in tree.body:
        if isinstance(node, ast.If):
            t = node.test
            if isinstance(t, ast.Compare) and isinstance(t.left, ast.Name) and t.left.id == "__name__":
                return True
    return False


def _entrypoint_for(rel: str, tree, hook_tools: set, mcp_tools: set, ci_tools: set) -> str:
    """A `.py` tool's role, by fixed precedence: test > demo > hook > mcp-server > ci > cli > library. `test`
    and `demo` are name conventions; `hook`/`mcp-server`/`ci` are declared wirings (a hook payload, a
    registered MCP server, a check's `params.script`); `cli` has a `__main__` guard; else `library`."""
    base = os.path.basename(rel)
    if base.startswith("test_"):
        return "test"
    if base.startswith("demo_"):
        return "demo"
    if rel in hook_tools:
        return "hook"
    if rel in mcp_tools:
        return "mcp-server"
    if rel in ci_tools:
        return "ci"
    if _has_main_guard(tree):
        return "cli"
    return "library"


# ---- pure derivation layer (no committed-file IO; fixture-testable) --------------------------

def derive_entities(catalog: dict, manifests: list, inventory: list, claims: dict,
                    deployment_contracts=()) -> list:
    """The whole entity set, derived from the live sources, sorted by id. `manifests` is the list of
    (relpath, manifest) pairs from discover_manifests(); `inventory` the engine file relpaths;
    `claims` the {relpath: [module-id]} ownership map; `deployment_contracts` the per-instance eADR
    relpaths (the deployment-owned stream, in no module's `provides` — Pass 1b). All edges are
    MECHANICAL and OUTGOING."""
    surfaces = (catalog or {}).get("surfaces", {})
    entities: dict = {}
    path_to_id: dict = {}
    contract_fm_by_id: dict = {}                        # contract entity id -> its parsed frontmatter (Pass 3b)

    # Pass 1 — one entity per owned engine file that lives under a catalogued surface.
    for rel in inventory:
        surface = _surface_for(rel, surfaces)
        if surface is None:
            continue                                   # foundation/infra/module-manifest/knowledge dir
        owners = claims.get(rel) or []
        if not owners:
            continue                                   # an unowned file is a coherence anomaly, caught elsewhere
        slug = _instance_slug(surface, rel)
        eid = f"{surface}:{slug}"
        rec = surfaces[surface] or {}
        preds = {"provided_by": [f"module:{owners[0]}"]}
        governing = rec.get("governing_schema")
        if governing and not governing.startswith("http"):   # an in-repo schema file, not the dialect URI
            preds["governed_by"] = [f"schema:{_slug(governing)}"]
        ent = {
            "id": eid, "type": surface, "name": rel, "slug": slug,
            "source": {"path": rel, "fingerprint": source_fingerprint(rel)},
            "owner": owners[0], "predicates": preds,
        }
        # Harvest the surface's DECLARED attributes. Parse the file ONCE by its catalog class
        # (prose -> frontmatter; structured -> JSON; code/other -> nothing). A malformed file harvests
        # nothing (its own schema check is the gate); the harvesters are pure (operate on parsed dicts).
        fm, jd = {}, {}
        try:
            if rec.get("class") == "prose":
                fm = validate.frontmatter(os.path.join(validate.ROOT, rel)) or {}
            elif rec.get("class") == "structured":
                jd = validate.load_json(os.path.join(validate.ROOT, rel))
        except Exception:
            fm, jd = {}, {}
        ent["status"] = _status_for(surface, fm, None)
        tier = _tier_for(surface, jd)
        if tier is not None:
            ent["tier"] = tier
        title = _title_for(surface, jd if rec.get("class") == "structured" else fm)
        if title is not None:
            ent["title"] = title
        ent.update(_discriminators_for(surface, fm, jd, None))
        if surface == "contract":
            contract_fm_by_id[eid] = fm
        entities[eid] = ent
        path_to_id[rel] = eid

    # Pass 1b — one NON-CANON entity per deployment-authored eADR (the per-instance stream, in no module's
    # `provides`, so it is absent from `inventory` and Pass 1 never sees it). By design, the
    # knowledge graph derives an entity per eADR by the same presence walk. A deployment entity carries NO
    # `provided_by` edge (that absence is the non-canon signal Pass 3b reads) and the reserved `owner` token,
    # but IS `governed_by` contract.v1 like any contract. It runs before Pass 3 (so the widened contract
    # checks' `targets` resolve to these ids via `path_to_id`) and before Pass 3b (so `canon_ids` excludes it).
    contract_rec = surfaces.get("contract") or {}
    contract_governing = contract_rec.get("governing_schema")
    for rel in deployment_contracts:
        slug = _instance_slug("contract", rel)         # path-qualified `instance.<stem>` (collision-proof)
        eid = f"contract:{slug}"
        preds = {}
        if contract_governing and not contract_governing.startswith("http"):
            preds["governed_by"] = [f"schema:{_slug(contract_governing)}"]
        ent = {
            "id": eid, "type": "contract", "name": rel, "slug": slug,
            "source": {"path": rel, "fingerprint": source_fingerprint(rel)},
            "owner": DEPLOYMENT_OWNER, "predicates": preds,   # non-canon: NO provided_by
        }
        try:
            fm = validate.frontmatter(os.path.join(validate.ROOT, rel)) or {}
        except Exception:
            fm = {}
        ent["status"] = _status_for("contract", fm, None)
        title = _title_for("contract", fm)
        if title is not None:
            ent["title"] = title
        ent.update(_discriminators_for("contract", fm, {}, None))
        contract_fm_by_id[eid] = fm
        entities[eid] = ent
        path_to_id[rel] = eid

    # Pass 2 — one entity per installed module.
    for path, m in manifests:
        mid = m.get("id")
        eid = f"module:{mid}"
        preds = {}
        deps = sorted((m.get("depends") or {}).keys())
        if deps:
            preds["depends_on"] = [f"module:{d}" for d in deps]
        ent = {
            "id": eid, "type": "module", "name": mid, "slug": mid,
            "source": {"path": path, "fingerprint": source_fingerprint(path)},
            "owner": mid, "predicates": preds,
        }
        ent["status"] = _status_for("module", {}, m)
        ent.update(_discriminators_for("module", {}, {}, m))   # version
        entities[eid] = ent
        path_to_id[path] = eid

    # Pass 3 — `targets` edges for check entities (needs the full path->id map).
    for rel, eid in path_to_id.items():
        if entities[eid]["type"] != "check":
            continue
        try:
            rule = validate.load_json(os.path.join(validate.ROOT, rel))
        except Exception:
            continue                                   # a malformed check is caught by its schema check
        matched = [_rel(p) for p in validate.target_files(rule)]
        targets = sorted({path_to_id[mp] for mp in matched if mp in path_to_id})
        if targets:
            entities[eid]["predicates"]["targets"] = targets

    # Pass 3b — `supersedes` edges (contract->contract, DEPLOYMENT-STREAM only). Canon contracts are those
    # a module's `provides` claims (told apart by provides-membership, never a path/marker) — in the
    # graph, a canon contract carries a `provided_by` edge (Pass 1) and a deployment eADR does not (Pass 1b).
    # `_supersedes_edges` emits an edge only when BOTH ends are non-canon, so with the deployment stream now
    # entitized the leg is live for deployment eADRs and stays inert for the canon.
    contract_entities = [entities[k] for k in sorted(entities) if entities[k]["type"] == "contract"]
    canon_ids = {e["id"] for e in contract_entities if e["predicates"].get("provided_by")}
    for src_id, targets in _supersedes_edges(contract_entities, contract_fm_by_id, canon_ids).items():
        if targets:
            entities[src_id]["predicates"]["supersedes"] = targets

    # Pass 4 — code-dependency (imports/tests), wiring (enforced_by/wires_hook/implemented_by), and per-tool
    # identity attributes (summary/entrypoint/guarded), all from the tool tree's DECLARED facts. A dangling
    # in-repo import raises (loud): the graph refuses a fabricated edge, so the session that introduced it
    # fixes it before merge (the CI fingerprint gate fails closed on the raise) and the committed graph on the
    # default branch can never carry a dangling import.
    tools_root_abs = os.path.join(validate.ENGINE_DIR, TOOLS_DIRNAME)
    tools_root_rel = _rel(tools_root_abs)                  # ".engine/tools"
    # Classify every tool's guarded-ness in ONE guard scan. `flagged_changes` derives the check-script set AND
    # the instance floor once and threads them through is_guardrail (its own "one disk scan per run, not per
    # file" contract), so this stays on the guard's PUBLIC seam yet avoids re-reading every check rule once per
    # tool. A synthetic canary rides the same batch: it reads guarded ONLY under the classifier's blanket
    # fail-safe (an unreadable check rule), so a degraded all-true `guarded` fails generation loud instead of
    # landing in the committed graph.
    guard_canary = tools_root_rel + "/__knowledge_guard_canary__.py"
    _tool_paths = [entities[e]["source"]["path"] for e in entities if entities[e]["type"] == "tool"]
    guarded_set = {name for _st, name in weakening_guard.flagged_changes(
        [{"filename": p, "status": "modified"} for p in _tool_paths + [guard_canary]])}
    if guard_canary in guarded_set:
        raise ValueError(
            "knowledge graph: the guardrail classifier is in its blanket fail-safe (a check rule under "
            ".engine/check/ could not be read), so every tool would be marked guarded. Refusing to record a "
            "graph with a degraded 'guarded' derivation — fix the unreadable check rule and regenerate.")
    mod_index = _tool_module_index(tools_root_abs)
    hook_tools_by_mod = _hook_wired_tools(manifests)
    hook_tool_set = {p for ps in hook_tools_by_mod.values() for p in ps}
    handle_to_tool = _mcp_handle_to_tool(os.path.join(validate.ROOT, ".mcp.json"))
    mcp_tool_set = set(handle_to_tool.values())

    # enforced_by (check -> the tool its `params.script` runs) + the ci-entrypoint set.
    ci_tool_set: set = set()
    for rel, eid in path_to_id.items():
        if entities[eid]["type"] != "check":
            continue
        try:
            rule = validate.load_json(os.path.join(validate.ROOT, rel))
        except Exception:
            continue                                       # a malformed check is caught by its schema check
        script = (rule.get("params") or {}).get("script")
        if isinstance(script, str) and script in path_to_id:
            entities[eid]["predicates"]["enforced_by"] = [path_to_id[script]]
            ci_tool_set.add(script)

    # wires_hook (module -> the payload tools it wires as hooks).
    for _, m in manifests:
        meid = f"module:{m.get('id')}"
        tids = sorted({path_to_id[p] for p in hook_tools_by_mod.get(m.get("id"), []) if p in path_to_id})
        if tids:
            entities[meid]["predicates"]["wires_hook"] = tids

    # implemented_by (interface -> the tool its fallback handle registers in .mcp.json).
    for eid in list(entities):
        ent = entities[eid]
        if ent["type"] != "interface":
            continue
        handle = ent.get("fallback")
        tool = handle_to_tool.get(handle) if isinstance(handle, str) else None
        if tool and tool in path_to_id:
            ent["predicates"]["implemented_by"] = [path_to_id[tool]]

    # imports / tests edges + summary / entrypoint / guarded attributes, per tool entity.
    for eid in list(entities):
        ent = entities[eid]
        if ent["type"] != "tool":
            continue
        rel = ent["source"]["path"]
        ent["guarded"] = rel in guarded_set                  # EVERY tool entity, incl. non-.py (e.g. .sh)
        if not rel.endswith(".py"):
            continue                                       # imports/summary/entrypoint are .py-only
        tree = _parse_tool_ast(os.path.join(validate.ROOT, rel))
        if tree is None:
            continue                                       # a malformed .py harvests nothing (its own gate)
        targets = _resolve_tool_imports(rel, tree, mod_index, tools_root_rel)
        predicate = "tests" if os.path.basename(rel).startswith("test_") else "imports"
        tids = sorted({path_to_id[t] for t in targets if t in path_to_id and path_to_id[t] != eid})
        if tids:
            ent["predicates"][predicate] = tids
        summary = _summary_for(tree)
        if summary:
            ent["summary"] = summary
        ent["entrypoint"] = _entrypoint_for(rel, tree, hook_tool_set, mcp_tool_set, ci_tool_set)

    return [entities[k] for k in sorted(entities)]


def render_graph(entities: list) -> str:
    """The whole deterministic graph JSON: sorted keys, 2-space indent, LF, exactly one final newline
    — so regenerate-and-compare is a valid byte-equality test."""
    graph = {"schema_version": SCHEMA_VERSION, "entities": entities}
    return json.dumps(graph, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


# ---- pure drift logic (no IO; fixture-testable) ---------------------------------------------

def drift_finding(canonical: str, committed: str | None, path: str, tier: str = "hard") -> dict:
    """The fingerprint gate as a pure function. `note` when the committed text equals the canonical
    derivation; the rule's `tier` (hard) when it drifted or is absent. The hard finding names the one
    fix (regenerate + commit) and the file — never a stack trace."""
    name, where = _display(path), _loc_opt(path)
    if committed is None:
        return validate.finding(
            tier,
            f"The knowledge graph ({name}) has not been generated yet. Create it with "
            f"`{REGEN_CMD}` and commit the result.",
            where)
    if committed != canonical:
        return validate.finding(
            tier,
            f"The knowledge graph ({name}) is out of date — it no longer matches the surfaces it is "
            f"generated from (a surface changed, was added, or was removed without regenerating). "
            f"Regenerate it with `{REGEN_CMD}` and commit the result.",
            where)
    return validate.finding(
        "note",
        f"The knowledge graph ({name}) is in sync with the surfaces it is generated from.",
        where)


# ---- IO / source layer ----------------------------------------------------------------------

def deployment_contract_inventory() -> list:
    """The deployment-owned per-instance eADR stream (`.engine/contracts/instance/*eADR-*.md`) — committed,
    in NO module's `provides`, so it never appears in the ownership `inventory`. Read by its own presence
    walk. The `*eADR-*` glob matches both a bare `eADR-####` record and a project-namespaced
    `<project-slug>-eADR-####` record (the deployment naming scheme, eADR-0017), in lockstep with the
    contract checks' target. FAIL-SAFE by construction: `glob.glob` returns `[]` when `instance/` does not
    exist (a deployed repo may never have created it), so the derivation stays deterministic and never raises
    here. Excludes `instance/README.md` (the folder's guide, not an eADR — it has no `eADR` in its name). One
    level deep — the documented flat, one-file-per-decision layout; the contract checks' `**` target is
    deliberately broader (depth-agnostic, the more-protective choice), so an eADR nested deeper would be
    checked but not indexed — and it would also trip the ownership walk as an unowned surface, so it could not
    merge in a deployed repo regardless."""
    pattern = os.path.join(validate.ROOT, ".engine", "contracts", "instance", "*eADR-*.md")
    return sorted(_rel(p) for p in _glob.glob(pattern) if os.path.isfile(p))


def load_sources():
    """The live sources: (catalog dict, [(relpath, manifest)], [surface-instance file relpaths],
    {relpath: [module-id]}, [deployment-eADR relpaths]). Reuses module_coherence's present-set + ownership
    readers so the graph and the module manager read the same installed set; the inventory is the
    catalog-location-driven surface walk (`surface_instance_inventory`), which spans .engine/ AND .claude/
    and drops placeholders. The deployment-eADR stream is walked separately (it is in no module's
    `provides`). Raises (loud) on a malformed source."""
    catalog = validate.load_json(validate.CATALOG_PATH)
    manifests = module_coherence.discover_manifests()
    claims = module_coherence.provides_claims(manifests)
    inventory = surface_instance_inventory(catalog, claims)
    deployment_contracts = deployment_contract_inventory()
    return catalog, manifests, inventory, claims, deployment_contracts


def canonical_graph() -> str:
    """The canonical graph rendered from the live sources."""
    return render_graph(derive_entities(*load_sources()))


def read_committed(path: str):
    """The committed graph's exact bytes-as-text, or None if absent. newline='' so universal-newline
    translation cannot mask a CRLF-vs-LF difference in the equality test."""
    if not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8", newline="") as fh:
        return fh.read()


def write_graph(text: str, path: str) -> None:
    """Write the graph verbatim (newline='' so the LF content is not platform-translated), ATOMICALLY: a
    regen killed mid-write must never leave a truncated graph.json (the corrupt-input producer the reader
    now tolerates in knowledge_index._load_graph). Write a temp then os.replace — mirroring the index's
    atomic build_index. The temp lives in a `.cache/` dir BESIDE the target (derived from the target's own
    directory, so a test/demo scratch path stays self-contained): `.cache/` is on the same filesystem —
    so os.replace is an atomic rename, never a cross-device failure — and is gitignored + pruned from the
    ownership walk, so a crash-orphaned temp is invisible to git and never a false ownership orphan."""
    parent = os.path.dirname(path) or "."
    os.makedirs(parent, exist_ok=True)
    cache = os.path.join(parent, ".cache")
    os.makedirs(cache, exist_ok=True)
    tmp = os.path.join(cache, f"{os.path.basename(path)}.building.{os.getpid()}")
    if os.path.exists(tmp):
        os.remove(tmp)
    with open(tmp, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)
    os.replace(tmp, path)


def generate(path: str | None = None) -> dict:
    """(Re)write the committed graph from the live sources. Returns a `note` finding stating whether
    the file changed. `path` defaults to the live GRAPH_PATH (resolved at call time so a test may
    redirect it)."""
    path = GRAPH_PATH if path is None else path
    canonical = canonical_graph()
    changed = read_committed(path) != canonical
    write_graph(canonical, path)
    name = _display(path)
    msg = (f"Wrote the knowledge graph ({name})." if changed
           else f"The knowledge graph ({name}) was already up to date.")
    return validate.finding("note", msg, _loc_opt(path))


def check(path: str | None = None, tier: str = "hard") -> dict:
    """The fingerprint gate over the live sources + the committed file at `path` (defaults to the live
    GRAPH_PATH, resolved at call time so a test may redirect it). The drift severity is the caller's
    tier (the relaying rule's tier); in-sync is a `note`."""
    path = GRAPH_PATH if path is None else path
    return drift_finding(canonical_graph(), read_committed(path), path, tier)


# ---- the commit-boundary regen hook ----------------------------------------------------------
# Fires at the `git commit` boundary — the classifier is hooks._is_git_commit, shared with the other
# commit-boundary hooks (self_map's regen, validation's pre-commit nudge) rather than copied here.


def _regen_handler(payload: dict) -> dict:
    """The `PreToolUse` regen behaviour: on a `git commit`, refresh the committed graph best-effort, then
    ALWAYS proceed. This is the one hook that legitimately mutates committed state (it writes the real
    GRAPH_PATH). It NEVER blocks and NEVER injects: a regen failure proceeds (the commit is allowed) and
    is caught downstream by the CI fingerprint check. It is a MUTATION, not a gate, so it does not promote
    a finding (that law is for a gate that goes blind) — but it is never silent on failure (a plain note
    to stderr). The regen fires even when the commit will be denied by another `PreToolUse` hook (e.g.
    modes' Explore write-gate): both hooks run and `deny` wins, so the regen only ever refreshes an
    unstaged file the denied commit never captures — harmless."""
    if not hooks._is_git_commit(payload):
        return hooks.proceed()
    try:
        result = generate()  # best-effort: refresh the committed graph (UNSTAGED) in the working tree
    except Exception as exc:  # noqa: BLE001 — a best-effort MUTATION, never a gate: proceed, never block;
        #   the CI knowledge-coverage fingerprint check is the durable backstop for any resulting staleness.
        sys.stderr.write(
            f"(knowledge) the commit-boundary knowledge-graph refresh could not run "
            f"({type(exc).__name__}: {exc}); your commit was not affected — the merge-time check will "
            f"catch any staleness.\n")
        return hooks.proceed()
    # Not silent when it changed something: a plain best-effort note (on a proceeding `PreToolUse` this
    # reaches the debug log, not the transcript — the durable record is the working-tree change the CI gate
    # forces into a following commit). Keyed to generate()'s own "Wrote ..." message (same file).
    if (result.get("message") or "").startswith("Wrote"):
        sys.stderr.write(
            "(knowledge) refreshed the knowledge graph (.engine/knowledge/graph.json) for this commit; it "
            "is left in your working tree for the next commit — your commit was not affected.\n")
    return hooks.proceed()


# ---- CLI ------------------------------------------------------------------------------------

def _hook_demo(_argv: list) -> int:
    """Show the commit-boundary regen WITHOUT touching the committed graph: which tool calls trigger it,
    that a refresh writes the graph, and that it never blocks. The real graph.json is untouched."""
    commit = {"tool_name": "Bash", "tool_input": {"command": "git add -A && git commit -m 'x'"}}
    status = {"tool_name": "Bash", "tool_input": {"command": "git status"}}
    a_read = {"tool_name": "Read", "tool_input": {"file_path": "x"}}
    print("Which tool calls fire the commit-boundary regen (the PreToolUse hook tests this in-script):")
    ok = True
    for label, p, expected in (("git add -A && git commit", commit, True), ("git status", status, False),
                               ("a Read", a_read, False)):
        fired = hooks._is_git_commit(p)
        ok = ok and fired == expected
        print(f"    {'FIRES' if fired else 'skips'} - {label}")
    with tempfile.TemporaryDirectory() as d:
        scratch = os.path.join(d, "graph.json")
        print("\nWhen it fires it refreshes the graph (shown on a throwaway copy):")
        gen = generate(scratch)
        print("    " + validate.fmt(gen))
        ok = ok and (gen.get("message") or "").startswith("Wrote")
    print("\nThe hook ALWAYS proceeds: a commit is never blocked, and on any failure the commit still "
          "goes through (the merge-time fingerprint check catches any staleness). Your real "
          ".engine/knowledge/graph.json was never touched.")
    if not ok:
        print("\nDEMO UNEXPECTED: a `git commit` must fire the regen (a status/read must not) and the "
              "refresh must write the file.", file=sys.stderr)
        return 1
    return 0


def _demo(_argv: list) -> int:
    """A safe, scripted fail->pass on a THROWAWAY COPY — never touches the committed graph."""
    with tempfile.TemporaryDirectory() as d:
        scratch = os.path.join(d, "graph.json")
        print("Generating the knowledge graph onto a throwaway copy (your committed file is untouched)...")
        print("    " + validate.fmt(generate(scratch)))
        print("(i) Checking it — should be in sync...")
        c1 = check(scratch)
        print("    " + validate.fmt(c1))
        print("(ii) Now hand-editing the copy to simulate drift...")
        with open(scratch, "a", encoding="utf-8", newline="") as fh:
            fh.write("a hand-edited line the generator would never write\n")
        c2 = check(scratch)
        print("    " + validate.fmt(c2))
        print("(iii) Regenerating to heal it...")
        print("    " + validate.fmt(generate(scratch)))
        c3 = check(scratch)
        print("    " + validate.fmt(c3))
        print("Done — a hand-edit was caught (drift) and regeneration restored the file (in sync). "
              "Your real .engine/knowledge/graph.json was never touched.")
        ok = c1["severity"] != "hard" and c2["severity"] == "hard" and c3["severity"] != "hard"
    if not ok:
        print("\nDEMO UNEXPECTED: expected in-sync, then drift caught, then in-sync after regen.",
              file=sys.stderr)
        return 1
    return 0


def main(argv: list) -> int:
    cmd = argv[0] if argv else "show"
    try:
        if cmd == "show":
            sys.stdout.write(canonical_graph())
            return 0
        if cmd == "generate":
            path = argv[1] if len(argv) > 1 else None
            print(validate.fmt(generate(path)))
            return 0
        if cmd == "check":
            path = argv[1] if len(argv) > 1 else None
            f = check(path)
            print(validate.fmt(f))
            return 1 if f["severity"] == "hard" else 0
        if cmd == "demo":
            return _demo(argv[1:])
        if cmd == "hook-demo":
            return _hook_demo(argv[1:])
        if cmd == "hook":  # the PreToolUse entry the engine wires: regen at the git-commit boundary
            return hooks.run_hook("PreToolUse", _regen_handler)
        print(f"usage: knowledge_gen.py {{show|generate|check|demo|hook-demo|hook}} [path]\n"
              f"unknown command {cmd!r}", file=sys.stderr)
        return 2
    except (OSError, ValueError) as exc:  # a malformed source / unwritable path -> plain, no traceback
        print(f"CONFIG ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
