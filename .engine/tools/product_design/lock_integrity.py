#!/usr/bin/env python3
"""Lock-integrity re-acceptance check — the `custom/script` entry for engine/check/product-lock-integrity
(the product-design module's teeth: a *settled* product-spec document cannot change, or be reopened, without
the operator's recorded re-acceptance, or the merge is held).

What it does: on a pull request, it finds every `docs/spec/*.md` capability document that was **settled at the
pull request's base** (the immutable starting point), and, if any such document's content has changed — edited,
reopened, or removed — at the pull request's head, it BLOCKS the merge until the operator deliberately confirms
the change by applying the `guardrail-ack` label. That label is the operator's existing "I have reviewed this
flagged change and deliberately approve it" gesture (a settled-description change is that class of event); the
finding names each settled document that changed so the approval is informed.

Why "settled at base": the base commit is the prior-state correlate — immutable where force-push is blocked
(the protected branch) — so the record of *what was settled* cannot be edited away in the same change that edits
the body. The acknowledgment is an action on the pull request (the applied label), never an AI-writable
committed field, so no single session can supply both the change and the re-acceptance in one stroke.

Where it runs (and the weakening-guard isolation it DELEGATES rather than re-implements): this check rides the existing
`engine-ci` required check via CI-suite membership (`suites: ["CI"]`) — it does NOT run from the trusted base
like the weakening guard, and so it forgoes that guard's "never executes head code" property. That is safe
here for two reasons, both load-bearing: (a) the actor model is all-same-repo pull requests under one identity —
there is no fork contributor whose head code is hostile; and (b) this check's OWN code (this file, under
`.engine/tools/`, and its rule, under `.engine/check/`) sits under the weakening guard's guarded prefixes,
so an attempt to edit it to neuter the gate trips `engine-guard` from the trusted base and is held behind a
deliberate `guardrail-ack`. The weakening-detecting guard stays non-falsifiable; this check inherits that
protection instead of duplicating it. Riding `engine-ci` also means it self-removes from the derived CI roster
when the product-design module is uninstalled — never an orphaned required check that deadlocks merges.

Engine/product wall: it judges only that a settled description CHANGED without a recorded OK —
never whether the change is a good one. It reads files; it never writes them.

Operator-communication law: the engine-internal lifecycle ladder (the stub/draft/locked markers a
document carries) NEVER surfaces to the operator — findings say "settled", never the raw token. The findings
frame the event as a change to the operator's PRODUCT description, never as a safety-gate weakening, even though
the acknowledgment gesture (the `guardrail-ack` label) is shared with the safety guard.

Fail posture (mirrors protection_guard's local-soft / CI-hard split): with no pull-request context (no token /
no event — the normal state on the operator's own machine, and in the unittest self-test), it FAILS OPEN with a
single soft note, so a local `validate.py --suite CI` run is never falsely blocked. In real CI, where the token
and the pull-request event are always present, an unreadable history or API failure FAILS CLOSED with a hard
finding — it never waves a change to settled ground through on a partial view.

Contract: invoked by the validator with NO arguments, it prints a finding.v1 JSON array to stdout and exits 0.
A separate `demo` subcommand runs a falsifiable self-check (pure, no network).
"""
from __future__ import annotations
import json
import os
import sys
import urllib.error
import urllib.parse

# Make the sibling `.engine/tools/` modules importable whether imported as `product_design.lock_integrity`
# or run directly as the wired check script (the spec_form / migration_discipline idiom).
_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PARENT not in sys.path:
    sys.path.insert(0, _PARENT)

import validate  # noqa: E402 — ROOT (test-redirectable) + the finding.v1 helper
# Reuse, never re-declare: the settled-status + path grammar from spec_form; the shared authenticated GitHub
# API client (get_json + base64 decode) from the core github_client module; and the acknowledgment-label
# constant from the weakening guard — that is guard POLICY, not client logic, so it stays imported there.
from product_design import spec_form  # noqa: E402
from github_client import get_json, decode_content  # noqa: E402
from weakening_guard import ACK_LABEL  # noqa: E402

