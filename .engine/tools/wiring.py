#!/usr/bin/env python3
"""The wiring library — applies and reverses module `wires` over a CLOSED seam vocabulary.

A module touches shared state (Claude settings, MCP registration, the ontology catalog,
`.gitignore`) only through a small, reviewed set of directive types, each with a *guaranteed
reverser*. This library is the permanent home of those applier/reverser pairs — both
provisioning subsystems (the one-time instantiator and the permanent module manager) and the
CODEOWNERS renderer call it, so the wiring logic does not die with the self-deleting
instantiator.

THE R5 FIREWALL. The seam vocabulary is closed to seven types — hook, mcp, ontology-entry,
permission, gitignore, codex-hook, codex-mcp — and there is **no `custom/script` escape hatch**: an
arbitrary shared-state mutation with no guaranteed reverser *is* the R5 failure (module-system 99-103).
A new seam is a reviewed change to this file (a new applier/reverser pair), not a runtime
directive. The dispatch is reject-by-default: an unknown type mutates nothing.

REVERSAL KEYS ON ENGINE-NAMESPACED IDENTITY, never bare content (module-system 105-115):
  - hook        -> .claude/settings.json, keyed on {event, matcher, type, command}; command -> .engine/
  - mcp         -> root .mcp.json, keyed on the engine-prefixed server name; never writes approval
  - ontology-entry -> the engine-owned catalog (.engine/schemas/surface-catalog.json), keyed on surface name
  - permission  -> .claude/settings.json; a bare string is NOT namespaceable, so reverse "errs toward
                   leaving it" (a documented no-op) — the worst case is a tolerated residual, never
                   removing one the operator wanted (module-system 110-113)
  - gitignore   -> root .gitignore, engine lines inside a comment-fenced engine-managed block
  - codex-hook  -> .codex/hooks.json (the Codex runtime's registration), keyed on {event, matcher,
                   type, command} exactly like `hook`; command -> .engine/. Codex trusts hooks per
                   exact definition, so every apply/reverse that changes the file carries the
                   re-trust notice in its finding (eADR-0034).
  - codex-mcp   -> .codex/config.toml, the engine server rendered inside a comment-fenced
                   engine-managed block keyed on the engine-prefixed server name — a fence, never a
                   whole-file TOML rewrite, so product tables outside the fences are untouched; the
                   whole file must parse as TOML before AND after, or the engine refuses (eADR-0034)

Apply **inserts iff absent**; reverse **removes only the engine-identified entry** (an operator's
or product's identical-looking entry is left untouched). Every apply and reverse is **idempotent**,
so a crashed half-install is safe to re-run (module-system 117-119).

MUTATOR POSTURE — fail open and flag (the inverse of validate.py's fail-closed checker). On a file
it cannot safely parse, the library does NOT mutate (no blind overwrite of operator data) and does
NOT crash (no traceback to a non-engineer); it returns a plain-language finding.v1 and is re-runnable
once the file is fixed (hooks fail-open-and-flag).

The comment-fenced-block helper (fence_apply/fence_reverse) is a *library helper*, NOT a module
`wires` seam: the `gitignore` seam calls it, and so do the foundation `.gitignore` block
(apply_foundation_ignores) and the CODEOWNERS renderer at provisioning (provisioning 189-195, 254-267).

CLI (the operator-runnable demo — the `gitignore` seam is the only one with a live target today):
  uv run --directory .engine -- python tools/wiring.py demo-gitignore /tmp/demo.gitignore
  uv run --directory .engine -- python tools/wiring.py gitignore-apply   /tmp/mine.gitignore ".engine/.venv/"
  uv run --directory .engine -- python tools/wiring.py gitignore-reverse /tmp/mine.gitignore
"""
from __future__ import annotations
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate  # noqa: E402


# ---- engine-identity constants (one declared convention; module-system 105-115) ------------
# The fence marker (build-spec leaf b, decided with the maintainer): the conventional BEGIN/END
# form plus a plain-language cue, so a non-engineer who opens the file is never confused. `{id}`
# distinguishes coexisting fences (a module id, "foundation-ignores", "codeowners", ...). No
# checksum/provenance tag lives in the file — the manifest `wires` block is the complete record
# (module-system 114).
FENCE_BEGIN = "# BEGIN engine-managed block: {id} - do not edit inside"
FENCE_END = "# END engine-managed block: {id}"
# Stable prefixes used to detect a forged marker in a body line, regardless of id.
_FENCE_BEGIN_TOKEN = "# BEGIN engine-managed block:"
_FENCE_END_TOKEN = "# END engine-managed block:"
# The Markdown/HTML-comment fence style — same BEGIN/END grammar inside an HTML comment, for a keyed
# engine section in a Markdown file (the root CLAUDE.md floor) where a leading '#' would render as a
# heading. Selected via the `style=` parameter on the fence primitives; the '#' style stays the default.
MD_FENCE_BEGIN = "<!-- BEGIN engine-managed block: {id} - do not edit inside -->"
MD_FENCE_END = "<!-- END engine-managed block: {id} -->"
_MD_FENCE_BEGIN_TOKEN = "<!-- BEGIN engine-managed block:"
_MD_FENCE_END_TOKEN = "<!-- END engine-managed block:"

MCP_NAME_PREFIX = "engine-"               # an engine MCP server name is engine-prefixed (module-system 106)
CLAUDE_PROJECT_DIR = "${CLAUDE_PROJECT_DIR:-.}"  # the literal MCP path form (module-system 155-156); never locally expanded
ENGINE_DIR_MARKER = ".engine/"            # an engine hook command resolves under here (module-system 106)

# Identity-token patterns reused from the repo's existing grammar — the path/marker-injection
# firewall. Anchored with \A...\Z (NOT ^...$): in Python `$` also matches just before a trailing
# newline, so "^[a-z]+$" would admit "core\n" — a token that forges a split fence marker on disk.
# \Z matches only the true end of string, so '/', '\n', '\r', '..', and marker fragments are all rejected.
_ID_RE = re.compile(r"\A[a-z][a-z0-9-]*\Z")        # module ids / fence ids / mcp name suffix (module.v1 id pattern)
_SURFACE_NAME_RE = re.compile(r"\A[a-z][a-z-]*\Z")  # surface names (surface-catalog.schema.json $defs.surfaceName)

