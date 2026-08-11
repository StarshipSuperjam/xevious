#!/usr/bin/env python3
"""The kind-label applicator — the on:issues CI net that keeps the GitHub-native kind labels consistent.

WHAT THIS IS. Sessions apply `bug`/`enhancement`/… ad hoc, so browsing/filtering on GitHub is unreliable. This
backstop derives the fitting GitHub-native label from an Issue's title kind prefix and applies it, on every
Issue `opened` or `edited` — so the kind axis is consistent by construction, not by a session remembering.
It maps only onto the four labels GitHub already ships in every repo (`bug`, `enhancement`, `documentation`,
`question`); it mints NOTHING and invents no taxonomy (eADR-0021: the ban is on new labels, not the natives).

TITLE-DERIVED, NEVER TITLE-INTERPOLATED. The action is read from the Issue *title* — an attacker-controllable
field — but the title never reaches a shell: the workflow runs this tool, which reads the title from the event
JSON at `$GITHUB_EVENT_PATH` (the safe pattern), and the label it applies is a fixed enum from
`native_label_for_title`, never raw title text. So there is no title→shell and no title→label-value injection.

APPLY-ONLY, NEVER MINT (skip-if-absent). A repo owner may have deleted a GitHub default. If the derived native
label does not exist on the repo, this SKIPS it (disclosed in the run log) rather than creating it — so the
engine stays a pure *producer* of pre-existing labels, never an unprovisioned minter (which would stretch the
control-plane label law). A decorative label simply isn't applied where its target was removed.

NEVER GATES, NEVER LOOPS. GitHub cannot gate Issue creation, so this is an honest backstop, not a wall. It only
ADDS a label (v1 is additive: a kind changed by a later edit leaves any now-stale native label in place — a
disclosed boundary, not a gap). Adding a label fires a `labeled` event, which the `[opened, edited]` trigger
does not watch, so the apply never self-retriggers; this tool never edits a title/body (which would fire
`edited`), so no loop is possible.

FAIL CONTRACT (a safety-net, never a gate). No readable event, no issue, or a title with no mappable kind →
a quiet exit 0 (no-op). Work to do but GITHUB_REPOSITORY/GITHUB_TOKEN unset, or a genuine GitHub API failure →
a non-zero exit so the net's OWN breakage is a visible red run, never a silent pass. A red here gates nothing —
the Issue already exists.

CLI (operator-runnable, falsifiable — the live net is what the workflow invokes):
  uv run --directory .engine -- python tools/issue_kind_label.py demo   # scripted, fake GitHub, self-checks
"""
from __future__ import annotations

import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import issue_label_client  # noqa: E402  (the shared per-Issue label client + injectable transport)

USER_AGENT = "engine-issue-kind-label"

# Each issue-title kind prefix and the GitHub-native label it maps to. Issue-title kinds are a SUPERSET of the
# PR/release kinds (`release_cut._RELEASE_NOTE_KINDS`): issues add `Engine fault`/`Bug`/`Defect` for the faults
# telemetry and sessions file. Kept deliberately as its OWN table, not a slice of release_cut — importing that
# module would drag the module-manager/coherence stack into this per-issue CI hot path for a one-line regex,
# and its vocabulary is a different (PR) axis that could drift out from under this one.
_NATIVE_BY_KIND = {
    "bug": "bug",
    "fix": "bug",
    "engine fault": "bug",
    "defect": "bug",
    "security": "bug",
    "feature": "enhancement",
    "improvement": "enhancement",
    "docs": "documentation",
    "documentation": "documentation",
    "question": "question",
}
# The GitHub-native labels this applicator may apply — the complete value range, for tests and disclosure.
NATIVE_KIND_LABELS = tuple(dict.fromkeys(_NATIVE_BY_KIND.values()))
# `^<Kind>:` at the very start, case-insensitive. Longer kinds first so `documentation` is never shadowed by
# `docs` (the `:` anchor already prevents it, but ordering makes the intent explicit and robust). Each kind is
# regex-escaped so a multi-word kind (`engine fault`) and any future metacharacter match literally.
_KIND_PREFIX_RE = re.compile(
    r"^\s*(" + "|".join(re.escape(k) for k in sorted(_NATIVE_BY_KIND, key=len, reverse=True)) + r")\s*:",
    re.IGNORECASE,
)


