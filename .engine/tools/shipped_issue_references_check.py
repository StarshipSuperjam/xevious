#!/usr/bin/env python3
"""Shipped bare-issue-reference floor (engine-template StarshipSuperjam/engine-template#640) — the
custom/script entry for engine/check/shipped-issue-references.

A bare issue reference (a plain `#` followed by a number) written in a file that ships into a generated
repository resolves there to *that* repository's own issue of the same number — a real page about something
else. It is worse than a dangling link: it leads a reader somewhere confidently wrong. This check refuses a
bare reference in the prose of any file that ships, so the class stays shut once swept. The fix an author
applies is to name the repository: write `StarshipSuperjam/engine-template#N` instead of a bare `#N`.
(This file's own prose therefore illustrates the bad shape with a capital-letter placeholder, `#N`, never a
real number, so it does not trip its own rule.)

WHAT IT SCANS — the shipped surface only (StarshipSuperjam/engine-template#640):
  - `.engine/**` MINUS the first-run retire set (read from `.engine/provisioning/first-run-assets.json` as
    plain data, never by importing the retiring instantiator — the reference-closure lesson) MINUS the
    excluded paths below. The `.engine/` ownership invariant makes every committed file there owned-and-
    shipping or a named carve-out, so tree-minus-retire is the shipped `.engine/` surface.
  - PLUS the members of `module_coherence.FOUNDATION_INFRA` that live OUTSIDE `.engine/` (the root
    CLAUDE.md / AGENTS.md / .gitignore and the engine-owned `.github/` control-plane files), glob-expanded.
  The seeded root files (README.md, SECURITY.md) are product territory that never ships, so they are never
  scanned — the divergence from a whole-tree walk is deliberate.
  - EXCLUDES `test_*.py` and `demo_*.py` (which DO ship). Their prose is dominated by SYNTHETIC scenario
    numbers used as fixture data (a comment like "opens #N", or "PR #N comes first but #M merged")
    that a mechanical check cannot tell from a genuine reference — flagging them would over-fire,
    which trains an operator to click past findings, so the gate stays out of the test/demo corpus by design.
    A genuine reference sitting only in a test/demo comment is therefore an unpoliced residual (usually
    duplicated in the production file the test covers); this is a disclosed narrowing, decided with the
    operator (StarshipSuperjam/engine-template#640).

WHAT COUNTS AS PROSE (the failure surface, never a string literal):
  - `.py`: a bare reference inside COMMENT tokens (via `tokenize`) and DOCSTRINGS (via `ast` — a
    module/class/function first-statement string, told apart from an ordinary literal by AST position).
    Every other string literal is excluded BY CONSTRUCTION: they hold test-fixture data, reference-grammar
    under test (close_linkage_preflight / release_cut / engine_todo), and behavior-bearing messages, where a
    rewrite would change machine semantics or break an assertion. This is a disclosed narrowing — a genuine
    reference inside a production message string is not policed here; the one-off sweep enumerates that
    residual separately.
  - `.md` / `.yml` / `.yaml` / `.sh` / `.toml`: whole text, minus the carve-outs.
  - `.json`: only lines carrying a PROSE key (`description` / `message` / `why` / `reason` / `note` /
    `detail` / `comment` / `$comment` / `title`), so a machine-read value is never rewritten. (A prose value
    spanning multiple lines is matched only on the key's own line — a disclosed limitation; the engine's
    JSON prose values are single-line today.)

THE MATCHER — two passes, so a chain tail is never lost (a naive `#`-number regex with a `/`-lookbehind
drops the second reference in a chain like `#N/#M`):
  1. MASK every genuinely-qualified `owner/repo#N` span (a slug, a `/`, a repo, `#N`);
  2. in the remainder MASK the closed carve-out set (ordinals; PR-linkage grammar incl. comma lists);
  3. in what is left, match a bare `#N` (or the slice form `#Na`) AND the partly-qualified
     `engine-template#N` / `engine-template #N` (owner missing). All three rewrite to the one canonical
     `StarshipSuperjam/engine-template#N`, so the sweep that consumes this check's findings leaves nothing
     hand-swept outside the gate.

CARVE-OUTS (closed set — widening one turns a match into flagging the FIX as well as the defect, which trains
an operator to click past findings; the same rationale local_references.py records for its marker set):
  - Ordinals (a bare `#N` naming a numbered THING, never an issue): `concern #N`, `required check #N`, the
    ordinal-adjective "the #1 trust dependency", and a closed set of ordinal nouns (`step #N`, `option #N`,
    `item #N`, `tier #N`, …). A real reference never wears these nouns. A bare `check #N` (not "required
    check") IS a reference and stays flagged; the `the #N <word>` carve-out stays SINGLE-digit because a
    multi-digit "the #N footgun" is a real reference. These are BRIDGED across a line boundary so a wrapped
    ordinal is recognised.
  - PR-linkage grammar: `(Closes|Fixes|Resolves|...|Part of) #N[, #M...]` — in a shipped file this is always
    grammar documentation or an instruction about the ADOPTER'S OWN issues; qualifying it would tell an
    adopter to close the engine's issues. Grammar-scoped (not line-scoped) so the comma-trap `Closes #1, #2`
    is carved whole while a stray reference elsewhere on the line is still caught — and NOT bridged, so a
    genuine reference opening a line after a sentence ending in "resolve" is never swallowed.
  - Excluded paths: `.github/pull_request_template.md` (wholly adopter-facing), `.engine/_fixtures/**`,
    `.engine/tools/memory/semantic/**` (model vocabulary — a rewrite corrupts it), the derived/sealed
    `.engine/knowledge/graph.json` / `.engine/audits/audit-digest.md` / `.engine/state/**`.

DISCLOSED LIMITATIONS:
  - A `#N` glued directly to a preceding letter/digit with no separator (`PR#123`) is not matched — the bare
    pass requires a non-word char before `#`. No such shape exists in the shipped tree today, and the one
    glued cross-repo form present (`claude-code#20397`) is a genuine other-repo reference that correctly
    stays bare. A future glued own-repo reference would ship unqualified; the reconciliation test shares this
    lookbehind, so it would not catch it either.
  - A file that cannot be read (OSError/decode error) is skipped without a finding. An unreadable committed
    file is an anomaly other checks surface; this check's fail-closed guarantee is scoped to the retire
    census, whose absence it DOES turn into a hard finding.

HOME-SCOPED (StarshipSuperjam/engine-template#640): it acts only in the engine's own home repository (git
origin == recorded `home_repository`, the shared `repo_identity.is_home_repo` seam over the REAL root, not
overridable). In a deployed repo a bare reference in a product file is CORRECT, so it no-ops there — the same
ships-and-no-ops posture as census_completeness / memory_pointer_public_safety, disclosed by
construction-scoped.json. Within home it FAILS CLOSED: an unreadable retire census yields a hard finding, not
a silent pass.

Runs as a hard CI custom/script check: finding.v1 JSON on stdout, return 0 on a successful evaluation (empty
array = no bare reference ships). A crash returns non-zero, which the kind turns into a hard fail-closed
finding.
"""
from __future__ import annotations
import ast
import io
import json
import os
import re
import sys
import tokenize

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate  # noqa: E402  (finding.v1, ROOT, env_override_path)
import repo_identity  # noqa: E402  (is_home_repo — the shared origin==home seam this gates on)
import module_coherence  # noqa: E402  (FOUNDATION_INFRA — the authoritative foundation set)