# Target files are HARDCODED — never derived from directive content (path-escape firewall).
# Module-level so tests can redirect them to a temp dir.
SETTINGS_PATH = os.path.join(validate.ROOT, ".claude", "settings.json")   # hook, permission
MCP_PATH = os.path.join(validate.ROOT, ".mcp.json")                       # mcp
GITIGNORE_PATH = os.path.join(validate.ROOT, ".gitignore")               # gitignore
CATALOG_PATH = validate.CATALOG_PATH                                      # ontology-entry
CATALOG_SCHEMA_PATH = os.path.join(validate.SCHEMAS_DIR, "surface-catalog.schema.json")
CODEX_HOOKS_PATH = os.path.join(validate.ROOT, ".codex", "hooks.json")    # codex-hook
CODEX_CONFIG_PATH = os.path.join(validate.ROOT, ".codex", "config.toml")  # codex-mcp

# The plain-language re-trust notice every codex-hook change carries: Codex records trust against each
# hook's exact definition, so a new or changed registration is silently SKIPPED until the operator
# re-trusts it — the one moment to say so is when the engine makes the change (eADR-0034).
CODEX_RETRUST_NOTE = ("Codex skips new or changed hooks until you approve them again — open Codex "
                      "and run /hooks to re-approve the engine's hooks.")


class WiringError(Exception):
    """A refusable directive or an unsafe target — caught and turned into a hard finding;
    the library mutates nothing on a WiringError (fail-open)."""


# ---- small shared helpers --------------------------------------------------------------------

def _rel(path: str) -> str:
    rel = os.path.relpath(path, validate.ROOT)
    return path if rel.startswith("..") else rel.replace(os.sep, "/")


def _loc_opt(path: str):
    rel = os.path.relpath(path, validate.ROOT)
    return None if rel.startswith("..") else {"file": rel.replace(os.sep, "/"), "line": None}


def _ok(msg: str, path: str | None = None) -> dict:
    """A non-blocking 'note' finding (success / no-op / intentional-leave)."""
    return validate.finding("note", msg, _loc_opt(path) if path else None)


def _fail(msg: str, path: str | None = None) -> dict:
    """A 'hard' finding — the flag half of fail-open. report() never renders this as OK."""
    return validate.finding("hard", msg, _loc_opt(path) if path else None)


def _check_id(fence_id: str) -> None:
    if not isinstance(fence_id, str) or not _ID_RE.match(fence_id):
        raise WiringError(f"refused: {fence_id!r} is not a valid engine identity token "
                          f"(lowercase letters, digits and hyphens; must start with a letter).")


# ---- the comment-fenced-block helper (library helper, NOT a wires seam) -----------------------

class _FenceStyle:
    """A comment-fence style: the begin/end line templates (each carrying `{id}`) and the stable
    begin/end token prefixes used to detect a forged marker in a body line. Two styles ship — the
    default '#' style (gitignore, CODEOWNERS) and the Markdown/HTML-comment style (the CLAUDE.md floor)."""
    __slots__ = ("begin", "end", "begin_token", "end_token")

    def __init__(self, begin, end, begin_token, end_token):
        self.begin = begin
        self.end = end
        self.begin_token = begin_token
        self.end_token = end_token


HASH_FENCE = _FenceStyle(FENCE_BEGIN, FENCE_END, _FENCE_BEGIN_TOKEN, _FENCE_END_TOKEN)
MD_FENCE = _FenceStyle(MD_FENCE_BEGIN, MD_FENCE_END, _MD_FENCE_BEGIN_TOKEN, _MD_FENCE_END_TOKEN)
# A body line that forges EITHER style's marker is refused regardless of the active style (defense-in-depth).
_ALL_FENCE_STYLES = (HASH_FENCE, MD_FENCE)


def _find_fence(lines: list, fence_id: str, style: _FenceStyle = HASH_FENCE):
    """Locate the single well-formed begin..end pair for `fence_id` in `style`. Returns (begin_idx,
    end_idx), or None if absent. Raises WiringError if the fence is malformed (begin-without-end,
    orphan-end, duplicate-begin, begin-after-end, nesting) — the caller then leaves the file
    UNCHANGED and flags, never guessing a boundary and never deleting to EOF."""
    begin = style.begin.format(id=fence_id)
    end = style.end.format(id=fence_id)
    begins = [i for i, ln in enumerate(lines) if ln == begin]
    ends = [i for i, ln in enumerate(lines) if ln == end]
    if not begins and not ends:
        return None
    if len(begins) == 1 and len(ends) == 1 and begins[0] < ends[0]:
        return begins[0], ends[0]
    raise WiringError(
        f"the engine-managed block '{fence_id}' in this file is malformed "
        f"(found {len(begins)} begin and {len(ends)} end marker(s)); the engine left the file "
        f"unchanged. Remove the stray marker line(s) and re-run.")


def fence_apply(text: str, fence_id: str, body_lines: list, *, style: _FenceStyle = HASH_FENCE) -> str:
    """Insert-iff-absent / replace-only-as-a-block, in `style` (default '#'; the CLAUDE.md floor uses
    MD_FENCE). If the keyed fence is absent, append a fresh block; if present, replace only its body
    between its own markers. Bytes OUTSIDE the fence — including an operator line identical to a body
    line — are never touched. Idempotent: an identical re-apply returns identical text.
    (module-system 108-109, 117; provisioning 254.)"""
    _check_id(fence_id)
    body = list(body_lines)
    for bl in body:
        if not isinstance(bl, str):
            raise WiringError("refused: a line to add is not text.")
        if "\n" in bl or "\r" in bl:
            raise WiringError("refused: a line to add contains a line break.")
        stripped = bl.strip()
        if any(stripped.startswith(s.begin_token) or stripped.startswith(s.end_token)
               for s in _ALL_FENCE_STYLES):
            raise WiringError("refused: a line to add would forge an engine fence marker.")
    lines = text.split("\n")
    span = _find_fence(lines, fence_id, style)
    block = [style.begin.format(id=fence_id)] + body + [style.end.format(id=fence_id)]
    if span is not None:
        b, e = span
        return "\n".join(lines[:b] + block + lines[e + 1:])
    if text == "":
        return "\n".join(block + [""])
    if lines[-1] == "":                      # text already ends with a newline
        return "\n".join(lines[:-1] + block + [""])
    return "\n".join(lines + block + [""])   # terminate the final line, then append (content preserved)


def fence_reverse(text: str, fence_id: str, *, style: _FenceStyle = HASH_FENCE) -> str:
    """Remove ONLY the named fence's begin..end span (in `style`); leave every other line byte-identical.
    No-op if the fence is absent. Raises WiringError (→ leave unchanged + flag) if malformed —
    NEVER deletes to EOF on an unterminated fence."""
    _check_id(fence_id)
    lines = text.split("\n")
    span = _find_fence(lines, fence_id, style)
    if span is None:
        return text
    b, e = span
    return "\n".join(lines[:b] + lines[e + 1:])