# This check's GitHub API User-Agent. It previously inherited the weakening guard's UA (it borrowed that
# module's api_get); now it identifies as itself — an identification-only string GitHub does not gate on.
_UA = "engine-product-lock-integrity"

# The settled stage, as the raw frontmatter token (engine-internal; never shown to the operator).
_SETTLED = "locked"
# The committed product-spec tree and its master index, as repo-root-relative POSIX paths (GitHub API paths
# are always POSIX; the CI runner and the maintainer's machine both join with "/").
_SPEC_DIR = "docs/spec"
_INDEX_REL = _SPEC_DIR + "/index.md"
# A single directory listing is capped by the Contents API at 1000 entries with no pagination; a listing at the
# cap means we cannot prove we saw every document, so we fail closed (the weakening-guard completeness property).
_CONTENTS_DIR_CAP = 1000

_BOUND_TAIL = ("This checks only that a settled description isn't changed without your say-so — never whether "
               "the change is a good one; that's your call.")


# --------------------------------------------------------------------------------------------------
# The pure core — the testable boundary (no network, no event, no working tree)
# --------------------------------------------------------------------------------------------------

def _norm(text: "str | None") -> "str | None":
    """Normalize file content for a change comparison: strip a leading BOM, normalize line endings, and drop
    trailing blank lines — so a checkout's line-ending or final-newline rewrite never reads as a real edit.
    Returns None unchanged (a document absent at head)."""
    if text is None:
        return None
    if text.startswith("﻿"):
        text = text[1:]
    return text.replace("\r\n", "\n").replace("\r", "\n").rstrip("\n")


def _changed(base: str, head: "str | None") -> bool:
    """True when a settled-at-base document has changed at head — including a reopen (a frontmatter-status
    change is a content change) and a removal (head is None). Whole-file comparison: settling covers the whole
    document, so any normalized content difference is a change to settled ground."""
    if head is None:
        return True
    return _norm(base) != _norm(head)


def classify(base_locked_docs: dict, head_docs: dict, label_present: bool, tier: str) -> list:
    """The lock-integrity findings, as a list of finding.v1 dicts. The pure decision over already-gathered
    inputs (this is the unit the tests drive directly):

    - `base_locked_docs`: {repo-root-relative POSIX path -> full file content} for every capability document
      that was SETTLED at the pull request's base. A document not settled at base is simply absent here.
    - `head_docs`: {same path -> content, or None if the document is absent at head (removed/renamed away)}.
    - `label_present`: whether the operator has applied the `guardrail-ack` re-acceptance label.

    Empty list = clean (no settled document changed, or the operator has re-accepted). Otherwise one finding
    per changed settled document, at `tier` (hard), naming the document and the label to apply. The label is
    global: when present, it clears every settled-document change in the pull request (informed by the per-doc
    findings the operator saw before applying it)."""
    if label_present:
        return []
    out = []
    for rel in sorted(base_locked_docs):
        if _changed(base_locked_docs[rel], head_docs.get(rel)):
            removed = head_docs.get(rel) is None
            out.append(validate.finding(tier, _reaccept_message(rel, removed), {"file": rel, "line": None}))
    return out


# --------------------------------------------------------------------------------------------------
# Operator-facing messages (plain language; framed as a product-description change, never a safety event)
# --------------------------------------------------------------------------------------------------

def _reaccept_message(rel: str, removed: bool) -> str:
    verb = "removes" if removed else "changes"
    reopen_note = ("" if removed else
                   " (Reopening a settled description to keep working on it counts as a change too — the same "
                   "label records that.)")
    return (
        f"This pull request {verb} `{rel}` — a part of your product description you had settled, the ground "
        f"the build works from. A settled description shouldn't change quietly. If you mean to make this "
        f"change, confirm it by applying the `guardrail-ack` label to this pull request — one deliberate "
        f"action, distinct from clicking merge — and this check clears on its own.{reopen_note} " + _BOUND_TAIL
    )