_ENGINE_REL = ".engine"
_RETIRE_MANIFEST_REL = os.path.join(".engine", "provisioning", "first-run-assets.json")
_PRUNE_DIRS = {"__pycache__", ".venv", ".pytest_cache", ".cache", ".uv", ".git"}
_SCAN_EXTS = {".py", ".md", ".yml", ".yaml", ".sh", ".toml", ".json"}

# Paths never scanned (repo-relative). Prefixes end in "/"; the rest are exact files.
_EXCLUDED_PREFIXES = (
    ".engine/_fixtures/",              # test data — bare refs here are deliberate fixtures
    ".engine/tools/memory/semantic/",  # WordPiece vocab + model assets — a rewrite corrupts them
    ".engine/state/",                 # regenerated per-deployment runtime state
)
_EXCLUDED_EXACT = frozenset({
    ".github/pull_request_template.md",  # wholly adopter-facing — its refs are the adopter's own issues
    ".engine/knowledge/graph.json",     # derived from source docstrings — regenerate, never hand-edit
    ".engine/audits/audit-digest.md",   # sealed audit output — an edit breaks the seal
})
# JSON keys whose string value is human prose (the only place a bare ref is scanned in a .json file).
# `justification` is the audit concern-list's prose field; the reconciliation test is the drift canary that
# forces a new prose key to be added here rather than silently under-scanned.
_JSON_PROSE_KEYS = ("description", "message", "why", "reason", "note", "detail",
                    "comment", "$comment", "title", "justification")