def fence_present(text: str, fence_id: str, *, style: _FenceStyle = HASH_FENCE) -> bool:
    """True iff a well-formed `fence_id` fence of `style` is present. Reuses _find_fence: absent → False;
    a well-formed span → True; malformed → WiringError (so the caller degrades and leaves the file
    unchanged rather than guessing a boundary)."""
    _check_id(fence_id)
    return _find_fence(text.split("\n"), fence_id, style) is not None


def fence_read(text: str, fence_id: str, *, style: _FenceStyle = HASH_FENCE) -> "list | None":
    """Return the BODY lines between a well-formed `fence_id` fence of `style` — the read-side inverse of
    fence_apply, and the one place the engine reads a fenced block back OUT of a file. The floor source lives
    fenced in the release's root CLAUDE.md/AGENTS.md (not a whole-file `.deployed.md`), so the upgrade and
    arrival paths extract it with this. Returns the body lines exactly as fenced (no whole-file trailing-newline
    artifact to trim); None when the fence is absent (no source to read — the caller treats it like a release
    that ships no floor); raises WiringError when the fence is malformed (begin-without-end, duplicate, nesting),
    so the caller degrades rather than guessing a boundary."""
    _check_id(fence_id)
    lines = text.split("\n")
    span = _find_fence(lines, fence_id, style)
    if span is None:
        return None
    b, e = span
    return lines[b + 1:e]


CODEOWNERS_FENCE = "codeowners"


def render_codeowners(existing_text: str, path_set: list, handle: str) -> str:
    """Render the engine's comment-fenced CODEOWNERS ownership block into `existing_text`, returning the
    new text. Each engine-owned path becomes one file-precise, root-anchored line `/<path> @<owner>`, so
    the engine owns exactly its own files (the engine/product wall). Reuses the SAME comment-fenced-block helper the gitignore
    seam uses, so the block is insert-iff-absent / replaced-only-as-a-block and the operator's own
    CODEOWNERS lines are never touched. The block is APPENDED after any existing content: CODEOWNERS is
    last-match-wins, so the engine block placed last defeats shadowing by earlier product rules over
    engine paths. Greenfield (existing_text == "") yields a block-only file; a re-render replaces the
    block in place. The mutator wrapper apply_codeowners() (below) drives this at BOTH render sites — the
    first-run instantiation and an engine upgrade, which re-renders with the new release's engine paths so
    the wall stays complete. The owner `handle` is the operator's, captured at first run as preserved
    config — passed IN here, never read from the network; this
    renderer is pure — the primitive only. The instantiator wires it into the live first-run
    render with the stored handle, and owns the handle capture + its config home."""
    owner = handle.strip()
    if not owner:
        raise WiringError("refused: rendering CODEOWNERS needs a non-empty owner handle.")
    if not owner.startswith("@"):
        owner = "@" + owner
    body = [f"/{p.lstrip('/')} {owner}" for p in path_set]
    return fence_apply(existing_text, CODEOWNERS_FENCE, body)


def apply_codeowners(co_path: str, path_set: list, handle: str) -> dict:
    """Render the engine ownership block for (`path_set` × `handle`) into the CODEOWNERS at `co_path` and
    write iff changed; returns {"status": "written"|"already", "owner": handle, "paths": len(path_set)}.
    The operator's own CODEOWNERS lines (outside the engine fence) are never touched — render_codeowners
    routes through fence_apply, which replaces only the engine block. Raises WiringError on a bad handle:
    the CALLER owns the degrade + its own operator copy (the pure-primitive posture, mirroring the way
    the gitignore seam's callers own their messaging). This is the ONE render-and-write home both
    first-run instantiation and an engine upgrade call (via module_coherence.codeowners_path_set), so the
    ownership wall renders one way and the two sites cannot drift. Idempotent (write-iff-changed)."""
    existing = _read_text(co_path)
    new_text = render_codeowners(existing, path_set, handle)
    if new_text == existing:
        return {"status": "already", "owner": handle, "paths": len(path_set)}
    _write_text(co_path, new_text)
    return {"status": "written", "owner": handle, "paths": len(path_set)}


# ---- the foundation .gitignore block (a library helper, NOT a module `wires` seam) -------------
# The engine's own tool-runtime + platform artifacts are ignored by a SINGLE foundation-keyed fence,
# distinct from any module `gitignore` block in the same file.
# Placed by the instantiator's apply step and re-asserted on upgrade, NOT declared by any manifest's
# `wires` — the CODEOWNERS precedent (a foundation fence a library helper renders), so the orphan-wire
# reverse coherence leg carves it out (see applied_engine_wires) and a module's uninstall reverser, which
# touches only its own manifest-declared lines, never removes it.
FOUNDATION_IGNORES_FENCE = "foundation-ignores"
# The fence body IS this list, byte-for-byte (single source of truth — the committed .gitignore fence body
# is asserted equal to it by a test, so the two homes cannot drift). No in-fence header line: any line not
# in this list would make the first apply rewrite the file. `.engine/.venv/` + `.engine/.uv/` are the uv
# tool-runtime; `.claude/worktrees/` is the platform's per-session worktree dir (so a sibling session never
# pollutes the main tree's git status, keeping the operator-checkout-strand pre-check's clean-tree read true).
FOUNDATION_IGNORE_LINES = [".engine/.venv/", ".engine/.uv/", ".claude/worktrees/"]


def apply_foundation_ignores(path: str) -> dict:
    """Place the engine's foundation ignore block into the `.gitignore` at `path`, writing iff changed;
    returns {"status": "written"|"already"|"degraded"}. Insert-iff-absent / replaced-only-as-a-block
    (fence_apply), so an operator's own ignore lines and any module `gitignore` block are never touched, and
    greenfield (the committed fence travels wholesale) + brownfield (this appends the fence) converge on the
    same keyed block. Fails open on an unparseable/marker-forging file (status 'degraded', no mutation) — the
    mutator posture; unlike apply_codeowners there is no operator-config decision here to disclose, so it
    swallows the WiringError into a status rather than raising for a caller to render copy. Both the first-run
    instantiator and an engine upgrade call this, so the block renders one way and is release-evolvable (a
    future line change reaches a provisioned repo on upgrade)."""
    try:
        existing = _read_text(path)
        new_text = fence_apply(existing, FOUNDATION_IGNORES_FENCE, FOUNDATION_IGNORE_LINES)
    except WiringError as exc:
        return {"status": "degraded", "detail": str(exc)}
    if new_text == existing:
        return {"status": "already"}
    _write_text(path, new_text)
    return {"status": "written"}


# ---- tolerant IO (the mutator posture: fail open, never clobber, never crash) -----------------