_NO_CONTEXT_MESSAGE = (
    "Settled-description checking isn't active here — it runs on your pull requests, where it can see what a "
    "change does to a description you've already settled, and there's no pull request to check here. That's the "
    "normal, expected state on your own machine, not an error: it does its work in the pull request's checks. "
    + _BOUND_TAIL
)


def _fail_closed_message(detail: str) -> str:
    return (
        "The settled-description check couldn't read this pull request's starting point "
        f"({detail}), so it can't confirm whether a part of your product description you'd settled has changed. "
        "Rather than let a change to settled ground through unchecked, it's holding the merge. Re-running the "
        "check usually clears a transient read problem; a very large change can also be split into smaller pull "
        "requests so every document can be read. " + _BOUND_TAIL
    )


# --------------------------------------------------------------------------------------------------
# The I/O wrapper — gather the inputs, then defer to classify(); fail closed on any read failure (in CI)
# --------------------------------------------------------------------------------------------------

def _read_base_content(repo: str, base_sha: str, path: str, token: str) -> str:
    """The content of `path` at `base_sha`, via the Contents API (Contents: read). Falls back to the Git blob
    API for a file over the Contents API's 1 MB inline limit. Decodes with `utf-8-sig` so a leading byte-order
    mark (common from Windows editors) is stripped — the SAME tolerance spec_form's reader and the head reader
    apply, so a settled doc with a committed BOM reads identically on both sides (else it would be settled-and-
    valid per the form check yet invisible here — an under-gate). The path is percent-encoded so a spec
    filename with a space or other reserved character produces a valid request rather than a false block.
    Raises on any unreadable response so the caller fails closed."""
    quoted = urllib.parse.quote(path, safe="/")
    obj = get_json(f"/repos/{repo}/contents/{quoted}?ref={base_sha}", token, user_agent=_UA)
    if isinstance(obj, dict) and obj.get("encoding") == "base64" and "content" in obj:
        return decode_content(obj, codec="utf-8-sig")
    sha = obj.get("sha") if isinstance(obj, dict) else None
    if sha:  # >1 MB: Contents returns empty content / encoding "none"; read the blob by sha (also Contents: read)
        blob = get_json(f"/repos/{repo}/git/blobs/{sha}", token, user_agent=_UA)
        if isinstance(blob, dict) and blob.get("encoding") == "base64" and "content" in blob:
            return decode_content(blob, codec="utf-8-sig")
    raise RuntimeError(f"could not read {path} at the base commit")


def _spec_md_paths_at_base(repo: str, base_sha: str, token: str) -> list:
    """Repo-root-relative POSIX paths of every `*.md` under `docs/spec/` (recursive, excluding the master
    index) at `base_sha`. Returns [] when `docs/spec/` does not exist at base (a 404 is the expected
    "no spec tree" state, not a read failure). Raises on any other API failure (caller fails closed). The
    recursion matches spec_form's whole-tree discovery, so a settled document in a subdirectory is never
    missed (an under-gate). A directory listing at the 1000-entry cap fails closed (completeness). Only regular
    files are gated: a spec document delivered as a symlink (Contents-API `type: "symlink"`) is skipped — a
    settled spec doc is an ordinary committed file under the operator's own (single-identity) repo, so a
    symlinked one is not a reachable state."""
    out, queue = [], [_SPEC_DIR]
    while queue:
        directory = queue.pop()
        try:
            quoted = urllib.parse.quote(directory, safe="/")
            entries = get_json(f"/repos/{repo}/contents/{quoted}?ref={base_sha}", token, user_agent=_UA)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                continue  # this directory is absent at base (top-level docs/spec absent => no settled docs)
            raise
        if not isinstance(entries, list):
            raise RuntimeError(f"unexpected Contents response for {directory}")
        if len(entries) >= _CONTENTS_DIR_CAP:
            raise RuntimeError(f"{directory} has too many entries to read in one pass")
        for entry in entries:
            etype, epath = entry.get("type"), entry.get("path")
            if etype == "dir" and epath:
                queue.append(epath)
            elif etype == "file" and epath and epath.endswith(".md") and epath != _INDEX_REL:
                out.append(epath)
    return out