def native_label_for_title(title) -> "str | None":
    """The GitHub-native kind label for an Issue title's leading `Kind:` prefix, or None when the title has no
    mappable kind (e.g. `Migration M3:`, `Delivery wave 2`, a bare descriptive title) — never a guess. The
    single source both the on:issues applicator and any one-time backfill call, so the two can never disagree."""
    if not isinstance(title, str):
        return None
    m = _KIND_PREFIX_RE.match(title)
    if not m:
        return None
    return _NATIVE_BY_KIND[m.group(1).strip().lower()]


def _labels_of(issue: dict) -> list:
    """The label names on an issue event payload (`.issue.labels[].name`), defensively."""
    return [lab.get("name") for lab in (issue.get("labels") or []) if isinstance(lab, dict)]


def apply_kind_label(issue: dict, client) -> str:
    """Ensure the title-derived native label is present on one Issue, idempotently and WITHOUT ever creating it.
    Returns a short action word for the log/demo. Assumes `issue` has a numeric `number`. Any GitHub failure
    propagates as DegradedWriteError (→ a red run). The order matters: derive → already-present? → exists on the
    repo? → add. Skipping a repo-absent label is what keeps this apply-only (never a minter)."""
    native = native_label_for_title(issue.get("title") or "")
    if native is None:
        return "no-kind"
    if native in _labels_of(issue):
        return "already"
    if not client.label_exists(native):
        return "absent"                      # the repo owner removed this default — skip, never mint
    client.add_label(issue["number"], native)
    return "labelled"


def _issue_or_none(event):
    """The issue dict from an issues-event payload IFF it has a numeric id; else None (the caller no-ops). Unlike
    the conformance net, this applies to ANY Issue — the kind axis is orthogonal to the `engine` label."""
    if not isinstance(event, dict):
        return None
    issue = event.get("issue")
    if not isinstance(issue, dict) or not isinstance(issue.get("number"), int):
        return None
    return issue


def _load_event():
    """The issue event JSON from $GITHUB_EVENT_PATH (read from the file, never a shell-interpolated argument),
    or None when unavailable/unreadable (a local run, a partial event) → the caller no-ops quietly."""
    path = os.environ.get("GITHUB_EVENT_PATH")
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _run() -> int:
    event = _load_event()
    if event is None:
        print("kind-label: no readable issue event — nothing to do.")
        return 0
    issue = _issue_or_none(event)
    if issue is None:
        print("kind-label: no issue in the event — no action.")
        return 0
    if native_label_for_title(issue.get("title") or "") is None:
        print("kind-label: title has no mappable kind — no action.")
        return 0
    repo = os.environ.get("GITHUB_REPOSITORY")
    token = os.environ.get("GITHUB_TOKEN")
    if not repo or not token:
        print("kind-label: GITHUB_REPOSITORY / GITHUB_TOKEN unset — cannot reach GitHub.", file=sys.stderr)
        return 1
    client = issue_label_client.IssueLabelClient(repo, token, user_agent=USER_AGENT)
    try:
        action = apply_kind_label(issue, client)
    except issue_label_client.DegradedWriteError as exc:
        print(f"kind-label: a GitHub API call failed — {exc}", file=sys.stderr)
        return 1
    # Each action word rendered as a sentence a person scanning an Actions run can read cold — `absent`
    # in particular must read as a deliberate skip (the repo owner removed that default), never a fault.
    explained = {
        "labelled": "native kind label applied",
        "already": "native kind label already present — nothing to do",
        "absent": "skipped — that native label was removed from this repo, and this tool never creates one",
        "no-kind": "title has no mappable kind — no action",
    }
    print(f"kind-label: issue #{issue['number']} -> {action} ({explained[action]})")
    return 0


# ---- the operator-runnable demo (the live net is what the workflow invokes) -------------------