def _read_json_tolerant(path: str, create: bool):
    """Returns (data_dict, None) or (None, hard_finding). Absent → {} when create else a hard
    'missing' finding; empty/whitespace → {}; malformed JSON or non-object top level → a hard
    finding and NO mutation (the file is left exactly as the operator left it)."""
    if not os.path.exists(path):
        if create:
            return {}, None
        return None, _fail(f"cannot apply: the expected file {_rel(path)} is missing — the "
                           f"repository looks malformed. The engine made no change.", path)
    try:
        text = validate.read(path)
    except OSError as exc:
        return None, _fail(f"could not read {_rel(path)}: {exc}. The engine made no change.", path)
    if text.strip() == "":
        return {}, None
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        return None, _fail(f"could not safely update {_rel(path)} because it is not valid JSON "
                           f"({exc.msg}, line {exc.lineno}); the engine left it unchanged. "
                           f"Fix the file and re-run.", path)
    if not isinstance(data, dict):
        return None, _fail(f"could not safely update {_rel(path)}: its top level is not a JSON "
                           f"object; the engine left it unchanged.", path)
    return data, None


def _write_json(path: str, data) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")


def _read_text(path: str) -> str:
    return validate.read(path) if os.path.exists(path) else ""


def _write_text(path: str, text: str) -> None:
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def _json_apply(path: str, transform, success: str, noop: str, create: bool) -> dict:
    """Tolerant read → pure transform → write-iff-changed. transform(data) -> (data, changed)
    and may raise WiringError. All error paths fail open with a hard finding; success/no-op
    return a note."""
    try:
        data, err = _read_json_tolerant(path, create=create)
        if err is not None:
            return err
        data, changed = transform(data)
    except WiringError as exc:
        return _fail(str(exc), path)
    if changed:
        _write_json(path, data)
        return _ok(success, path)
    return _ok(noop, path)


def _text_fence_apply(path: str, fence_id: str, body_lines: list, create: bool) -> dict:
    try:
        text = _read_text(path)
        if not os.path.exists(path) and not create:
            return _fail(f"cannot apply: {_rel(path)} is missing. The engine made no change.", path)
        new = fence_apply(text, fence_id, body_lines)
    except WiringError as exc:
        return _fail(str(exc), path)
    if new != text:
        _write_text(path, new)
        return _ok(f"Added the engine-managed block '{fence_id}' to {_rel(path)}.", path)
    return _ok(f"Nothing to change - the engine-managed block '{fence_id}' is already present "
               f"in {_rel(path)}.", path)


def _text_fence_reverse(path: str, fence_id: str) -> dict:
    try:
        text = _read_text(path)
        new = fence_reverse(text, fence_id)
    except WiringError as exc:
        return _fail(str(exc), path)
    if new != text:
        _write_text(path, new)
        return _ok(f"Removed only the engine-managed block '{fence_id}' from {_rel(path)}; "
                   f"your own lines are untouched.", path)
    return _ok(f"Nothing to remove - no engine-managed block '{fence_id}' in {_rel(path)}.", path)


# ---- pure per-seam transforms (data in, (data, changed) out) ----------------------------------

def settings_add_hook(data: dict, directive: dict):
    event = directive["event"]
    matcher = directive["matcher"]
    entry = directive["hook"]
    command = entry.get("command", "")
    if ENGINE_DIR_MARKER not in command:
        raise WiringError("refused: a hook command must point into .engine/ to be an engine-owned "
                          f"hook (got {command!r}).")
    groups = data.setdefault("hooks", {}).setdefault(event, [])
    group = next((g for g in groups if g.get("matcher") == matcher), None)
    if group is None:
        group = {"matcher": matcher, "hooks": []}
        groups.append(group)
    hooks = group.setdefault("hooks", [])
    if any(h.get("type") == entry.get("type") and h.get("command") == entry.get("command")
           for h in hooks):
        return data, False
    hooks.append(dict(entry))
    return data, True


def settings_remove_hook(data: dict, directive: dict):
    event = directive["event"]
    matcher = directive["matcher"]
    entry = directive["hook"]
    groups = (data.get("hooks") or {}).get(event)
    if not groups:
        return data, False
    changed = False
    for group in list(groups):
        if group.get("matcher") != matcher:
            continue
        existing = group.get("hooks") or []
        kept = [h for h in existing
                if not (h.get("type") == entry.get("type")
                        and h.get("command") == entry.get("command"))]
        if len(kept) != len(existing):
            changed = True
            if kept:
                group["hooks"] = kept
            else:
                groups.remove(group)
    if changed and not groups:
        del data["hooks"][event]
    if changed and not data.get("hooks"):   # prune the engine-created container iff now empty
        data.pop("hooks", None)
    return data, changed


def settings_add_permission(data: dict, directive: dict):
    value = directive["value"]
    allow = data.setdefault("permissions", {}).setdefault("allow", [])
    if value in allow:
        return data, False
    allow.append(value)
    return data, True


def _validate_mcp_name(name: str) -> None:
    if not isinstance(name, str) or not name.startswith(MCP_NAME_PREFIX) \
            or not _ID_RE.match(name[len(MCP_NAME_PREFIX):]):
        raise WiringError(f"refused: an MCP server name must be engine-prefixed "
                          f"({MCP_NAME_PREFIX!r} + a valid id); got {name!r}.")


def mcp_add(data: dict, directive: dict):
    name = directive["name"]
    definition = directive["definition"]
    _validate_mcp_name(name)
    servers = data.setdefault("mcpServers", {})
    if servers.get(name) == definition:
        return data, False
    servers[name] = definition
    return data, True


def mcp_remove(data: dict, directive: dict):
    name = directive["name"]
    _validate_mcp_name(name)
    servers = data.get("mcpServers") or {}
    if name in servers:
        del servers[name]
        if not servers:                      # prune the engine-created container iff now empty
            data.pop("mcpServers", None)
        return data, True
    return data, False


def catalog_add(data: dict, directive: dict, schema: dict):
    name = directive["name"]
    record = directive["record"]
    if not isinstance(name, str) or not _SURFACE_NAME_RE.match(name):
        raise WiringError(f"refused: {name!r} is not a valid surface name (lowercase letters and "
                          f"hyphens).")
    # Authority-tier reservation (issue #401): the top two authority ranks are reserved to the
    # self-referential core (`contract`/`policy`); a module `ontology-entry` may not mint or downgrade one.
    # Name-bound half (the seam has no module identity) — the owner-based half is authority_reservation_findings
    # at the merge gate. Refused fail-closed here so a bad record never lands, mirroring the schema re-check.
    reason = validate.reserved_authority_reason(
        name, record.get("authority") if isinstance(record, dict) else None)
    if reason:
        raise WiringError(f"refused to add the ontology-entry '{name}': {reason} The engine made no change.")
    surfaces = data.setdefault("surfaces", {})
    if surfaces.get(name) == record:
        return data, False
    surfaces[name] = record
    errors = list(validate.Draft202012Validator(schema).iter_errors(data))
    if errors:
        raise WiringError(f"refused to add the ontology-entry '{name}': the resulting catalog is "
                          f"not schema-valid ({errors[0].message}). The engine made no change.")
    return data, True


