#!/usr/bin/env python3
"""Standing product-spec-conformance sweep — the mechanical half of the audit's conditional conformance leg
(design of record: issue #449; the criterion record it reads is the
product-design spec-obligation matrix, built in PR-1).

WHAT IT IS. The audit persona runs a *judgment* each cron: given a product that has SETTLED a `docs/spec/`,
does the built code still meet each frozen acceptance criterion? The persona is read-only (it cannot run git
or write issues), so this core tool does the mechanical work around that judgment, in two modes the audit-prep
workflow invokes:

  feed     — before the persona runs: read the committed spec-obligation matrix (BY PRESENCE, never importing
             the optional product-design module) and the `docs/spec/` lock status, decide the conditional
             state, and print the persona's hunt-set + the honest coverage disclosure. SILENT when nothing is
             settled (an MVP is a first-class choice, never nagged); DEGRADE-AND-DISCLOSE the distinct
             case where a spec IS locked but its matrix is missing.
  promote  — after the persona runs: parse the persona's machine-readable `conformance-verdicts.v1` block out of its
             digest body, STRIP it (so the committed digest stays clean prose and the JSON never feeds back
             into a later run as stale judgment), and open-or-update one deduped engine issue per DIVERGENCE
             verdict — plus the degradation gap. Report-only; the reconcile Build PR is the adjudication.

WHY A CORE TOOL THAT READS AN OPTIONAL MODULE'S ARTIFACT. The audit is required core; the matrix is the
optional product-design module's. So the sweep reads the committed matrix JSON by PRESENCE (no import, no
`depends`) and validates it against the pinned `product-spec-matrix.v1` contract — a rename on the producer
side surfaces as a contract failure a test catches, never a silent break. It reads lock status via CORE
`spec_referent._frontmatter_status`, and enumerates `docs/spec/*.md` itself (core has no walk helper). In the
engine's own construction repo (no `docs/spec/`) every path is the silent no-op, exercised only by fixtures.

HONESTY. A standing conformance finding is the engine's AI JUDGEMENT of the built code against the
frozen criterion, carrying NO behavioural correlate at cron cadence (the demonstration harness is the
operator-run correlate at the reconcile merge, not re-run here). The promoted issue SAYS exactly that — it is
a prompt to look, never a confirmed defect. The digest prose is the record; the machine block is a best-effort
accelerator, so an absent/malformed/duplicated block is a clean no-op, never a failure.

Prioritised, not exhaustive: the hunt-set is the rows the matrix STALE-FLAGS (a `(doc, digest)` pair absent from
the previous committed matrix version — a re-worded criterion OR a freshly-locked one, both read over the
commits+contents API on the shallow cron checkout, never `git log`) plus a small ROTATING sample of stable
rows (persisting no count). The disclosure names what was re-hunted and what was not, so the digest never
implies a clean whole-spec pass it did not perform.

CLI (the audit-prep workflow's two steps; both fail-open):
  uv run --directory .engine -- python tools/conformance_sweep.py feed
  uv run --directory .engine -- python tools/conformance_sweep.py promote <digest-body-file>
  uv run --directory .engine -- python tools/conformance_sweep.py state          # print the conditional state
  uv run --directory .engine -- python tools/conformance_sweep.py demo            # safe fail->pass, no writes
"""
from __future__ import annotations

import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate          # noqa: E402  ROOT/ENGINE_DIR, the env seam, the prompt-fence defang
import telemetry         # noqa: E402  the GitHub boundary + promote_finding + marker-safety + the benign class
import issue_author      # noqa: E402  the shared engine-Issue body contract
import spec_referent     # noqa: E402  CORE lock-status reader (never the product-design parser)
import github_client     # noqa: E402  the commits/contents API for the matrix's own recent history
from audit_soft_promote import _neutralize  # noqa: E402  the shared author-text neutraliser

# ---- constants ------------------------------------------------------------------------------

# The committed matrix's home (mirrors obligation_matrix.MATRIX_PATH; defined here, NOT imported, so the core
# sweep takes no dependency on the optional product-design module).
MATRIX_PATH = os.path.join(validate.ENGINE_DIR, "product-spec-matrix.json")
MATRIX_SCHEMA_PATH = os.path.join(validate.ENGINE_DIR, "schemas", "product-spec-matrix.v1.json")
VERDICTS_SCHEMA_PATH = os.path.join(validate.ENGINE_DIR, "schemas", "conformance-verdicts.v1.json")