class _FakeGitHub:
    """A scripted GitHub for the demo/tests: records every (method, path, body) and returns canned
    (status, json), so the REAL apply_kind_label logic runs with no network. `label_exists` decides whether the
    repo-label GET reports the native label present."""

    def __init__(self, *, label_exists: bool = True):
        self.calls = []
        self._label_exists = label_exists

    def __call__(self, method, path, body=None):
        self.calls.append((method, path, body))
        if "/issues/" in path and path.endswith("/labels"):   # POST: add a label to an issue
            return 200, []
        if "/labels/" in path:                                # GET: does the repo label exist?
            return (200 if self._label_exists else 404), None
        return 200, None

    def issue_label_adds(self):
        return [c for c in self.calls if c[0] == "POST" and "/issues/" in c[1] and c[1].endswith("/labels")]


def _client(gh):
    return issue_label_client.IssueLabelClient("o/r", "t", user_agent=USER_AGENT, transport=gh)


def _demo() -> int:
    """Runs the REAL apply_kind_label / native_label_for_title over synthetic issue events against a fake
    GitHub, printing the actual mapping and self-checking every outcome. Returns 1 on any unexpected result
    (the failure path the in_tool_demo_failure_path floor requires)."""
    ok = True

    def check(desc: str, cond: bool) -> None:
        nonlocal ok
        if not cond:
            ok = False
        print(f"  {desc:64} -> {'OK' if cond else 'UNEXPECTED'}")

    print("The kind-label applicator — what it does for each issue event (real logic, fake GitHub):\n")

    # 1. a mappable title, label present on the repo, absent on the issue -> ONE add
    gh = _FakeGitHub(label_exists=True)
    action = apply_kind_label({"number": 1, "title": "Fix: quote the hook path", "labels": []}, _client(gh))
    check("mappable title, label on repo, not on issue: labelled (one add)",
          action == "labelled" and len(gh.issue_label_adds()) == 1)

    # 2. a title with no mappable kind -> a pure no-op (no client calls at all)
    gh2 = _FakeGitHub()
    action2 = apply_kind_label({"number": 2, "title": "Migration M3: rename the routine", "labels": []}, _client(gh2))
    check("unmappable title: no-op (no GitHub calls)", action2 == "no-kind" and gh2.calls == [])

    # 3. a mappable title but the native label was DELETED from the repo -> skip, never mint
    gh3 = _FakeGitHub(label_exists=False)
    action3 = apply_kind_label({"number": 3, "title": "Feature: add a thing", "labels": []}, _client(gh3))
    check("mappable title, native label absent on repo: skipped, not minted",
          action3 == "absent" and gh3.issue_label_adds() == [])

    # 4. the native label is already on the issue -> no redundant add
    gh4 = _FakeGitHub(label_exists=True)
    action4 = apply_kind_label(
        {"number": 4, "title": "Improvement: tidy", "labels": [{"name": "enhancement"}]}, _client(gh4))
    check("native label already on the issue: no redundant add",
          action4 == "already" and gh4.issue_label_adds() == [])

    # 5. out-of-scope events are filtered before any client is built
    check("partial/malformed event: out of scope", _issue_or_none({"issue": None}) is None)

    # 6. the mapping itself — the load-bearing derivation, spot-checked across kinds + edge cases
    cases = {
        "Fix: x": "bug", "Bug: x": "bug", "Engine fault: x": "bug", "Defect: x": "bug", "Security: x": "bug",
        "Feature: x": "enhancement", "Improvement: x": "enhancement",
        "Docs: x": "documentation", "Documentation: x": "documentation", "Question: x": "question",
        "fix: lowercase": "bug", "  Feature: leading space": "enhancement", "Engine fault : spaced colon": "bug",
        "Maintenance: x": None, "Delivery wave 2": None, "no prefix at all": None, "": None,
    }
    for title, expected in cases.items():
        check(f"native_label_for_title({title!r}) == {expected!r}", native_label_for_title(title) == expected)

    print("\n  title kind prefix  ->  GitHub-native label it applies:")
    for kind, native in _NATIVE_BY_KIND.items():
        print(f"    {kind + ':':22} {native}")
    print(f"\n  Native labels this applicator may apply (never mints): {', '.join(NATIVE_KIND_LABELS)}")

    if not ok:
        print("\nDEMO UNEXPECTED: an outcome did not match the applicator's contract.", file=sys.stderr)
        return 1
    return 0


def main(argv: list) -> int:
    if argv and argv[0] == "demo":
        return _demo()
    return _run()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