def catalog_remove(data: dict, directive: dict):
    name = directive["name"]
    surfaces = data.get("surfaces") or {}
    if name in surfaces:
        del surfaces[name]
        return data, True
    return data, False


def _codex_matcher(directive: dict):
    """A codex-hook directive's matcher, normalized: Codex omits the matcher key for an
    always-fire group (an empty matcher string is undocumented there), so '' and absent both
    normalize to None and the file carries the key only when a real matcher is set."""
    return directive.get("matcher") or None


def codex_hooks_add(data: dict, directive: dict):
    event = directive["event"]
    matcher = _codex_matcher(directive)
    entry = directive["hook"]
    command = entry.get("command", "")
    if ENGINE_DIR_MARKER not in command:
        raise WiringError("refused: a hook command must point into .engine/ to be an engine-owned "
                          f"hook (got {command!r}).")
    groups = data.setdefault("hooks", {}).setdefault(event, [])
    group = next((g for g in groups if g.get("matcher") == matcher), None)
    if group is None:
        group = {"hooks": []} if matcher is None else {"matcher": matcher, "hooks": []}
        groups.append(group)
    hooks = group.setdefault("hooks", [])
    if any(h.get("type") == entry.get("type") and h.get("command") == entry.get("command")
           for h in hooks):
        return data, False
    hooks.append(dict(entry))
    return data, True


def codex_hooks_remove(data: dict, directive: dict):
    event = directive["event"]
    matcher = _codex_matcher(directive)
    entry = directive["hook"]
    groups = (data.get("hooks") or {}).get(event)
    if not groups:
        return data, False
    changed = False
    for group in list(groups):
        if group.get("matcher") != matcher:
            continue
        existing = group.get("hooks") or []
        kept = [h for h in existing
                if not (h.get("type") == entry.get("type")
                        and h.get("command") == entry.get("command"))]
        if len(kept) != len(existing):
            changed = True
            if kept:
                group["hooks"] = kept
            else:
                groups.remove(group)
    if changed and not groups:
        del data["hooks"][event]
    if changed and not data.get("hooks"):   # prune the engine-created container iff now empty
        data.pop("hooks", None)
    return data, changed


# ---- the codex-mcp TOML fence renderer (deterministic; scalars via JSON, whose string escapes
# are TOML-legal basic-string escapes) ----------------------------------------------------------

_CODEX_MCP_KEY_ORDER = ("command", "args", "cwd", "env")   # the launch keys first, then sorted rest
_TOML_BARE_KEY_RE = re.compile(r"\A[A-Za-z0-9_-]+\Z")