# The regenerate step the degradation gap names — the one action that closes a spec-locked-but-matrix-missing
# gap. A string, not an import (same no-dependency reason).
MATRIX_REGEN_CMD = "uv run --directory .engine -- python tools/product_design/obligation_matrix.py generate"

_SETTLED = "locked"          # engine-internal lifecycle marker; operator surfaces say "settled"
_SPEC_SUBDIR = ("docs", "spec")

# The dedup namespaces. Disjoint from telemetry's own sources and the other producers, so these issues never
# collide with theirs. The finding key is the LOCKED matrix-row identity `(doc, digest)` — position-free, so a
# mid-table insert/delete never re-keys and re-nags the rows below it.
SOURCE_PREFIX = "product-conformance:"
DEGRADED_SOURCE_ID = "product-conformance-degraded:matrix-missing"

# stable-row sample: a small rotating window (>=1 a cycle), persisting no count — the rotation offset is
# the workflow RUN NUMBER (GITHUB_RUN_NUMBER), which grows every cron run, so the window advances each cycle
# without stored state (the matrix's own commit count does NOT grow per-cron — it only moves when the spec is
# edited — so it cannot drive the rotation).
STABLE_SAMPLE_SIZE = 3

# The recency window for "stale-flagged" rows. A row is stale-flagged when its `(doc, digest)` changed within
# this many days of committed matrix history — so a fresh spec edit is re-hunted for a few cycles and then ages
# out to the rotating sample, and a FROZEN spec's stale set is empty (never re-hunts the whole spec every cron,
# which "prioritised, not exhaustive" forbids). A plain bound — a build-spec leaf — ~5 weekly crons wide.
_RECENCY_DAYS = 35
# How many recent matrix commits to inspect for the recency baseline (one API page; GitHub caps a page at 100).
_HISTORY_PAGE = 30

# The machine channel: an HTML-comment-wrapped conformance-verdicts.v1 block, appended AFTER the digest prose (the
# prompt fixes it as the one trailing thing). Invisible in the rendered digest (mirrors telemetry's
# `<!-- engine-signal -->` marker); parsed then STRIPPED before the digest is sealed.
_BLOCK_MARKER = "<!-- conformance-verdicts.v1"

# The feed's own fence (the persona reads this between the workflow's BEGIN/END markers). Three notices:
# SILENT — no spec settled, a first-class choice, skip and stay quiet; UNAVAILABLE — the check could not run
# (disclose it, never a false "no spec"); plus the degraded/active feeds build() assembles.
_FEED_SILENT = (
    "PRODUCT SPEC CONFORMANCE: not applicable this run — this project has not settled a `docs/spec/`, so there "
    "is nothing to check the build against. Skip the spec-conformance check entirely and do not mention it in "
    "your digest (its absence is a normal, first-class choice, never something to flag).")
_FEED_UNAVAILABLE = (
    "PRODUCT SPEC CONFORMANCE: the spec-conformance check could not run this cycle (its preparation failed). "
    "If this project has settled a `docs/spec/`, say plainly in your digest that the spec-conformance check "
    "could not run this run and was not performed — do NOT imply a clean pass and do NOT conclude there is no "
    "spec. If it has no settled spec, there is simply nothing to report.")


# ---- environment seams (tests/fixtures redirect these; same names obligation_matrix uses) ----

def _root() -> str:
    return validate.env_override_path("ENGINE_SPEC_ROOT") or validate.ROOT


def _matrix_path() -> str:
    return validate.env_override_path("ENGINE_OBLIGATION_MATRIX_PATH") or MATRIX_PATH


def _spec_dir(root: str) -> str:
    return os.path.join(root, *_SPEC_SUBDIR)


# ---- pure reads (fixture-testable; no network) ----------------------------------------------

def locked_docs(root: str) -> list:
    """Repo-relative, forward-slashed paths of every `locked` `docs/spec/*.md` document under `root`, sorted.
    Enumerates the tree itself (core has no walk helper) and reads lock status via CORE
    spec_referent._frontmatter_status — never the product-design parser, so no `depends`."""
    spec_dir = _spec_dir(root)
    out = []
    if not os.path.isdir(spec_dir):
        return out
    for dirpath, _dirs, files in os.walk(spec_dir):
        for name in sorted(files):
            if not name.endswith(".md"):
                continue
            full = os.path.join(dirpath, name)
            try:
                with open(full, encoding="utf-8") as fh:
                    text = fh.read()
            except OSError:
                continue
            if spec_referent._frontmatter_status(text) == _SETTLED:
                out.append(os.path.relpath(full, root).replace(os.sep, "/"))
    return sorted(out)