# --- the matcher -----------------------------------------------------------------------------------------
# (1) a genuinely-qualified cross-repo reference: <slug>/<repo>#N — masked first so its number tail is never
#     mistaken for a bare ref, and so the sweep's own StarshipSuperjam/engine-template#N output is not
#     re-flagged. The slug/repo classes require a letter-led owner, which a bare `#N/#M` chain never satisfies.
_QUALIFIED = re.compile(r"[A-Za-z][\w.-]*/[\w.-]+#\d+")
# (2a) ORDINAL carve-outs — a bare #N naming a numbered THING (a concern, a step, a tier), never an issue
#      reference: a real reference never wears one of these nouns as a small ordinal. Closed set,
#      documented; extend it when a new ordinal noun over-fires. These are BRIDGED across a line boundary
#      (a keyword ending line N, its #N opening line N+1) so a wrapped ordinal is still recognised.
#      the `the #N <word>` carve-out stays SINGLE-digit on purpose: a multi-digit "the #N footgun" is a real
#      reference, so a general multi-digit "the #N <word>" cannot be carved.
_ORDINAL_CARVEOUTS = (
    re.compile(r"(?i)concern #\d+"),
    re.compile(r"(?i)required check #\d+"),     # the two CI required checks — a bare `check #N` stays flagged
    re.compile(r"(?i)the #[1-9] [A-Za-z]"),    # "the #2 priority" — SINGLE-digit only: a multi-digit
    #                                            "the #N footgun" is a real reference and must stay flagged
    re.compile(r"(?i)\b(?:option|step|item|part|phase|round|tier|level|point|rung|slot|lane|bucket|"
               r"scenario|figure|section|row|column|chapter|priority) #[1-9]\b"),  # SINGLE-digit: an ordinal
    #                                            is small; a multi-digit "part #NNN" is a real reference
)
# (2b) PR-linkage grammar — a `Closes/Fixes/Resolves/Part of #N[, #M]` clause (incl. the comma-trap). NOT
#      bridged: a genuine reference opening a line after an unrelated sentence that merely ends in "resolve"
#      must not be swallowed, which would MISS a real reference — the worse failure for this gate.
_LINKAGE_CARVEOUT = re.compile(
    r"(?i)\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?|part of)\s+#\d+(?:\s*,\s*#\d+)*")
# (3) what remains that is a defect: a bare `#N` (or slice `#Na`), or the partly-qualified engine-template
#     forms (owner missing). The bare pass must NOT exclude a `/`-preceded `#` — a true qualified `owner/repo#N`
#     is already masked away in pass 1, so a surviving `/#M` is a CHAIN TAIL (from `#N/#M`), a real bare ref.
#     `#` stays excluded so a `##2`-style vocab token is never matched.
_BARE = re.compile(r"(?<![\w#])#\d+[a-z]?\b")
_PARTIAL = re.compile(r"(?<![\w/#])engine-template ?#\d+")


_LEADER = re.compile(r"^(\s*)#[ \t]+")  # a comment leader (`# `) — NOT a bare ref, which has no space after #


def _prose(text: str) -> str:
    """Strip a single comment leader so a wrapped carve-out (a keyword ending line N, its `#N` opening line
    N+1 behind that line's own `#` leader) can be bridged. Requires a space after the `#`, so a reference
    written as the whole comment keeps its `#`."""
    return _LEADER.sub(r"\1", text)


def _blank(m):
    return " " * len(m.group())