def _toml_scalar(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
        raise WiringError(f"refused: the codex-mcp definition value {value!r} has no TOML "
                          f"rendering — fix the directive's value.")
    if isinstance(value, (str, int, float)):
        return json.dumps(value)
    if isinstance(value, list):
        if not all(isinstance(v, (str, int, float, bool)) for v in value):
            raise WiringError("refused: a codex-mcp definition list may hold scalars only.")
        return "[" + ", ".join(_toml_scalar(v) for v in value) + "]"
    raise WiringError(f"refused: a codex-mcp definition value of type {type(value).__name__} "
                      f"cannot be rendered.")


def _toml_key(key) -> str:
    if not isinstance(key, str) or not _TOML_BARE_KEY_RE.match(key):
        raise WiringError(f"refused: {key!r} is not a plain TOML key (letters, digits, "
                          f"underscores and hyphens).")
    return key


def render_codex_mcp_body(name: str, definition: dict) -> list:
    """The deterministic TOML lines for one engine server table — the codex-mcp fence body. The
    launch keys render first in a fixed order, the rest sorted; a dict-of-scalars value renders as
    that key's own sub-table, and a dict-of-dicts one level deeper (the per-tool approval tables),
    so the same definition always renders byte-identically (the is_applied mirror depends on it)."""
    if not isinstance(definition, dict) or not definition:
        raise WiringError("refused: a codex-mcp definition must be a non-empty object.")
    ordered = [k for k in _CODEX_MCP_KEY_ORDER if k in definition]
    ordered += sorted(k for k in definition if k not in _CODEX_MCP_KEY_ORDER)
    lines = [f"[mcp_servers.{_toml_key(name)}]"]
    tables = []
    for key in ordered:
        value = definition[key]
        if isinstance(value, dict):
            base = f"mcp_servers.{_toml_key(name)}.{_toml_key(key)}"
            if all(isinstance(v, dict) for v in value.values()) and value:
                for sub in sorted(value):
                    tables.append(f"[{base}.{_toml_key(sub)}]")
                    for k2 in sorted(value[sub]):
                        tables.append(f"{_toml_key(k2)} = {_toml_scalar(value[sub][k2])}")
            else:
                tables.append(f"[{base}]")
                for k2 in sorted(value):
                    tables.append(f"{_toml_key(k2)} = {_toml_scalar(value[k2])}")
        else:
            lines.append(f"{_toml_key(key)} = {_toml_scalar(value)}")
    return lines + tables


# ---- the five directive-level applier/reverser pairs -----------------------------------------

def hook_apply(directive: dict) -> dict:
    return _json_apply(SETTINGS_PATH, lambda d: settings_add_hook(d, directive),
                       "Wired the hook into .claude/settings.json.",
                       "Nothing to change - the hook is already wired.", create=True)


def hook_reverse(directive: dict) -> dict:
    return _json_apply(SETTINGS_PATH, lambda d: settings_remove_hook(d, directive),
                       "Removed the engine hook from .claude/settings.json; other hooks were left "
                       "untouched.",
                       "Nothing to remove - the engine hook is not present.", create=True)


def permission_apply(directive: dict) -> dict:
    return _json_apply(SETTINGS_PATH, lambda d: settings_add_permission(d, directive),
                       "Added the permission to .claude/settings.json.",
                       "Nothing to change - the permission is already present.", create=True)


def permission_reverse(directive: dict) -> dict:
    # Deliberate no-op — "errs toward leaving it" (module-system 110-113). A bare permission cannot
    # be proven engine-only, so the engine never auto-removes it; the residual is surfaced, not silent.
    return _ok(f"Left the permission {directive.get('value')!r} in place by design: a bare "
               f"permission cannot be proven engine-only, so the engine never removes it (you or "
               f"another module may still need it).", SETTINGS_PATH)


def mcp_apply(directive: dict) -> dict:
    return _json_apply(MCP_PATH, lambda d: mcp_add(d, directive),
                       "Registered the MCP server in .mcp.json.",
                       "Nothing to change - the MCP server is already registered.", create=True)


def mcp_reverse(directive: dict) -> dict:
    return _json_apply(MCP_PATH, lambda d: mcp_remove(d, directive),
                       "Removed the engine MCP server from .mcp.json; any operator approval was "
                       "left untouched.",
                       "Nothing to remove - the engine MCP server is not registered.", create=True)


def ontology_entry_apply(directive: dict) -> dict:
    try:
        schema = validate.load_json(CATALOG_SCHEMA_PATH)
    except (OSError, json.JSONDecodeError) as exc:
        return _fail(f"could not load the catalog schema to validate the new record: {exc}.")
    return _json_apply(CATALOG_PATH, lambda d: catalog_add(d, directive, schema),
                       "Added the surface record to the ontology catalog.",
                       "Nothing to change - the surface record is already present.", create=False)


def ontology_entry_reverse(directive: dict) -> dict:
    return _json_apply(CATALOG_PATH, lambda d: catalog_remove(d, directive),
                       "Removed the engine surface record from the ontology catalog.",
                       "Nothing to remove - the surface record is not present.", create=True)


# Fence keys the FOUNDATION owns in .gitignore — a module `gitignore` wire must never claim one, or its
# apply would collide with the foundation body and its uninstall reverser would rip out the foundation block
# (which the orphan-wire carve-out then hides from coherence). Reserved and refused fail-closed (#409).
_RESERVED_GITIGNORE_KEYS = {FOUNDATION_IGNORES_FENCE}


def gitignore_apply(directive: dict) -> dict:
    if "key" not in directive or "lines" not in directive:
        return _fail("refused: a gitignore directive needs 'key' and 'lines'.", GITIGNORE_PATH)
    if directive["key"] in _RESERVED_GITIGNORE_KEYS:
        return _fail(f"refused: '{directive['key']}' is a reserved foundation fence key — a module may not "
                     "claim it.", GITIGNORE_PATH)
    return _text_fence_apply(GITIGNORE_PATH, directive["key"], directive["lines"], create=True)


def gitignore_reverse(directive: dict) -> dict:
    if "key" not in directive:
        return _fail("refused: a gitignore directive needs 'key'.", GITIGNORE_PATH)
    if directive["key"] in _RESERVED_GITIGNORE_KEYS:
        return _fail(f"refused: '{directive['key']}' is a reserved foundation fence key — a module may not "
                     "claim it.", GITIGNORE_PATH)
    return _text_fence_reverse(GITIGNORE_PATH, directive["key"])


def codex_hook_apply(directive: dict) -> dict:
    out = _json_apply(CODEX_HOOKS_PATH, lambda d: codex_hooks_add(d, directive),
                      f"Wired the hook into .codex/hooks.json. {CODEX_RETRUST_NOTE}",
                      "Nothing to change - the hook is already wired.", create=True)
    return out


def codex_hook_reverse(directive: dict) -> dict:
    return _json_apply(CODEX_HOOKS_PATH, lambda d: codex_hooks_remove(d, directive),
                       f"Removed the engine hook from .codex/hooks.json; other hooks were left "
                       f"untouched. {CODEX_RETRUST_NOTE}",
                       "Nothing to remove - the engine hook is not present.", create=True)


def _codex_config_toml_guard(text: str, when: str):
    """Refuse (fail-open) when .codex/config.toml does not parse as TOML — before a change (a file
    the engine cannot safely read is never blind-edited) and after the pure fence transform (a
    change that would leave the file unparseable is never written)."""
    import tomllib
    if text.strip() == "":
        return
    try:
        tomllib.loads(text)
    except Exception as exc:
        raise WiringError(f"refused: .codex/config.toml is not valid TOML {when} ({exc}). "
                          f"The engine made no change.")


def codex_mcp_apply(directive: dict) -> dict:
    try:
        name = directive["name"]
        _validate_mcp_name(name)
        body = render_codex_mcp_body(name, directive["definition"])
        text = _read_text(CODEX_CONFIG_PATH)
        _codex_config_toml_guard(text, "so the engine will not edit it")
        new = fence_apply(text, name, body)
        _codex_config_toml_guard(new, "after this change, so the change was not written")
    except (WiringError, KeyError, TypeError) as exc:
        return _fail(str(exc) if isinstance(exc, WiringError)
                     else f"refused: malformed codex-mcp directive ({exc}).", CODEX_CONFIG_PATH)
    if new != text:
        _write_text(CODEX_CONFIG_PATH, new)
        return _ok(f"Registered the engine server '{name}' in .codex/config.toml (inside its own "
                   f"engine-managed block; anything else in the file is untouched).", CODEX_CONFIG_PATH)
    return _ok(f"Nothing to change - the engine server '{name}' is already registered.",
               CODEX_CONFIG_PATH)


def codex_mcp_reverse(directive: dict) -> dict:
    try:
        name = directive["name"]
        _validate_mcp_name(name)
        text = _read_text(CODEX_CONFIG_PATH)
        _codex_config_toml_guard(text, "so the engine will not edit it")
        new = fence_reverse(text, name)
        _codex_config_toml_guard(new, "after this change, so the change was not written")
    except (WiringError, KeyError, TypeError) as exc:
        return _fail(str(exc) if isinstance(exc, WiringError)
                     else f"refused: malformed codex-mcp directive ({exc}).", CODEX_CONFIG_PATH)
    if new != text:
        _write_text(CODEX_CONFIG_PATH, new)
        return _ok(f"Removed the engine server '{name}' from .codex/config.toml; your own "
                   f"configuration is untouched.", CODEX_CONFIG_PATH)
    return _ok(f"Nothing to remove - the engine server '{name}' is not registered.",
               CODEX_CONFIG_PATH)


# ---- the closed dispatch (the R5 firewall as code) -------------------------------------------

_APPLIERS = {
    "hook": hook_apply,
    "mcp": mcp_apply,
    "ontology-entry": ontology_entry_apply,
    "permission": permission_apply,
    "gitignore": gitignore_apply,
    "codex-hook": codex_hook_apply,
    "codex-mcp": codex_mcp_apply,
}
_REVERSERS = {
    "hook": hook_reverse,
    "mcp": mcp_reverse,
    "ontology-entry": ontology_entry_reverse,
    "permission": permission_reverse,
    "gitignore": gitignore_reverse,
    "codex-hook": codex_hook_reverse,
    "codex-mcp": codex_mcp_reverse,
}
SEAMS = frozenset(_APPLIERS)  # the closed seam vocabulary (must equal the module.v1 wires.type enum)


def _dispatch(table: dict, directive: dict, verb: str) -> dict:
    if not isinstance(directive, dict):
        return _fail(f"refused to {verb} wiring: a directive must be an object.")
    seam = directive.get("type")
    try:
        fn = table.get(seam)              # guard an unhashable type (a list/dict) — no traceback
    except TypeError:
        fn = None
    if fn is None:
        return _fail(f"refused to {verb} wiring of unknown seam type {seam!r}. The seam vocabulary "
                     f"is closed to {sorted(table)}; a new seam is a reviewed change to the wiring "
                     f"library, never a runtime directive.")
    try:
        return fn(directive)
    except WiringError as exc:
        return _fail(str(exc))
    except (KeyError, TypeError) as exc:
        return _fail(f"refused to {verb} the {seam} directive: malformed directive ({exc}).")


def apply(directive: dict) -> dict:
    """Apply one wiring directive. Returns a finding.v1 (note on success/no-op, hard on refusal)."""
    return _dispatch(_APPLIERS, directive, "apply")


def reverse(directive: dict) -> dict:
    """Reverse one wiring directive. Returns a finding.v1."""
    return _dispatch(_REVERSERS, directive, "reverse")


def apply_all(directives: list) -> list:
    """Apply each directive independently (idempotent ⇒ safe to re-run); collect findings."""
    return [apply(d) for d in directives]


def reverse_all(directives: list) -> list:
    return [reverse(d) for d in directives]


def is_applied(directive: dict) -> bool:
    """Is this directive's engine entry currently applied in its target file — by FULL CONTENT, not
    just by name? Mirrors each applier's insert-iff-absent test exactly (hook: would-be-no-op;
    gitignore: fence presence; permission: value membership; mcp / ontology-entry: definition /
    record EQUALITY), so a same-name-but-DRIFTED entry reads as NOT applied — an apply would rewrite
    it. Exposed as a reusable predicate so the coherence wiring leg (module_coherence) and the module
    manager build the declared→applied direction without re-deriving. (The orphan-wire REVERSE direction
    — nothing engine-identified applied that no manifest declares — is the companion enumerator
    applied_engine_wires() below, over declared_wire_identity for like-with-like comparison.)"""
    seam = directive.get("type") if isinstance(directive, dict) else None
    try:
        if seam == "gitignore":
            return _find_fence(_read_text(GITIGNORE_PATH).split("\n"), directive["key"]) is not None
        if seam in ("hook", "permission"):
            data, err = _read_json_tolerant(SETTINGS_PATH, create=True)
            if err is not None:
                return False
            if seam == "permission":
                return directive["value"] in (data.get("permissions", {}).get("allow") or [])
            _, changed = settings_add_hook(json.loads(json.dumps(data)), directive)
            return not changed  # would-be-no-op ⇒ already present
        if seam == "mcp":
            data, err = _read_json_tolerant(MCP_PATH, create=True)
            # FULL-CONTENT, mirroring mcp_add's `servers.get(name) == definition` (a drifted
            # same-name definition reads as NOT applied, exactly as the applier would rewrite it).
            return err is None and (data.get("mcpServers") or {}).get(directive["name"]) == directive["definition"]
        if seam == "ontology-entry":
            data, err = _read_json_tolerant(CATALOG_PATH, create=True)
            # FULL-CONTENT, mirroring catalog_add's `surfaces.get(name) == record`.
            return err is None and (data.get("surfaces") or {}).get(directive["name"]) == directive["record"]
        if seam == "codex-hook":
            data, err = _read_json_tolerant(CODEX_HOOKS_PATH, create=True)
            if err is not None:
                return False
            _, changed = codex_hooks_add(json.loads(json.dumps(data)), directive)
            return not changed  # would-be-no-op ⇒ already present
        if seam == "codex-mcp":
            # FULL-CONTENT: re-render the fence body and test would-be-no-op, so a drifted
            # engine table reads as NOT applied — an apply would rewrite it.
            body = render_codex_mcp_body(directive["name"], directive["definition"])
            text = _read_text(CODEX_CONFIG_PATH)
            return fence_apply(text, directive["name"], body) == text
    except (WiringError, KeyError, TypeError):
        return False
    return False


# ---- the orphan-wire REVERSE enumerator (applied -> declared) ----------------------
# The companion to is_applied()'s forward test: is_applied asks "is THIS declared directive applied?";
# applied_engine_wires() asks "what engine-identified wiring is applied that may match NO directive?".
# Both compute identity by the SAME rule (declared_wire_identity below) so the reverse-leg comparison
# in module_coherence is like-with-like and the keying is single-homed.

def declared_wire_identity(directive: dict):
    """The (seam_type, identity_key) a DECLARED wires directive resolves to — in the SAME vocabulary
    applied_engine_wires() emits, so the orphan-wire reverse leg compares like with like from ONE
    identity rule. Returns None for the seams the reverse leg excludes — a `permission` (a bare string
    is not engine-identifiable; reversal "errs toward leaving it"), an `ontology-entry` (the engine-owned
    catalog is governed by the ownership + catalog-coverage gates, not this leg) — and for a
    malformed/unknown directive."""
    if not isinstance(directive, dict):
        return None
    seam = directive.get("type")
    if seam == "hook":
        h = directive.get("hook") or {}
        return ("hook", (directive.get("event"), directive.get("matcher"),
                         h.get("type"), h.get("command")))
    if seam == "mcp":
        return ("mcp", directive.get("name"))
    if seam == "gitignore":
        return ("gitignore", directive.get("key"))
    if seam == "codex-hook":
        h = directive.get("hook") or {}
        return ("codex-hook", (directive.get("event"), _codex_matcher(directive),
                               h.get("type"), h.get("command")))
    if seam == "codex-mcp":
        return ("codex-mcp", directive.get("name"))
    return None  # permission / ontology-entry / unknown: outside the reverse-leg seam set


def _applied_fence_ids(path: str | None = None) -> list:
    """The ids of every well-formed engine-managed fence currently in `path` (default: the live
    GITIGNORE_PATH, resolved at call time so test redirection holds; the codex-mcp leg passes
    .codex/config.toml). The id is parsed from
    each begin marker (single-homed off FENCE_BEGIN) and confirmed as a single well-formed begin..end
    pair via _find_fence; a malformed/half fence is skipped (the forward leg / fence_reverse surface it).
    Returns EVERY fence id, including the foundation FOUNDATION_IGNORES_FENCE — its carve-out from the
    orphan-wire reverse leg is applied one level up, in applied_engine_wires (it is a library-helper fence
    no manifest declares, so the reverse leg must not treat it as undeclared module wiring). This enumerator stays a pure "all fences" reader."""
    pre, post = FENCE_BEGIN.split("{id}")
    lines = _read_text(GITIGNORE_PATH if path is None else path).split("\n")
    ids = []
    for ln in lines:
        if ln.startswith(pre) and ln.endswith(post) and len(ln) > len(pre) + len(post):
            fence_id = ln[len(pre):len(ln) - len(post)]
            try:
                if _ID_RE.match(fence_id) and _find_fence(lines, fence_id) is not None:
                    ids.append(fence_id)
            except WiringError:
                continue
    return ids


def applied_engine_wires() -> list:
    """Every ENGINE-IDENTIFIED wiring entry currently APPLIED in the PLATFORM-SHARED files, as
    (seam_type, identity_key, target_label) — the input to the orphan-wire reverse coherence leg
    (validate.orphan_wire_findings). Covers the three platform-shared-file seams, the only place an
    orphan has no OTHER governance:
      - hook: a hook in .claude/settings.json whose command resolves under .engine/ (ENGINE_DIR_MARKER);
        an operator's own non-engine hook is skipped (the engine-namespaced-identity keying firewall).
      - mcp: an engine-prefixed server name in .mcp.json (MCP_NAME_PREFIX).
      - gitignore: each well-formed engine-managed MODULE fence id in .gitignore.
    PERMISSION (not engine-identifiable), ONTOLOGY-ENTRY (engine-owned catalog, covered by the ownership +
    catalog-coverage gates), and the foundation FOUNDATION_IGNORES_FENCE (a library-helper fence no manifest
    declares) are excluded by construction — see declared_wire_identity and
    validate.orphan_wire_findings. Reads the live files with the same tolerant readers is_applied uses;
    an absent/unreadable file yields no entries for that seam (fail-open)."""
    out = []
    data, err = _read_json_tolerant(SETTINGS_PATH, create=True)
    if err is None:
        for event, groups in (data.get("hooks") or {}).items():
            for group in (groups or []):
                if not isinstance(group, dict):
                    continue
                matcher = group.get("matcher")
                for h in (group.get("hooks") or []):
                    command = (h or {}).get("command", "") if isinstance(h, dict) else ""
                    if isinstance(command, str) and ENGINE_DIR_MARKER in command:
                        out.append(("hook", (event, matcher, h.get("type"), command),
                                    _rel(SETTINGS_PATH)))
    data, err = _read_json_tolerant(MCP_PATH, create=True)
    if err is None:
        for name in (data.get("mcpServers") or {}):
            if isinstance(name, str) and name.startswith(MCP_NAME_PREFIX):
                out.append(("mcp", name, _rel(MCP_PATH)))
    for fence_id in _applied_fence_ids():
        if fence_id == FOUNDATION_IGNORES_FENCE:
            continue   # the foundation fence is a library-helper block, not module wiring — never an orphan
        out.append(("gitignore", fence_id, _rel(GITIGNORE_PATH)))
    data, err = _read_json_tolerant(CODEX_HOOKS_PATH, create=True)
    if err is None:
        for event, groups in (data.get("hooks") or {}).items():
            for group in (groups or []):
                if not isinstance(group, dict):
                    continue
                matcher = group.get("matcher")
                for h in (group.get("hooks") or []):
                    command = (h or {}).get("command", "") if isinstance(h, dict) else ""
                    if isinstance(command, str) and ENGINE_DIR_MARKER in command:
                        out.append(("codex-hook", (event, matcher, h.get("type"), command),
                                    _rel(CODEX_HOOKS_PATH)))
    for fence_id in _applied_fence_ids(CODEX_CONFIG_PATH):
        if fence_id.startswith(MCP_NAME_PREFIX):
            out.append(("codex-mcp", fence_id, _rel(CODEX_CONFIG_PATH)))
    return out


# ---- CLI (the operator-runnable gitignore demo, on a scratch file) ---------------------------

def _indent(text: str) -> str:
    return "".join("        " + ln + "\n" for ln in text.split("\n") if ln != "") or "        (empty)\n"


def _demo_gitignore(path: str) -> int:
    sample = "build/\n*.log\n"
    _write_text(path, sample)
    print(f"Starting from a sample file ({_rel(path)}) with two of your own lines:")
    print(_indent(sample), end="")
    print("(i) Applying the engine's gitignore directive...")
    f1 = _text_fence_apply(path, "demo", [".engine/.venv/"], create=True)
    print("    " + validate.fmt(f1))
    print(_indent(_read_text(path)), end="")
    print("(ii) Applying the SAME directive again (should change nothing)...")
    before_reapply = _read_text(path)
    print("    " + validate.fmt(_text_fence_apply(path, "demo", [".engine/.venv/"], create=True)))
    idempotent = _read_text(path) == before_reapply
    print("(iii) Now add your OWN identical-looking line OUTSIDE the engine block...")
    _write_text(path, _read_text(path) + ".engine/.venv/\n")
    print(_indent(_read_text(path)), end="")
    print("    Reversing the engine directive...")
    f3 = _text_fence_reverse(path, "demo")
    print("    " + validate.fmt(f3))
    final = _read_text(path)
    print(_indent(final), end="")
    print(f"Done - your own '.engine/.venv/' line survived; the engine block is gone. "
          f"Delete {path} when finished.")
    # Self-check: the directive applied cleanly (not a hard finding), re-applying changed nothing
    # (idempotent), and after reversal the operator's OWN '.engine/.venv/' line is the only one left —
    # the engine-managed block is gone.
    ok = (f1.get("severity") != "hard" and idempotent and f3.get("severity") != "hard"
          and final.count(".engine/.venv/") == 1)
    if not ok:
        print("\nDEMO UNEXPECTED: the gitignore fence did not apply idempotently and reverse cleanly while "
              "preserving the operator's own line.", file=sys.stderr)
        return 1
    return 0


def main(argv: list) -> int:
    if not argv:
        print("usage: wiring.py {demo-gitignore|gitignore-apply|gitignore-reverse} <file> [lines...]",
              file=sys.stderr)
        return 2
    cmd = argv[0]
    try:
        if cmd == "demo-gitignore":
            return _demo_gitignore(argv[1])
        if cmd == "gitignore-apply":
            path, lines = argv[1], argv[2:]
            finding = _text_fence_apply(path, "demo", lines, create=True)
            print(validate.fmt(finding))
            return 1 if finding["severity"] == "hard" else 0
        if cmd == "gitignore-reverse":
            path = argv[1]
            finding = _text_fence_reverse(path, "demo")
            print(validate.fmt(finding))
            return 1 if finding["severity"] == "hard" else 0
        print(f"unknown command {cmd!r}", file=sys.stderr)
        return 2
    except IndexError:
        print("CONFIG ERROR: missing the <file> argument.", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"CONFIG ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