def _load_schema(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _schema_errors(instance, schema: dict) -> list:
    """Draft2020-12 validation error messages (empty == valid). jsonschema is a core tool-runtime dep."""
    from jsonschema import Draft202012Validator  # lazy: the validation engine's own dependency
    return [e.message for e in Draft202012Validator(schema).iter_errors(instance)]


def load_matrix(path: str | None = None):
    """The committed matrix as a dict, or None when it is absent, unreadable, or does not satisfy the pinned
    `product-spec-matrix.v1` contract. Treating a contract violation as 'missing' (rather than raising) means a
    producer-side shape drift degrades to the disclosed spec-locked-but-matrix-missing path — never a crash,
    never a silent wrong read. Reads the JSON DIRECTLY (never imports obligation_matrix — the no-`depends`
    rule)."""
    path = _matrix_path() if path is None else path
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    try:
        if _schema_errors(data, _load_schema(MATRIX_SCHEMA_PATH)):
            return None
    except (OSError, ValueError):
        return None
    return data


def conditional_state(root: str | None = None, matrix=None, *, _matrix_loaded=False) -> str:
    """'silent' | 'degraded' | 'active'. Silent when nothing is settled (never nag toward a spec);
    degraded when a spec IS locked but its matrix is missing/empty/unreadable (the one actionable gap);
    active when a locked spec and a rows-bearing matrix are both present."""
    root = _root() if root is None else root
    if not _matrix_loaded:
        matrix = load_matrix()
    if not locked_docs(root):
        return "silent"
    if matrix is None or not matrix.get("rows"):
        return "degraded"
    return "active"


# ---- the matrix's own recent history (over the API, never `git log` — the shallow-checkout seam) ----

class _MatrixHistory:
    """The committed matrix's recent-history boundary (same-repo, own-repo token), mirroring
    audit_digest._DigestHistory: the commits+contents APIs for one file, injectable transport so tests/the
    demo fake ONLY the network. Deliberately NOT a `fetch-depth: 0` deep clone (the cron checkout is shallow)."""

    def __init__(self, repo: str, token: str, *, path: str, base: str = "main", transport=None):
        self.repo = repo
        self.token = token
        self.path = path
        self.base = base
        self._transport = transport or self._http

    def _http(self, method: str, api_path: str, body=None):
        import urllib.error
        import urllib.request
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = github_client.request(api_path, self.token, user_agent="engine-conformance-sweep",
                                    method=method, data=data)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode("utf-8")
                return resp.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as exc:
            return exc.code, None

    def baseline_pairs(self, *, window_days: int, now):
        """The `(doc, digest)` set the CURRENT matrix is diffed against to find what changed RECENTLY — the
        matrix content as of the newest commit that is OLDER than `window_days`. So a spec edit within the
        window shows up as stale-flagged (current \\ baseline), and a FROZEN spec (no edit inside the window)
        has baseline == current, i.e. an empty stale set — never re-hunting the whole spec every cron.
        Returns a frozenset of `(doc, digest)`; `frozenset()` when the whole recent history is inside the
        window or the file has no commits (a fresh lock — every current row is stale-flagged for the window);
        or None when the history could not be read (a DISCLOSED gap, never a silent 'nothing moved')."""
        status, commits = self._transport(
            "GET", f"/repos/{self.repo}/commits?path={self.path}&sha={self.base}&per_page={_HISTORY_PAGE}", None)
        if status in (404, 409):
            return frozenset()                         # no history yet: fresh lock, all rows stale-flagged
        if status >= 400 or commits is None:
            return None                                # unreadable: disclosed, not 'nothing moved'
        if not commits:
            return frozenset()
        baseline_sha = None
        for commit in commits:                         # newest-first; take the first one older than the window
            date = (((commit.get("commit") or {}).get("committer") or {}).get("date"))
            when = _parse_iso(date) if date else None
            if when is not None and (now - when).days >= window_days:
                baseline_sha = commit.get("sha")
                break
        if baseline_sha is None:
            # no listed commit is older than the window: if we saw the whole (short) history, everything is
            # recent -> baseline empty (all stale); otherwise older commits exist beyond this page -> approximate
            # the baseline with the oldest one we can see.
            if len(commits) < _HISTORY_PAGE:
                return frozenset()
            baseline_sha = commits[-1].get("sha")
        if not baseline_sha:
            return None
        cstatus, payload = self._transport(
            "GET", f"/repos/{self.repo}/contents/{self.path}?ref={baseline_sha}", None)
        if cstatus >= 400 or payload is None or "content" not in payload:
            return None
        try:
            prior = json.loads(github_client.decode_content(payload, codec="utf-8"))
        except (ValueError, KeyError):
            return None
        return frozenset((r.get("doc"), r.get("digest")) for r in prior.get("rows", []))


def _parse_iso(s: str):
    """Parse a GitHub ISO-8601 commit date to an aware datetime, or None. Tolerant of the trailing 'Z' across
    Python versions; never raises."""
    from datetime import datetime
    try:
        return datetime.fromisoformat((s or "").replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _now():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc)


def _rotation_offset() -> int:
    """The stable-sample rotation offset: the workflow RUN NUMBER, which grows every cron run (so the spot-check
    window advances each cycle) without any stored state. 0 off the runner (a local run / tests)."""
    try:
        return int(os.environ.get("GITHUB_RUN_NUMBER", "0"))
    except (TypeError, ValueError):
        return 0


def _read_history(matrix_repo_path: str):
    """The recency baseline `(doc, digest)` set from the environment's repo/token, or None when they are absent
    (a local run) or the history is unreadable — a DISCLOSED gap, never a silent 'nothing moved'."""
    repo = os.environ.get("GITHUB_REPOSITORY")
    token = os.environ.get("GITHUB_TOKEN")
    if not repo or not token:
        return None
    try:
        return _MatrixHistory(repo, token, path=matrix_repo_path).baseline_pairs(
            window_days=_RECENCY_DAYS, now=_now())
    except Exception:  # noqa: BLE001 — any transport surprise is a disclosed history gap, never a crash
        return None


# ---- prioritisation (pure) ------------------------------------------------------------------

def stale_rows(rows: list, baseline_pairs) -> list:
    """Rows whose `(doc, digest)` is absent from the recency BASELINE (the matrix as of just before the recency
    window) — a criterion re-worded or freshly locked WITHIN the window. When the history is unreadable
    (baseline_pairs is None) this is empty and the caller discloses the gap; when the baseline is empty
    (frozenset() — a fresh lock, or a whole history inside the window) every row is stale-flagged for the
    window; a frozen spec has baseline == current, so its stale set is empty."""
    if baseline_pairs is None:
        return []
    prior = set(baseline_pairs)
    return [r for r in rows if (r.get("doc"), r.get("digest")) not in prior]


def sample_stable(stable: list, offset: int, k: int = STABLE_SAMPLE_SIZE) -> list:
    """A small rotating window of stable rows (>=1 a cycle when any exist), advancing by `offset` (the workflow
    run number) so it moves each cron cycle without persisting a count."""
    if not stable:
        return []
    n = len(stable)
    k = min(k, n)
    start = offset % n
    return [stable[(start + i) % n] for i in range(k)]


# ---- the feed (mode: feed) ------------------------------------------------------------------

def _feed_row_line(row: dict) -> str:
    """One hunt-set line for the persona, criterion text DEFANGED (author-influenced `docs/spec/` content;
    same fence-marker defang every other persona feed applies)."""
    doc = validate.defang_prompt_fence_markers(str(row.get("doc", "")))
    criterion = validate.defang_prompt_fence_markers(str(row.get("criterion", "")))
    digest = str(row.get("digest", ""))
    return f"- [{doc}] (id {digest})\n    criterion: {criterion}"


def build_feed(root: str | None = None, *, matrix=None, baseline_pairs=None, rotation: int = 0,
               history_readable: bool = True) -> str:
    """The persona feed text for the current conditional state. Pure given its inputs (the workflow supplies
    history via _read_history); returns the silent notice, the degradation notice, or the active hunt-set +
    the honest coverage disclosure. The feed tells the persona WHICH criteria to check (the priorities, which
    need history it cannot reach); the persona re-derives each obligation from the `docs/spec/` span itself,
    which it reads with its own read access — the feed line is orientation, never the basis of judgment."""
    root = _root() if root is None else root
    state = conditional_state(root, matrix, _matrix_loaded=True)
    if state == "silent":
        return _FEED_SILENT
    if state == "degraded":
        docs = ", ".join(validate.defang_prompt_fence_markers(d) for d in locked_docs(root)) or "(a settled document)"
        return (
            "PRODUCT SPEC CONFORMANCE: a spec IS settled here but its generated criterion record is missing or "
            "unreadable, so the build cannot be checked against it this run. In your digest, state plainly that "
            "the spec-conformance check could not run because that record is missing, and that regenerating it "
            f"({MATRIX_REGEN_CMD}) is the one step that restores it. Do not guess at conformance and do not "
            f"imply a clean pass. Settled documents: {docs}.")

    rows = matrix.get("rows", [])
    stale = stale_rows(rows, baseline_pairs)
    stale_keys = {(r.get("doc"), r.get("digest")) for r in stale}
    stable = [r for r in rows if (r.get("doc"), r.get("digest")) not in stale_keys]
    sampled = sample_stable(stable, rotation)
    hunt = stale + [r for r in sampled if (r.get("doc"), r.get("digest")) not in stale_keys]

    lines = [
        "PRODUCT SPEC CONFORMANCE: this project has settled a `docs/spec/`. The criteria listed below are the "
        "ones to check THIS cycle (their priorities were computed from history you cannot reach yourself). For "
        "each, OPEN its `docs/spec/` document yourself (you have read access) and re-derive the obligation from "
        "the criterion IN ITS FULL CONTEXT — its surrounding Summary and Behavior, not the one-line summary here "
        "— then judge whether the built code still does what it says. The line below is orientation and an "
        "identifier, never the basis of your judgment. This is a FORWARD check only: do not hunt for code that "
        "traces to no criterion (that is not this review's job on a whole-repo scan).",
        "",
        f"Criteria to check this cycle ({len(hunt)} of {len(rows)} settled criteria):",
    ]
    if not history_readable:
        lines.append(
            "  (NOTE: the record's history could not be read this run, so the 'recently changed' set could not "
            "be computed — you are seeing a spot-check sample only. Say so in your digest.)")
    for r in hunt:
        lines.append(_feed_row_line(r))
    lines += [
        "",
        "COVERAGE DISCLOSURE — in your digest, state plainly what you re-checked this cycle and what you did "
        f"NOT: you checked {len(hunt)} of {len(rows)} settled criteria "
        f"({len(stale)} changed recently, {len(hunt) - len(stale)} spot-checked). Never imply a clean "
        "whole-spec pass you did not perform.",
        "",
        "Then, AFTER your plain-language digest, append your machine-readable verdicts as ONE block exactly in "
        "this form (it is invisible to the reader and is removed before the digest is saved) — one item per "
        "criterion you actually checked, verdict one of \"diverges\" | \"meets\" | \"unsure\", and `criterion` "
        "the frozen wording as you read it from the spec (so a tracked issue can show it):",
        "<!-- conformance-verdicts.v1",
        '{"kind": "product-conformance", "items": [',
        '  {"doc": "<doc>", "digest": "<id above>", "verdict": "diverges", '
        '"criterion": "<the criterion as written>", "note": "<plain reason>"}',
        "]}",
        "-->",
    ]
    return "\n".join(lines)


def emit_feed() -> int:
    """mode: feed — print the persona feed to stdout (the workflow captures it to a file and cats it into the
    prompt between its BEGIN/END markers). Never raises: an unexpected surprise degrades to the UNAVAILABLE
    notice (disclose the check could not run — never a false 'no spec here')."""
    try:
        root = _root()
        matrix = load_matrix()
        matrix_repo_path = os.path.relpath(_matrix_path(), root).replace(os.sep, "/")
        baseline = _read_history(matrix_repo_path)
        sys.stdout.write(build_feed(root, matrix=matrix, baseline_pairs=baseline, rotation=_rotation_offset(),
                                    history_readable=baseline is not None))
        sys.stdout.write("\n")
    except Exception as exc:  # noqa: BLE001 — the feed must never fail the self-review, but must not lie either
        sys.stdout.write(_FEED_UNAVAILABLE + "\n")
        sys.stderr.write(f"(conformance-sweep) feed could not be prepared ({type(exc).__name__}: {exc}).\n")
    return 0


# ---- the machine block: parse + strip (pure) ------------------------------------------------

def extract_block(body: str):
    """`(items, stripped_body)`. The block is the ONE trailing thing after the digest (the prompt fixes it
    there), so the strip removes everything from the FIRST block marker to the end of the body — robust even
    when a persona `note` itself contains `-->` (a naive `<!-- … -->` regex would stop at that inner `-->` and
    leave a fragment in the committed digest). Return the conformance items ONLY when exactly one well-formed,
    schema-valid, product-conformance block is present; zero / multiple / malformed / off-kind all yield [] (a
    clean no-op — the digest prose stays the record)."""
    body = body or ""
    count = body.count(_BLOCK_MARKER)
    if count == 0:
        return [], body
    start = body.index(_BLOCK_MARKER)
    stripped = body[:start].rstrip()                       # the block is trailing: drop it and any trailing ws
    if count != 1:
        return [], stripped                                # ambiguous: no promote, but everything is stripped
    region = body[start + len(_BLOCK_MARKER):]
    close = region.rfind("-->")                            # the LAST '-->' is the real close (an inner one in a
    if close == -1:                                        # note is inside a JSON string and precedes it)
        return [], stripped
    try:
        data = json.loads(region[:close].strip())
    except ValueError:
        return [], stripped
    try:
        if _schema_errors(data, _load_schema(VERDICTS_SCHEMA_PATH)):
            return [], stripped
    except (OSError, ValueError):
        return [], stripped
    if data.get("kind") != "product-conformance":
        return [], stripped
    return data.get("items", []), stripped


# ---- issue rendering (author-influenced text neutralised; carries the artifact-warrant honesty) ----

def _render_divergence(item: dict) -> tuple:
    """(title, body_core) for one 'diverges' verdict. The criterion + the persona's note are author-influenced
    (they include `docs/spec/` content), so both are neutralised before they enter the rendered issue body. The
    body carries the artifact-warrant honesty: this is judgement with no behavioural check, a prompt to
    look, adjudicated at the reconcile merge."""
    doc = item.get("doc", "")
    where = _neutralize(doc)
    criterion = _neutralize(item.get("criterion", "") or "")
    note = _neutralize(item.get("note", "") or "")
    title = f"Spec conformance: the build may have drifted from a settled criterion in {doc}"
    what_this_is = (
        "During its standing self-review, the engine read one of your SETTLED acceptance criteria and the code "
        "built for it, and judged that the code may no longer do what that criterion says.\n\n"
        f"- **The settled criterion** (from `{where}`): {criterion or '(see your settled-criteria record)'}\n"
        f"- **What the review saw:** {note or '(no detail was recorded this run)'}")
    whats_next = (
        "This is the engine's own JUDGEMENT, reading the built code against the wording you froze — **no "
        "behavioural test was run here**, so treat it as a prompt to look, not a confirmed defect.\n\n"
        "- **To reconcile it,** make an ordinary change that brings the code back in line with the criterion — "
        "or, if the code is right and the wording is stale, re-settle the criterion. The merge is the decision.\n"
        "- **Or decline** — if you read the code as still meeting the criterion, close this; you know your "
        "product. A later review may raise it again if the criterion or the code changes.\n"
        "- This is also in your standing self-review; this issue just keeps it tracked until you resolve or "
        "close it.")
    body_core = issue_author.render_engine_issue_body(what_this_is=what_this_is, whats_next=whats_next)
    return title, body_core


def _render_degraded(root: str) -> tuple:
    """(title, body_core) for the spec-locked-but-matrix-missing gap — one deduped issue, the one step that
    closes it, plain language."""
    docs = locked_docs(root)
    listed = ", ".join(f"`{_neutralize(d)}`" for d in docs[:5]) or "(a settled document)"
    more = "" if len(docs) <= 5 else f" (and {len(docs) - 5} more)"
    title = "Spec conformance: your settled spec has no record for the standing review to check"
    what_this_is = (
        "You have SETTLED at least one capability in your product spec, but the engine could not find the "
        "generated record of your settled acceptance criteria that its standing review reads to check the build "
        "against them. So this run it could not check whether the build still matches your frozen spec.\n\n"
        f"- **Settled here:** {listed}{more}\n"
        "- **What's missing:** the generated record of those criteria.")
    whats_next = (
        "- **To close this,** regenerate the record and commit it:\n"
        f"  `{MATRIX_REGEN_CMD}`\n"
        "- Until then the standing review skips the spec-conformance check and says so plainly — it never "
        "guesses at a pass.\n"
        "- This is also in your standing self-review; this issue keeps it tracked until the record is back.")
    body_core = issue_author.render_engine_issue_body(what_this_is=what_this_is, whats_next=whats_next)
    return title, body_core


# ---- records + promotion --------------------------------------------------------------------

def conformance_records(items: list, root: str) -> list:
    """A finding-record per 'diverges' verdict, deduped on the LOCKED matrix-row identity (doc, digest) — no
    position (position cascades and would re-nag). An id that is not marker-safe is SKIPPED, never
    promoted with a corrupt key (the derive_ci_records discipline)."""
    records = []
    for item in items:
        if item.get("verdict") != "diverges":
            continue
        doc, digest = item.get("doc"), item.get("digest")
        if not doc or not digest:
            continue
        source_id = f"{SOURCE_PREFIX}{doc}:{digest}"
        if not telemetry.source_id_is_marker_safe(source_id):
            continue
        title, body_core = _render_divergence(item)
        records.append({
            "source_id": source_id,
            "severity": telemetry.PERSISTENT_BENIGN,
            "message": _neutralize(item.get("note", "") or "the built code may have drifted from a settled criterion"),
            "location": {"file": doc},
            "title": title,
            "body_core": body_core,
        })
    return records


def degraded_record(root: str) -> dict:
    """The single deduped spec-locked-but-matrix-missing record."""
    title, body_core = _render_degraded(root)
    return {
        "source_id": DEGRADED_SOURCE_ID,
        "severity": telemetry.PERSISTENT_BENIGN,
        "message": "a settled spec has no committed criterion record for the standing review to read",
        "location": None,
        "title": title,
        "body_core": body_core,
    }


def promote(body_file: str, *, repo: str | None = None, token: str | None = None,
            transport=None, root: str | None = None) -> tuple:
    """mode: promote — strip the machine block from the digest body (ALWAYS, first, so a failure never leaves
    the JSON committed), then open-or-update one deduped engine issue per divergence verdict, plus the
    degradation gap when a spec is locked but its matrix is missing. Returns (tracked, degraded_github). The
    body file is rewritten in place with the block removed, ready for the seal step. Fail-open at the CLI.
    `repo`/`token` default to the environment (GITHUB_REPOSITORY / GITHUB_TOKEN); tests inject them."""
    root = _root() if root is None else root

    # 1) Read + STRIP the block first — the clean-digest / no-feedback guarantee comes before any network.
    body = ""
    if body_file and os.path.isfile(body_file):
        with open(body_file, encoding="utf-8") as fh:
            body = fh.read()
    items, stripped = extract_block(body)
    if body_file and stripped != body:
        with open(body_file, "w", encoding="utf-8") as fh:
            fh.write(stripped)

    # 2) Build the records: divergences from the block + the mechanical degradation gap. Gate BOTH on the
    #    conditional state — a divergence issue is promoted ONLY when a spec is settled AND its matrix is
    #    present (active). This re-asserts the silence guarantee mechanically: even if the persona
    #    disobeyed the feed and emitted verdicts with no settled spec (silent) or no matrix (degraded), no
    #    divergence issue opens; the degraded state gets its own one gap notice instead.
    state = conditional_state(root)
    records = conformance_records(items, root) if state == "active" else []
    if state == "degraded":
        records.insert(0, degraded_record(root))
    if not records:
        return 0, False

    # 3) Promote (open-or-update, never close), deduped by source_id. Requires repo/token.
    repo = repo if repo is not None else os.environ.get("GITHUB_REPOSITORY")
    token = token if token is not None else os.environ.get("GITHUB_TOKEN")
    if not repo or not token:
        return 0, True
    github = telemetry.GitHubIssues(repo, token, transport=transport)
    now = telemetry.utc_now()
    tracked, degraded = 0, False
    for r in records:
        result = telemetry.promote_finding(github, r, now, title=r["title"], body_core=r["body_core"])
        if result is False:
            degraded = True
        else:
            tracked += 1
    return tracked, degraded


# ---- CLI ------------------------------------------------------------------------------------

def _demo(_argv: list) -> int:
    """A safe, scripted fail->pass on THROWAWAY fixtures — never touches real state or the network. Shows: a
    settled spec with a matrix is 'active' and feeds a hunt-set with the coverage disclosure; a machine block
    round-trips (parse a divergence, strip the block); a settled spec with no matrix is 'degraded' with the one
    fix; and a repo with no spec is the SILENT no-op."""
    import shutil
    import tempfile

    def _seed(files: dict) -> str:
        d = tempfile.mkdtemp(prefix="engine-conformance-sweep-demo-")
        for rel, body in files.items():
            path = os.path.join(d, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(body)
        return d

    def _cap() -> str:
        return "---\nstatus: locked\n---\n# A capability\n\n## Acceptance criteria\n\n| C | How | Who |\n"

    digest = "sha256:" + "a" * 64
    matrix = {"schema_version": 1, "source": "docs/spec",
              "rows": [{"doc": "docs/spec/a.md", "position": 0, "digest": digest,
                        "criterion": "It works end to end", "how_verified": "a demo", "who": "operator"}]}
    ok = True

    active_root = _seed({"docs/spec/a.md": _cap()})
    empty_root = _seed({"README.md": "hi"})
    try:
        # (A) active — a hunt-set + disclosure; empty baseline (frozenset) -> the row is stale-flagged.
        feed = build_feed(active_root, matrix=matrix, baseline_pairs=frozenset(), rotation=0)
        state = conditional_state(active_root, matrix, _matrix_loaded=True)
        print(f"(A) A settled spec + its record -> state '{state}'. The feed lists the hunt-set and discloses "
              f"coverage:")
        print("    " + feed.splitlines()[0])
        ok = ok and state == "active" and "COVERAGE DISCLOSURE" in feed and digest in feed

        # (B) the machine block round-trips: parse a divergence, strip it from the body.
        body = ('Digest prose the operator reads.\n\n<!-- conformance-verdicts.v1\n'
                '{"kind": "product-conformance", "items": '
                f'[{{"doc": "docs/spec/a.md", "digest": "{digest}", "verdict": "diverges", "note": "drifted"}}]}}'
                '\n-->\n')
        items, stripped = extract_block(body)
        recs = conformance_records(items, active_root)
        print(f"(B) The persona's machine block parses to {len(items)} verdict(s) -> {len(recs)} tracked "
              f"issue(s); the block is stripped from the saved digest ({'clean' if '<!--' not in stripped else 'STILL PRESENT'}).")
        ok = ok and len(items) == 1 and len(recs) == 1 and "<!--" not in stripped
        ok = ok and recs[0]["source_id"] == f"{SOURCE_PREFIX}docs/spec/a.md:{digest}"

        # (C) degraded — a settled spec, no matrix -> the one fix, never a guess.
        dstate = conditional_state(active_root, None, _matrix_loaded=True)
        dfeed = build_feed(active_root, matrix=None, baseline_pairs=None)
        print(f"(C) A settled spec with NO record -> state '{dstate}'; the feed names the one fix, never a pass.")
        ok = ok and dstate == "degraded" and MATRIX_REGEN_CMD in dfeed

        # (D) silent — no spec at all -> nothing to check, nothing said.
        sstate = conditional_state(empty_root, None, _matrix_loaded=True)
        sfeed = build_feed(empty_root, matrix=None, baseline_pairs=None)
        print(f"(D) No settled spec -> state '{sstate}'; the feed is the silent no-op (skip, don't mention).")
        ok = ok and sstate == "silent" and sfeed == _FEED_SILENT
    finally:
        shutil.rmtree(active_root, ignore_errors=True)
        shutil.rmtree(empty_root, ignore_errors=True)

    print("Done — active feeds a disclosed hunt-set, a machine block parses-and-strips to a deduped issue, a "
          "missing record degrades to one clear fix, and no spec is a silent no-op. No real state was touched.")
    if not ok:
        print("\nDEMO UNEXPECTED: expected active(disclosed) -> block round-trip -> degraded(fix) -> silent.",
              file=sys.stderr)
        return 1
    return 0


def main(argv: list) -> int:
    cmd = argv[0] if argv else None
    if cmd == "feed":
        return emit_feed()
    if cmd == "promote":
        body_file = argv[1] if len(argv) > 1 else ""
        try:
            tracked, degraded = promote(body_file)
        except Exception as exc:  # noqa: BLE001 — fail-open: a blip must never fail the self-review
            print(f"Could not track standing conformance findings this run ({exc}); the self-review continues "
                  f"and any divergence stays in the digest. Nothing was lost.")
            return 0
        if degraded:
            print("Could not reach GitHub to track one or more standing conformance findings this run; they "
                  "stay in the digest and re-fire next run. Nothing was lost.")
        elif tracked:
            print(f"Tracked {tracked} standing conformance finding(s) as engine issue(s).")
        else:
            print("No standing conformance findings to track this run.")
        return 0
    if cmd == "state":
        print(conditional_state())
        return 0
    if cmd == "demo":
        return _demo(argv[1:])
    print("usage: conformance_sweep.py {feed|promote <digest-body-file>|state|demo}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
