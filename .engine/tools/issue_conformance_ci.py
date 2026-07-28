#!/usr/bin/env python3
"""The engine-Issue conformance backstop — the on:issues CI safety-net for the body contract.

WHAT THIS IS. The after-the-fact catch for a malformed engine-labelled GitHub Issue that the in-session
reroute gate (issue_gate.py) could not inspect. Run by the engine-issue-conformance workflow on every Issue
`opened` or `edited`: it reads the issue event, and for an `engine`-labelled Issue whose body is NOT in the
control-plane body contract's shape it FLAGS the Issue — applies the `needs-reauthoring` label and posts ONE
advisory comment carrying the conforming skeleton — so the slip enters the engine's own detect→surface→remediate
loop. When a later edit makes the body conform, it removes the label. It NEVER gates Issue creation (GitHub
cannot), so it is an honest backstop, not a second wall.

KEYS ON BODY SHAPE AND THE ENGINE LABEL, NEVER PROVENANCE. The conformance test is the SAME predicate the
in-session gate uses — `all(marker in body for marker in issue_gate.CONTRACT_MARKERS)` — imported from
issue_gate (the single source), so the two layers can never drift. Only an Issue carrying the `engine` label is
ever touched; an ordinary or human Issue is out of scope.

IDEMPOTENT, NEVER AN OPERATOR CHORE. The comment is posted at most once per Issue (a `<!-- engine-issue-
conformance -->` marker in the bot comment is the dedup key, recovered by listing the Issue's comments — the
telemetry._SENTINEL_RE pattern). The label is re-affirmed without duplication and is removed automatically the
moment the body conforms. An Issue that is never re-authored simply keeps a harmless label; it never becomes a
task for the operator. The workflow serialises same-Issue runs (a per-Issue concurrency group) so the
comment-dedup cannot race itself on a rapid opened+edited.

FAIL CONTRACT (a safety-net, never a gate). Out of scope, an unreadable/partial event, or a non-engine Issue →
a quiet exit 0 (no-op). A genuine GitHub API failure on a label/comment write (auth, scope, outage) → a
non-zero exit so the net's OWN breakage is visible as a red run, never a silent pass. An on:issues run never
gates Issue creation — the Issue already exists — so a red here blocks nothing.

KNOWN RESIDUAL (honest). The trigger is `[opened, edited]` (the control-plane design's shape). An `engine` label
applied in a SEPARATE step AFTER creation fires a `labeled` event, which this trigger does not watch, so such an
Issue is caught only on its next body edit. Cold sessions apply `--label engine` AT create (caught on `opened`)
and the in-session gate is the first line — widening the trigger would diverge from the locked design.

SELF-CONTAINED TRANSPORT. telemetry.GitHubIssues has no per-Issue label/comment operations (its label is baked
in at construction and it only opens/updates engine-health Issues), so this tool carries its own transport — its
own urlopen + (status, json) return + injectable `_transport` seam (30s timeout), building requests through the
shared `github_client` (the audit_digest / telemetry idiom) — over the per-Issue label and comment operations it
needs (label ensure/add/remove, comment list/post). telemetry.py is left untouched; the markers are
single-sourced from issue_gate.

CLI (operator-runnable, falsifiable — the live net is what the workflow invokes):
  uv run --directory .engine -- python tools/issue_conformance_ci.py demo   # scripted, fake GitHub, self-checks
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import issue_author  # noqa: E402
import github_client  # noqa: E402  (the shared authenticated GitHub API client; request-build)
import issue_gate    # noqa: E402

USER_AGENT = "engine-issue-conformance"

# The label applied to a non-conforming engine Issue. The design names the signal `needs-reauthoring`
# and leaves the concrete string an operator-facing build-spec leaf; the maintainer
# chose this string verbatim. Because the label keeps the design term, the COMMENT (below) carries the
# plain-language reassurance that it is not a task for the operator.
NEEDS_REAUTHORING_LABEL = "needs-reauthoring"
# The label's colour + description are public so provisioning can mirror them into its one-place label set
# (a drift-test in test_bootstrap holds the mirror equal to these). This module remains the canonical home.
NEEDS_REAUTHORING_LABEL_COLOR = "d4c5f9"  # a calm lavender — distinct from the engine label's grey, never an alarm red
NEEDS_REAUTHORING_LABEL_DESCRIPTION = "Engine Issue not yet in the engine's standard format — the engine will re-file it."
_LABEL_COLOR = NEEDS_REAUTHORING_LABEL_COLOR              # internal aliases (kept so existing call sites read unchanged)
_LABEL_DESCRIPTION = NEEDS_REAUTHORING_LABEL_DESCRIPTION

# The invisible dedup marker stamped into the bot comment so a re-fire never double-comments and the un-flag
# path can recognise the net's own comment. Mirrors telemetry's _SENTINEL_TEMPLATE; no collision with the
# existing markers (telemetry's `<!-- engine-signal: … -->`, instantiator's `<!-- engine-template:landing-front -->`).
COMMENT_MARKER = "<!-- engine-issue-conformance -->"


class DegradedWriteError(Exception):
    """Raised when a GitHub API call the backstop depends on fails. It is NEVER swallowed as success — a real
    API failure must surface as a red CI run (the net's own breakage is visible), never a silent pass."""


class IssueConformanceClient:
    """The per-Issue label/comment client. Mirrors telemetry/audit_digest's injectable-transport seam:
    `transport(method, path, body) -> (status, json|None)` is injectable, so the demo and tests fake ONLY the
    network and run the real logic. Deliberately NOT telemetry.GitHubIssues — that class is engine-issue-domain
    shaped (label baked in, opens/updates whole Issues); this exposes only the per-Issue label and comment
    operations the backstop needs (ensure/add/remove a label, list/post a comment)."""

    def __init__(self, repo: str, token: str, *, transport=None):
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
        except urllib.error.HTTPError as exc:           # 4xx/5xx — surface the status, never swallow
            return exc.code, None
        except urllib.error.URLError as exc:             # network unreachable — a write failure
            raise DegradedWriteError(f"GitHub is unreachable: {exc}") from exc

    def ensure_label(self, name: str, color: str, description: str) -> None:
        """Idempotently ensure a repo label exists (create it iff absent). Mirrors telemetry.ensure_label,
        parametrised on name/color/description (telemetry's is hardcoded to the engine label). The name is
        URL-encoded into the GET path — the label string is an operator-picked build-spec leaf and could
        carry a space (e.g. `engine: needs formatting`), so it must never be interpolated raw into a URL."""
        status, _ = self._transport("GET", f"/repos/{self.repo}/labels/{urllib.parse.quote(name, safe='')}", None)
        if status == 404:
            self._transport("POST", f"/repos/{self.repo}/labels",
                            {"name": name, "color": color, "description": description})
        elif status >= 400:
            raise DegradedWriteError(f"GitHub returned {status} checking the '{name}' label")

    def add_label(self, number: int, name: str) -> None:
        status, _ = self._transport("POST", f"/repos/{self.repo}/issues/{number}/labels", {"labels": [name]})
        if status >= 400:
            raise DegradedWriteError(f"GitHub returned {status} adding '{name}' to issue #{number}")

    def remove_label(self, number: int, name: str) -> None:
        # 404 = the label was not on the Issue — a tolerated no-op (the state we wanted is already true).
        # The name is URL-encoded (the label is an operator-picked leaf that could carry a space/`/`).
        status, _ = self._transport(
            "DELETE", f"/repos/{self.repo}/issues/{number}/labels/{urllib.parse.quote(name, safe='')}", None)
        if status not in (200, 204, 404):
            raise DegradedWriteError(f"GitHub returned {status} removing '{name}' from issue #{number}")

    def list_comments(self, number: int) -> list:
        """Every comment on the Issue, paginated to exhaustion (the endpoint defaults to 30/page — telemetry's
        list_open_engine_issues idiom). RAISES on a read failure so the dedup is never silently empty."""
        out, page = [], 1
        while True:
            status, data = self._transport(
                "GET", f"/repos/{self.repo}/issues/{number}/comments?per_page=100&page={page}", None)
            if status >= 400 or data is None:
                raise DegradedWriteError(f"GitHub returned {status} listing comments on issue #{number}")
            out.extend(data)
            if len(data) < 100:
                break
            page += 1
        return out

    def post_comment(self, number: int, body: str) -> None:
        status, _ = self._transport("POST", f"/repos/{self.repo}/issues/{number}/comments", {"body": body})
        if status >= 400:
            raise DegradedWriteError(f"GitHub returned {status} commenting on issue #{number}")


def _is_conforming(body: str) -> bool:
    """The body-contract test — the SAME predicate the in-session gate uses, over issue_gate's single-source
    markers, so the two layers can never disagree about what 'conforming' means."""
    return all(marker in body for marker in issue_gate.CONTRACT_MARKERS)


def skeleton_comment() -> str:
    """The STATIC advisory comment posted on a non-conforming engine Issue. It echoes ZERO of the Issue's own
    title or body (no attacker-controlled markdown / @mentions / #refs are re-emitted by the bot). It LEADS
    with one plain line for the operator — this is not your task — and tucks the engine-facing conforming
    skeleton + the helper pointer in a collapsed <details>. Carries the dedup marker on its first line."""
    skeleton = issue_author.render_engine_issue_body(
        what_this_is="<one sentence: what this engine item is and why it's here>",
        whats_next="<one sentence: what the operator must decide, or what happens next>",
    )
    return (
        f"{COMMENT_MARKER}\n"
        "The engine filed this item in a format that isn't its standard shape, so it may read as raw text. "
        f"**Nothing for you to do** — the engine will re-file it in its standard shape, and the "
        f"`{NEEDS_REAUTHORING_LABEL}` label clears automatically once it does.\n\n"
        "<details><summary>For the engine — the standard shape to re-author this Issue into</summary>\n\n"
        f"{skeleton}\n"
        f"Render it with `{issue_gate.HELPER}` (`render_engine_issue_body`), or write those three parts "
        "directly, then re-file the body with `--body-file`.\n"
        "</details>"
    )


def _labels_of(issue: dict) -> list:
    """The label names on an issue event payload (`.issue.labels[].name`), defensively."""
    return [lab.get("name") for lab in (issue.get("labels") or []) if isinstance(lab, dict)]


def engine_issue_or_none(event):
    """The issue dict from an issues-event payload IFF it is an engine-labelled Issue with a numeric id;
    otherwise None (out of scope → the caller no-ops, no GitHub call). Defensive against a partial event."""
    if not isinstance(event, dict):
        return None
    issue = event.get("issue")
    if not isinstance(issue, dict) or not isinstance(issue.get("number"), int):
        return None
    if issue_gate.ENGINE_LABEL not in _labels_of(issue):
        return None
    return issue


def reconcile(issue: dict, client: IssueConformanceClient) -> str:
    """Bring one engine-labelled Issue into agreement with its body's conformance, idempotently. Returns a
    short action word for the log/demo. Assumes `issue` is already known engine-labelled with a numeric id
    (engine_issue_or_none). Any GitHub failure propagates as DegradedWriteError (→ a red run)."""
    number = issue["number"]
    labels = _labels_of(issue)
    body = issue.get("body") or ""
    if _is_conforming(body):
        if NEEDS_REAUTHORING_LABEL in labels:   # a conform-after-edit: tidy the flag, never leave a chore
            client.remove_label(number, NEEDS_REAUTHORING_LABEL)
            return "cleared"
        return "conforming"
    client.ensure_label(NEEDS_REAUTHORING_LABEL, _LABEL_COLOR, _LABEL_DESCRIPTION)
    if NEEDS_REAUTHORING_LABEL not in labels:
        client.add_label(number, NEEDS_REAUTHORING_LABEL)
    if not any(COMMENT_MARKER in (c.get("body") or "") for c in client.list_comments(number)):
        client.post_comment(number, skeleton_comment())
    return "flagged"


def _load_event():
    """The issue event JSON from $GITHUB_EVENT_PATH (the safe pattern of validate.get_pr_body — read from the
    file, never a shell-interpolated argument), or None when unavailable/unreadable (a local run, a partial
    event) → the caller no-ops quietly."""
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
        print("issue-conformance: no readable issue event — nothing to check.")
        return 0
    issue = engine_issue_or_none(event)
    if issue is None:
        print("issue-conformance: not an engine-labelled issue — no action.")
        return 0
    repo = os.environ.get("GITHUB_REPOSITORY")
    token = os.environ.get("GITHUB_TOKEN")
    if not repo or not token:
        # An engine-labelled Issue we cannot act on (no token/repo): surface it as a red run, not a silent pass.
        print("issue-conformance: GITHUB_REPOSITORY / GITHUB_TOKEN unset — cannot reach GitHub.", file=sys.stderr)
        return 1
    client = IssueConformanceClient(repo, token)
    try:
        action = reconcile(issue, client)
    except DegradedWriteError as exc:
        print(f"issue-conformance: a GitHub API call failed — {exc}", file=sys.stderr)
        return 1
    print(f"issue-conformance: issue #{issue['number']} -> {action}")
    return 0


# ---- the operator-runnable demo (the live net is what the workflow invokes) -------------------

class _FakeGitHub:
    """A scripted GitHub for the demo/tests: records every (method, path, body) and returns canned
    (status, json), so the REAL reconcile logic runs with no network. `comments` seeds list_comments;
    `label_exists` decides whether ensure_label's GET reports the label already present."""

    def __init__(self, *, comments=None, label_exists: bool = True):
        self.calls = []
        self._comments = comments or []
        self._label_exists = label_exists

    def __call__(self, method, path, body=None):
        self.calls.append((method, path, body))
        if "/comments" in path:
            return (200, list(self._comments)) if method == "GET" else (201, {"id": 1})
        if "/issues/" in path and "/labels" in path:        # add (POST) / remove (DELETE) a label on an issue
            return 200, []
        if path.endswith("/labels"):                         # POST: create a repo label
            return 201, {}
        if "/labels/" in path:                               # GET: does the repo label exist?
            return (200 if self._label_exists else 404), None
        return 200, None

    def posted_comments(self):
        return [c for c in self.calls if c[0] == "POST" and c[1].endswith("/comments")]

    def issue_label_writes(self, method):
        return [c for c in self.calls if c[0] == method and "/issues/" in c[1] and "/labels" in c[1]]


def _demo() -> int:
    """Runs the REAL reconcile / engine_issue_or_none over synthetic issue events against a fake GitHub,
    printing the actual label string and comment copy, and self-checking every outcome. Returns 1 on any
    unexpected result (the failure path the in_tool_demo_failure_path floor requires)."""
    ok = True

    def check(desc: str, cond: bool) -> None:
        nonlocal ok
        if not cond:
            ok = False
        print(f"  {desc:62} -> {'OK' if cond else 'UNEXPECTED'}")

    free_text = "just some free text with no contract markers at all"
    conforming = issue_author.render_engine_issue_body(what_this_is="a demo item", whats_next="nothing to do")
    engine = [{"name": "engine"}]
    engine_flagged = [{"name": "engine"}, {"name": NEEDS_REAUTHORING_LABEL}]

    print("The engine-Issue conformance backstop — what it does for each issue event (real logic, fake GitHub):\n")

    # 1. engine-labelled, non-conforming, no prior bot comment -> label added + ONE comment posted
    gh = _FakeGitHub(comments=[])
    action = reconcile({"number": 1, "labels": engine, "body": free_text}, IssueConformanceClient("o/r", "t", transport=gh))
    check("non-conforming engine issue: flagged + one comment posted",
          action == "flagged" and len(gh.posted_comments()) == 1 and len(gh.issue_label_writes("POST")) == 1)

    # 2. re-fire over an already-commented issue -> NO second comment (dedup by marker holds)
    gh2 = _FakeGitHub(comments=[{"body": COMMENT_MARKER + "\nprior advisory"}])
    reconcile({"number": 1, "labels": engine_flagged, "body": free_text}, IssueConformanceClient("o/r", "t", transport=gh2))
    check("re-fire with the prior comment present: no second comment", len(gh2.posted_comments()) == 0)

    # 3. conform-after-edit (label present) -> label removed
    gh3 = _FakeGitHub()
    action3 = reconcile({"number": 1, "labels": engine_flagged, "body": conforming}, IssueConformanceClient("o/r", "t", transport=gh3))
    check("conforming after an edit: label removed", action3 == "cleared" and len(gh3.issue_label_writes("DELETE")) == 1)

    # 4. conforming, never flagged -> a pure no-op (no GitHub calls at all)
    gh4 = _FakeGitHub()
    action4 = reconcile({"number": 1, "labels": engine, "body": conforming}, IssueConformanceClient("o/r", "t", transport=gh4))
    check("conforming, unflagged: pure no-op", action4 == "conforming" and gh4.calls == [])

    # 5. out-of-scope events are filtered before any client is built
    check("non-engine issue: out of scope",
          engine_issue_or_none({"issue": {"number": 2, "labels": [{"name": "bug"}], "body": free_text}}) is None)
    check("partial/malformed event: out of scope", engine_issue_or_none({"issue": None}) is None)

    print(f"\nThe label an operator sees on a malformed engine issue:  '{NEEDS_REAUTHORING_LABEL}'\n")
    print("The advisory comment the net posts (its first line is plain, for the operator):\n")
    print("    " + skeleton_comment().replace("\n", "\n    "))
    if not ok:
        print("\nDEMO UNEXPECTED: an outcome did not match the backstop's contract.", file=sys.stderr)
        return 1
    return 0


def main(argv: list) -> int:
    if argv and argv[0] == "demo":
        return _demo()
    return _run()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