def _find_refs(text: str, prefix: str = "") -> list:
    """Every offending reference in `text`, as matched strings. `prefix` is the immediately-preceding prose
    line's text, joined ahead of `text` ONLY so a carve-out that wraps across the line boundary (a keyword on
    the previous line, its `#N` opening this one) still masks — matches are then read from the `text` portion
    only. Masks qualified spans, then carve-outs, then finds bare + partly-qualified refs in what survives."""
    combined = (prefix + " " + text) if prefix else text
    off = len(prefix) + 1 if prefix else 0
    masked = _QUALIFIED.sub(_blank, combined)
    for pat in _ORDINAL_CARVEOUTS:          # ordinals bridge across the line boundary
        masked = pat.sub(_blank, masked)
    cur = masked[off:]
    cur = _LINKAGE_CARVEOUT.sub(_blank, cur)  # linkage grammar: current line only, never bridged
    hits = [m.group() for m in _PARTIAL.finditer(cur)]
    # remove the partly-qualified spans before the bare pass so their number tail is not double-counted
    cur = _PARTIAL.sub(_blank, cur)
    hits += [m.group() for m in _BARE.finditer(cur)]
    return hits


# --- prose extraction ------------------------------------------------------------------------------------
def _py_prose_fragments(source: str) -> list:
    """(lineno, text) fragments that are PROSE in a Python file: every comment token, plus every line of a
    module/class/function docstring. Ordinary string literals are excluded by construction."""
    frags = []
    try:
        toks = tokenize.generate_tokens(io.StringIO(source).readline)
        for tok in toks:
            if tok.type == tokenize.COMMENT:
                frags.append((tok.start[0], tok.string))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        pass  # a comment-scan failure falls through to the docstring leg; check() fails closed on parse below
    lines = source.splitlines()
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return frags
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        doc = ast.get_docstring(node, clean=False)
        if doc is None:
            continue
        expr = node.body[0]  # get_docstring guarantees body[0] is the docstring Expr
        start = expr.lineno
        end = getattr(expr, "end_lineno", start)
        for ln in range(start, end + 1):
            if 1 <= ln <= len(lines):
                frags.append((ln, lines[ln - 1]))
    return frags


def _text_fragments(text: str, is_json: bool) -> list:
    """(lineno, text) fragments for a non-Python file: every line, except a .json file yields only lines
    carrying a prose key (so a machine-read value is never scanned)."""
    frags = []
    for i, line in enumerate(text.splitlines(), start=1):
        if is_json and not any(f'"{k}"' in line for k in _JSON_PROSE_KEYS):
            continue
        frags.append((i, line))
    return frags


# --- the shipped surface ---------------------------------------------------------------------------------
def _retire_set(root: str):
    """(files, dir_prefixes) retired at first run, from the committed manifest as plain data. Returns None on
    any fault (missing/unreadable/malformed) so check() can fail closed — a completeness guard that degrades
    to a pass would green-light the drift it exists to catch."""
    try:
        with open(os.path.join(root, _RETIRE_MANIFEST_REL), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("files"), list):
        return None
    files = {f for f in data["files"] if isinstance(f, str)}
    dirs = tuple(d.rstrip("/") + "/" for d in data.get("dirs", []) if isinstance(d, str))
    return files, dirs


def _foundation_outside_engine(root: str) -> list:
    """The FOUNDATION_INFRA members that live outside `.engine/`, glob-expanded against the tree — the
    root CLAUDE.md/AGENTS.md/.gitignore and the engine-owned `.github/` control-plane files that ship."""
    import glob
    out = []
    for member in module_coherence.FOUNDATION_INFRA:
        if member.startswith(".engine/"):
            continue
        if "*" in member:
            for abspath in glob.glob(os.path.join(root, *member.split("/"))):
                out.append(os.path.relpath(abspath, root).replace(os.sep, "/"))
        else:
            if os.path.isfile(os.path.join(root, *member.split("/"))):
                out.append(member)
    return out


def _excluded(rel: str) -> bool:
    return rel in _EXCLUDED_EXACT or any(rel.startswith(p) for p in _EXCLUDED_PREFIXES)


def _scan_targets(root: str, retire_files: set, retire_dirs: tuple) -> list:
    """Every shipped, scannable, repo-relative file: `.engine/**` minus retire minus excluded, plus the
    foundation files outside `.engine/`."""
    targets = []
    engine_dir = os.path.join(root, _ENGINE_REL)
    for cur, dirs, names in os.walk(engine_dir):
        dirs[:] = [d for d in dirs if d not in _PRUNE_DIRS]
        for name in names:
            rel = os.path.relpath(os.path.join(cur, name), root).replace(os.sep, "/")
            if os.path.splitext(name)[1] not in _SCAN_EXTS:
                continue
            if name.startswith(("test_", "demo_")):
                continue  # test/demo prose is dominated by synthetic scenario numbers — see docstring
            if rel in retire_files or (retire_dirs and rel.startswith(retire_dirs)):
                continue
            if _excluded(rel):
                continue
            targets.append(rel)
    for rel in _foundation_outside_engine(root):
        if os.path.splitext(rel)[1] in _SCAN_EXTS and not _excluded(rel):
            targets.append(rel)
    return sorted(set(targets))