def _gather_base_locked_docs(repo: str, base_sha: str, token: str) -> dict:
    """{path -> content} for every capability document SETTLED at base. Reads each spec `*.md` once, keeps the
    ones whose frontmatter stage is the settled marker."""
    locked = {}
    for rel in _spec_md_paths_at_base(repo, base_sha, token):
        content = _read_base_content(repo, base_sha, rel, token)
        if spec_form._frontmatter_status(content) == _SETTLED:
            locked[rel] = content
    return locked


def _read_head(root: str, rel: str) -> "str | None":
    """The working-tree (head) content of `rel`, or None when the file is absent at head (removed/renamed
    away). A genuine read error (permissions/IO) raises, so the caller fails closed — an unreadable head is
    never silently treated as "unchanged" (which would let a settled-document change through)."""
    full = os.path.join(root, rel)
    if not os.path.exists(full):
        return None
    with open(full, "r", encoding="utf-8-sig", errors="replace") as fh:
        return fh.read()


def emit_findings() -> int:
    """The no-argument path the validator invokes: gather inputs, classify, print the finding.v1 array, return
    0. Fail OPEN (one soft note) with no pull-request context — the normal local state — so a local CI-suite
    run is never falsely blocked. In real CI (token + event present) an unreadable history fails CLOSED (hard).
    Violations carry the rule's declared tier (ENGINE_RULE_TIER, default hard)."""
    tier = os.environ.get("ENGINE_RULE_TIER", "hard")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    token = os.environ.get("GITHUB_TOKEN", "")
    event_path = os.environ.get("GITHUB_EVENT_PATH", "")
    # No credentials / no event => local or non-PR context: FAIL OPEN soft (never a false local block); the CI
    # run, which always has both, performs the real check. Mirrors protection_guard's local-soft posture.
    if not (repo and token and event_path and os.path.exists(event_path)):
        return _emit([validate.disclosed_noop(_NO_CONTEXT_MESSAGE, None)])
    try:
        with open(event_path, encoding="utf-8") as fh:
            event = json.load(fh)
        pr = event.get("pull_request")
        if not pr:
            # Not a pull-request event: nothing to evaluate => soft (non-blocking), never a false block.
            return _emit([validate.disclosed_noop(_NO_CONTEXT_MESSAGE, None)])
        base_sha = ((pr.get("base") or {}).get("sha")) or ""
        if not base_sha:
            # A pull request with no readable base => fail closed (only reachable in CI).
            return _emit([validate.finding(tier, _fail_closed_message("no base commit in the event"), None)])
        label_present = ACK_LABEL in {lbl.get("name") for lbl in (pr.get("labels") or [])}
        base_locked = _gather_base_locked_docs(repo, base_sha, token)
        head = {rel: _read_head(validate.ROOT, rel) for rel in base_locked}
        result = classify(base_locked, head, label_present, tier)
    except Exception as exc:  # any read failure => fail closed, never wave a settled-ground change through
        return _emit([validate.finding(tier, _fail_closed_message(str(exc)), None)])
    return _emit(result)


def _emit(findings: list) -> int:
    print(json.dumps(findings))
    return 0


# --------------------------------------------------------------------------------------------------
# Falsifiable self-check (pure — drives classify over crafted inputs; no network, no working tree)
# --------------------------------------------------------------------------------------------------

