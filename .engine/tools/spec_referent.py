#!/usr/bin/env python3
"""Resolve the settled-description referent a build is checked against: work item -> spec doc -> acceptance criteria.

build-orchestration (core) owns the spec-referent resolution — a **path read** of the committed `docs/spec/`
corpus: when a
build realizes a product-design **work item** (an ordinary GitHub Issue that points at its settled description),
the orchestrator resolves **work item -> spec doc -> acceptance criteria** at Plan, gated on a **settled** spec
(`status: locked`). This tool is that resolution's mechanical core, with ONE resolution feeding TWO consumers:

  - `resolve`      -> the referent (the settled doc + its acceptance criteria) the two referent review passes
                      check a build against — `product-intent` (plan-review) and `spec-conformance`
                      (pre-submission). The pass consumes it as context and renders its OWN disclosed no-op when
                      none resolves; this tool never judges built-vs-spec (a persona judges, a check gates).
  - `review-steps` -> the PR Review section's operator-runnable acceptance steps: the steps the operator can run themselves, copied VERBATIM from the
                      settled doc's operator-runnable rows, in two plain groups — "things you can confirm
                      yourself" and "things I checked for you" — or a plain reason-named line when nothing is
                      operator-runnable. The orchestrator renders, never authors: this tool copies rows and
                      sorts them by FORM, never grades or paraphrases a recipe.

Both verbs are projections of the SAME resolution (consumed, not re-resolved): one path read, no parallel one.

SELF-CONTAINED ON PURPOSE. This is a CORE tool, but the spec corpus is authored by the OPTIONAL product-design
module. So it imports NO product-design code — a required tool must not depend on an optional module, or it would
crash on every repo that never installed product-design. It carries its own minimal markdown parser (a knowing
duplicate of the trivial bits of `product_design/spec_form.py`); its GitHub boundary builds requests through the
shared `github_client` (the gh-client consolidation engine-template #295 began) while keeping its own
injectable-transport seam (`milestone_emit` / `standing_situation`).

Fail-closed (the load-bearing safety): the work-item body is the one REMOTE read. A read FAILURE (HTTP >= 400, a
null body, an unreachable host, an unexpected shape) RAISES `SpecReferentError` — it is NEVER read as "no spec ->
disclosed no-op", which would let a build sail past both referent passes with a clean-looking "nothing to check".
The spec being ABSENT (no link, no `docs/spec/`, not yet settled, no criteria) is a disclosed no-op; a read that
FAILS is an error. The two are distinct.

Engine/product wall: the tool OPENS the pointed-at file to read its criteria, so a pointer that escapes
`docs/spec/` is rejected (a disclosed no-op), NEVER opened — the confined-read guard runs on both entry points
(`--issue` and `--doc`) before any open.

Operator-communication law: the engine-internal reason tokens (`doc-not-locked`, `ambiguous-pointer`, ...)
and the criteria typing (`operator`/`engine`) NEVER surface to the operator as raw tokens — `resolve` emits them
for the orchestrator, but every rendered (operator-facing) line is plain language ("nothing settled yet", "things
you can confirm yourself").

Operator demo (faked GitHub + a throwaway spec tree, real logic — no real Issue, no token):
  uv run --directory .engine -- python tools/spec_referent.py demo
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # the sibling tools dir, for github_client
import github_client  # noqa: E402 — the shared authenticated GitHub API client; request-build

# ---- constants -------------------------------------------------------------

USER_AGENT = "engine-spec-referent"

# The committed product spec tree, relative to the repository root.
_SPEC_DIR = os.path.join("docs", "spec")

# <repo>/.engine/tools/spec_referent.py -> <repo>. A pure leaf: computed from __file__, no sibling import. The
# CLI reads the spec tree from here (this engine's own tree). The engine-mechanic reads the PRODUCT's spec by
# running the PRODUCT checkout's OWN spec_referent in place (subprocess-in-place, eADR-0026 / build-orchestration.md
# owned-product arm), whose _ROOT is anchored to the checkout — so there is no need for this copy to be redirected
# at a foreign tree. (The confined-read wall in resolve_doc still self-relativizes to whatever root it is passed.)
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# The acceptance-criteria grammar — a knowing duplicate of product_design/spec_form.py's (a CORE tool must not
# import the OPTIONAL package). A criterion row is `| Criterion | How verified | Who checks it |`; who is who can
# discharge it. "locked" is the settled stage that gates the referent.
_CRITERIA_COLUMNS = ("criterion", "how verified", "who checks it")
_CRITERIA_SECTION = "Acceptance criteria"
_DISCHARGE_VALUES = ("operator", "engine")
_LOCKED = "locked"

# A how-verified cell is a bare terminal command — rule 4: a CLI-only check a non-engineer realistically will not
# run goes on the engine's account, not under "things you can confirm yourself" — ONLY when a CODE SPAN in the
# cell holds one. A FORM read of the row, never a grade of the recipe, and deliberately narrow: plain prose has no
# code span and is NEVER demoted, so a plain-language operator step ("Go to the checkout page and confirm the
# total") stays runnable — erring toward SHOWING the operator a step, never hiding one. A code span is a command
# when its first word is a known command (`uv`, `gh`, `pytest`, the in-tool `demo` — AI-run, so not
# operator-runnable, ...) or it is a `./script`. (Matching the whole cell would mis-read English verbs that are
# also command names — "Go ...", "Make ..." — so the match is confined to code spans.)
_COMMAND_WORDS = frozenset({
    "uv", "uvx", "python", "python3", "py", "gh", "git", "npm", "npx", "pnpm", "yarn", "pip", "make",
    "bash", "sh", "zsh", "node", "cargo", "go", "docker", "curl", "pytest", "demo",
})


class SpecReferentError(Exception):
    """A REMOTE read failed (HTTP >= 400, an unreachable host, a null/unexpected body). NEVER swallowed as
    "no spec": a swallowed auth/scope failure rendered as a disclosed no-op would let a build pass the referent
    passes on a clean-looking "nothing to check". The caller (the orchestrator) surfaces it. Spec ABSENCE is a
    disclosed no-op; a read that FAILS is this error."""


# ---- the no-op vocabulary (internal reason tokens + their plain renders) ---------------

# Every reason a resolution yields nothing to check. The TOKEN is engine-internal (for the orchestrator); the
# PLAIN render is the only thing the operator ever sees. "all-engine-account" is review-steps-only: the doc DID
# resolve, but nothing in it is operator-runnable (reason (i) of the README's bounded set).
_PLAIN_NOOP = {
    "no-spec-installed": "there's no settled description for this project yet",
    "no-issue-pointer": "this change isn't linked to a settled description",
    "pointer-not-under-docs-spec": "this change isn't linked to a settled description",
    "ambiguous-pointer": "this change points at more than one description, so I can't tell which one to check",
    "doc-missing": "the linked description couldn't be found",
    "doc-not-locked": "the linked description isn't settled yet",
    "no-criteria": "the settled description doesn't list anything to check",
    "all-engine-account": "everything for this change is checked on the engine's account",
}


def _noop(reason: str, extra: str = "") -> dict:
    detail = _PLAIN_NOOP[reason]
    if extra:
        detail = f"{detail} ({extra})"
    return {"ok": False, "no_op_reason": reason, "detail": detail}


# ---- parse helpers (ported from product_design/spec_form.py; trivial, stdlib-only) -----------------

def _frontmatter_status(text: str) -> "str | None":
    """The lowercased value of the first `status:` key inside the leading `---` frontmatter block, or None.
    Tolerant of malformed content — it never raises. (Port of spec_form._frontmatter_status.)"""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    for line in lines[1:]:
        if line.strip() == "---":
            break
        m = re.match(r"\s*status\s*:\s*(.+?)\s*$", line)
        if m:
            value = re.sub(r"\s+#.*$", "", m.group(1).strip())  # drop a trailing inline YAML comment
            return value.strip().strip("'\"").lower()
    return None


def _section_body(text: str, heading: str) -> "str | None":
    """The text between a `## <heading>` line and the next `## ` (or EOF), or None if absent. (Port of
    spec_form._section_body.)"""
    lines = text.splitlines()
    want = heading.strip().lower()
    start = None
    for i, line in enumerate(lines):
        m = re.match(r"^##[ \t]+(.+?)[ \t]*#*$", line)
        if m and m.group(1).strip().lower() == want:
            start = i + 1
            break
    if start is None:
        return None
    end = len(lines)
    for j in range(start, len(lines)):
        if re.match(r"^##[ \t]+", lines[j]):
            end = j
            break
    return "\n".join(lines[start:end])


def _is_table_row(line: str) -> bool:
    s = line.strip()
    return s.startswith("|") and s.count("|") >= 2


def _is_separator_row(line: str) -> bool:
    s = line.strip()
    return bool(s) and bool(re.fullmatch(r"\|[\s:\-|]+\|?", s)) and "-" in s


def _cells(line: str) -> list:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _tables(text: str) -> list:
    """Every pipe-table in `text`, as (header_cells_lowercased, [data_row_cells, ...]). (Port of spec_form._tables.)"""
    lines = text.splitlines()
    out, i, n = [], 0, len(lines)
    while i < n:
        if _is_table_row(lines[i]) and i + 1 < n and _is_separator_row(lines[i + 1]):
            header = [c.lower() for c in _cells(lines[i])]
            rows, j = [], i + 2
            while j < n and _is_table_row(lines[j]) and not _is_separator_row(lines[j]):
                rows.append(_cells(lines[j]))
                j += 1
            out.append((header, rows))
            i = j
        else:
            i += 1
    return out


def _table_with_columns(text: str, columns: tuple) -> "list | None":
    """The data rows of the first table whose leading columns are `columns` (case-insensitive); a prefix match,
    so a trailing extra column (e.g. Notes) is allowed. (Port of spec_form._table_with_columns.)"""
    n = len(columns)
    for header, rows in _tables(text):
        if tuple(header[:n]) == tuple(columns):
            return rows
    return None


# ---- the work-item pointer (a parseable markdown link into docs/spec/) -----------------------------

def _md_link_targets(body: str) -> list:
    """Every Markdown-link target `[text](path)` in the body, normalized to a candidate path: a GitHub blob URL
    is reduced to its in-repo path (after `/blob/<ref>/`), and a trailing #fragment/?query is dropped. Only
    targets that name a `.md` document are returned."""
    out = []
    for m in re.finditer(r"\[[^\]]*\]\(([^)]+)\)", body):
        raw = m.group(1).strip()
        blob = re.search(r"/blob/[^/]+/(.+)$", raw)        # a github.com/<o>/<r>/blob/<ref>/<path> URL
        path = blob.group(1) if blob else raw
        path = path.split("#", 1)[0].split("?", 1)[0].strip()
        if path.endswith(".md"):
            out.append(path)
    return out


def _spec_pointers(body: str) -> tuple:
    """(distinct repo-root-relative `docs/spec/*.md` pointers, count of all .md links). Each candidate is
    normalized; only those that land under `docs/spec/` by path are kept (the realpath containment guard is
    enforced AGAIN at open time, so a symlink cannot escape). The total-.md count lets the caller tell
    "a link exists but not under docs/spec" from "no link at all"."""
    md = _md_link_targets(body)
    under = []
    for path in md:
        norm = os.path.normpath(path.lstrip("/"))          # a leading slash means repo-root
        if norm == _SPEC_DIR or norm.startswith(_SPEC_DIR + os.sep):
            under.append(norm)
    return sorted(set(under)), len(md)


# ---- resolving a doc to its referent (the confined read; both entry points pass through here) -------

def _read_doc(real_path: str) -> str:
    # utf-8-sig strips a BOM (Windows editors), exactly as the spec readers do.
    with open(real_path, "r", encoding="utf-8-sig", errors="replace") as fh:
        return fh.read()


def _criteria(text: str) -> list:
    """The acceptance-criteria rows of a doc, as [{criterion, how_verified, who}, ...] — who lowercased as
    written. Every row is kept (the referent the conformance pass reads must see ALL criteria, not only the
    well-typed ones); a degenerate row missing the who column is skipped. Empty when there is no criteria table."""
    body = _section_body(text, _CRITERIA_SECTION) or ""
    rows = _table_with_columns(body, _CRITERIA_COLUMNS)
    if not rows:
        return []
    out = []
    for r in rows:
        if len(r) < 3:
            continue
        out.append({"criterion": r[0].strip(), "how_verified": r[1].strip(), "who": r[2].strip().lower()})
    return out


def resolve_doc(root: str, pointer: str, *, require_locked: bool = True) -> dict:
    """Resolve a single `docs/spec/<doc>.md` pointer (repo-root-relative) to its referent, or a disclosed no-op.
    The CONFINED-READ guard runs first: the real path must stay under `docs/spec/`, or it is a
    `pointer-not-under-docs-spec` no-op and the file is NEVER opened (the engine/product wall). Then: the doc
    must exist (`doc-missing`), be settled (`doc-not-locked` for stub/draft/none), and carry at least one
    acceptance criterion (`no-criteria`).

    `require_locked` defaults True — the BUILD-REFERENT contract: a build is only ever checked against a SETTLED
    spec, so the lock gate is load-bearing and every build-path caller (`resolve` / `resolve_from_body`) inherits
    it. `require_locked=False` is the intake-time acceptance-split path ONLY (#420): at intake a capability doc is
    still a draft, so the count skips the lock gate — but every OTHER guard (the confined-read wall, existence,
    `no-criteria`) still runs, and the returned `status` is the doc's OBSERVED frontmatter (never a hardcoded
    "locked"), so a relaxed resolution can never be mistaken for a settled build referent."""
    spec_real = os.path.realpath(os.path.join(root, _SPEC_DIR))
    real = os.path.realpath(os.path.join(root, pointer))
    if not (real == spec_real or real.startswith(spec_real + os.sep)):
        return _noop("pointer-not-under-docs-spec")        # escapes docs/spec/ — rejected, never opened
    if not os.path.isfile(real):
        return _noop("doc-missing")
    text = _read_doc(real)
    status = _frontmatter_status(text)
    if require_locked and status != _LOCKED:
        return _noop("doc-not-locked")
    criteria = _criteria(text)
    if not criteria:
        return _noop("no-criteria")
    rel = os.path.relpath(real, os.path.realpath(root))
    return {"ok": True, "doc_path": rel, "status": status, "criteria": criteria}


def resolve_from_body(root: str, body: str) -> dict:
    """Resolve a work-item body to its referent or a disclosed no-op: extract the `docs/spec/*.md` pointer
    (none -> `no-issue-pointer`; a .md link that isn't under docs/spec -> `pointer-not-under-docs-spec`; more
    than one distinct spec doc -> `ambiguous-pointer`, never first-match-guessed), then resolve the doc."""
    under, total_md = _spec_pointers(body)
    if not under:
        return _noop("pointer-not-under-docs-spec" if total_md else "no-issue-pointer")
    if len(under) > 1:
        return _noop("ambiguous-pointer", extra="; ".join(under))
    return resolve_doc(root, under[0])


def resolve(root: str, *, issue=None, doc=None, gh=None) -> dict:
    """The referent for a build, or a disclosed no-op. `--doc` resolves a known spec doc directly (local only);
    `--issue` reads the work item's body through `gh` (a REMOTE read that RAISES on failure — fail-closed) and
    follows its pointer. No `docs/spec/` tree at all -> `no-spec-installed` (holds whether or not the optional
    product-design module is installed)."""
    if not os.path.isdir(os.path.join(root, _SPEC_DIR)):
        return _noop("no-spec-installed")
    if doc is not None:
        return resolve_doc(root, doc)
    if issue is None or gh is None:                         # a mis-call — a clear error, never an opaque crash
        raise SpecReferentError("resolve() needs doc=<path>, or both issue=<n> and a GitHub client")
    body = gh.issue_body(issue)                             # RAISES SpecReferentError on a read failure
    return resolve_from_body(root, body)


# ---- the review-steps projection (#282 — operator-runnable acceptance steps, two plain groups) -----

def _is_terminal_command(how_verified: str) -> bool:
    """Whether a how-verified cell is a bare terminal command (-> the engine's account, rule 4). A FORM read: it
    is a command only when a CODE SPAN in the cell holds one — its first word is a known command, or it is a
    `./script`. Plain prose carries no code span and is NEVER demoted, so a plain-language operator step stays
    runnable even when it opens with a word that is also a command name (erring toward showing, never hiding)."""
    def is_command(span: str) -> bool:
        s = span.strip().lstrip("$").strip().lower()
        if s.startswith("./"):
            return True
        words = s.split()
        return bool(words) and words[0] in _COMMAND_WORDS
    return any(is_command(m.group(1)) for m in re.finditer(r"`{1,3}([^`]+)`{1,3}", how_verified))


def review_steps(resolved: dict) -> dict:
    """The operator-runnable projection of a resolved referent: two groups of VERBATIM rows — `runnable`
    ("things you can confirm yourself") and `engine_account` ("things I checked for you") — plus a `no_op_reason`
    when nothing is operator-runnable. A row is runnable iff the operator can discharge it (`who == operator`)
    AND its how-verified is not a bare terminal command (a CLI-only/demo correlate is the engine's account).
    Reasons (ii) "can't run in the operator's environment" and (iv) "trivial fast-path" are the orchestrator's
    judgment, applied at render time, not here."""
    if not resolved.get("ok"):
        return {"runnable": [], "engine_account": [], "no_op_reason": resolved["no_op_reason"]}
    runnable, engine_account = [], []
    for c in resolved["criteria"]:
        if c.get("who") == "operator" and not _is_terminal_command(c.get("how_verified", "")):
            runnable.append(c)
        else:
            engine_account.append(c)                       # who == engine, an unrecognized who, or a command
    no_op = None if runnable else "all-engine-account"
    return {"runnable": runnable, "engine_account": engine_account, "no_op_reason": no_op}


_PROMISE_CAVEAT = ("_These are things you can run yourself to watch the change work if it matters — a step you "
                   "haven't run is a promise, not proof, so it never stands in for a check that already passed._")
_ENGINE_FRAMING = ("_(these ran on the engine's side — listed so you know what was checked; nothing for you to "
                   "do)_")


def render_review_steps(resolved: dict) -> str:
    """The plain-language Review-section block the orchestrator drops in verbatim. Two labelled groups when there
    are runnable steps; a single plain reason-named line when there are none — never a raw reason token, never a
    typing token, never "settled"/"locked" framework vocabulary. Deterministic, so it is testable."""
    proj = review_steps(resolved)

    def rows(items):
        return "\n".join(f"- {c['criterion']}: {c['how_verified']}" for c in items)

    def engine_block(items):
        # the engine's-account group reads verbatim, with one plain line of framing so a non-engineer isn't
        # handed a bare command under a reassuring heading.
        return "**Things I checked for you**\n" + _ENGINE_FRAMING + "\n" + rows(items)

    if not proj["runnable"]:
        plain = _PLAIN_NOOP[proj["no_op_reason"]]
        line = f"Nothing here is something you can run yourself — {plain}."
        if proj["engine_account"]:
            line += "\n\n" + engine_block(proj["engine_account"])
        return line

    out = "**Things you can confirm yourself**\n" + rows(proj["runnable"])
    if proj["engine_account"]:
        out += "\n\n" + engine_block(proj["engine_account"])
    out += "\n\n" + _PROMISE_CAVEAT
    return out


# ---- the acceptance-split projection (#420 — the two-tier count for the intake/settle surface) -----
# The SAME classification as review-steps, projected to a COUNT rather than the verbatim steps: at the PR/merge
# moment the operator reads the grouped steps; at the intake/settle moment they read the count, before they lock.
# Reusing review_steps() (not a second classifier) is the point — a criterion is typed identically at both
# moments, so the split the operator sees at settle cannot drift from the one the merge readout shows for that
# same doc. The readout NEVER collapses into one "all good": both numbers are always stated.

def acceptance_split(resolved: dict) -> dict:
    """Counts of a resolved doc's acceptance criteria by who discharges them, reusing review_steps' classifier:
    `{"operator_verifiable": <n>, "engine_account": <m>}`. A no-op resolution yields zeros on both sides — the
    counts alone cannot tell a genuine zero from a no-op, so a caller that must distinguish them (as
    render_acceptance_split does) consults review_steps()'s `no_op_reason`, never the two ints."""
    proj = review_steps(resolved)
    return {"operator_verifiable": len(proj["runnable"]), "engine_account": len(proj["engine_account"])}


# The no-op reasons REACHABLE from the intake acceptance-split path (resolve_doc, require_locked=False, a local
# --doc read): a capability still being described (`no-criteria` — the common case), a mistyped path
# (`doc-missing`), or a path outside the description folder (`pointer-not-under-docs-spec`). The shared
# _PLAIN_NOOP strings are written for the PR/merge-review context ("this change", "linked", "settled
# description") and read WRONG at intake — where the doc is a DRAFT the operator has not settled, nothing is
# "linked", and nothing is "settled" yet — so the intake surface gets its own plain, draft-framed,
# action-guiding phrasings. Still leak-free: plain words only, no raw lifecycle/reason token.
_INTAKE_NOOP = {
    "no-criteria": ("This description doesn't list any acceptance criteria yet — add a row for each thing that "
                    "must be true (what it is, how it's checked, and whether you or the engine confirms it), "
                    "and I'll show you how they split between what you can confirm yourself and what's on the "
                    "engine's account."),
    "doc-missing": "I couldn't find that description to read its acceptance criteria.",
    "pointer-not-under-docs-spec": ("That isn't a path inside the description folder (docs/spec/), so I can't "
                                    "read it as a description."),
}


def render_acceptance_split(resolved: dict) -> str:
    """The plain-language two-tier count for a SINGLE capability, for the intake/settle surface. The readout
    never collapses into one "all good" — it always states BOTH numbers, even when one side is zero, so structure
    cannot manufacture confidence the verification does not deliver. A resolved no-op renders an intake-framed,
    action-guiding plain line (never the merge-review "settled/linked" vocabulary, and never a raw token).
    It reports the shape of acceptance (criteria as written), never a pass/green tally. Deterministic, so it is
    testable. The no-op is read from the resolution, never from the {0, 0} counts."""
    proj = review_steps(resolved)
    if not resolved.get("ok"):
        return _INTAKE_NOOP.get(proj["no_op_reason"], "There's nothing to count for this description yet.")
    n, m = len(proj["runnable"]), len(proj["engine_account"])
    return (f"Of what this description says must be true, {n} {'is' if n == 1 else 'are'} something you can "
            f"confirm yourself and {m} {'is' if m == 1 else 'are'} on the engine's account.")


# ---- the GitHub boundary (the only network seam; transport is injectable) --------------------------

class GitHubIssues:
    """The Issue-read boundary. Mirrors the engine's injectable-transport seam (`milestone_emit.GitHubMilestones`
    / `standing_situation`): `transport(method, path, body) -> (status, json)` is injectable, so the demo and
    tests fake ONLY the network and run the real resolution. A read failure RAISES `SpecReferentError` (never a
    silent no-op). Its `_http` builds the request through the shared `github_client`, keeping its own status/error handling."""

    def __init__(self, repo: str, token: str, transport=None):
        self.repo = repo
        self.token = token
        self._transport = transport or self._http

    def _http(self, method: str, path: str, body=None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = github_client.request(path, self.token, user_agent=USER_AGENT, method=method, data=data)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode("utf-8")
                return resp.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as exc:               # 4xx/5xx — surface the status, never swallow
            return exc.code, None
        except urllib.error.URLError as exc:                # network unreachable — a read failure
            raise SpecReferentError(f"GitHub is unreachable: {exc}") from exc

    def issue_body(self, number) -> str:
        """The work item's body text. RAISES on any HTTP error or unexpected shape — an auth/scope/network
        failure must never read as "no pointer" (which would silently no-op past the referent passes). A 200 with
        an empty or pointer-less body is a SUCCESSFUL read and returns "" (-> a disclosed no-op downstream); only
        a FAILED read raises."""
        status, data = self._transport("GET", f"/repos/{self.repo}/issues/{number}", None)
        if status >= 400 or data is None:
            raise SpecReferentError(f"GitHub returned {status} reading issue #{number}")
        if not isinstance(data, dict):
            raise SpecReferentError("issue response was not an object")
        return data.get("body") or ""


# ---- operator-runnable demo (real resolution; only the GitHub network + a throwaway spec tree) -----

class _FakeGitHub:
    """A fake network for the demo/tests: a transport that returns a canned issue body, or a chosen HTTP status
    to exercise the fail-closed RAISE. Only the network is faked; the real resolution runs ([[demo-must-exercise-real-logic]])."""

    def __init__(self, body="", *, status=200):
        self.body = body
        self.status = status

    def transport(self, method, path, body):
        if method == "GET" and "/issues/" in path:
            if self.status >= 400:
                return self.status, None
            return 200, {"number": 1, "body": self.body}
        return 404, None


def _client(fake) -> GitHubIssues:
    return GitHubIssues("demo/your-project", "fake-token", transport=fake.transport)


def _demo() -> int:
    import shutil
    import tempfile

    print("How a build is checked against your settled description — the engine follows a work item to its\n"
          "description, reads what 'done' means, and surfaces the steps you can run yourself. No real GitHub\n"
          "call and no real files are touched.\n")

    def _seed(files: dict) -> str:
        d = tempfile.mkdtemp(prefix="engine-spec-referent-demo-")
        for rel, text in files.items():
            path = os.path.join(d, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(text)
        return d

    def _doc(status, rows):
        head = f"---\nstatus: {status}\n---\n\n# A capability\n\n## Summary\nx\n\n## Behavior\ny\n\n## Acceptance criteria\n"
        table = "\n| Criterion | How verified | Who checks it |\n| --- | --- | --- |\n" + "".join(rows)
        return head + table

    op_row = "| The total shows tax | Open the checkout screen and confirm the total includes tax | operator |\n"
    verb_row = "| Receipt arrives | Make a purchase and confirm the receipt email arrives | operator |\n"
    cmd_row = "| The API returns 200 | Run `uv run pytest tests/api.py` | operator |\n"
    eng_row = "| Data is encrypted at rest | The storage layer's encryption test | engine |\n"

    # A repo whose settled doc carries plain operator steps (one opening with a command-word verb, "Make ..."),
    # an operator-but-terminal step (a code-span command), and an engine step.
    rich = _seed({"docs/spec/index.md": "# Spec\n",
                  "docs/spec/checkout.md": _doc("locked", [op_row, verb_row, cmd_row, eng_row])})
    body_ok = "Builds the checkout. See [Checkout](docs/spec/checkout.md) for what done means.\n"

    results = {}

    # (1) Happy path: a settled doc resolves to its criteria, typed operator/engine.
    r1 = resolve(rich, issue=1, gh=_client(_FakeGitHub(body_ok)))
    results["1 happy"] = r1

    # (2) review-steps splits the rows into the two plain groups; the command + engine rows go to the engine's
    #     account, the plain operator row is runnable; verbatim text is preserved; no token leaks.
    render = render_review_steps(r1)
    results["2 render"] = render

    # (3) A draft (not-yet-settled) doc -> disclosed no-op, never a silent green.
    draft = _seed({"docs/spec/index.md": "# Spec\n", "docs/spec/checkout.md": _doc("draft", [op_row])})
    r3 = resolve(draft, issue=1, gh=_client(_FakeGitHub(body_ok)))

    # (4) A work item with no docs/spec link -> no-op.
    r4 = resolve(rich, issue=1, gh=_client(_FakeGitHub("Just some prose, no link.")))

    # (5) A .md link that isn't under docs/spec -> no-op (not a spec pointer).
    r5 = resolve(rich, issue=1, gh=_client(_FakeGitHub("See [readme](README.md).")))

    # (6) A link to a missing doc -> no-op.
    r6 = resolve(rich, issue=1, gh=_client(_FakeGitHub("See [Ghost](docs/spec/ghost.md).")))

    # (7) Two distinct spec docs linked -> ambiguous, never first-match guessed.
    two = _seed({"docs/spec/index.md": "# Spec\n",
                 "docs/spec/checkout.md": _doc("locked", [op_row]),
                 "docs/spec/search.md": _doc("locked", [op_row])})
    r7 = resolve(two, issue=1,
                 gh=_client(_FakeGitHub("[A](docs/spec/checkout.md) and [B](docs/spec/search.md)")))

    # (8) A settled doc with no criteria table -> no-op (never a vacuous pass).
    nocrit = _seed({"docs/spec/index.md": "# Spec\n",
                    "docs/spec/empty.md": "---\nstatus: locked\n---\n\n# E\n\n## Summary\nx\n"})
    r8 = resolve(nocrit, issue=1, gh=_client(_FakeGitHub("[E](docs/spec/empty.md)")))

    # (9) No docs/spec tree at all -> no-op (holds with or without the optional module).
    bare = _seed({"README.md": "hi"})
    r9 = resolve(bare, issue=1, gh=_client(_FakeGitHub(body_ok)))

    # (10) FAIL-CLOSED: a 403 and a 404 on the work-item read RAISE — never read as "no spec".
    raised_403 = raised_404 = False
    try:
        resolve(rich, issue=1, gh=_client(_FakeGitHub(status=403)))
    except SpecReferentError:
        raised_403 = True
    try:
        resolve(rich, issue=1, gh=_client(_FakeGitHub(status=404)))
    except SpecReferentError:
        raised_404 = True

    # (11) TRAVERSAL on the --doc entry: a pointer escaping docs/spec is a no-op, and the target is NEVER read.
    walled = _seed({"docs/spec/index.md": "# Spec\n", "secret.md": "TOP SECRET — must never be read\n"})
    r11 = resolve(walled, doc="docs/spec/../secret.md")

    # (12) review-steps over an all-engine doc -> a plain reason-named no-op line (reason (i)).
    alleng = _seed({"docs/spec/index.md": "# Spec\n", "docs/spec/x.md": _doc("locked", [eng_row])})
    r12 = resolve(alleng, doc="docs/spec/x.md")
    render_eng = render_review_steps(r12)

    # (13) acceptance-split (#420): the intake two-tier COUNT, over a DRAFT doc (pre-lock), reusing the SAME
    #      classifier. Same four rows as (1): two plain operator rows are verify-yourself; the operator-but-
    #      terminal row and the engine row are on the engine's account (rule 4 in the count, matching merge).
    draft_split = _seed({"docs/spec/index.md": "# Spec\n",
                         "docs/spec/checkout.md": _doc("draft", [op_row, verb_row, cmd_row, eng_row])})
    split_resolved = resolve_doc(draft_split, "docs/spec/checkout.md", require_locked=False)
    split = acceptance_split(split_resolved)
    split_render = render_acceptance_split(split_resolved)

    # (14) acceptance-split over a described-but-criteria-less DRAFT -> the intake no-op is draft-framed and
    #      action-guiding (never the merge-review "settled description" wording).
    draft_nocrit = _seed({"docs/spec/index.md": "# Spec\n",
                          "docs/spec/wip.md": "---\nstatus: draft\n---\n\n# WIP\n\n## Summary\nx\n"})
    nocrit_render = render_acceptance_split(resolve_doc(draft_nocrit, "docs/spec/wip.md", require_locked=False))

    print("A settled checkout description resolved with these criteria (typed by who can check them):")
    for c in (r1.get("criteria") or []):
        print(f"   - [{c['who']}] {c['criterion']}")
    print("\nThe operator-facing Review block the engine would render:\n")
    for ln in render.splitlines():
        print("   " + ln)
    print()

    # Self-check: each invariant must hold, or the demo fails (a falsification that can fail).
    proj = review_steps(r1)
    runnable_text = " ".join(c["how_verified"] for c in proj["runnable"])
    engine_text = " ".join(c["how_verified"] for c in proj["engine_account"])
    # Operator-communication law: the rendered (operator-facing) blocks must leak no engine/framework vocabulary or raw reason token.
    # "engine"/"operator" as plain words are allowed — the design's own operator phrase is "on the engine's
    # account"; what is banned is the lifecycle/framework tokens, the raw reason-class tokens, and the typing
    # values used AS labels (e.g. "[operator]").
    leak_tokens = ("locked", "stub", "draft", "referent", "lens", "spec-conformance",
                   "all-engine-account", "no-issue-pointer", "doc-not-locked", "[operator]", "[engine]",
                   "who checks it")
    leaks = [t for t in leak_tokens if t.lower() in render.lower() or t.lower() in render_eng.lower()
             or t.lower() in split_render.lower()                    # #420: the intake split render...
             or t.lower() in nocrit_render.lower()]                  # ...and its no-op are leak-scanned too

    checks = {
        "a settled doc resolves to its criteria": r1.get("ok") and len(r1["criteria"]) == 4,
        "the plain operator step is runnable": any("checkout screen" in c["how_verified"] for c in proj["runnable"]),
        "a plain operator step opening with a command-word verb ('Make ...') stays runnable":
            any("Make a purchase" in c["how_verified"] for c in proj["runnable"]),
        "an operator-but-terminal step is on the engine's account": "uv run pytest" in engine_text,
        "an engine-typed step is on the engine's account": "encryption test" in engine_text,
        "the operator step's text is verbatim in the render": "Open the checkout screen and confirm the total includes tax" in render,
        "a not-yet-settled doc is a disclosed no-op": (not r3["ok"]) and r3["no_op_reason"] == "doc-not-locked",
        "no link is a disclosed no-op": r4["no_op_reason"] == "no-issue-pointer",
        "a non-spec .md link is a disclosed no-op": r5["no_op_reason"] == "pointer-not-under-docs-spec",
        "a missing doc is a disclosed no-op": r6["no_op_reason"] == "doc-missing",
        "two specs is ambiguous, not guessed": r7["no_op_reason"] == "ambiguous-pointer",
        "a settled doc with no criteria is a no-op (no vacuous pass)": r8["no_op_reason"] == "no-criteria",
        "no docs/spec tree is a disclosed no-op": r9["no_op_reason"] == "no-spec-installed",
        "a 403 read RAISES (fail-closed, never a silent no-op)": raised_403,
        "a 404 read RAISES (fail-closed, never a silent no-op)": raised_404,
        "a traversal pointer is a no-op and never read": r11["no_op_reason"] == "pointer-not-under-docs-spec",
        "an all-engine doc renders a plain reason-named no-op": "run yourself" in render_eng.lower(),
        "no framework/typing token leaks into the operator render": not leaks,
        # (#420) the pre-lock intake count reuses the classifier: 2 verify-yourself, 2 on the engine's account
        # (the terminal-command operator row demotes, matching the merge readout), and a draft doc counts.
        "a draft doc resolves for the count (require_locked=False), keeping its observed status":
            split_resolved.get("ok") and split_resolved.get("status") == "draft",
        "the intake count reuses the classifier (rule-4 demotion in the count)":
            split == {"operator_verifiable": 2, "engine_account": 2},
        "the split render states BOTH numbers and never collapses to 'all good'":
            "confirm yourself" in split_render and "engine's account" in split_render
            and "all good" not in split_render.lower() and "all green" not in split_render.lower(),
        # (#420) the intake no-op guides the operator to add criteria and NEVER calls their draft "settled".
        "the intake no-op is draft-framed and action-guiding (not the merge-review 'settled' wording)":
            "acceptance criteria" in nocrit_render.lower() and "settled" not in nocrit_render.lower(),
    }
    bad = [name for name, ok in checks.items() if not ok]
    for d in (rich, draft, two, nocrit, bare, walled, alleng, draft_split, draft_nocrit):
        shutil.rmtree(d, ignore_errors=True)

    if bad:
        print("DEMO UNEXPECTED — these invariants did not hold:", file=sys.stderr)
        for name in bad:
            print(f"  - {name}", file=sys.stderr)
        if leaks:
            print(f"  (leaked tokens: {leaks})", file=sys.stderr)
        return 1
    print("All path-read invariants held: a settled description resolves to its criteria; a read failure is loud,\n"
          "spec absence is a disclosed no-op; an escaping pointer is never read; the operator's runnable steps are\n"
          "copied verbatim into two plain groups with no framework vocabulary.")
    return 0


# ---- CLI -------------------------------------------------------------------

def _parse_target(argv: list) -> tuple:
    """(issue_number_or_None, doc_path_or_None) from `--issue N` / `--doc PATH`."""
    issue = doc = None
    i = 0
    while i < len(argv):
        if argv[i] == "--issue" and i + 1 < len(argv):
            issue = argv[i + 1]
            i += 2
        elif argv[i] == "--doc" and i + 1 < len(argv):
            doc = argv[i + 1]
            i += 2
        else:
            i += 1
    return issue, doc


def _gh_from_env() -> "GitHubIssues | None":
    repo = os.environ.get("GITHUB_REPOSITORY")
    token = os.environ.get("GITHUB_TOKEN")
    if not repo or not token:
        print("usage: spec_referent.py <resolve|review-steps> --issue N   (needs GITHUB_REPOSITORY and "
              "GITHUB_TOKEN in the environment, as in CI)", file=sys.stderr)
        return None
    return GitHubIssues(repo, token)


def _resolved_for_cli(argv: list) -> "dict | int":
    """Resolve the referent for a CLI verb from `--issue N` (remote) or `--doc PATH` (local), or an exit code on
    a usage/read error."""
    issue, doc = _parse_target(argv)
    if doc is not None:
        return resolve(_ROOT, doc=doc)
    if issue is not None:
        gh = _gh_from_env()
        if gh is None:
            return 2
        try:
            return resolve(_ROOT, issue=issue, gh=gh)
        except SpecReferentError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
    print("usage: spec_referent.py <resolve|review-steps> (--issue N | --doc docs/spec/<doc>.md)", file=sys.stderr)
    return 2


def main(argv: "list | None" = None) -> int:
    argv = list(argv if argv is not None else sys.argv[1:])
    if argv and argv[0] == "demo":
        return _demo()
    if argv and argv[0] == "resolve":
        out = _resolved_for_cli(argv[1:])
        if isinstance(out, int):
            return out
        print(json.dumps(out))
        return 0
    if argv and argv[0] == "review-steps":
        out = _resolved_for_cli(argv[1:])
        if isinstance(out, int):
            return out
        print(render_review_steps(out))
        return 0
    if argv and argv[0] == "acceptance-split":
        # Intake-time two-tier count (#420): a LOCAL, OFFLINE `--doc`-only read — at intake there is no settled
        # work item to follow, so `--issue` (the remote path) is deliberately not offered here. It routes
        # STRAIGHT through resolve_doc (so the confined-read wall guard still runs) with require_locked=False,
        # because at step 5 the capability doc is still a draft.
        _, doc = _parse_target(argv[1:])
        if doc is None:
            print("usage: spec_referent.py acceptance-split --doc docs/spec/<doc>.md   (a local, offline read; "
                  "--issue is not supported for this verb)", file=sys.stderr)
            return 2
        print(render_acceptance_split(resolve_doc(_ROOT, doc, require_locked=False)))
        return 0
    print("usage: spec_referent.py [resolve|review-steps|acceptance-split|demo] "
          "[--issue N | --doc docs/spec/<doc>.md]", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