def _in_home_repo() -> bool:
    """True iff this checkout is the engine's OWN home repo (origin == recorded home_repository). Reads the
    REAL root via the shared seam, NOT overridable — a backdoor would let the check fire in a deployed repo
    where bare refs are correct. Named so tests can drive the gate."""
    return repo_identity.is_home_repo(validate.ROOT)


def _message(rel: str, lineno: int, hits: list) -> str:
    shown = ", ".join(sorted(set(hits)))
    return (
        f"`{rel}` line {lineno} carries a bare `#N` reference ({shown}) in a file that ships into every "
        f"generated repository, where a bare `#N` resolves to THAT repository's own issue of the same number "
        f"— a real page about something else. Two fixes, depending on what it is: (1) if it IS a reference to "
        f"an engine issue, name the repository — write `StarshipSuperjam/engine-template#N`; (2) if it is NOT "
        f"a reference — an ordinal (\"the 2nd step\"), a count, or a version — reword it so it is not a bare "
        f"`#N`, because a shipped `#N` reads as an engine-issue reference. Ordinals like \"concern #1\", "
        f"\"step #3\", \"the #1 trust\" and PR-linkage lines like \"Closes #1\" are already exempt and not "
        f"flagged. If the file does not actually ship into a generated repository, it belongs in the "
        f"first-run retirement set (`.engine/provisioning/first-run-assets.json`).")


def _retire_fault_message() -> str:
    return (
        f"The engine can't read the list of files removed when a project is first set up "
        f"(`{_RETIRE_MANIFEST_REL}`). Without it this check can't tell which files ship, so it can't confirm "
        f"that no bare issue reference travels into a generated repository, and it can't pass. Restore that "
        f"file from the project's history — it is permanent data — then re-run this check.")


def check(root: str | None = None) -> list:
    """Every offending reference as a list of `hard` findings (empty = no bare reference ships). No-ops
    (empty) OUTSIDE the home repo. WITHIN the home repo it fails CLOSED on an unreadable retire census."""
    if not _in_home_repo():
        return []
    root = root or validate.ROOT
    retire = _retire_set(root)
    if retire is None:
        return [validate.finding("hard", _retire_fault_message(),
                                 {"file": _RETIRE_MANIFEST_REL, "line": None})]
    retire_files, retire_dirs = retire
    findings = []
    for rel in _scan_targets(root, retire_files, retire_dirs):
        try:
            with open(os.path.join(root, rel), encoding="utf-8") as fh:
                text = fh.read()
        except (OSError, UnicodeDecodeError):
            continue
        if rel.endswith(".py"):
            try:
                ast.parse(text)
            except SyntaxError:
                continue  # an unparseable .py is caught by other checks; don't crash this one
            frags = _py_prose_fragments(text)
        else:
            frags = _text_fragments(text, is_json=rel.endswith(".json"))
        # lineno -> leader-stripped prose (merge any two fragments sharing a line), so a carve-out can be
        # bridged from the immediately-preceding prose line (prefix), never a distant one.
        prose_by_line: dict = {}
        for lineno, fragment in frags:
            prose_by_line[lineno] = (prose_by_line.get(lineno, "") + " " + _prose(fragment)).strip()
        for lineno in sorted(prose_by_line):
            hits = _find_refs(prose_by_line[lineno], prose_by_line.get(lineno - 1, ""))
            if hits:
                findings.append(validate.finding("hard", _message(rel, lineno, hits),
                                                 {"file": rel, "line": lineno}))
    return findings


def main() -> int:
    # ENGINE_REF_SCAN_ROOT (unset in production) lets the negative-fixture meta-check point the scan at a
    # seeded mini-tree carrying a bare reference in a shipped file, so the gate is witnessed biting a real
    # bad input. The home-scope gate still reads the REAL root, so the fixture bites only in the home repo's
    # CI (never in a deployed one), which is why construction-scoped.json accompanies the fixture.
    print(json.dumps(check(validate.env_override_path("ENGINE_REF_SCAN_ROOT"))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