def demo() -> int:
    """Prove the check: a settled document unchanged passes; a settled document edited, reopened, or removed
    without the label is a hard finding naming the document and the `guardrail-ack` label; the same change WITH
    the label clears; a not-yet-settled document is never gated; a line-ending/BOM-only difference is not a
    change. Every finding is framed as a product-description change (never a safety event) and never leaks a raw
    lifecycle token. RETURNS NON-ZERO if any invariant is broken (the falsification can fail)."""
    settled = "---\nstatus: locked\n---\n\n# Checkout\n\nThe checkout flow.\n"
    edited = "---\nstatus: locked\n---\n\n# Checkout\n\nThe checkout flow, revised.\n"
    reopened = "---\nstatus: draft\n---\n\n# Checkout\n\nThe checkout flow.\n"
    crlf_bom = "﻿" + settled.replace("\n", "\r\n") + "\n\n"

    base = {"docs/spec/checkout.md": settled}
    cases = []  # (label, base_locked, head, label_present, predicate)
    cases.append(("an unchanged settled document passes cleanly",
                  base, {"docs/spec/checkout.md": settled}, False, lambda fs: fs == []))
    cases.append(("a line-ending/BOM-only difference is not treated as a change",
                  base, {"docs/spec/checkout.md": crlf_bom}, False, lambda fs: fs == []))
    cases.append(("an edited settled document with no label is a hard finding naming the doc and the label",
                  base, {"docs/spec/checkout.md": edited}, False,
                  lambda fs: len(fs) == 1 and fs[0]["severity"] == "hard"
                  and "docs/spec/checkout.md" in fs[0]["message"] and "guardrail-ack" in fs[0]["message"]
                  and "changes" in fs[0]["message"]))
    cases.append(("reopening a settled document (stage flip) with no label is a hard finding",
                  base, {"docs/spec/checkout.md": reopened}, False,
                  lambda fs: len(fs) == 1 and fs[0]["severity"] == "hard"))
    cases.append(("removing a settled document with no label is a hard finding worded as a removal",
                  base, {"docs/spec/checkout.md": None}, False,
                  lambda fs: len(fs) == 1 and fs[0]["severity"] == "hard" and "removes" in fs[0]["message"]))
    cases.append(("an edited settled document WITH the label clears (global re-acceptance)",
                  base, {"docs/spec/checkout.md": edited}, True, lambda fs: fs == []))
    cases.append(("a document not settled at base is never gated (it is simply absent from base_locked_docs)",
                  {}, {"docs/spec/draft-thing.md": edited}, False, lambda fs: fs == []))
    cases.append(("two settled docs, one changed+unacked and one unchanged: exactly one finding, for the right doc",
                  {"docs/spec/a.md": settled, "docs/spec/b.md": settled},
                  {"docs/spec/a.md": edited, "docs/spec/b.md": settled}, False,
                  lambda fs: len(fs) == 1 and "docs/spec/a.md" in fs[0]["message"]))

    failures = []
    for label, base_locked, head, has_label, ok in cases:
        result = classify(base_locked, head, has_label, "hard")
        for f in result:
            for token in ("stub", "draft", "locked"):
                if _word(token, f["message"]):
                    failures.append(f"{label}: a finding leaked the raw lifecycle token '{token}': {f['message']}")
            if "safety gate" in f["message"] or "safety check" in f["message"]:
                failures.append(f"{label}: a finding framed a product change as a safety event: {f['message']}")
            if "product description" not in f["message"]:
                failures.append(f"{label}: a finding did not frame the event as a product-description change: {f['message']}")
        if not ok(result):
            failures.append(f"{label}: invariant broken, got {result}")

    # Tier is carried through, not hardcoded.
    soft_run = classify(base, {"docs/spec/checkout.md": edited}, False, "soft")
    if not (len(soft_run) == 1 and soft_run[0]["severity"] == "soft"):
        failures.append(f"tier discipline: a 'soft' run did not carry soft severity, got {soft_run}")

    if failures:
        print("DEMO FAILED — the lock-integrity check broke an invariant:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("DEMO PASSED — the lock-integrity check passes an unchanged settled document, blocks an edited, "
          "reopened, or removed one (at hard severity, naming the document and the guardrail-ack label) until "
          "the label is applied, never gates a not-yet-settled document, ignores a line-ending-only difference, "
          "carries the rule's tier, frames every finding as a product-description change, and never leaks a raw "
          "lifecycle token.")
    return 0


def _word(token: str, text: str) -> bool:
    import re
    return bool(re.search(rf"\b{token}\b", text))


def main(argv: list) -> int:
    if argv and argv[0] == "demo":
        return demo()
    return emit_findings()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
